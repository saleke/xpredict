"""Unit tests for the LISA Calibration Metrics Engine (lisa.calibration)."""
from __future__ import annotations

import math
import pytest

from lisa.calibration import (
    CalibrationBin,
    CalibrationReport,
    compute_brier_score,
    compute_ece_mce,
    compute_log_loss,
    evaluate_by_market,
    evaluate_calibration,
    format_calibration_report,
)


def test_compute_brier_score_analytical() -> None:
    # Perfect forecast
    assert compute_brier_score([1.0, 1.0], [1.0, 1.0]) == 0.0
    assert compute_brier_score([0.0, 0.0], [0.0, 0.0]) == 0.0

    # Completely wrong forecast
    assert compute_brier_score([1.0], [0.0]) == 1.0
    assert compute_brier_score([0.0], [1.0]) == 1.0

    # 50/50 uninformative coin-flip forecast
    assert pytest.approx(compute_brier_score([0.5, 0.5], [1.0, 0.0])) == 0.25

    # Hand-calculated multi-sample
    # p = [0.8, 0.7], y = [1.0, 0.0]
    # err1 = (0.8 - 1)^2 = 0.04; err2 = (0.7 - 0)^2 = 0.49
    # bs = (0.04 + 0.49) / 2 = 0.265
    assert pytest.approx(compute_brier_score([0.8, 0.7], [1.0, 0.0])) == 0.265


def test_compute_brier_score_validation_errors() -> None:
    with pytest.raises(ValueError, match="Length mismatch"):
        compute_brier_score([0.8], [1.0, 0.0])
    with pytest.raises(ValueError, match="empty sequences"):
        compute_brier_score([], [])


def test_compute_log_loss_analytical() -> None:
    # 50/50 coin flip -> -ln(0.5) = ln(2) ~= 0.693147
    ll = compute_log_loss([0.5, 0.5], [1.0, 0.0])
    assert pytest.approx(ll) == math.log(2.0)

    # Clamping behavior: p=1.0 and y=0.0 must not produce math domain error or inf
    ll_wrong = compute_log_loss([1.0], [0.0])
    assert math.isfinite(ll_wrong)
    assert ll_wrong > 30.0  # -ln(1e-15) ~= 34.5


def test_ece_and_mce_perfect_calibration() -> None:
    # 10 predictions at p=0.8, exactly 8 win and 2 lose
    preds = [0.8] * 10
    outs = [1.0] * 8 + [0.0] * 2
    bins = [(0.75, 0.85)]
    ece, mce, cal_bins = compute_ece_mce(preds, outs, bin_edges=bins)

    assert pytest.approx(ece) == 0.0
    assert pytest.approx(mce) == 0.0
    assert len(cal_bins) == 1
    assert cal_bins[0].count == 10
    assert pytest.approx(cal_bins[0].pred_mean) == 0.8
    assert pytest.approx(cal_bins[0].win_rate) == 0.8
    assert pytest.approx(cal_bins[0].bias) == 0.0


def test_ece_and_mce_under_and_overconfident() -> None:
    # Bin 1: predicted 0.75, actual win rate 1.0 -> bias = +0.25 (underconfident)
    # Bin 2: predicted 0.90, actual win rate 0.5 -> bias = -0.40 (overconfident)
    preds = [0.75, 0.75, 0.90, 0.90]
    outs = [1.0, 1.0, 1.0, 0.0]
    bins = [(0.70, 0.80), (0.85, 0.95)]
    ece, mce, cal_bins = compute_ece_mce(preds, outs, bin_edges=bins)

    # Bin 1 err = 0.25 (weight 0.5)
    # Bin 2 err = 0.40 (weight 0.5)
    # ECE = 0.5 * 0.25 + 0.5 * 0.40 = 0.325
    # MCE = 0.40
    assert pytest.approx(ece) == 0.325
    assert pytest.approx(mce) == 0.40
    assert pytest.approx(cal_bins[0].bias) == 0.25
    assert pytest.approx(cal_bins[1].bias) == -0.40


