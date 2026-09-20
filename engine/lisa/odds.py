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
                for part in outcome_name.split():
                    try:
                        line = float(part)
                        break
                    except ValueError:
                        pass
            if line is None:
                return None
            total = self.home_score + self.away_score
            diff = total - line
            if abs(diff) < 1e-6:
                return "VOID"
            name_lower = outcome_name.strip().lower()
            if "over" in name_lower:
                return "WIN" if diff > 0 else "LOSS"
            if "under" in name_lower:
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

        if market in ("double_chance", "dc"):
            name_lower = outcome_name.strip().lower()
            # 1X: Home or Draw
            if (
                name_lower in ("1x", "home or draw", "home/draw")
                or f"{self.home_team.lower()} or draw" in name_lower
                or "1x" in name_lower
            ):
                return "WIN" if self.home_score >= self.away_score else "LOSS"
            # X2: Away or Draw
            if (
                name_lower in ("x2", "draw or away", "draw/away")
                or f"draw or {self.away_team.lower()}" in name_lower
                or "x2" in name_lower
            ):
                return "WIN" if self.away_score >= self.home_score else "LOSS"
            # 12: Home or Away (no draw)
            if (
                name_lower in ("12", "home or away", "home/away")
                or f"{self.home_team.lower()} or {self.away_team.lower()}" in name_lower
                or "12" in name_lower
            ):
                return "WIN" if self.home_score != self.away_score else "LOSS"
            return None

        if market in ("btts", "both_teams_to_score"):
            btts = self.home_score > 0 and self.away_score > 0
            name_lower = outcome_name.strip().lower()
            if "yes" in name_lower:
                return "WIN" if btts else "LOSS"
            if "no" in name_lower:
                return "WIN" if not btts else "LOSS"
            return None

        if market in ("team_totals", "team_total"):
            if line is None:
                for part in outcome_name.split():
                    try:
                        line = float(part)
                        break
                    except ValueError:
                        pass
            if line is None:
                return None
            name_lower = outcome_name.strip().lower()
            if self.home_team.lower() in name_lower:
                diff = self.home_score - line
            elif self.away_team.lower() in name_lower:
                diff = self.away_score - line
            else:
                return None
            if abs(diff) < 1e-6:
                return "VOID"
            if "over" in name_lower:
                return "WIN" if diff > 0 else "LOSS"
            if "under" in name_lower:
                return "WIN" if diff < 0 else "LOSS"
            return None

        return None