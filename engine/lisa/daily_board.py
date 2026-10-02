"""Daily board — organizes matches by date for the product interface.

Groups matches into:
  * Today
  * Tomorrow
  * This Week
  * All Upcoming

Each match carries its tier classification and available data sources.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import match_router as mr
from .odds import Match, utcnow

logger = logging.getLogger(__name__)


@dataclass
class BoardEntry:
    """A single match on the daily board."""
    match: Match
    league_tier: str  # "A", "B", or "C"
    has_odds: bool
    has_scores: bool
    #: Which free calendar sources cover this league, cheapest-first.
    sources: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize for API response."""
        return {
            "match_id": self.match.id,
            "sport_key": self.match.sport_key,
            "home_team": self.match.home_team,
            "away_team": self.match.away_team,
            "commence_time": self.match.commence_time.isoformat(),
            "completed": self.match.completed,
            "league_tier": self.league_tier,
            "has_odds": self.has_odds,
            "has_scores": self.has_scores,
            "sources": list(self.sources),
        }


@dataclass
class DailyBoard:
    """Matches grouped by date."""
    today: list[BoardEntry] = field(default_factory=list)
    tomorrow: list[BoardEntry] = field(default_factory=list)
    this_week: list[BoardEntry] = field(default_factory=list)
    all_upcoming: list[BoardEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for API response."""
        return {
            "today": [e.to_dict() for e in self.today],
            "tomorrow": [e.to_dict() for e in self.tomorrow],
            "this_week": [e.to_dict() for e in self.this_week],
            "all_upcoming": [e.to_dict() for e in self.all_upcoming],
        }


class DailyBoardBuilder:
    """Builds the daily board from match data sources."""

    def __init__(self, *, router: Optional[mr.MatchRouter] = None):
        self.router = router or mr.match_router

    def _classify_entry(self, match: Match) -> BoardEntry:
        """Classify a match into a board entry."""
        tier = self.router.classify_league(match.sport_key)
        return BoardEntry(
            match=match,
            league_tier=tier.tier,
            has_odds=tier.has_odds,
            has_scores=tier.has_scores,
            sources=tier.sources,
        )

    def build_board(self, matches: list[Match], *,
                    now: Optional[datetime] = None) -> DailyBoard:
        """Build a daily board from a list of matches.

        Args:
            matches: List of matches to organize
            now: Reference time (defaults to utcnow())

        Returns:
            DailyBoard with matches grouped by date
        """
        now = now or utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timedelta(days=1)
        tomorrow_end = today_end + timedelta(days=1)
        week_end = today_start + timedelta(days=7)

        board = DailyBoard()

        for match in matches:
            entry = self._classify_entry(match)
            ct = match.commence_time

            # Skip completed matches
            if match.completed:
                continue

            # Categorize by date
            if today_start <= ct < today_end:
                board.today.append(entry)
            elif today_end <= ct < tomorrow_end:
                board.tomorrow.append(entry)
            elif ct < week_end:
                board.this_week.append(entry)
            else:
                board.all_upcoming.append(entry)

        # Sort each group by commence time
        board.today.sort(key=lambda e: e.match.commence_time)
        board.tomorrow.sort(key=lambda e: e.match.commence_time)
        board.this_week.sort(key=lambda e: e.match.commence_time)
        board.all_upcoming.sort(key=lambda e: e.match.commence_time)

        return board

    def build_from_leagues(self, sport_keys: list[str], *,
                           days_ahead: int = 7,
                           now: Optional[datetime] = None) -> DailyBoard:
        """Build a daily board by fetching matches from multiple leagues.

        Args:
            sport_keys: List of league identifiers
            days_ahead: Number of days ahead to fetch
            now: Reference time

        Returns:
            DailyBoard with matches grouped by date
        """
        all_matches: list[Match] = []
        for sport_key in sport_keys:
            try:
                matches = self.router.get_matches(sport_key, days_ahead=days_ahead)
                all_matches.extend(matches)
            except Exception as exc:
                logger.warning("Failed to fetch matches for %s: %s", sport_key, exc)

        return self.build_board(all_matches, now=now)

    def get_tier_matches(self, board: DailyBoard, tier: str) -> DailyBoard:
        """Filter board entries by league tier.

        Args:
            board: DailyBoard to filter
            tier: "A", "B", or "C"

        Returns:
            Filtered DailyBoard
        """
        return DailyBoard(
            today=[e for e in board.today if e.league_tier == tier],
            tomorrow=[e for e in board.tomorrow if e.league_tier == tier],
            this_week=[e for e in board.this_week if e.league_tier == tier],
            all_upcoming=[e for e in board.all_upcoming if e.league_tier == tier],
        )

    def get_free_tier_board(self, board: DailyBoard) -> DailyBoard:
        """Get board entries available to free users (tier C only)."""
        return self.get_tier_matches(board, "C")

    def get_paid_tier_board(self, board: DailyBoard) -> DailyBoard:
        """Get board entries available to paid users (tier A + B)."""
        return DailyBoard(
            today=[e for e in board.today if e.league_tier in ("A", "B")],
            tomorrow=[e for e in board.tomorrow if e.league_tier in ("A", "B")],
            this_week=[e for e in board.this_week if e.league_tier in ("A", "B")],
            all_upcoming=[e for e in board.all_upcoming if e.league_tier in ("A", "B")],
        )


# Global builder instance
daily_board_builder = DailyBoardBuilder()
