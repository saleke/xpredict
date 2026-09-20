"""Unit tests for Quantitative Bankroll Management & Fractional Kelly Staking Engine."""
from __future__ import annotations

from datetime import datetime, timezone
import pytest

from lisa.gate import evaluate as evaluate_gate
from lisa.odds import Book, Match
from lisa.consensus import refine as refine_match
from lisa.staking import (
    KellyRecommendation, compute_kelly_stake,
    DEFAULT_KELLY_FRACTION, MAX_SINGLE_BET_FRACTION,
)


def test_kelly_formula_standard_growth() -> None:
    # True win prob = 60%, execution odds = 2.0 (EV = +20%)
    # Net odds b = 1.0. Full Kelly f* = 0.20 / 1.0 = 20.0%
    rec = compute_kelly_stake(
        p_true=0.60,
        odds=2.0,
        bankroll=10000.0,
        fraction=0.25,  # Quarter-Kelly
        max_stake_fraction=0.10,
        cv=0.0,  # Zero dispersion
    )
    assert rec.expected_value == pytest.approx(0.20)
    assert rec.full_kelly_f == pytest.approx(0.20)
    # Quarter Kelly = 0.20 * 0.25 = 0.05 (5%)
    assert rec.stake_fraction == pytest.approx(0.05)
    assert rec.stake_amount == pytest.approx(500.0)
    assert rec.recommended_units == pytest.approx(5.0)
    assert "Prime Alpha" in rec.advice or "Standard Execution" in rec.advice


def test_negative_ev_suppression() -> None:
    # True win prob = 50%, execution odds = 1.90 (EV = -5.0%)
    rec = compute_kelly_stake(
        p_true=0.50,
        odds=1.90,
        bankroll=10000.0,
    )
    assert rec.expected_value == pytest.approx(-0.05)
    assert rec.full_kelly_f == 0.0
    assert rec.stake_fraction == 0.0
    assert rec.stake_amount == 0.0
    assert rec.recommended_units == 0.0
    assert "Non-positive expected value" in rec.advice


def test_max_stake_cap() -> None:
    # Huge edge: p = 90%, odds = 2.0 -> Full Kelly = 80%, Quarter = 20%
    # But max_stake_fraction is 5.0%
    rec = compute_kelly_stake(
        p_true=0.90,
        odds=2.0,
        bankroll=50000.0,
        fraction=0.25,
        max_stake_fraction=0.05,
    )
    assert rec.stake_fraction == pytest.approx(0.05)
    assert rec.stake_amount == pytest.approx(2500.0)
    assert rec.recommended_units == pytest.approx(5.0)


def test_dispersion_shrinkage() -> None:
    # Baseline with low CV = 0.01 vs high CV = 0.08 (when max_cv = 0.10)
    rec_tight = compute_kelly_stake(p_true=0.80, odds=1.35, cv=0.01, max_cv=0.10)
    rec_loose = compute_kelly_stake(p_true=0.80, odds=1.35, cv=0.08, max_cv=0.10)

    assert rec_tight.dispersion_shrinkage > rec_loose.dispersion_shrinkage
    assert rec_tight.stake_fraction > rec_loose.stake_fraction


def test_drawdown_circuit_breaker() -> None:
    # In a 30% drawdown: stakes should be scaled down by 70%
    rec_normal = compute_kelly_stake(p_true=0.80, odds=1.35, current_drawdown=0.0)
    rec_dd = compute_kelly_stake(p_true=0.80, odds=1.35, current_drawdown=0.30)

    assert rec_dd.drawdown_factor == pytest.approx(0.70)
    assert rec_dd.stake_fraction < rec_normal.stake_fraction
    assert rec_dd.stake_amount == pytest.approx(round(rec_normal.stake_amount * 0.70, 2), abs=1.0)


def test_degenerate_inputs_handling() -> None:
    # Odds <= 1.0
    rec1 = compute_kelly_stake(p_true=0.80, odds=1.0)
    assert rec1.stake_fraction == 0.0

    # Prob <= 0 or >= 1
    rec2 = compute_kelly_stake(p_true=1.5, odds=1.5)
    assert rec2.stake_fraction == 0.0

    # Bankroll <= 0
    rec3 = compute_kelly_stake(p_true=0.8, odds=1.5, bankroll=-100.0)
    assert rec3.stake_fraction == 0.0


def test_gate_integration_populates_staking() -> None:
    commence = datetime(2026, 9, 20, 20, 0, 0, tzinfo=timezone.utc)
    t_now = datetime(2026, 9, 20, 18, 0, 0, tzinfo=timezone.utc)
    b_emit = [
        Book("pinnacle", "Pinnacle", t_now, {"TeamA": 1.45, "TeamB": 4.50}),
        Book("book2", "Book 2", t_now, {"TeamA": 1.40, "TeamB": 4.60}),
        Book("book3", "Book 3", t_now, {"TeamA": 1.42, "TeamB": 4.40}),
        Book("book4", "Book 4", t_now, {"TeamA": 1.41, "TeamB": 4.50}),
        Book("book5", "Book 5", t_now, {"TeamA": 1.40, "TeamB": 4.70}),
    ]
    m = Match(
        id="m-stake", sport_key="basketball_nba", commence_time=commence,
        home_team="TeamA", away_team="TeamB", completed=False,
        market="h2h", bookmakers=tuple(b_emit),
    )
    cons = refine_match(m, now=t_now, min_books=3)
    res = evaluate_gate(cons, threshold=0.65, min_books=3)

    assert res.pick is not None
    assert res.pick.recommended_stake_pct > 0.0
    assert res.pick.recommended_units > 0.0
