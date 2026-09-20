"""Comprehensive tests for the large-scale LISA historical backtest and calibration engine."""
from __future__ import annotations

import argparse
from pathlib import Path
import pytest

from lisa.backtest import (
    BacktestEngine,
    BacktestReport,
    BacktestMatchRecord,
    compute_wilson_ci,
    format_backtest_report,
)
from lisa.cli import _cmd_backtest
from lisa.history import HISTORICAL_ODDS, HISTORICAL_SCORES


def test_wilson_score_interval_math():
    """Verify mathematical properties of the Wilson score confidence interval."""
    low, high = compute_wilson_ci(42, 50, z=1.96)
    assert 0.70 < low < 0.75
    assert 0.90 < high < 0.95
    assert low < 42 / 50 < high

    # Edge cases
    assert compute_wilson_ci(0, 0) == (0.0, 0.0)


def test_backtest_runs_successfully():
    """Verify that BacktestEngine processes all 80 historical matches with honest grading."""
    engine = BacktestEngine(initial_bankroll=10000.0, flat_stake_unit=100.0)
    report = engine.run()

    assert isinstance(report, BacktestReport)
    assert report.total_matches == 80
    assert report.executed_bets == 50
    assert report.wins == 42
    assert report.losses == 8
    assert report.pushes == 0
    assert report.win_rate == pytest.approx(0.84, abs=0.01)

    # Wilson Score 95% Confidence Interval bounds
    assert report.wilson_ci_lower == pytest.approx(0.715, abs=0.01)
    assert report.wilson_ci_upper == pytest.approx(0.917, abs=0.01)

    # Grade breakdown
    assert report.grade_a_count == 29
    assert report.grade_a_wins == 24
    assert report.grade_a_win_rate == pytest.approx(0.8276, abs=0.01)

    assert report.grade_b_count == 21
    assert report.grade_b_wins == 18
    assert report.grade_b_win_rate == pytest.approx(0.8571, abs=0.01)


def test_backtest_statistical_calibration():
    """Verify Murphy decomposition, Expected Calibration Error, and Brier accuracy."""
    engine = BacktestEngine()
    report = engine.run()

    # Empirical calibration error must be exceptionally tight (< 5%)
    assert report.ece < 0.05
    assert report.ece == pytest.approx(0.0361, abs=0.01)
    assert report.mce < 0.10

    # Murphy decomposition components
    assert report.reliability < 0.01  # Near 0 reflects near-perfect probabilistic calibration
    assert report.resolution > 0.0
    assert report.uncertainty > 0.0

    # Closing Line Value
    assert report.positive_clv_rate == 1.0


def test_backtest_capital_preservation_and_counterfactual():
    """Verify that Grade C pass advisories provide honest counterfactual analysis."""
    engine = BacktestEngine()
    report = engine.run()

    assert report.grade_c_traps_avoided == 30
    assert report.grade_c_traps_that_lost == 17
    assert report.grade_c_traps_that_won == 13
    assert report.capital_preserved_dollars == 1700.0
    assert report.net_counterfactual_value == 660.0

    # Inspect the avoided matches
    avoided_records = [r for r in report.records if r.grade == "GRADE_C"]
    assert len(avoided_records) == 30
    for rec in avoided_records:
        assert rec.hazard_warning is not None
        if rec.result == "PASS_TRAP_AVOIDED":
            assert rec.capital_saved == 100.0
        else:
            assert rec.capital_saved == 0.0


def test_backtest_risk_and_drawdown_metrics():
    """Verify Maximum Drawdown, Sharpe ratio, Sortino ratio, and Profit Factor."""
    engine = BacktestEngine(initial_bankroll=10000.0, flat_stake_unit=100.0)
    report = engine.run()

    assert report.ending_bankroll > report.initial_bankroll
    assert report.net_profit > 0.0

    # Drawdown must be bounded (< 5% max peak-to-trough drop)
    assert 0.0 < report.max_drawdown_pct < 5.0
    assert report.max_drawdown_dollars > 0.0

    # Risk-adjusted ratios
    assert report.sharpe_ratio > 0.2
    assert report.sortino_ratio > 0.1
    assert report.profit_factor > 1.0


def test_backtest_filter_by_sport():
    """Verify that filtering by sport isolates only the requested sport keys."""
    engine = BacktestEngine()
    report = engine.run(sport_keys=["soccer_epl"])

    assert report.total_matches == 20
    assert report.executed_bets == 14  # 6 sucker traps avoided
    assert report.wins == 12
    assert report.losses == 2
    assert "soccer_epl" in report.sport_breakdown
    assert len(report.sport_breakdown) == 1


def test_format_backtest_report():
    """Verify that format_backtest_report generates clean output without errors."""
    engine = BacktestEngine()
    report = engine.run()

    formatted = format_backtest_report(report, verbose=True)
    assert "LISA QUANTITATIVE HISTORICAL BACKTEST" in formatted
    assert "EXECUTIVE ACCURACY & PREDICTION METRICS" in formatted
    assert "STATISTICAL CALIBRATION & PROBABILITY BENCHMARKS" in formatted
    assert "FINANCIAL & RISK-ADJUSTED PERFORMANCE" in formatted
    assert "SPORT & LEAGUE BREAKDOWN" in formatted
    assert "DETAILED MATCH-BY-MATCH AUDIT TRAIL" in formatted


def test_cli_backtest_execution(tmp_path: Path):
    """Test CLI backtest command with JSON export."""
    export_file = tmp_path / "backtest_test.json"
    args = argparse.Namespace(
        sports="soccer_epl,basketball_nba",
        export_json=str(export_file),
        json=False,
        verbose=False,
    )

    exit_code = _cmd_backtest(args)
    assert exit_code == 0
    assert export_file.exists()
    assert export_file.stat().st_size > 0


def test_multi_strategy_profiles_and_comparison_matrix():
    """Verify that multi-strategy profiles and comparison matrix evaluate properly."""
    engine = BacktestEngine()
    report = engine.run()

    # 1. Comparison Matrix structure
    matrix = report.strategy_comparison_matrix
    assert len(matrix) == 4
    strategy_ids = [s["strategy_id"] for s in matrix]
    assert strategy_ids == ["conservative", "high_yield_pivots", "smart_parlays", "hybrid_portfolio"]

    # 2. Conservative baseline preserved
    cons = report.strategies["conservative"]["summary"]
    assert cons["win_rate"] == pytest.approx(0.84, abs=0.01)
    assert cons["net_profit"] == pytest.approx(143.40, abs=0.50)
    assert cons["max_drawdown_pct"] == pytest.approx(2.89, abs=0.10)

    # 3. High-Yield Pivots produces substantially higher cash profit
    piv = report.strategies["high_yield_pivots"]["summary"]
    assert piv["win_rate"] >= 0.75
    assert piv["net_profit"] > 1000.0  # Much higher cash profit than conservative singles
    assert piv["avg_odds"] > 1.70

    # 4. Smart Parlays yields high profit while retaining >70% win rate
    par = report.strategies["smart_parlays"]["summary"]
    assert par["win_rate"] >= 0.70
    assert par["net_profit"] > 1500.0
    assert par["avg_odds"] > 1.85

    # 5. Hybrid Portfolio provides balanced compounding
    hyb = report.strategies["hybrid_portfolio"]["summary"]
    assert hyb["win_rate"] >= 0.78
    assert hyb["net_profit"] > 800.0
    assert hyb["max_drawdown_pct"] < 4.5

