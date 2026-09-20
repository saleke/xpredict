"""Core domain types: raw snapshots normalised from The Odds API.

All times are timezone-aware UTC datetimes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

H2H = "h2h"


def utcnow() -> datetime:
    """Test-friendly single source of "now" (UTC)."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Book:
    """One sportsbook's prices for the ``h2h`` market of a match.

    ``outcomes`` maps outcome name (team name or "Draw") -> decimal odds.
    """

    key: str
    title: str
    last_update: Optional[datetime]
    outcomes: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Match:
    """A single game with one or more books, normalised for the engine."""

    id: str
    sport_key: str
    commence_time: datetime
    home_team: str
    away_team: str
    completed: bool
    market: str = H2H
    bookmakers: tuple[Book, ...] = ()


@dataclass(frozen=True)
class SportInfo:
    key: str
    active: bool
    title: str


@dataclass(frozen=True)
class Score:
    """Official result used for settlement."""

    match_id: str
    sport_key: str
    commence_time: datetime
    completed: bool
    home_score: Optional[int]
    away_score: Optional[int]
    status: str  # final | live | postponed | cancelled | scheduled ...
    home_team: str = ""
    away_team: str = ""

    def winner(self) -> Optional[str]:
        """Outcome name of the winner, or None when not decidable yet."""
        if not self.completed or self.home_score is None or self.away_score is None:
            return None
        if self.home_score > self.away_score:
            return self.home_team
        if self.away_score > self.home_score:
            return self.away_team
        return "Draw"