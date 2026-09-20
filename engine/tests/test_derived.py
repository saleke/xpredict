"""Unit tests for derived calibration micro-bets (Double Chance & Double-Poisson)."""
from __future__ import annotations

from datetime import datetime, timezone
import pytest

from lisa import consensus
from lisa.derived import (
    derive_double_chance,
    derive_micro_bets,
    solve_poisson_lambda_total,
    solve_poisson_split,
    _poisson_cdf,
)
from lisa.fixtures import LA_LIGA_ODDS, LA_LIGA_TOTALS_ODDS, NBA_ODDS
from lisa.parsing import parse_odds_payload

NOW = datetime(2026, 9, 20, 15, 0, 0, tzinfo=timezone.utc)


def test_double_chance_closed_form():
    dc = derive_double_chance(0.60, 0.25, 0.15)
    assert dc.p_1x == pytest.approx(0.85)
    assert dc.p_x2 == pytest.approx(0.40)
    assert dc.p_12 == pytest.approx(0.75)


def test_double_chance_normalizes_unscaled_inputs():
    dc = derive_double_chance(1.20, 0.50, 0.30)  # sum = 2.0
    assert dc.p_1x == pytest.approx(0.85)
    assert dc.p_x2 == pytest.approx(0.40)
    assert dc.p_12 == pytest.approx(0.75)


def test_solve_poisson_lambda_total_inversion():
    # Over/Under 2.5 goals with 50% under
    lam = solve_poisson_lambda_total(0.50, 2.5)
    assert 2.67 < lam < 2.68
    # Invert back to confirm CDF matches 0.50
    assert _poisson_cdf(2, lam) == pytest.approx(0.50, abs=1e-5)


def test_solve_poisson_lambda_total_monotonicity():
    # Higher Under probability implies lower expected goals
    lam_high_under = solve_poisson_lambda_total(0.70, 2.5)
    lam_low_under = solve_poisson_lambda_total(0.30, 2.5)
    assert lam_high_under < lam_low_under


def test_solve_poisson_split_symmetry():
    # Symmetric match P_H == P_A -> equal lambdas
    lam_h, lam_a = solve_poisson_split(2.6, 0.35, 0.35)
    assert lam_h == pytest.approx(lam_a, abs=1e-3)
    assert lam_h + lam_a == pytest.approx(2.6, abs=1e-5)


def test_solve_poisson_split_favorite():
    # Heavy home favorite P_H > P_A -> lambda_home > lambda_away
    lam_h, lam_a = solve_poisson_split(2.8, 0.75, 0.10)
    assert lam_h > lam_a
    assert lam_h + lam_a == pytest.approx(2.8, abs=1e-5)

    # Away favorite P_A > P_H -> lambda_away > lambda_home
    lam_h2, lam_a2 = solve_poisson_split(2.4, 0.15, 0.65)
    assert lam_a2 > lam_h2
    assert lam_h2 + lam_a2 == pytest.approx(2.4, abs=1e-5)


def test_derive_micro_bets_soccer_end_to_end():
    h2h_match = parse_odds_payload(LA_LIGA_ODDS, market_keys=("h2h",))[0]
    tot_match = parse_odds_payload(LA_LIGA_TOTALS_ODDS, market_keys=("totals",))[0]

    h2h_c = consensus.refine(h2h_match, now=NOW, min_books=5)
    tot_c = consensus.refine(tot_match, now=NOW, min_books=5)
    assert h2h_c is not None and tot_c is not None

    mb = derive_micro_bets(h2h_c, tot_c)
    assert mb.match_id == "lig-a"
    assert mb.poisson is not None
    assert mb.poisson.converged is True

    # Real Madrid vs Elche: Real Madrid is heavy favorite (~78% true win prob)
    assert mb.double_chance.p_1x > 0.90
    assert mb.poisson.lambda_home > mb.poisson.lambda_away

    # Probabilities in valid ranges
    assert mb.btts_yes is not None and 0.0 < mb.btts_yes < 1.0
    assert mb.btts_no == pytest.approx(1.0 - mb.btts_yes, abs=1e-6)
    assert mb.home_clean_sheet is not None and 0.0 < mb.home_clean_sheet < 1.0
    assert mb.away_clean_sheet is not None and 0.0 < mb.away_clean_sheet < 1.0

    # Real Madrid clean sheet > Elche clean sheet
    assert mb.home_clean_sheet > mb.away_clean_sheet

    # Team totals monotonicity
    assert mb.home_over_0_5 is not None and mb.home_over_1_5 is not None
    assert mb.home_over_0_5 > mb.home_over_1_5 > 0.0


def test_derive_micro_bets_basketball_skips_poisson():
    # Basketball: Double Chance works (Draw=0), Poisson goal model is skipped
    nba_match = parse_odds_payload(NBA_ODDS, market_keys=("h2h",))[0]
    c = consensus.refine(nba_match, now=NOW, min_books=5)
    assert c is not None

    mb = derive_micro_bets(c, totals_consensus=None)
    assert mb.poisson is None
    assert mb.btts_yes is None
    assert mb.double_chance.p_12 == pytest.approx(1.0)  # No draw in NBA
