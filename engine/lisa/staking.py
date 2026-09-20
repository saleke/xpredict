"""Quantitative Bankroll Management & Fractional Kelly Staking Engine.

Implements the Kelly Criterion (Kelly 1956) for optimal capital growth under risk,
adapted for sports markets with:
1. Fractional Kelly scaling (e.g. Quarter-Kelly lambda = 0.25) to curb variance and eliminate risk of ruin.
2. Consensus dispersion shrinkage: scales down wager size as cross-bookmaker disagreement (CV) widens.
3. Drawdown protection circuit breaker: systematically reduces stake allocations during high-variance runs.
4. Negative EV suppression: strictly enforces zero stake when expected value is non-positive.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

DEFAULT_KELLY_FRACTION = 0.25      # Quarter-Kelly: institutional standard
MAX_SINGLE_BET_FRACTION = 0.05     # 5.0% maximum bankroll allocation cap
MIN_ODDS_THRESHOLD = 1.01


@dataclass(frozen=True)
class KellyRecommendation:
    """Optimal capital allocation recommendations for a qualifying pick."""
    full_kelly_f: float            # unconstrained optimal growth fraction
    stake_fraction: float          # recommended fraction of bankroll [0.0, max_stake_fraction]
    stake_amount: float            # absolute currency amount for given bankroll
    recommended_units: float       # stake expressed in units (1 unit = 1% bankroll)
    expected_value: float          # EV = p * odds - 1.0
    fractional_multiplier: float   # applied lambda (e.g. 0.25)
    dispersion_shrinkage: float    # penalty factor for bookmaker disagreement [0.2, 1.0]
    drawdown_factor: float         # penalty factor for current bankroll drawdown [0.0, 1.0]
    advice: str                    # actionable recommendation string

    def to_dict(self) -> dict:
        return {
            "full_kelly_f": self.full_kelly_f,
            "stake_fraction": self.stake_fraction,
            "stake_amount": self.stake_amount,
            "recommended_units": self.recommended_units,
            "expected_value": self.expected_value,
            "fractional_multiplier": self.fractional_multiplier,
            "dispersion_shrinkage": self.dispersion_shrinkage,
            "drawdown_factor": self.drawdown_factor,
            "advice": self.advice,
        }


def compute_kelly_stake(
    p_true: float,
    odds: float,
    *,
    bankroll: float = 10000.0,
    fraction: float = DEFAULT_KELLY_FRACTION,
    max_stake_fraction: float = MAX_SINGLE_BET_FRACTION,
    cv: float = 0.02,
    max_cv: float = 0.10,
    current_drawdown: float = 0.0,
) -> KellyRecommendation:
    """Compute mathematically optimal, variance-controlled wager allocation.

    Formulation:
      Net payout odds: b = odds - 1.0
      Full Kelly: f* = (p * odds - 1.0) / (odds - 1.0) = EV / b
      Dispersion Shrinkage: S_disp = max(0.2, 1.0 - (cv / max_cv))
      Drawdown Scaling: S_dd = max(0.0, 1.0 - current_drawdown)
      Recommended fraction: f_rec = min(max_stake_fraction, lambda * max(0, f*) * S_disp * S_dd)
    """
    # Defensive parameter checks
    if (
        not math.isfinite(p_true)
        or not math.isfinite(odds)
        or not math.isfinite(bankroll)
        or p_true <= 0.0
        or p_true >= 1.0
        or odds <= MIN_ODDS_THRESHOLD
        or bankroll <= 0.0
    ):
        return KellyRecommendation(
            full_kelly_f=0.0,
            stake_fraction=0.0,
            stake_amount=0.0,
            recommended_units=0.0,
            expected_value=0.0,
            fractional_multiplier=fraction,
            dispersion_shrinkage=1.0,
            drawdown_factor=1.0,
            advice="Pass: Invalid market or bankroll parameters",
        )

    b = odds - 1.0
    ev = (p_true * odds) - 1.0

    # Negative or zero expected value -> do not allocate capital
    if ev <= 0.0 or b <= 0.0:
        return KellyRecommendation(
            full_kelly_f=0.0,
            stake_fraction=0.0,
            stake_amount=0.0,
            recommended_units=0.0,
            expected_value=round(ev, 4),
            fractional_multiplier=fraction,
            dispersion_shrinkage=1.0,
            drawdown_factor=1.0,
            advice="Pass: Non-positive expected value (no edge)",
        )

    full_f = ev / b

    # Consensus dispersion shrinkage
    if math.isfinite(cv) and cv > 0 and max_cv > 0:
        disp_shrinkage = max(0.2, 1.0 - (cv / max_cv))
    else:
        disp_shrinkage = 1.0

    # Drawdown protection scaling
    dd = max(0.0, min(1.0, current_drawdown)) if math.isfinite(current_drawdown) else 0.0
    dd_factor = max(0.0, 1.0 - dd)

    # Scaled fractional Kelly
    target_f = full_f * fraction * disp_shrinkage * dd_factor
    f_rec = min(max_stake_fraction, max(0.0, target_f))

    stake_amount = round(bankroll * f_rec, 2)
    units = round(f_rec * 100.0, 2)  # 1 unit = 1.0% of bankroll

    if f_rec >= 0.03:
        advice = f"Prime Alpha: High conviction allocation ({units:.1f} units)"
    elif f_rec >= 0.015:
        advice = f"Standard Execution: Moderate edge allocation ({units:.1f} units)"
    elif f_rec > 0.0:
        advice = f"Marginal Value: Conservative stake ({units:.1f} units)"
    else:
        advice = "Pass: Edge curtailed by risk controls"

    return KellyRecommendation(
        full_kelly_f=round(full_f, 4),
        stake_fraction=round(f_rec, 4),
        stake_amount=stake_amount,
        recommended_units=units,
        expected_value=round(ev, 4),
        fractional_multiplier=fraction,
        dispersion_shrinkage=round(disp_shrinkage, 4),
        drawdown_factor=round(dd_factor, 4),
        advice=advice,
    )
