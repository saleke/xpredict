"""Core domain types: raw snapshots normalised from The Odds API.

All times are timezone-aware UTC datetimes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

H2H = "h2h"


def _invert_handicap(graded: dict) -> dict:
    """Flip a home-perspective handicap grade to the opposite side.

    A bet on the away side at line L is the exact complement of a home bet at
    -L: what was a full win is a full loss, a half win a half loss, and a push
    is still a push (neither side was right). Quarter balls invert cleanly
    because the two half-staked sub-lines swap with the sign.
    """
    flipped = dict(graded)
    result = graded.get("result")
    if result == "WIN":
        flipped["result"] = "LOSS"
        flipped["won_fraction"] = 0.0
    elif result == "LOSS":
        flipped["result"] = "WIN"
        flipped["won_fraction"] = graded.get("stake_fraction", 1.0)
    elif result == "HALF_WIN":
        flipped["result"] = "HALF_LOSS"
        flipped["won_fraction"] = 0.0
    elif result == "HALF_LOSS":
        flipped["result"] = "HALF_WIN"
        flipped["won_fraction"] = graded.get("stake_fraction", 0.5)
    # PUSH / VOID is unchanged: it was undecided on both sides.
    return flipped


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
            name_lower = outcome_name.strip().lower()
            # Asian totals can be quarter balls too ("Over 2.5,3.0" is the same
            # stake split across 2.5 and 3.0), so a whole-ball push test alone
            # is not enough. markets.grade_total owns that logic.
            from .markets import grade_total
            if "over" in name_lower:
                graded = grade_total(self.home_score, self.away_score, line, "over")
            elif "under" in name_lower:
                graded = grade_total(self.home_score, self.away_score, line, "under")
            else:
                return None
            result = graded.get("result")
            return "VOID" if result == "PUSH" else result

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
            # Quarter balls (-0.25, +0.75, ...) are two half-staked lines and
            # resolve to HALF_WIN / HALF_LOSS, never to a single WIN / LOSS.
            # Routing through markets.grade_asian_handicap makes this the one
            # grading implementation in the codebase; the inline arithmetic that
            # used to live here silently rounded a quarter ball to a whole one,
            # so a -0.25 bet that half-lost was graded as a full loss.
            from .markets import grade_asian_handicap
            # ``line`` here is the OUTCOME's own handicap, not the home team's.
            # markets.grade_asian_handicap always reads the line as the home
            # team's point, so an away bet is graded by mirroring it: settle
            # ``-line`` from the home perspective and invert the result.
            #
            # The previous inline arithmetic here used the outcome's own line
            # directly, which was right, but routing it through markets naively
            # (side="away" with the same line) is wrong: markets negates the
            # whole delta, including the line, so Knicks at +8.0 graded as a
            # LOSS when it is a PUSH.
            is_home = outcome_name == self.home_team
            graded = grade_asian_handicap(self.home_score, self.away_score,
                                          line if is_home else -line, "home")
            if not is_home:
                graded = _invert_handicap(graded)
            result = graded.get("result")
            # markets grades an exactly-on-the-line bet as PUSH. The ledger's
            # terminal vocabulary is WIN / LOSS / VOID, so translate at the
            # boundary rather than widening the storage contract to accept both
            # spellings of the same thing.
            return "VOID" if result == "PUSH" else result

        if market in ("btts", "both_teams_to_score"):
            name_lower = outcome_name.strip().lower()
            both = (self.home_score or 0) > 0 and (self.away_score or 0) > 0
            if name_lower in ("yes", "y", "btts yes"):
                return "WIN" if both else "LOSS"
            if name_lower in ("no", "n", "btts no"):
                return "LOSS" if both else "WIN"
            return None

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