"""Live Real-Time Odds Ingestion and Alert Dispatch Daemon.

Orchestrates:
  * Continuous polling of bookmaker lines across active leagues.
  * Real-time Shin de-vigging and consensus convergence evaluation.
  * Instant alert dispatch on newly uncovered Diamond picks via Telegram and webhooks.
  * Real-time Trap Advisory generation when market variance spikes or sharp books diverge.
  * Automated match settlement and live synchronisation with web/data/dashboard.json.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from . import config as cfg
from .consensus import refine
from .gate import Pick, evaluate as evaluate_gate
from .notify import CompositeNotifier, LogNotifier, Notifier, pick_alert_text
from .odds import Match, utcnow
from .parsing import parse_odds_payload
from .storage import Storage
from .telegram_bot import TelegramBot

logger = logging.getLogger(__name__)


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
        dashboard_path: str = "web/data/dashboard.json",
        auto_settle: bool = True,
        notify_settle: bool = False,
    ):
        self.client = client
        self.settings = settings
        self.notifier = notifier or LogNotifier()
        self.telegram_bot = telegram_bot
        self.storage = storage
        self.dashboard_path = dashboard_path
        self.auto_settle = auto_settle
        self.notify_settle = notify_settle

        # State tracking for deduplication and alert suppression
        self._alerted_keys: Set[str] = set()
        self._alerted_traps: Set[str] = set()

    def poll_league(self, sport_key: str) -> list[Match]:
        """Fetch and parse live/pre-match odds for a single sport league."""
        try:
            raw_data = self.client.get_odds(sport_key, regions=self.settings.regions)
            return parse_odds_payload(raw_data)
        except Exception as exc:
            logger.warning("Failed to fetch odds for league %s: %s", sport_key, exc)
            return []

    def _sync_dashboard(self, res: IngestCycleResult, now_iso: str) -> None:
        """Safely sync active counts, timestamp, and settled metrics to dashboard.json."""
        if not self.dashboard_path or not os.path.exists(self.dashboard_path):
            return
        try:
            with open(self.dashboard_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return

            if "meta" in data and isinstance(data["meta"], dict):
                data["meta"]["generated_at"] = now_iso

            if "summary" in data and isinstance(data["summary"], dict):
                s = data["summary"]
                s["total_matches_evaluated"] = int(s.get("total_matches_evaluated", 0)) + res.matches_seen
                s["diamonds_count"] = int(s.get("diamonds_count", 0)) + res.diamonds_found
                s["traps_avoided_month"] = int(s.get("traps_avoided_month", 0)) + res.traps_found

                if self.storage is not None:
                    settled = self.storage.list_settled_picks()
                    if settled:
                        won = sum(1 for p in settled if p.get("result") == "WIN")
                        s["settled_picks_count"] = len(settled)
                        s["win_rate"] = won / len(settled)

            tmp_path = f"{self.dashboard_path}.tmp.{os.getpid()}"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, self.dashboard_path)
        except Exception as exc:
            logger.warning("[live-ingest] Failed to sync dashboard %s: %s", self.dashboard_path, exc)

    def run_cycle(self) -> IngestCycleResult:
        """Run one full evaluation pass across all configured leagues."""
        now = utcnow()
        now_iso = now.isoformat()
        res = IngestCycleResult(
            timestamp=now_iso,
            leagues_polled=0,
            matches_seen=0,
            diamonds_found=0,
            traps_found=0,
            alerts_dispatched=0,
        )

        all_matches: list[Match] = []
        new_picks: list[Pick] = []

        for sport in self.settings.sports:
            matches = self.poll_league(sport)
            res.leagues_polled += 1
            res.matches_seen += len(matches)
            all_matches.extend(matches)

            for match in matches:
                # 1. Run consensus convergence (refine)
                consensus = refine(
                    match,
                    now=now,
                    min_books=self.settings.min_books_telemetry,
                    sharp_keys=self.settings.sharp_keys,
                    sharp_multiplier=self.settings.sharp_multiplier,
                    margin_weighted=self.settings.margin_weighted,
                    min_margin_floor=self.settings.min_margin_floor,
                    max_book_age_sec=self.settings.stale_prematch_sec,
                )
                if consensus is None:
                    continue

                # 2. Check for Market Traps (High CV or sharp divergence on public favorites)
                for outcome_name, p_val in consensus.p.items():
                    if p_val >= 0.65 and (consensus.cv > self.settings.max_cv or consensus.stdev > 0.012):
                        trap_key = f"{match.id}::{outcome_name}::trap"
                        if trap_key not in self._alerted_traps:
                            self._alerted_traps.add(trap_key)
                            res.traps_found += 1
                            if self.telegram_bot:
                                reason = f"Sharp divergence (stdev={consensus.stdev:.3f}, CV={consensus.cv:.1%})"
                                self.telegram_bot.broadcast_trap(
                                    home_team=match.home_team,
                                    away_team=match.away_team,
                                    sport_key=match.sport_key,
                                    public_favorite=outcome_name,
                                    reason=reason,
                                    cv=consensus.cv,
                                )

                # 3. Check for Diamond execution candidates
                gate_result = evaluate_gate(
                    consensus,
                    threshold=self.settings.gate_threshold,
                    min_books=self.settings.min_books_alert,
                    max_cv=self.settings.max_cv,
                    ev_min=self.settings.ev_min,
                    require_positive_ev=self.settings.require_positive_ev,
                )
                if gate_result.pick is not None:
                    pick = gate_result.pick
                    res.diamonds_found += 1
                    new_picks.append(pick)
                    dedupe_key = f"{pick.match_id}::{pick.market}::{pick.outcome_name}"

                    if dedupe_key not in self._alerted_keys:
                        self._alerted_keys.add(dedupe_key)
                        res.alerts_dispatched += 1

                        # Store in persistent/memory ledger
                        if self.storage is not None:
                            self.storage.insert_pick(pick)

                        # Fan out to standard notifier (Log / Discord / Telegram webhook)
                        alert_msg = pick_alert_text(pick)
                        self.notifier.send(alert_msg)

                        # Fan out to rich Telegram Bot broadcast if available
                        if self.telegram_bot:
                            self.telegram_bot.broadcast_diamond(pick)

        # 4. Automated Settlement Evaluation
        if self.auto_settle and self.storage is not None and hasattr(self.client, "get_scores"):
            try:
                from .settle import run_settlement
                s_rep = run_settlement(self.client, self.storage, self.settings, now=now)
                if s_rep.settled_picks:
                    res.settled_count = len(s_rep.settled_picks)
                    logger.info(
                        "[live-ingest] Settled %d picks: %d won, %d lost, %d void",
                        res.settled_count, s_rep.won, s_rep.lost, s_rep.void
                    )
                    if self.notify_settle and self.telegram_bot:
                        for sp in s_rep.settled_picks:
                            self.telegram_bot.broadcast_settlement(sp)
            except Exception as exc:
                logger.warning("[live-ingest] Settlement pass error: %s", exc)

        # 5. Live synchronization with dashboard.json
        self._sync_dashboard(res, now_iso)

        logger.info(
            "[live-ingest] Cycle complete: %d leagues, %d matches, %d diamonds, %d traps, %d alerts, %d settled",
            res.leagues_polled,
            res.matches_seen,
            res.diamonds_found,
            res.traps_found,
            res.alerts_dispatched,
            res.settled_count,
        )
        return res

    def run_daemon(self, max_iterations: Optional[int] = None, interval_sec: int = 60) -> None:
        """Run continuous live polling daemon."""
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

