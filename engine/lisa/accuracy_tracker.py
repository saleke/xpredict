"""Accuracy Tracker — ROI, CLV, and calibration metrics.

Tracks the performance of picks across multiple dimensions:
- ROI (Return on Investment)
- CLV (Closing Line Value)
- Calibration (predicted vs actual)
- Win rate by tier, odds range, sport

This is the honest measurement layer that proves the model works.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .odds import utcnow
from .storage import Storage

logger = logging.getLogger(__name__)


@dataclass
class AccuracyMetrics:
    """Comprehensive accuracy metrics."""
    # Overall
    total_picks: int = 0
    settled_picks: int = 0
    wins: int = 0
    losses: int = 0
    voids: int = 0
    win_rate: float = 0.0
    roi: float = 0.0
    roi_pct: float = 0.0

    # CLV
    mean_clv: float = 0.0
    positive_clv_share: float = 0.0

    # Calibration
    calibration_error: float = 0.0  # Brier score
    calibration_curve: list[dict] = field(default_factory=list)

    # By tier
    by_tier: dict[str, dict] = field(default_factory=dict)

    # By odds range
    by_odds_range: dict[str, dict] = field(default_factory=dict)

    # By sport
    by_sport: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_picks": self.total_picks,
            "settled_picks": self.settled_picks,
            "wins": self.wins,
            "losses": self.losses,
            "voids": self.voids,
            "win_rate": round(self.win_rate, 4),
            "roi": round(self.roi, 4),
            "roi_pct": round(self.roi_pct, 2),
            "mean_clv": round(self.mean_clv, 4),
            "positive_clv_share": round(self.positive_clv_share, 4),
            "calibration_error": round(self.calibration_error, 4),
            "by_tier": self.by_tier,
            "by_odds_range": self.by_odds_range,
            "by_sport": self.by_sport,
        }


class AccuracyTracker:
    """Tracks and calculates accuracy metrics for picks.

    The tracker reads settled picks from storage and calculates:
    - ROI: (profit / stake) * 100
    - CLV: (closing odds - fair odds) / fair odds
    - Calibration: Brier score (lower is better)
    - Win rate by tier, odds range, sport
    """

    def __init__(self, *, storage: Storage):
        self.storage = storage

    def _get_odds_range(self, odds: float) -> str:
        """Classify odds into ranges."""
        if odds < 1.50:
            return "under_1.50"
        if odds < 2.00:
            return "1.50-2.00"
        if odds < 3.00:
            return "2.00-3.00"
        if odds < 5.00:
            return "3.00-5.00"
        if odds < 10.00:
            return "5.00-10.00"
        return "10.00+"

    def _calculate_roi(self, picks: list[dict]) -> float:
        """Calculate ROI: (profit / stake) * 100."""
        total_stake = 0.0
        total_profit = 0.0

        for pick in picks:
            if pick.get("result") not in ("WIN", "HALF_WIN", "LOSS", "HALF_LOSS", "VOID"):
                continue

            # Forecast accuracy includes every published selection; financial
            # returns require an actual quoted recommendation at publication.
            if not pick.get("is_recommendation", True):
                continue
            odds = pick.get("best_odds")
            if not isinstance(odds, (int, float)) or not math.isfinite(odds) or odds <= 1:
                continue
            stake = 1.0  # Flat-unit analytical return, not an account balance.

            total_stake += stake
            from .contracts import unit_profit
            total_profit += unit_profit(pick['result'], odds) * stake

        if total_stake == 0:
            return 0.0
        return (total_profit / total_stake) * 100

    def _calculate_clv(self, picks: list[dict]) -> tuple[float, float]:
        """Calculate mean CLV and positive CLV share."""
        clv_values = []
        positive_count = 0

        for pick in picks:
            clv = pick.get("clv")
            if clv is None:
                continue
            clv_values.append(clv)
            if clv > 0:
                positive_count += 1

        if not clv_values:
            return 0.0, 0.0

        mean_clv = sum(clv_values) / len(clv_values)
        positive_share = positive_count / len(clv_values)
        return mean_clv, positive_share

    def _calculate_calibration(self, picks: list[dict]) -> float:
        """Calculate Brier score (calibration error). Lower is better."""
        total_error = 0.0
        count = 0

        for pick in picks:
            if pick.get("result") not in ("WIN", "LOSS"):
                continue

            from .contracts import is_binary_contract
            if not is_binary_contract(pick.get("market"), pick.get("line")):
                continue
            p_true = pick.get("p_true", 0.5)
            actual = 1.0 if pick["result"] == "WIN" else 0.0

            total_error += (p_true - actual) ** 2
            count += 1

        if count == 0:
            return 0.0
        return total_error / count

    def _group_by(self, picks: list[dict], key_fn) -> dict[str, dict]:
        """Group picks by a key function and calculate metrics."""
        groups: dict[str, list[dict]] = {}
        for pick in picks:
            key = key_fn(pick)
            if key not in groups:
                groups[key] = []
            groups[key].append(pick)

        result = {}
        for key, group_picks in groups.items():
            wins = sum(1 for p in group_picks if p.get("result") == "WIN")
            losses = sum(1 for p in group_picks if p.get("result") == "LOSS")
            total = wins + losses
            win_rate = wins / total if total > 0 else 0.0
            roi = self._calculate_roi(group_picks)

            result[key] = {
                "count": len(group_picks),
                "wins": wins,
                "losses": losses,
                "win_rate": round(win_rate, 4),
                "roi": round(roi, 2),
            }

        return result

    def calculate_metrics(self, *, tier: Optional[str] = None,
                          now: Optional[datetime] = None) -> AccuracyMetrics:
        """Calculate all accuracy metrics.

        Args:
            tier: Filter by tier (None = all tiers)
            now: Reference time

        Returns:
            AccuracyMetrics with all calculated metrics
        """
        now = now or utcnow()
        metrics = AccuracyMetrics()

        # Get settled picks
        settled = self.storage.list_settled_picks()
        if tier:
            settled = [p for p in settled if p.get("tier") == tier]

        metrics.settled_picks = len(settled)
        metrics.wins = sum(1 for p in settled if p.get("result") == "WIN")
        metrics.losses = sum(1 for p in settled if p.get("result") == "LOSS")
        metrics.voids = sum(1 for p in settled if p.get("result") == "VOID")

        total_decided = metrics.wins + metrics.losses
        metrics.win_rate = metrics.wins / total_decided if total_decided > 0 else 0.0

        # ROI
        metrics.roi_pct = self._calculate_roi(settled)
        metrics.roi = metrics.roi_pct / 100

        # CLV
        metrics.mean_clv, metrics.positive_clv_share = self._calculate_clv(settled)

        # Calibration
        metrics.calibration_error = self._calculate_calibration(settled)

        # By tier
        metrics.by_tier = self._group_by(settled, lambda p: p.get("tier", "unknown"))

        # By odds range
        metrics.by_odds_range = self._group_by(
            settled,
            lambda p: self._get_odds_range(p.get("best_odds") or p.get("fair_odds") or 1.0),
        )

        # By sport
        metrics.by_sport = self._group_by(settled, lambda p: p.get("sport_key", "unknown"))

        return metrics

    def get_summary(self, *, tier: Optional[str] = None) -> dict[str, Any]:
        """Get a summary of accuracy metrics."""
        metrics = self.calculate_metrics(tier=tier)
        return metrics.to_dict()

    def get_calibration_curve(self, n_bins: int = 10) -> list[dict]:
        """Get calibration curve data for plotting.

        Args:
            n_bins: Number of probability bins

        Returns:
            List of {predicted, actual, count} dicts
        """
        settled = self.storage.list_settled_picks()
        bins = [{"predicted": i / n_bins, "actual": 0.0, "count": 0} for i in range(n_bins)]

        for pick in settled:
            if pick.get("result") not in ("WIN", "LOSS"):
                continue

            from .contracts import is_binary_contract
            if not is_binary_contract(pick.get("market"), pick.get("line")):
                continue
            p_true = pick.get("p_true", 0.5)
            bin_idx = min(int(p_true * n_bins), n_bins - 1)
            bins[bin_idx]["count"] += 1
            if pick["result"] == "WIN":
                bins[bin_idx]["actual"] += 1

        # Normalize actual to win rate
        for bin_data in bins:
            if bin_data["count"] > 0:
                bin_data["actual"] = round(bin_data["actual"] / bin_data["count"], 4)
            bin_data["predicted"] = round(bin_data["predicted"], 2)

        return bins


# Global tracker instance
accuracy_tracker = AccuracyTracker(storage=None)  # Will be initialized with storage when needed