def test_murphy_decomposition_identity() -> None:
    # Murphy (1973): For discrete forecasts f_k, BS = Reliability - Resolution + Uncertainty holds exactly.
    preds_discrete = [0.72, 0.72, 0.77, 0.77, 0.82, 0.82, 0.92, 0.92]
    outs_discrete = [1.0, 0.0, 1.0, 1.0, 1.0, 0.0, 1.0, 1.0]

    records_discrete = [
        {"p_true": p, "result": "WIN" if y == 1.0 else "LOSS"}
        for p, y in zip(preds_discrete, outs_discrete)
    ]
    rep_disc = evaluate_calibration(records_discrete)
    assert rep_disc.brier_score is not None
    assert rep_disc.reliability is not None
    assert rep_disc.resolution is not None
    assert rep_disc.uncertainty is not None

    exact_reconstructed = rep_disc.reliability - rep_disc.resolution + rep_disc.uncertainty
    assert pytest.approx(rep_disc.brier_score, abs=1e-9) == exact_reconstructed

    # For continuous forecasts, the difference is bounded by within-bin discretization variance
    preds_cont = [0.71, 0.74, 0.76, 0.79, 0.81, 0.84, 0.91, 0.94]
    records_cont = [
        {"p_true": p, "result": "WIN" if y == 1.0 else "LOSS"}
        for p, y in zip(preds_cont, outs_discrete)
    ]
    rep_cont = evaluate_calibration(records_cont)
    cont_reconstructed = rep_cont.reliability - rep_cont.resolution + rep_cont.uncertainty
    assert abs(rep_cont.brier_score - cont_reconstructed) < 0.01


def test_evaluate_calibration_handles_empty_and_void() -> None:
    # Empty collection
    rep_empty = evaluate_calibration([])
    assert rep_empty.total_evaluated == 0
    assert rep_empty.n_won == 0
    assert rep_empty.n_lost == 0
    assert rep_empty.n_void == 0
    assert rep_empty.brier_score is None
    assert rep_empty.ece is None

    # Only VOID records
    void_records = [
        {"p_true": 0.82, "result": "VOID"},
        {"p_true": 0.79, "result": "VOID"},
    ]
    rep_void = evaluate_calibration(void_records)
    assert rep_void.total_evaluated == 0
    assert rep_void.n_void == 2
    assert rep_void.brier_score is None


def test_evaluate_calibration_skips_invalid_data() -> None:
    records = [
        {"p_true": 0.80, "result": "WIN"},
        {"p_true": 0.85, "result": "LOSS"},
        {"p_true": "corrupted", "result": "WIN"},  # invalid float
        {"p_true": 1.5, "result": "WIN"},          # out of bounds [0, 1]
        {"p_true": -0.2, "result": "LOSS"},        # negative prob
        {"p_true": None, "result": "WIN"},          # missing prob
        {"p_true": 0.80, "result": "PENDING"},      # unsettled
        {"p_true": 0.80, "result": "VOID"},         # void counted separately
    ]
    rep = evaluate_calibration(records)
    assert rep.total_evaluated == 2
    assert rep.n_won == 1
    assert rep.n_lost == 1
    assert rep.n_void == 1
    assert pytest.approx(rep.win_rate) == 0.5


def test_evaluate_by_market() -> None:
    records = [
        {"market": "h2h", "p_true": 0.80, "result": "WIN"},
        {"market": "h2h", "p_true": 0.80, "result": "WIN"},
        {"market": "totals", "p_true": 0.75, "result": "LOSS"},
        {"market": "spreads", "p_true": 0.85, "result": "VOID"},
    ]
    by_market = evaluate_by_market(records)

    assert "overall" in by_market
    assert "h2h" in by_market
    assert "totals" in by_market
    assert "spreads" in by_market

    assert by_market["overall"].total_evaluated == 3  # 2 h2h + 1 totals
    assert by_market["overall"].n_void == 1           # 1 spreads
    assert by_market["h2h"].total_evaluated == 2
    assert by_market["h2h"].win_rate == 1.0
    assert by_market["totals"].total_evaluated == 1
    assert by_market["totals"].win_rate == 0.0
    assert by_market["spreads"].total_evaluated == 0
    assert by_market["spreads"].n_void == 1


def test_format_calibration_report_output() -> None:
    records = [
        {"p_true": 0.82, "result": "WIN"},
        {"p_true": 0.78, "result": "LOSS"},
    ]
    report = evaluate_calibration(records)
    table = format_calibration_report(report, title="Test Table")

    assert "=== Test Table ===" in table
    assert "Settled Picks Evaluated: 2" in table
    assert "Brier Score:" in table
    assert "ECE:" in table
    assert "Bin Range" in table

    # Empty report formatting check
    empty_rep = evaluate_calibration([])
    empty_table = format_calibration_report(empty_rep)
    assert "No settled non-void picks available to evaluate." in empty_table
