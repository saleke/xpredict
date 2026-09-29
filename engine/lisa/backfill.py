"""Settlement & CLV backfill from the BetExplorer results feed.

Why this exists
---------------
``settle.py`` can only grade fixtures that appeared inside a live Odds API
scores window, so a pick that fell out of that window can never be settled —
and ``accuracy_tracker`` can therefore never be backfilled over history. CLV
has a related weakness: the pipeline derives closing odds from the same snapshot
it priced from, which is not an independent close.

This module closes both by re-walking pending picks against archived
BetExplorer day pages, which carry a final score *and* the closing 1X2 line.

Safety rules, in priority order
-------------------------------
A wrong fixture join is worse than a missing one — it writes a wrong result
into the ledger and silently corrupts every downstream accuracy metric. So:

* **Only 1X2.** ``h2h`` picks only. This feed publishes no totals, spreads or
  handicaps, so any other market is counted as unsupported and left pending
  rather than graded from a market that isn't there.
* **Ambiguity is a refusal, not a coin flip.** Two fixtures matching a pick
  equally well means the join is not trustworthy; the pick is skipped and
  reported.
* **Dry run by default.** Nothing is written unless ``dry_run=False``.
* **Idempotent.** ``settle_pick`` is a no-op on an already-settled row, so
  re-running a window is safe.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional

from .betexplorer import (
    MIN_JOIN_SCORE,
    BetExplorerClient,
    BetExplorerError,
    BetExplorerMatch,
    team_match_score,
)
from .odds import H2H, utcnow
from .storage import Storage

logger = logging.getLogger(__name__)

#: How far either side of the pick's kickoff date a join is allowed to land.
#: The two feeds publish local kickoff dates and can disagree across midnight.
DATE_SLACK_DAYS = 1


@dataclass
class BackfillReport:
    """Outcome of one backfill pass."""

    #: pick rows examined after the market filter
    considered: int = 0
    #: dropped: market this feed cannot grade
    skipped_unsupported_market: int = 0
    #: no fixture on any fetched day joined the pick
    skipped_no_match: int = 0
    #: joined, but the fixture has no final score or no 1X2 line yet
    skipped_not_final: int = 0
    #: joined, but the picked side has no closing price
    skipped_no_price: int = 0
    #: two or more fixtures matched equally well
    skipped_ambiguous: int = 0
    #: settle_pick refused the row — it left the pending set mid-pass, e.g. the
    #: Odds API settler graded it first. Expected 0 in a single-writer run.
    already_settled: int = 0

    won: int = 0
    lost: int = 0
    void: int = 0
    settled: int = 0
    clv_updated: int = 0

    #: distinct calendar days actually fetched from Parse
    days_fetched: int = 0
    #: Parse credits spent this pass (0 on a dry run)
    api_calls: int = 0
    #: per-date failures, so one bad page cannot abort the pass
    errors: list[str] = field(default_factory=list)
    #: detail rows for anything that was written (or would be, on a dry run)
    settled_picks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "considered": self.considered,
            "settled": self.settled,
            "won": self.won,
            "lost": self.lost,
            "void": self.void,
            "clv_updated": self.clv_updated,
            "already_settled": self.already_settled,
            "skipped_unsupported_market": self.skipped_unsupported_market,
            "skipped_no_match": self.skipped_no_match,
            "skipped_not_final": self.skipped_not_final,
            "skipped_no_price": self.skipped_no_price,
            "skipped_ambiguous": self.skipped_ambiguous,
            "days_fetched": self.days_fetched,
            "api_calls": self.api_calls,
            "errors": list(self.errors),
        }


def _commence_date(row: dict) -> Optional[date]:
    raw = row.get("commence_time")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)).date()
    except ValueError:
        return None


class SettlementBackfiller:
    """Grades pending picks against archived BetExplorer day pages."""

    def __init__(self, *, storage: Storage, client: BetExplorerClient):
        self.storage = storage
        self.client = client

    # -- fixture join ------------------------------------------------------
    @staticmethod
    def _join(pick_row: dict, fixtures: list[BetExplorerMatch]
              ) -> tuple[Optional[BetExplorerMatch], bool]:
        """Find the fixture a pick row refers to.

        Returns ``(fixture, ambiguous)``.

        An ordered home/away comparison is used: a swapped pairing is a
        different fixture, and accepting it would let a mistyped side settle
        against the wrong result. The weaker leg of the pair sets the join
        score, so one exact side cannot carry a bad one — both sides must
        independently reach ``MIN_JOIN_SCORE``.
        """
        home = str(pick_row.get("home_team") or "")
        away = str(pick_row.get("away_team") or "")
        if not home or not away:
            return None, False

        best: Optional[BetExplorerMatch] = None
        best_score = 0.0
        ties = 0
        for fixture in fixtures:
            home_score = team_match_score(home, fixture.home_team)
            away_score = team_match_score(away, fixture.away_team)
            if home_score < MIN_JOIN_SCORE or away_score < MIN_JOIN_SCORE:
                continue
            # The weaker leg governs; both already passed the threshold.
            score = min(home_score, away_score)
            if score > best_score + 1e-9:
                best, best_score, ties = fixture, score, 1
            elif abs(score - best_score) <= 1e-9:
                ties += 1

        if best is None:
            return None, False
        return best, ties > 1

    # -- CLV ---------------------------------------------------------------
    @staticmethod
    def _clv(taken_odds: Optional[float], closing_odds: Optional[float]
             ) -> Optional[float]:
        """Closing line value, using the engine's own sign convention.

        ``pipeline._update_closing`` computes ``taken / closing - 1``, i.e.
        positive when the price beat the close. Reusing that formula keeps
        backfilled CLV directly comparable with live-computed CLV.
        """
        if not taken_odds or not closing_odds or closing_odds <= 0:
            return None
        return (taken_odds / closing_odds) - 1.0

    # -- main pass ---------------------------------------------------------
    def run(self, *, lookback_days: int = 7, dry_run: bool = True,
            now: Optional[datetime] = None,
            settle: bool = True) -> BackfillReport:
        """Re-walk pending picks over the last ``lookback_days`` calendar days.

        Args:
            lookback_days: how far back to fetch day pages, inclusive of today.
            dry_run: when True, compute and report but write nothing.
            now: reference time; defaults to :func:`utcnow`.
            settle: when False, only refresh CLV and leave results pending.
        """
        now = now or utcnow()
        report = BackfillReport()

        pending = self.storage.list_pending_picks()
        rows = [r for r in pending if r.get("market") == H2H]
        report.skipped_unsupported_market = len(pending) - len(rows)
        report.considered = len(rows)
        if not rows:
            return report

        # One fetch per calendar day for the whole window, shared by every pick.
        by_day: dict[date, list[dict]] = {}
        for row in rows:
            day = _commence_date(row)
            if day is None:
                report.skipped_no_match += 1
                continue
            by_day.setdefault(day, []).append(row)

        today = now.date()
        wanted: set[date] = set()
        for day in by_day:
            for shift in range(-DATE_SLACK_DAYS, DATE_SLACK_DAYS + 1):
                probe = day + timedelta(days=shift)
                if probe <= today:
                    wanted.add(probe)

        fixtures_by_day: dict[date, list[BetExplorerMatch]] = {}
        for day in sorted(wanted):
            try:
                fixtures = self.client.get_matches(day)
            except BetExplorerError as exc:
                report.errors.append(f"{day.isoformat()}: {exc}")
                logger.warning("backfill: skipping %s (%s)", day, exc)
                continue
            except Exception as exc:  # a bad page must not abort the pass
                report.errors.append(f"{day.isoformat()}: unexpected {exc!r}")
                logger.exception("backfill: unexpected failure on %s", day)
                continue
            report.days_fetched += 1
            fixtures_by_day[day] = fixtures

        report.api_calls = self.client.calls_made

        for pick_row in rows:
            self._process_one(pick_row, fixtures_by_day, report,
                              now=now, dry_run=dry_run, settle=settle)

        return report

    def _process_one(self, row: dict, fixtures_by_day: dict[date, list[BetExplorerMatch]],
                     report: BackfillReport, *, now: datetime,
                     dry_run: bool, settle: bool) -> None:
        day = _commence_date(row)
        if day is None:
            report.skipped_no_match += 1
            return

        # De-duplicate by event id. A single fixture can be reachable from more
        # than one candidate day, and counting the same event twice would look
        # like an ambiguous tie and wrongly refuse an exact match.
        candidates: dict[str, BetExplorerMatch] = {}
        for shift in range(-DATE_SLACK_DAYS, DATE_SLACK_DAYS + 1):
            for fixture in fixtures_by_day.get(day + timedelta(days=shift), []):
                candidates[fixture.event_id] = fixture
        candidate_list = list(candidates.values())

        fixture, ambiguous = self._join(row, candidate_list)
        if ambiguous:
            report.skipped_ambiguous += 1
            return
        if fixture is None:
            report.skipped_no_match += 1
            return
        if not fixture.is_final or not fixture.has_1x2:
            report.skipped_not_final += 1
            return

        outcome = str(row.get("outcome_name") or "")
        closing_odds = fixture.price_for(outcome)
        if closing_odds is None:
            report.skipped_no_price += 1
            return

        dedupe = str(row.get("dedupe_key") or "")
        taken = row.get("best_odds") or row.get("fair_odds")
        clv = self._clv(taken, closing_odds)
        if clv is not None and not dry_run:
            self.storage.update_pick_closing(dedupe, closing_odds, clv=clv)
        if clv is not None:
            report.clv_updated += 1

        if not settle:
            return

        # Grade with the engine's own team names so grade_pick compares like
        # with like; only the scoreline comes from the external feed.
        score = fixture.to_score(
            match_id=str(row.get("match_id") or ""),
            sport_key=str(row.get("sport_key") or ""),
            commence_time=datetime.fromisoformat(str(row["commence_time"])),
            home_team=str(row.get("home_team") or ""),
            away_team=str(row.get("away_team") or ""),
        )
        grade = score.grade_pick(H2H, outcome, row.get("line"))
        if grade is None:
            report.skipped_not_final += 1
            return

        written = True
        if not dry_run:
            written = self.storage.settle_pick(dedupe, grade, now)
        if not written:
            report.already_settled += 1
            return

        report.settled += 1
        if grade == "WIN":
            report.won += 1
        elif grade == "LOSS":
            report.lost += 1
        else:
            report.void += 1
        report.settled_picks.append({
            "dedupe_key": dedupe,
            "match_id": row.get("match_id"),
            "sport_key": row.get("sport_key"),
            "market": row.get("market"),
            "outcome_name": outcome,
            "home_team": row.get("home_team"),
            "away_team": row.get("away_team"),
            "p_true": row.get("p_true"),
            "line": row.get("line"),
            "best_odds": taken,
            "closing_odds": closing_odds,
            "clv": clv,
            "result": grade,
            "source_event_id": fixture.event_id,
            "source_league": fixture.league,
            "final_score": f"{fixture.score[0]}:{fixture.score[1]}"
                           if fixture.score else None,
            "dry_run": dry_run,
        })


def backfill_report_summary(report: BackfillReport) -> str:
    """One-line human summary, matching the engine's logging style."""
    return (
        f"considered={report.considered} settled={report.settled} "
        f"(W{report.won}/L{report.lost}/V{report.void}) clv={report.clv_updated} "
        f"skipped[market={report.skipped_unsupported_market} "
        f"nomatch={report.skipped_no_match} notfinal={report.skipped_not_final} "
        f"noprice={report.skipped_no_price} ambiguous={report.skipped_ambiguous}] "
        f"days={report.days_fetched} calls={report.api_calls} "
        f"errors={len(report.errors)}"
    )
