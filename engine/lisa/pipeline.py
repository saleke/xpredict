"""Stage 1 + orchestration — the worker pipeline.

  ingest (Stage 1) -> refine / consensus (Stage 2) -> gate (Stage 3)
  -> persist + notify (Stage 4)

Every method is designed to be idempotent per cycle: re-running a cycle over
the same data never duplicates ledger rows or alerts.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from . import config as cfg
from .consensus import refine as refine_match
from .dashboard import LIVE_SNAPSHOT_KEY, live_odds_key
from .gate import Pick, evaluate as evaluate_gate
from .notify import LogNotifier, Notifier, pick_alert_text
from .odds import Match, utcnow
from .parsing import parse_odds_payload
from .storage import Storage, pick_key

logger = logging.getLogger(__name__)

#: How long a raw book payload stays readable for the forecast board.
PAYLOAD_TTL_SEC = 90 * 60
#: How long the merged board rollup stays readable.
SNAPSHOT_TTL_SEC = 90 * 60


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
    notifications_sent: int = 0
    notifications_failed: int = 0
    notifications_retried: int = 0


class Pipeline:
    def __init__(self, client, storage: Storage, settings: cfg.Settings,
                 notifier: Optional[Notifier] = None):
        self.client = client
        self.storage = storage
        self._settings = settings
        self.notifier = notifier or LogNotifier()

    @property
    def settings(self):
        """Effective settings, re-read on every access.

        May be a ``Settings`` instance or a zero-argument callable returning one.
        The callable form is how the admin panel applies changes to a running
        process without a restart.
        """
        s = self._settings
        return s() if callable(s) else s

    # -- public --------------------------------------------------------------

    def run_cycle(self, sport_keys: Optional[Iterable[str]] = None, *,
                  live: bool = False,
                  prefetched: Optional[Mapping[str, Any]] = None,
                  now: Optional[datetime] = None) -> list[CycleReport]:
        """One ingestion+refinement pass over each requested sport.

        ``prefetched`` maps a sport key to an odds payload that the caller has
        already fetched. Reusing it keeps a cycle at exactly one upstream
        request per league, which matters when the API budget is limited.
        """
        now = now or utcnow()
        requested = tuple(sport_keys) if sport_keys is not None else self.settings.sports
        cfg.validate_sports(requested)
        # Deliver anything a previous cycle could not hand to the notifier before
        # new work is considered, so a transient outage cannot swallow an alert.
        self._flush_outbox()
        reports: list[CycleReport] = []
        for sport in requested:
            reports.append(self._run_sport(
                sport, live=live, now=now,
                payload=(prefetched or {}).get(sport),
            ))
        self._write_snapshot(reports, now=now, live=live)
        return reports

    # -- notification outbox -------------------------------------------------

    def _has_outbox(self) -> bool:
        return callable(getattr(self.storage, "enqueue_notification", None))

    def _flush_outbox(self, limit: int = 100) -> tuple[int, int]:
        """Send pending alerts. Returns ``(sent, failed)``.

        The ledger row is written before delivery, so dispatch has to be durable:
        a failure leaves the row PENDING and it is retried on the next cycle
        instead of being lost forever.
        """
        sent = failed = 0
        if not self._has_outbox():
            return sent, failed
        for row in self.storage.list_pending_notifications(limit=limit):
            try:
                self.notifier.send(row["text"])
            except Exception as exc:
                failed += 1
                self.storage.mark_notification_failed(row["dedupe_key"], repr(exc))
                logger.warning("Notification for %s failed: %r", row["dedupe_key"], exc)
                continue
            self.storage.mark_notification_sent(row["dedupe_key"])
            sent += 1
        return sent, failed

    def _enqueue_alert(self, pick: Pick, dedupe_key: Optional[str] = None) -> bool:
        """Record the alert durably. Returns False if the driver has no outbox."""
        if not self._has_outbox():
            return False
        key = dedupe_key or pick_key(pick.match_id, pick.market, pick.outcome_name)
        self.storage.enqueue_notification(key, pick_alert_text(pick))
        return True

    # -- internals -----------------------------------------------------------

    def _cache_payload(self, sport: str, payload: Any, *, live: bool) -> None:
        """Persist the raw book payload so the forecast board can read it.

        The daemon used to own this write, which meant the scheduler path
        fetched odds but never cached them and ``/api/forecast`` went dark.
        Caching where the payload is parsed keeps every path consistent, and
        the wrapper shape below is the contract ``/api/forecast`` reads.
        """
        if not isinstance(payload, (list, dict)):
            return
        remaining = getattr(self.client, "last_remaining", None)
        try:
            self.storage.upsert_live(
                live_odds_key(sport),
                {
                    "sport_key": sport,
                    "observed_at": time.time(),
                    "credits_remaining": (
                        remaining if isinstance(remaining, int) else None
                    ),
                    "payload": payload,
                },
                ttl_seconds=PAYLOAD_TTL_SEC,
            )
        except Exception as exc:
            # The cache only backs the UI; losing it must not stop picks.
            logger.warning("Could not cache odds payload for %s: %r", sport, exc)

    def _write_snapshot(self, reports: list[CycleReport], *,
                        now: datetime, live: bool) -> None:
        """Merge this cycle into the board rollup the dashboard reads.

        Merging rather than replacing is deliberate: a cycle that polled
        nothing (quota floor, upstream outage) must not erase the last thing we
        actually saw, and a stale league keeps its own age so the UI can say how
        old the numbers are.
        """
        if not hasattr(self.storage, "upsert_live"):
            return
        try:
            previous = self.storage.get_live_stale(LIVE_SNAPSHOT_KEY) or {}
        except Exception:
            previous = {}

        observed_at = time.time()
        merged: dict[str, dict] = {
            sport: dict(entry)
            for sport, entry in (previous.get("sports") or {}).items()
        }
        polled = False
        for report in reports:
            # An empty payload means the fetch failed for this league; keep the
            # previous row instead of recording a false zero.
            if not report.matches and report.errors and report.sport_key in merged:
                entry = dict(merged[report.sport_key])
                entry["error"] = report.errors[0]
                merged[report.sport_key] = entry
                continue
            polled = True
            merged[report.sport_key] = {
                "observed_at": observed_at,
                "n_matches": len(report.matches),
                "error": report.errors[0] if report.errors else None,
            }

        total = sum(int(v.get("n_matches", 0) or 0) for v in merged.values())
        stamps = [v["observed_at"] for v in merged.values() if v.get("observed_at")]
        remaining = getattr(self.client, "last_remaining", None)
        errors = [e for r in reports for e in r.errors]
        snapshot = {
            "observed_at": max(stamps) if stamps else None,
            "credits_remaining": (
                remaining if isinstance(remaining, int) else None
            ),
            "matches": [],
            "matches_observed": total if polled else int(
                previous.get("matches_observed", total) or 0),
            "sports": merged,
            "last_error": errors[0] if errors else previous.get("last_error"),
        }
        try:
            self.storage.upsert_live(
                LIVE_SNAPSHOT_KEY, snapshot, SNAPSHOT_TTL_SEC)
        except Exception as exc:
            logger.warning("Could not write board rollup: %r", exc)

    def _run_sport(self, sport: str, *, live: bool, now: datetime,
                   payload: Any = None) -> CycleReport:
        report = CycleReport(sport_key=sport, began=now)
        if payload is None:
            try:
                payload = self.client.get_odds(
                    sport, regions=self.settings.regions, markets=self.settings.markets)
            except Exception as exc:  # per-sport degradation, never a crash
                report.errors.append(f"{sport}: {exc!r}")
                report.finished = utcnow()
                return report

        self._cache_payload(sport, payload, live=live)

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

            # Never mint a "pre-game" pick for a fixture that already kicked off:
            # post-kickoff prices are live odds and the CLV/closing logic below
            # assumes the bet was available before kickoff.
            if match.commence_time is not None and match.commence_time <= now:
                report.suppressed.append(f"{match.id}:kickoff_passed")
                continue

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

                if not self._enqueue_alert(pick):
                    # Storage driver without an outbox: dispatch directly.
                    self.notifier.send(pick_alert_text(pick))
                dispatched_count += 1

        # Deliver everything queued this cycle, including re-alerts raised above.
        sent, failed = self._flush_outbox()
        report.notifications_sent += sent
        report.notifications_failed += failed
        if failed:
            report.errors.append(f"{failed} alert(s) still pending delivery")

        # -- Tier Curation ---------------------------------------------------
        # After all picks are emitted, curate them per tier
        if waiting_room:
            from .tier_curator import tier_curator
            all_picks = list(waiting_room)
            curated = tier_curator.curate(all_picks, now=now)
            # Store curated picks in hot layer for API access
            try:
                self.storage.upsert_live(
                    "curated_picks",
                    curated.to_dict(),
                    ttl_seconds=3600,
                )
            except Exception as exc:
                logger.warning("Could not store curated picks: %r", exc)

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
        base_key = pick_key(pick.match_id, pick.market, pick.outcome_name)
        # A re-alert is a distinct notification, so it needs its own outbox key.
        if not self._enqueue_alert(pick, f"{base_key}#re{int(now.timestamp())}"):
            # Storage driver without an outbox: fall back to direct dispatch.
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