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
    """One sportsbook's prices for one market of a match.

    ``outcomes`` maps outcome name (team, "Draw", "Over"/"Under") to decimal
    odds. ``line`` is the common quoted point for totals/spreads, or ``None``
    for point-less markets such as h2h.
    """

    key: str
    title: str
    last_update: Optional[datetime]
    outcomes: dict[str, float] = field(default_factory=dict)
    line: Optional[float] = None


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

    def grade_pick(self, market: str, outcome_name: str,
                   line: Optional[float] = None) -> Optional[str]:
        """Grade a pick row against this official score.

        Returns "WIN", "LOSS", "VOID", or None when the score is not final/decidable.
        """
        if not self.completed or self.home_score is None or self.away_score is None:
            return None

        if market == H2H:
            w = self.winner()
            if w is None:
                return None
            return "WIN" if w == outcome_name else "LOSS"

        if market == "totals":
            if line is None:
                return None
            total = self.home_score + self.away_score
            diff = total - line
            if abs(diff) < 1e-6:
                return "VOID"
            name_lower = outcome_name.strip().lower()
            if name_lower == "over":
                return "WIN" if diff > 0 else "LOSS"
            if name_lower == "under":
                return "WIN" if diff < 0 else "LOSS"
            return None

        if market == "spreads":
            if line is None:
                return None
            if outcome_name == self.home_team:
                team_score = self.home_score
                opp_score = self.away_score
            elif outcome_name == self.away_team:
                team_score = self.away_score
                opp_score = self.home_score
            else:
                return None
            diff = (team_score + line) - opp_score
            if abs(diff) < 1e-6:
                return "VOID"
            return "WIN" if diff > 0 else "LOSS"

        return None