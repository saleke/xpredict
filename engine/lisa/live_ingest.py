"""Live Real-Time Odds Ingestion and Alert Dispatch Daemon.

Orchestrates:
  * Continuous polling of bookmaker lines across active leagues, written to the
    shared live cache so that HTTP handlers never spend API credits.
  * Real-time Shin de-vigging and consensus convergence evaluation, delegated to
    ``Pipeline`` so the kickoff guard, durable alert outbox and closing-line
    logic are identical to the tested production path.
  * Real-time Trap Advisory generation when market variance spikes or sharp
    books diverge.
  * Automated match settlement.

Credit discipline: the daemon reads the remaining-quota header the API returns
and stops polling before the configured floor (``credit_warn``/``credit_stop``)
so a limited key is never burned by a runaway cadence. The UI then serves the
last real observation, clearly labelled with its age.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Set

from . import config as cfg
from .consensus import refine
from .dashboard import LIVE_SNAPSHOT_KEY, live_odds_key
from .notify import LogNotifier, Notifier
from .odds import utcnow
from .parsing import parse_odds_payload
from .pipeline import Pipeline
from .storage import Storage
from .telegram_bot import TelegramBot

logger = logging.getLogger(__name__)

#: How long a raw odds payload stays "current" in the live cache.
SNAPSHOT_TTL_SEC = 90 * 60

#: Trap advisories are operational history, not picks: kept a week for the ops view.
TRAPS_KEY = "live:traps"
TRAPS_KEEP = 25
TRAPS_TTL_SEC = 7 * 24 * 60 * 60


@dataclass
class IngestCycleResult:
    timestamp: str
    leagues_polled: int
    matches_seen: int
    diamonds_found: int
    traps_found: int
    alerts_dispatched: int
    settled_count: int = 0
    errors: list[str] = field(default_factory=list)


class LiveIngestionDaemon:
    """Automated real-time odds polling, evaluation, and alert dispatch daemon."""

    def __init__(
        self,
        client: Any,
        settings: cfg.Settings,
        notifier: Optional[Notifier] = None,
        telegram_bot: Optional[TelegramBot] = None,
        storage: Optional[Storage] = None,
        auto_settle: bool = True,
        notify_settle: bool = False,
    ):
        self.client = client
        self.settings = settings
        self.notifier = notifier or LogNotifier()
        self.telegram_bot = telegram_bot
        # The ledger is what makes picks real and dedupes alerts, so the daemon
        # always has one. Without a configured database it uses an in-memory
        # ledger for this process rather than silently dropping data.
        if storage is None:
            from .storage import InMemoryStorage
            storage = InMemoryStorage()
        self.storage = storage
        self.auto_settle = auto_settle
        self.notify_settle = notify_settle
        self._pipeline = Pipeline(client, storage, settings, notifier=self.notifier)

        # State tracking for trap suppression (pick dedupe lives in the ledger).
        self._alerted_traps: Set[str] = set()

    # -- credit discipline ---------------------------------------------------
    def credits_remaining(self) -> Optional[int]:
        remaining = getattr(self.client, "last_remaining", None)
        return remaining if isinstance(remaining, int) else None

    def _quota_blocked(self) -> Optional[str]:
        remaining = self.credits_remaining()
        if remaining is None:
            return None
        if remaining <= int(getattr(self.settings, "credit_stop", 20) or 20):
            return f"quota floor reached ({remaining} credits left)"
        return None

    # -- upstream polling ----------------------------------------------------
    def poll_league(self, sport_key: str) -> tuple[list[Any], list[Any], list[str]]:
        """Fetch and cache one league. Returns ``(matches, payload, errors)``."""
        try:
            raw_data = self.client.get_odds(
                sport_key,
                regions=self.settings.regions,
                markets=self.settings.markets,
            )
        except Exception as exc:
            logger.warning("Failed to fetch odds for league %s: %s", sport_key, exc)
            return [], None, [f"{sport_key}: {exc!r}"]

        now = time.time()
        if self.storage is not None and hasattr(self.storage, "upsert_live"):
            try:
                self.storage.upsert_live(
                    live_odds_key(sport_key),
                    {
                        "sport_key": sport_key,
                        "observed_at": now,
                        "credits_remaining": self.credits_remaining(),
                        "payload": raw_data,
                    },
                    SNAPSHOT_TTL_SEC,
                )
            except Exception as exc:
                logger.warning("[live-ingest] cache write failed for %s: %r", sport_key, exc)

        try:
            return parse_odds_payload(raw_data), raw_data, []
        except Exception as exc:
            logger.warning("Failed to parse odds for league %s: %s", sport_key, exc)
            return [], raw_data, [f"{sport_key}:parse:{exc!r}"]

    # -- live snapshot rollup -------------------------------------------------
    def _write_snapshot_rollup(self, now_iso: str, per_sport: dict[str, dict],
                               last_error: Optional[str]) -> None:
        """Record the board state, merging over the previous observation.

        A cycle that polls nothing (quota floor) must not erase what we last
        saw, so per-league entries are merged and only overwritten when the
        league was actually contacted this cycle.
        """
        if self.storage is None or not hasattr(self.storage, "upsert_live"):
            return
        merged: dict[str, dict] = {}
        previous = self.storage.get_live_stale(LIVE_SNAPSHOT_KEY) or {}
        for sport, entry in (previous.get("sports") or {}).items():
            merged[sport] = dict(entry)
        for sport, entry in per_sport.items():
            merged[sport] = dict(entry)
        # An error is only news while it is current; a stale league keeps its
        # age so the UI can still say how old the numbers are.
        total = sum(int(v.get("n_matches", 0)) for v in merged.values())
        observed = [v["observed_at"] for v in merged.values() if v.get("observed_at")]
        snapshot = {
            "observed_at": max(observed) if observed else None,
            "credits_remaining": self.credits_remaining(),
            "matches": [],
            "matches_observed": total,
            "sports": merged,
            "last_error": last_error,
        }
        if not per_sport and previous:
            # Nothing new was observed: keep the previous counts as-is.
            snapshot["matches_observed"] = int(previous.get("matches_observed", total) or 0)
        try:
            self.storage.upsert_live(LIVE_SNAPSHOT_KEY, snapshot, SNAPSHOT_TTL_SEC)
        except Exception as exc:
            logger.warning("[live-ingest] snapshot rollup failed: %r", exc)

    # -- traps ---------------------------------------------------------------
    def _scan_traps(self, matches: list[Any], now_iso: str) -> int:
        found = 0
        recorded: list[dict[str, Any]] = []
        for match in matches:
            consensus = refine(
                match,
                now=utcnow(),
                min_books=self.settings.min_books_telemetry,
                sharp_keys=self.settings.sharp_keys,
                sharp_multiplier=self.settings.sharp_multiplier,
                margin_weighted=self.settings.margin_weighted,
                min_margin_floor=self.settings.min_margin_floor,
                max_book_age_sec=self.settings.stale_prematch_sec,
            )
            if consensus is None:
                continue
            for outcome_name, p_val in consensus.p.items():
                if p_val < 0.65:
                    continue
                if consensus.cv <= self.settings.max_cv and consensus.stdev <= 0.012:
                    continue
                trap_key = f"{match.id}::{outcome_name}::trap"
                if trap_key in self._alerted_traps:
                    continue
                self._alerted_traps.add(trap_key)
                found += 1
                recorded.append({
                    "match_id": match.id,
                    "home_team": match.home_team,
                    "away_team": match.away_team,
                    "sport_key": match.sport_key,
                    "public_favorite": outcome_name,
                    "cv": round(float(consensus.cv), 4),
                    "stdev": round(float(consensus.stdev), 5),
                    "commence_time": (
                        match.commence_time.isoformat() if match.commence_time else None
                    ),
                    "observed_at": now_iso,
                })
                if self.telegram_bot:
                    self.telegram_bot.broadcast_trap(
                        home_team=match.home_team,
                        away_team=match.away_team,
                        sport_key=match.sport_key,
                        public_favorite=outcome_name,
                        reason=(
                            f"Sharp divergence (stdev={consensus.stdev:.3f}, "
                            f"CV={consensus.cv:.1%})"
                        ),
                        cv=consensus.cv,
                    )
        if recorded:
            self._record_traps(recorded)
        return found

    def _record_traps(self, recorded: list[dict[str, Any]]) -> None:
        """Keep the most recent real trap advisories for the ops view."""
        if self.storage is None or not hasattr(self.storage, "upsert_live"):
            return
        history: list[dict[str, Any]] = []
        try:
            existing = self.storage.get_live_stale(TRAPS_KEY)
            if isinstance(existing, list):
                history = existing
        except Exception:
            history = []
        merged = (recorded + history)[:TRAPS_KEEP]
        try:
            self.storage.upsert_live(TRAPS_KEY, merged, TRAPS_TTL_SEC)
        except Exception as exc:
            logger.warning("[live-ingest] trap history write failed: %r", exc)

    # -- cycle ---------------------------------------------------------------
    def run_cycle(self) -> IngestCycleResult:
        """One polling + evaluation + settlement pass over configured leagues."""
        now = utcnow()
        res = IngestCycleResult(
            timestamp=now.isoformat(), leagues_polled=0, matches_seen=0,
            diamonds_found=0, traps_found=0, alerts_dispatched=0,
        )

        blocked = self._quota_blocked()
        if blocked:
            res.errors.append(f"poll skipped: {blocked}")
            self._write_snapshot_rollup(now.isoformat(), {}, blocked)
            logger.info("[live-ingest] %s — serving last observation", blocked)
            return res

        all_matches: list[Any] = []
        prefetched: dict[str, Any] = {}
        per_sport: dict[str, dict] = {}
        for sport in self.settings.sports:
            matches, payload, errors = self.poll_league(sport)
            res.leagues_polled += 1
            res.matches_seen += len(matches)
            res.errors.extend(errors)
            all_matches.extend(matches)
            # Record the attempt for every league, including failures: an empty
            # payload tells the Pipeline this league was already polled so it
            # cannot spend a second request re-fetching it.
            prefetched[sport] = payload if payload is not None else []
            per_sport[sport] = {
                "observed_at": time.time(),
                "n_matches": len(matches),
                "error": errors[0] if errors else None,
            }

        res.traps_found = self._scan_traps(all_matches, res.timestamp)

        # Puts picks, alerts (durable outbox) and closing-line updates through the
        # single tested production path, reusing this cycle's fetches.
        for report in self._pipeline.run_cycle(prefetched=prefetched, now=now):
            res.diamonds_found += report.picks_emitted
            res.alerts_dispatched += report.picks_emitted
            res.errors.extend(report.errors)

        if self.auto_settle and self.storage is not None and hasattr(self.client, "get_scores"):
            try:
                from .settle import run_settlement
                s_rep = run_settlement(self.client, self.storage, self.settings, now=now)
                res.settled_count = len(s_rep.settled_picks)
                if res.settled_count:
                    logger.info(
                        "[live-ingest] Settled %d picks: %d won, %d lost, %d void",
                        res.settled_count, s_rep.won, s_rep.lost, s_rep.void,
                    )
                    if self.notify_settle and self.telegram_bot:
                        for sp in s_rep.settled_picks:
                            self.telegram_bot.broadcast_settlement(sp)
            except Exception as exc:
                res.errors.append(f"settlement:{exc!r}")
                logger.warning("[live-ingest] Settlement pass error: %s", exc)

        self._write_snapshot_rollup(
            res.timestamp, per_sport,
            res.errors[0] if res.errors else None,
        )

        logger.info(
            "[live-ingest] Cycle complete: %d leagues, %d matches, %d diamonds, "
            "%d traps, %d settled",
            res.leagues_polled, res.matches_seen, res.diamonds_found,
            res.traps_found, res.settled_count,
        )
        return res

    def run_daemon(self, max_iterations: Optional[int] = None,
                   interval_sec: Optional[int] = None) -> None:
        """Run continuous live polling daemon.

        The default cadence is the configured prematch interval, not a tight
        loop: every pass spends one request per league per region, so polling
        faster than the fixtures move is how a limited key gets drained.
        """
        if interval_sec is None:
            interval_sec = max(300, int(self.settings.cadence_prematch_sec or 3600))
        iteration = 0
        logger.info("Starting LISA Live Ingestion Daemon (interval=%ds)...", interval_sec)
        while max_iterations is None or iteration < max_iterations:
            iteration += 1
            try:
                self.run_cycle()
            except Exception as exc:
                logger.error("Error during live ingestion cycle: %s", exc)

            if max_iterations is None or iteration < max_iterations:
                time.sleep(interval_sec)


# Backward-compatible alias for CLI and external scripts
LiveIngestionEngine = LiveIngestionDaemon
