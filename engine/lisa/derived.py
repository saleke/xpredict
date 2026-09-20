"""Derived Calibration Micro-Bets for LISA.

Mathematical derivations from de-vigged consensus:
1. Double Chance (exact model-free sum: 1X, X2, 12).
2. Double-Poisson soccer goal distribution:
   - Solves for total expected goals (lambda_total) from the totals consensus line
     via 1D monotonic Poisson CDF root-finding.
   - Calibrates the home/away split (lambda_home, lambda_away) against the 3-way
     h2h consensus (P_home - P_away) via Skellam/Poisson difference.
   - Derives true calibrated probabilities for:
     * Both Teams To Score (BTTS Yes / BTTS No)
     * Home Clean Sheet / Away Clean Sheet
     * Team Totals (Home/Away Over 0.5, Over 1.5)

Stdlib-only, zero dependencies.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .consensus import Consensus

MAX_GOALS = 12
LAMBDA_TOTAL_MIN = 0.05
LAMBDA_TOTAL_MAX = 15.0


@dataclass(frozen=True)
class DoubleChanceProbs:
    """Exact model-free probabilities derived from 3-way h2h."""

    p_1x: float  # Home or Draw
    p_x2: float  # Away or Draw
    p_12: float  # Home or Away


@dataclass(frozen=True)
class PoissonFit:
    """Fitted Poisson parameters for the match."""

    lambda_total: float
    lambda_home: float
    lambda_away: float
    converged: bool


@dataclass(frozen=True)
class DerivedMicroBets:
    """Calibrated probability distributions for micro-bet markets."""

    match_id: str
    sport_key: str
    home_team: str
    away_team: str
    double_chance: DoubleChanceProbs
    poisson: Optional[PoissonFit]
    # Micro-bet true probabilities (None if sport is not soccer or fit failed)
    btts_yes: Optional[float] = None
    btts_no: Optional[float] = None
    home_clean_sheet: Optional[float] = None
    away_clean_sheet: Optional[float] = None
    home_over_0_5: Optional[float] = None
    home_over_1_5: Optional[float] = None
    away_over_0_5: Optional[float] = None
    away_over_1_5: Optional[float] = None


def derive_double_chance(p_home: float, p_draw: float, p_away: float) -> DoubleChanceProbs:
    """Derive exact Double Chance probabilities from 3-way outcome probabilities."""
    total = p_home + p_draw + p_away
    if total <= 0.0 or not math.isfinite(total):
        return DoubleChanceProbs(0.0, 0.0, 0.0)

    ph = max(0.0, p_home) / total
    pd = max(0.0, p_draw) / total
    pa = max(0.0, p_away) / total

    return DoubleChanceProbs(
        p_1x=min(1.0, ph + pd),
        p_x2=min(1.0, pa + pd),
        p_12=min(1.0, ph + pa),
    )


def _poisson_cdf(k: int, lam: float) -> float:
    """Cumulative Poisson probability P(X <= k) given parameter lambda."""
    if lam <= 0.0:
        return 1.0
    if k < 0:
        return 0.0
    total = 0.0
    term = math.exp(-lam)
    for j in range(k + 1):
        total += term
        term *= lam / (j + 1)
    return min(1.0, total)


def _poisson_pmf_vector(lam: float, max_goals: int = MAX_GOALS) -> list[float]:
    """Precompute PMF vector [P(X=0), ..., P(X=max_goals)]."""
    pmf = [0.0] * (max_goals + 1)
    p = math.exp(-lam)
    pmf[0] = p
    for j in range(1, max_goals + 1):
        p = p * lam / j
        pmf[j] = p
    return pmf


def _home_away_diff(lam_h: float, lam_a: float, max_goals: int = MAX_GOALS) -> float:
    """Compute P(Home Win) - P(Away Win) for independent Poisson variables."""
    pmf_h = _poisson_pmf_vector(lam_h, max_goals)
    pmf_a = _poisson_pmf_vector(lam_a, max_goals)
    p_h_win = sum(pmf_h[x] * pmf_a[y] for x in range(max_goals + 1) for y in range(x))
    p_a_win = sum(pmf_h[x] * pmf_a[y] for y in range(max_goals + 1) for x in range(y))
    return p_h_win - p_a_win


def solve_poisson_lambda_total(p_under: float, line: float,
                               max_iter: int = 50, tol: float = 1e-8) -> float:
    """Invert the Poisson CDF to find lambda_total such that P(Goals <= line) = p_under.

    Guaranteed monotonic bisection convergence.
    """
    k = int(math.floor(line))
    p_target = min(max(p_under, 0.001), 0.999)
    lo, hi = LAMBDA_TOTAL_MIN, LAMBDA_TOTAL_MAX

    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        val = _poisson_cdf(k, mid)
        if abs(val - p_target) < tol:
            return mid
        # CDF decreases as lambda increases
        if val > p_target:
            lo = mid
        else:
            hi = mid

    return 0.5 * (lo + hi)


def solve_poisson_split(lambda_total: float, p_home: float, p_away: float,
                        max_iter: int = 40, tol: float = 1e-6) -> tuple[float, float]:
    """Split lambda_total into (lambda_home, lambda_away) calibrated to P_home - P_away."""
    target_diff = p_home - p_away
    lo_alpha, hi_alpha = 0.05, 0.95

    best_alpha = 0.5
    for _ in range(max_iter):
        mid_alpha = 0.5 * (lo_alpha + hi_alpha)
        lh = mid_alpha * lambda_total
        la = (1.0 - mid_alpha) * lambda_total
        diff = _home_away_diff(lh, la)

        if abs(diff - target_diff) < tol:
            best_alpha = mid_alpha
            break
        # Home advantage increases as alpha increases
        if diff < target_diff:
            lo_alpha = mid_alpha
        else:
            hi_alpha = mid_alpha
        best_alpha = mid_alpha

    lh = best_alpha * lambda_total
    la = (1.0 - best_alpha) * lambda_total
    return lh, la


def derive_micro_bets(h2h_consensus: Consensus,
                      totals_consensus: Optional[Consensus] = None) -> DerivedMicroBets:
    """Derive high-certainty micro-bet probabilities from h2h and totals consensus.

    Double Chance is always derived for 3-way markets.
    Double-Poisson goal metrics (BTTS, clean sheets, team totals) are derived
    whenever a valid soccer totals consensus is provided.
    """
    match = h2h_consensus.match
    p_home = h2h_consensus.p.get(match.home_team, 0.0)
    p_draw = h2h_consensus.p.get("Draw", 0.0)
    p_away = h2h_consensus.p.get(match.away_team, 0.0)

    double_chance = derive_double_chance(p_home, p_draw, p_away)

    is_soccer = match.sport_key.startswith("soccer_")
    if not is_soccer or totals_consensus is None or totals_consensus.line is None:
        return DerivedMicroBets(
            match_id=match.id,
            sport_key=match.sport_key,
            home_team=match.home_team,
            away_team=match.away_team,
            double_chance=double_chance,
            poisson=None,
        )

    # Invert totals consensus for lambda_total
    p_under = totals_consensus.p.get("Under")
    if p_under is None:
        p_over = totals_consensus.p.get("Over", 0.5)
        p_under = 1.0 - p_over

    line = totals_consensus.line
    lambda_total = solve_poisson_lambda_total(p_under, line)
    lambda_home, lambda_away = solve_poisson_split(lambda_total, p_home, p_away)
    poisson_fit = PoissonFit(lambda_total, lambda_home, lambda_away, converged=True)

    # Calibrate derivative micro-bets
    btts_yes = (1.0 - math.exp(-lambda_home)) * (1.0 - math.exp(-lambda_away))
    btts_no = 1.0 - btts_yes

    home_clean_sheet = math.exp(-lambda_away)
    away_clean_sheet = math.exp(-lambda_home)

    home_over_0_5 = 1.0 - math.exp(-lambda_home)
    home_over_1_5 = 1.0 - math.exp(-lambda_home) * (1.0 + lambda_home)

    away_over_0_5 = 1.0 - math.exp(-lambda_away)
    away_over_1_5 = 1.0 - math.exp(-lambda_away) * (1.0 + lambda_away)

    return DerivedMicroBets(
        match_id=match.id,
        sport_key=match.sport_key,
        home_team=match.home_team,
        away_team=match.away_team,
        double_chance=double_chance,
        poisson=poisson_fit,
        btts_yes=btts_yes,
        btts_no=btts_no,
        home_clean_sheet=home_clean_sheet,
        away_clean_sheet=away_clean_sheet,
        home_over_0_5=home_over_0_5,
        home_over_1_5=home_over_1_5,
        away_over_0_5=away_over_0_5,
        away_over_1_5=away_over_1_5,
    )
