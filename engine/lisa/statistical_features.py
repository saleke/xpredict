"""Statistical features — WinDrawWin-style form, BTTS, and Over/Under stats.

These features power the free tier's statistical predictions and provide
input features for the hybrid model in paid tiers.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .odds import Match, Score, utcnow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TeamForm:
    """Recent form for a team (last N matches)."""
    team_name: str
    last_results: tuple[str, ...]  # "W", "L", "D" (most recent first)
    wins: int
    draws: int
    losses: int
    goals_scored: int
    goals_conceded: int
    goals_scored_home: int = 0
    goals_conceded_home: int = 0
    goals_scored_away: int = 0
    goals_conceded_away: int = 0

    @property
    def points(self) -> int:
        """3 points per win, 1 per draw."""
        return self.wins * 3 + self.draws

    @property
    def played(self) -> int:
        return self.wins + self.draws + self.losses

    @property
    def goal_difference(self) -> int:
        return self.goals_scored - self.goals_conceded

    @property
    def avg_scored(self) -> float:
        return self.goals_scored / max(1, self.played)

    @property
    def avg_conceded(self) -> float:
        return self.goals_conceded / max(1, self.played)

    def to_dict(self) -> dict[str, Any]:
        return {
            "team_name": self.team_name,
            "last_results": list(self.last_results),
            "wins": self.wins,
            "draws": self.draws,
            "losses": self.losses,
            "goals_scored": self.goals_scored,
            "goals_conceded": self.goals_conceded,
            "goal_difference": self.goal_difference,
            "avg_scored": round(self.avg_scored, 2),
            "avg_conceded": round(self.avg_conceded, 2),
            "points": self.points,
            "played": self.played,
        }


@dataclass(frozen=True)
class MatchStats:
    """Statistical features for a single match."""
    match_id: str
    home_team: str
    away_team: str
    home_form: Optional[TeamForm] = None
    away_form: Optional[TeamForm] = None
    # BTTS stats
    home_btts_rate: float = 0.0  # % of home games where both teams scored
    away_btts_rate: float = 0.0  # % of away games where both teams scored
    h2h_btts_rate: float = 0.0   # % of head-to-head matches with BTTS
    # Over/Under stats
    home_over25_rate: float = 0.0
    away_over25_rate: float = 0.0
    h2h_over25_rate: float = 0.0
    # Head-to-head
    h2h_home_wins: int = 0
    h2h_draws: int = 0
    h2h_away_wins: int = 0
    h2h_total: int = 0
    # Derived predictions
    predicted_btts: Optional[bool] = None
    predicted_over25: Optional[bool] = None
    most_likely_score: Optional[str] = None
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "home_form": self.home_form.to_dict() if self.home_form else None,
            "away_form": self.away_form.to_dict() if self.away_form else None,
            "home_btts_rate": round(self.home_btts_rate, 3),
            "away_btts_rate": round(self.away_btts_rate, 3),
            "h2h_btts_rate": round(self.h2h_btts_rate, 3),
            "home_over25_rate": round(self.home_over25_rate, 3),
            "away_over25_rate": round(self.away_over25_rate, 3),
            "h2h_over25_rate": round(self.h2h_over25_rate, 3),
            "h2h_home_wins": self.h2h_home_wins,
            "h2h_draws": self.h2h_draws,
            "h2h_away_wins": self.h2h_away_wins,
            "h2h_total": self.h2h_total,
            "predicted_btts": self.predicted_btts,
            "predicted_over25": self.predicted_over25,
            "most_likely_score": self.most_likely_score,
            "confidence": round(self.confidence, 3),
        }


class StatisticalFeatureEngine:
    """Computes statistical features from historical match data.

    This is the free-tier prediction engine. It uses simple statistical
    heuristics (form, BTTS rates, O/U rates) to generate predictions
    without any AI model. Paid tiers layer the hybrid model on top.
    """

    def __init__(self, *, form_window: int = 5, min_matches: int = 3):
        self.form_window = form_window
        self.min_matches = min_matches
        self._history: dict[str, list[Score]] = {}  # team -> scores

    def load_history(self, scores: list[Score]) -> None:
        """Load historical scores for feature computation."""
        for score in scores:
            if not score.completed:
                continue
            for team in (score.home_team, score.away_team):
                if team not in self._history:
                    self._history[team] = []
                self._history[team].append(score)
        # Sort each team's history by date
        for team in self._history:
            self._history[team].sort(key=lambda s: s.commence_time)

    def get_team_form(self, team_name: str, *,
                      now: Optional[datetime] = None) -> Optional[TeamForm]:
        """Compute recent form for a team."""
        now = now or utcnow()
        scores = self._history.get(team_name, [])
        if not scores:
            return None

        # Filter to matches before now
        past = [s for s in scores if s.commence_time < now]
        if len(past) < self.min_matches:
            return None

        # Take last N matches
        recent = past[-self.form_window:]

        results: list[str] = []
        wins = draws = losses = 0
        scored = conceded = 0
        scored_home = conceded_home = 0
        scored_away = conceded_away = 0

        for s in recent:
            is_home = s.home_team == team_name
            team_score = s.home_score if is_home else s.away_score
            opp_score = s.away_score if is_home else s.home_score

            if team_score is None or opp_score is None:
                continue

            scored += team_score
            conceded += opp_score

            if is_home:
                scored_home += team_score
                conceded_home += opp_score
            else:
                scored_away += team_score
                conceded_away += opp_score

            if team_score > opp_score:
                results.append("W")
                wins += 1
            elif team_score < opp_score:
                results.append("L")
                losses += 1
            else:
                results.append("D")
                draws += 1

        if not results:
            return None

        return TeamForm(
            team_name=team_name,
            last_results=tuple(results),
            wins=wins,
            draws=draws,
            losses=losses,
            goals_scored=scored,
            goals_conceded=conceded,
            goals_scored_home=scored_home,
            goals_conceded_home=conceded_home,
            goals_scored_away=scored_away,
            goals_conceded_away=conceded_away,
        )

    def _compute_btts_rate(self, team_name: str, *, now: Optional[datetime] = None,
                           home_only: bool = False) -> float:
        """Compute BTTS rate for a team."""
        now = now or utcnow()
        scores = self._history.get(team_name, [])
        if not scores:
            return 0.0

        past = [s for s in scores if s.commence_time < now and s.completed]
        if home_only:
            past = [s for s in past if s.home_team == team_name]

        if len(past) < self.min_matches:
            return 0.0

        btts_count = sum(
            1 for s in past
            if s.home_score is not None and s.away_score is not None
            and s.home_score > 0 and s.away_score > 0
        )
        return btts_count / len(past)

    def _compute_over25_rate(self, team_name: str, *, now: Optional[datetime] = None,
                             home_only: bool = False) -> float:
        """Compute Over 2.5 goals rate for a team."""
        now = now or utcnow()
        scores = self._history.get(team_name, [])
        if not scores:
            return 0.0

        past = [s for s in scores if s.commence_time < now and s.completed]
        if home_only:
            past = [s for s in past if s.home_team == team_name]

        if len(past) < self.min_matches:
            return 0.0

        over_count = sum(
            1 for s in past
            if s.home_score is not None and s.away_score is not None
            and (s.home_score + s.away_score) > 2.5
        )
        return over_count / len(past)

    def _compute_h2h(self, home_team: str, away_team: str, *,
                     now: Optional[datetime] = None) -> dict[str, Any]:
        """Compute head-to-head statistics."""
        now = now or utcnow()
        home_scores = self._history.get(home_team, [])
        away_scores = self._history.get(away_team, [])

        # Find matches where these two teams played each other
        h2h_matches: list[Score] = []
        for s in home_scores:
            if s.commence_time >= now:
                continue
            if (s.home_team == home_team and s.away_team == away_team) or \
               (s.home_team == away_team and s.away_team == home_team):
                h2h_matches.append(s)

        if not h2h_matches:
            return {"home_wins": 0, "draws": 0, "away_wins": 0, "total": 0,
                    "btts_rate": 0.0, "over25_rate": 0.0}

        home_wins = draws = away_wins = 0
        btts_count = 0
        over25_count = 0

        for s in h2h_matches:
            if s.home_score is None or s.away_score is None:
                continue

            # Normalize to the perspective of home_team
            if s.home_team == home_team:
                hs, aws = s.home_score, s.away_score
            else:
                hs, aws = s.away_score, s.home_score

            if hs > aws:
                home_wins += 1
            elif hs < aws:
                away_wins += 1
            else:
                draws += 1

            if s.home_score > 0 and s.away_score > 0:
                btts_count += 1
            if (s.home_score + s.away_score) > 2.5:
                over25_count += 1

        total = len(h2h_matches)
        return {
            "home_wins": home_wins,
            "draws": draws,
            "away_wins": away_wins,
            "total": total,
            "btts_rate": btts_count / total if total else 0.0,
            "over25_rate": over25_count / total if total else 0.0,
        }

    def _predict_scoreline(self, home_form: Optional[TeamForm],
                           away_form: Optional[TeamForm]) -> Optional[str]:
        """Predict the most likely scoreline from form data."""
        if home_form is None or away_form is None:
            return None

        # Simple Poisson-like estimate from average goals
        home_exp = home_form.avg_scored * 0.7 + away_form.avg_conceded * 0.3
        away_exp = away_form.avg_scored * 0.7 + home_form.avg_conceded * 0.3

        # Clamp to reasonable range
        home_exp = max(0.3, min(4.0, home_exp))
        away_exp = max(0.2, min(3.5, away_exp))

        # Most likely scoreline (rounded)
        home_goals = round(home_exp)
        away_goals = round(away_exp)

        return f"{home_goals}-{away_goals}"

    def compute_match_stats(self, match: Match, *,
                            now: Optional[datetime] = None) -> MatchStats:
        """Compute all statistical features for a match."""
        now = now or utcnow()

        home_form = self.get_team_form(match.home_team, now=now)
        away_form = self.get_team_form(match.away_team, now=now)

        home_btts = self._compute_btts_rate(match.home_team, now=now, home_only=True)
        away_btts = self._compute_btts_rate(match.away_team, now=now, home_only=False)

        home_over25 = self._compute_over25_rate(match.home_team, now=now, home_only=True)
        away_over25 = self._compute_over25_rate(match.away_team, now=now, home_only=False)

        h2h = self._compute_h2h(match.home_team, match.away_team, now=now)

        # Predictions
        predicted_btts = None
        if home_btts > 0.5 and away_btts > 0.5:
            predicted_btts = True
        elif home_btts < 0.3 and away_btts < 0.3:
            predicted_btts = False

        predicted_over25 = None
        if home_over25 > 0.6 and away_over25 > 0.6:
            predicted_over25 = True
        elif home_over25 < 0.4 and away_over25 < 0.4:
            predicted_over25 = False

        most_likely_score = self._predict_scoreline(home_form, away_form)

        # Confidence based on data availability
        confidence = 0.0
        if home_form and away_form:
            confidence += 0.3
        if h2h["total"] > 0:
            confidence += 0.2
        if home_btts > 0:
            confidence += 0.15
        if away_btts > 0:
            confidence += 0.15
        if home_over25 > 0:
            confidence += 0.1
        if away_over25 > 0:
            confidence += 0.1

        return MatchStats(
            match_id=match.id,
            home_team=match.home_team,
            away_team=match.away_team,
            home_form=home_form,
            away_form=away_form,
            home_btts_rate=home_btts,
            away_btts_rate=away_btts,
            h2h_btts_rate=h2h["btts_rate"],
            home_over25_rate=home_over25,
            away_over25_rate=away_over25,
            h2h_over25_rate=h2h["over25_rate"],
            h2h_home_wins=h2h["home_wins"],
            h2h_draws=h2h["draws"],
            h2h_away_wins=h2h["away_wins"],
            h2h_total=h2h["total"],
            predicted_btts=predicted_btts,
            predicted_over25=predicted_over25,
            most_likely_score=most_likely_score,
            confidence=round(confidence, 3),
        )

    def compute_board_stats(self, matches: list[Match], *,
                            now: Optional[datetime] = None) -> list[MatchStats]:
        """Compute stats for a list of matches."""
        return [self.compute_match_stats(m, now=now) for m in matches]


# Global engine instance
statistical_engine = StatisticalFeatureEngine()
