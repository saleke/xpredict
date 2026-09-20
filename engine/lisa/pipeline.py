"""Stage 1 + orchestration — the worker pipeline.

  ingest (Stage 1) -> refine / consensus (Stage 2) -> gate (Stage 3)
  -> persist + notify (Stage 4)

Every method is designed to be idempotent per cycle: re-running a cycle over
the same data never duplicates ledger rows or alerts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from . import config as cfg
from .consensus import refine as refine_match
from .gate import Pick, evaluate as evaluate_gate
from .notify import LogNotifier, Notifier, pick_alert_text
from .odds import Match, utcnow
from .parsing import parse_odds_payload
from .storage import Storage, pick_key


@dataclass
class CycleReport:
    sport_key: str
    began: datetime
    matches_seen: int = 0
    matches_refined: int = 0
    picks_emitted: int = 0
    suppressed: list[str] = field(default_factory=list)  # "match_id: reason"
    finished: Optional[datetime] = None
    errors: list[str] = field(default_factory=list)
    matches: tuple[Match, ...] = ()  # parsed matches, for cadence/tracking


class Pipeline:
    def __init__(self, client, storage: Storage, settings: cfg.Settings,
                 notifier: Optional[Notifier] = None):
        self.client = client
        self.storage = storage
        self.settings = settings
        self.notifier = notifier or LogNotifier()

    # -- public --------------------------------------------------------------

    def run_cycle(self, sport_keys: Optional[Iterable[str]] = None, *,
                  live: bool = False,
                  now: Optional[datetime] = None) -> list[CycleReport]:
        """One ingestion+refinement pass over each requested sport."""
        now = now or utcnow()
        requested = tuple(sport_keys) if sport_keys is not None else self.settings.sports
        cfg.validate_sports(requested)
        reports: list[CycleReport] = []
        for sport in requested:
            reports.append(self._run_sport(sport, live=live, now=now))
        return reports

    # -- internals -----------------------------------------------------------

    def _run_sport(self, sport: str, *, live: bool, now: datetime) -> CycleReport:
        report = CycleReport(sport_key=sport, began=now)
        try:
            payload = self.client.get_odds(
                sport, regions=self.settings.regions, markets=self.settings.markets)
        except Exception as exc:  # per-sport degradation, never a crash
            report.errors.append(f"{sport}: {exc!r}")
            report.finished = utcnow()
            return report

        market_keys = tuple(m.strip() for m in self.settings.markets.split(",") if m.strip())
        matches = parse_odds_payload(payload, market_keys=market_keys)
        report.matches = tuple(matches)
        report.matches_seen = len(matches)
        max_book_age = (
            self.settings.stale_live_sec if live else self.settings.stale_prematch_sec
        )

        waiting_room: list[Pick] = []

        for match in matches:
            self._persist_telemetry(match)

            consensus = refine_match(
                match, now=now,
                min_books=self.settings.min_books_telemetry,
                sharp_keys=self.settings.sharp_keys,
                sharp_multiplier=self.settings.sharp_multiplier,
                margin_weighted=self.settings.margin_weighted,
                min_margin_floor=self.settings.min_margin_floor,
                max_book_age_sec=max_book_age,
                lo=self.settings.odds_sanity[0],
                hi=self.settings.odds_sanity[1],
            )
            if consensus is None:
                report.suppressed.append(f"{match.id}:insufficient_books")
                continue
            report.matches_refined += 1

            result = evaluate_gate(
                consensus,
                threshold=self.settings.gate_threshold,
                min_books=self.settings.min_books_alert,
                max_cv=self.settings.max_cv,
                ev_min=self.settings.ev_min,
                require_positive_ev=self.settings.require_positive_ev,
            )
            if result.pick is None:
                report.suppressed.append(f"{match.id}:{result.reason}")
                continue

            pick = result.pick
            if self.storage.insert_pick(pick):
                report.picks_emitted += 1
                self._persist_telemetry_pick(pick)      # hot-layer mirror
                waiting_room.append(pick)
            else:
                self._maybe_update_closing(pick, match, consensus, now)
                if self._maybe_realert(pick, now):
                    report.suppressed.append(f"{match.id}:alert_moved")
                else:
                    report.suppressed.append(f"{match.id}:already_emitted")

        # -- Volume Controller / Waiting Room Dispatch -----------------------
        if waiting_room:
            has_limit = (
                self.settings.max_alerts_per_cycle > 0
                or self.settings.max_alerts_per_sport_cycle > 0
                or self.settings.conviction_min > 0
            )
            if has_limit:
                def _rank_key(p: Pick):
                    ts = p.commence_time.timestamp() if p.commence_time else 0.0
                    return (getattr(p, "conviction_score", 0.0), p.p_true, -ts)

                waiting_room.sort(key=_rank_key, reverse=True)

            dispatched_count = 0
            for pick in waiting_room:
                score = getattr(pick, "conviction_score", 0.0)
                if self.settings.conviction_min > 0 and score < self.settings.conviction_min:
                    report.suppressed.append(f"{pick.match_id}:below_conviction_min")
                    continue

                if self.settings.max_alerts_per_sport_cycle > 0 and dispatched_count >= self.settings.max_alerts_per_sport_cycle:
                    report.suppressed.append(f"{pick.match_id}:waiting_room_sport_quota")
                    continue

                if self.settings.max_alerts_per_cycle > 0 and dispatched_count >= self.settings.max_alerts_per_cycle:
                    report.suppressed.append(f"{pick.match_id}:waiting_room_cycle_quota")
                    continue

                self.notifier.send(pick_alert_text(pick))
                dispatched_count += 1

        report.finished = utcnow()
        return report

    # -- telemetry -----------------------------------------------------------

    def _persist_telemetry(self, match: Match) -> None:
        self.storage.upsert_live(f"match:{match.id}", {
            "sport_key": match.sport_key,
            "commence_time": match.commence_time.isoformat(),
            "home_team": match.home_team,
            "away_team": match.away_team,
            "book_count": len(match.bookmakers),
            "last_seen": utcnow().isoformat(),
        }, self.settings.live_ttl_sec)

    def _persist_telemetry_pick(self, pick: Pick) -> None:
        self.storage.upsert_live(f"pick:{pick_key(pick.match_id, pick.market, pick.outcome_name)}", {
            "p_true": pick.p_true,
            "fair_odds": pick.fair_odds,
            "line": pick.line,
            "conviction_score": getattr(pick, "conviction_score", 0.0),
            "state": pick.state,
            "best_book": pick.best_execution.book_key if pick.best_execution else None,
            "emitted_at": utcnow().isoformat(),
        }, self.settings.live_ttl_sec)

    def _maybe_realert(self, pick: Pick, now: datetime) -> bool:
        """Live-cycle alert policy: re-alert an already-emitted pick only when
        the consensus moved meaningfully and the cooldown has elapsed."""
        hot = self.storage.get_live(
            f"pick:{pick_key(pick.match_id, pick.market, pick.outcome_name)}")
        if not hot or "p_true" not in hot:
            return False
        moved = abs(pick.p_true - float(hot["p_true"])) >= self.settings.alert_min_delta
        try:
            last = datetime.fromisoformat(hot["emitted_at"])
            cooled = (now - last).total_seconds() >= self.settings.alert_cooldown_sec
        except (KeyError, ValueError, TypeError):
            cooled = True
        if not (moved and cooled):
            return False
        hot["p_true"] = pick.p_true
        hot["emitted_at"] = now.isoformat()
        self.storage.upsert_live(
            f"pick:{pick_key(pick.match_id, pick.market, pick.outcome_name)}",
            hot, self.settings.live_ttl_sec)
        self.notifier.send(pick_alert_text(pick))
        return True

    def _maybe_update_closing(self, pick: Pick, match: Match,
                              consensus, now: datetime) -> None:
        """If this pick was already emitted and kickoff hasn't occurred, update
        the pre-kickoff closing odds snapshot and calculate CLV."""
        commence = match.commence_time
        if commence.tzinfo is None and now.tzinfo is not None:
            commence = commence.replace(tzinfo=timezone.utc)
        elif commence.tzinfo is not None and now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if now > commence:
            return  # pre-match closing snapshot is locked once match starts

        key = pick_key(pick.match_id, pick.market, pick.outcome_name)
        existing = self.storage.get_pick(key)

        target_outcome = pick.outcome_name
        exec_book = None
        emit_odds = None
        if existing:
            exec_book = existing.get("best_book")
            if existing.get("best_odds") is not None:
                emit_odds = float(existing["best_odds"])

        if exec_book is None and pick.best_execution:
            exec_book = pick.best_execution.book_key
        if emit_odds is None:
            emit_odds = pick.best_execution.odds if pick.best_execution else pick.fair_odds

        closing_odds = None

        # Prioritize odds from the original execution bookmaker
        if exec_book:
            for b in match.bookmakers:
                if pick.line is not None and b.line is not None and abs(b.line - pick.line) > 1e-4:
                    continue
                if b.key == exec_book and target_outcome in b.outcomes:
                    closing_odds = b.outcomes[target_outcome]
                    break

        # Fallback to best available odds among bookmakers in this line bucket
        if closing_odds is None:
            candidate_odds = []
            for b in match.bookmakers:
                if pick.line is not None and b.line is not None and abs(b.line - pick.line) > 1e-4:
                    continue
                if target_outcome in b.outcomes:
                    candidate_odds.append(b.outcomes[target_outcome])
            if candidate_odds:
                closing_odds = max(candidate_odds)

        if closing_odds is None or closing_odds <= 0:
            return

        closing_p_true = consensus.p.get(target_outcome, pick.p_true)
        clv = (emit_odds / closing_odds) - 1.0 if closing_odds > 0 else 0.0

        self.storage.update_pick_closing(key, closing_odds=closing_odds,
                                         closing_p_true=closing_p_true, clv=clv)

        hot = self.storage.get_live(f"pick:{key}")
        if hot:
            hot["closing_odds"] = closing_odds
            hot["closing_p_true"] = closing_p_true
            hot["clv"] = clv
            self.storage.upsert_live(f"pick:{key}", hot, self.settings.live_ttl_sec)