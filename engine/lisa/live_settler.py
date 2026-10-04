"""Live Settler — real-time score polling and settlement.

Polls live match scores every 60 seconds during matches and updates
the ledger with real-time results. This replaces the current 3-hour
delayed settlement with instant settlement.

The settler is idempotent: already-terminal rows are never rewritten.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import config as cfg
from .odds import Match, Score, utcnow
from .storage import Storage

logger = logging.getLogger(__name__)

#: How often to poll live scores (seconds)
POLL_INTERVAL_SEC = 60

#: How long after kickoff to start polling
POLL_START_OFFSET_SEC = 0

#: How long after full-time to stop polling
POLL_END_OFFSET_SEC = 30 * 60  # 30 minutes after full-time


@dataclass
class LiveSettlementReport:
    """Report from a live settlement pass."""
    matches_polled: int = 0
    matches_settled: int = 0
    matches_updated: int = 0
    errors: list[str] = field(default_factory=list)
    settled_picks: list[dict] = field(default_factory=list)


class LiveSettler:
    """Real-time score polling and settlement.

    The settler:
    1. Finds matches that are live (started but not finished)
    2. Polls their scores from the data source
    3. Updates the ledger with real-time results
    4. Settles picks when matches go full-time
    """

    def __init__(self, *, storage: Storage, settings: Optional[cfg.Settings] = None,
                 poll_interval: int = POLL_INTERVAL_SEC):
        self.storage = storage
        self.settings = settings
        self.poll_interval = poll_interval
        self._running = False

    def _get_live_matches(self, matches: list[Match], *,
                           now: Optional[datetime] = None) -> list[Match]:
        """Get matches that are currently live (started but not finished)."""
        now = now or utcnow()
        live = []
        for match in matches:
            if match.completed:
                continue
            # Match is live if it started within the last 3 hours
            if timedelta(0) <= now - match.commence_time <= timedelta(hours=3):
                live.append(match)
        return live

    def _fetch_score(self, match: Match) -> Optional[Score]:
        """Fetch the latest score for a match from the data source."""
        # Try the match router first (the free calendar sources)
        try:
            from .match_router import match_router
            score = match_router.get_scores(match.id, match.sport_key)
            if score is not None:
                return score
        except Exception as exc:
            logger.debug("Failed to fetch score for %s: %s", match.id, exc)

        # Fallback: return a score from the match data if available
        return None

    def _settle_pick(self, pick: dict, score: Score) -> Optional[str]:
        """Settle a pick based on the score. Returns the result or None."""
        market = pick.get("market", "h2h")
        outcome = pick.get("outcome_name", "")
        line = pick.get("line")

        # Use the Score's grade_pick method
        result = score.grade_pick(market, outcome, line)
        return result

    def settle_match(self, match: Match, score: Score, *,
                     now: Optional[datetime] = None) -> int:
        """Settle all picks for a match. Returns the number of picks settled."""
        now = now or utcnow()
        settled_count = 0

        # Get all pending picks for this match
        pending = self.storage.list_pending_picks()
        for pick in pending:
            if pick.get("match_id") != match.id:
                continue

            result = self._settle_pick(pick, score)
            if result is None:
                continue

            # Settle the pick
            dedupe_key = pick["dedupe_key"]
            if self.storage.settle_pick(dedupe_key, result, now, state="SETTLED"):
                settled_count += 1
                logger.info(
                    "Settled pick %s: %s (%s %s)",
                    dedupe_key, result, pick.get("home_team"), pick.get("away_team"),
                )

        return settled_count

    def run_pass(self, matches: list[Match], *, now: Optional[datetime] = None) -> LiveSettlementReport:
        """Run a single settlement pass over live matches.

        Args:
            matches: List of all known matches
            now: Reference time

        Returns:
            LiveSettlementReport with results
        """
        now = now or utcnow()
        report = LiveSettlementReport()

        # Get live matches
        live_matches = self._get_live_matches(matches, now=now)
        report.matches_polled = len(live_matches)

        for match in live_matches:
            try:
                # Fetch the latest score
                score = self._fetch_score(match)
                if score is None:
                    continue

                # If the match is full-time, settle all picks
                if score.completed:
                    settled = self.settle_match(match, score, now=now)
                    report.matches_settled += 1
                    report.settled_picks.extend([
                        {"match_id": match.id, "result": "settled"}
                        for _ in range(settled)
                    ])
                else:
                    # Match is still live — update scores but don't settle yet
                    report.matches_updated += 1

            except Exception as exc:
                report.errors.append(f"{match.id}: {exc}")
                logger.warning("Failed to settle match %s: %s", match.id, exc)

        return report

    def run_forever(self, matches: list[Match], *,
                    max_iterations: Optional[int] = None) -> None:
        """Run the settler in a loop until stopped.

        Args:
            matches: List of all known matches
            max_iterations: Maximum number of iterations (None = infinite)
        """
        self._running = True
        iteration = 0

        while self._running:
            report = self.run_pass(matches)
            if report.matches_settled > 0:
                logger.info(
                    "Live settlement: %d matches settled, %d updated",
                    report.matches_settled, report.matches_updated,
                )

            iteration += 1
            if max_iterations is not None and iteration >= max_iterations:
                break

            time.sleep(self.poll_interval)

    def stop(self) -> None:
        """Stop the settler loop."""
        self._running = False


# Global settler instance
live_settler = LiveSettler(storage=None)  # Will be initialized with storage when needed
