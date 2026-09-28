"""Exact Score — Poisson most-likely scoreline prediction.

Uses the Elo + Poisson model to calculate the most likely exact score
for a match. This is the WinDrawWin-style signature feature that every
tip comes with a correct score prediction.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

from .model import EloPoissonModel, _poisson_pmf

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScorelinePrediction:
    """A predicted scoreline with confidence."""
    home_goals: int
    away_goals: int
    probability: float
    confidence: str  # "high" | "medium" | "low"

    def __str__(self) -> str:
        return f"{self.home_goals}-{self.away_goals}"

    def to_dict(self) -> dict:
        return {
            "scoreline": str(self),
            "home_goals": self.home_goals,
            "away_goals": self.away_goals,
            "probability": round(self.probability, 4),
            "confidence": self.confidence,
        }


class ExactScorePredictor:
    """Predicts the most likely exact scoreline using Poisson probabilities.

    The predictor uses the Elo + Poisson model to calculate the probability
    of each possible scoreline and returns the most likely one.
    """

    def __init__(self, *, model: Optional[EloPoissonModel] = None,
                 max_goals: int = 8):
        self.model = model or EloPoissonModel()
        self.max_goals = max_goals

    def predict(self, league: str, home: str, away: str) -> Optional[ScorelinePrediction]:
        """Predict the most likely exact scoreline for a match.

        Args:
            league: League identifier
            home: Home team name
            away: Away team name

        Returns:
            ScorelinePrediction or None if model is not ready
        """
        if not self.model.ready(league, home, away):
            return None

        # Get expected goals from the model
        lam_home, lam_away = self.model._lambdas(league, home, away)

        # Calculate the most likely scoreline
        best_score = None
        best_prob = 0.0

        for hg in range(self.max_goals + 1):
            for ag in range(self.max_goals + 1):
                prob = _poisson_pmf(hg, lam_home) * _poisson_pmf(ag, lam_away)
                if prob > best_prob:
                    best_prob = prob
                    best_score = (hg, ag)

        if best_score is None:
            return None

        # Determine confidence based on probability
        confidence = "low"
        if best_prob >= 0.15:
            confidence = "high"
        elif best_prob >= 0.10:
            confidence = "medium"

        return ScorelinePrediction(
            home_goals=best_score[0],
            away_goals=best_score[1],
            probability=best_prob,
            confidence=confidence,
        )

    def predict_with_alternatives(self, league: str, home: str, away: str,
                                   top_n: int = 3) -> list[ScorelinePrediction]:
        """Predict the top N most likely scorelines.

        Args:
            league: League identifier
            home: Home team name
            away: Away team name
            top_n: Number of alternative scorelines to return

        Returns:
            List of ScorelinePrediction, sorted by probability (highest first)
        """
        if not self.model.ready(league, home, away):
            return []

        lam_home, lam_away = self.model._lambdas(league, home, away)

        # Calculate all scoreline probabilities
        scores: list[tuple[float, int, int]] = []
        for hg in range(self.max_goals + 1):
            for ag in range(self.max_goals + 1):
                prob = _poisson_pmf(hg, lam_home) * _poisson_pmf(ag, lam_away)
                scores.append((prob, hg, ag))

        # Sort by probability (highest first)
        scores.sort(reverse=True)

        # Return top N
        predictions = []
        for prob, hg, ag in scores[:top_n]:
            confidence = "low"
            if prob >= 0.15:
                confidence = "high"
            elif prob >= 0.10:
                confidence = "medium"

            predictions.append(ScorelinePrediction(
                home_goals=hg,
                away_goals=ag,
                probability=prob,
                confidence=confidence,
            ))

        return predictions

    def get_scoreline_matrix(self, league: str, home: str, away: str,
                             max_goals: int = 5) -> list[list[float]]:
        """Get the full scoreline probability matrix.

        Args:
            league: League identifier
            home: Home team name
            away: Away team name
            max_goals: Maximum goals to consider

        Returns:
            2D list where [i][j] is the probability of score i-j
        """
        if not self.model.ready(league, home, away):
            return []

        lam_home, lam_away = self.model._lambdas(league, home, away)

        matrix = []
        for hg in range(max_goals + 1):
            row = []
            for ag in range(max_goals + 1):
                prob = _poisson_pmf(hg, lam_home) * _poisson_pmf(ag, lam_away)
                row.append(round(prob, 4))
            matrix.append(row)

        return matrix


# Global predictor instance
exact_score_predictor = ExactScorePredictor()
