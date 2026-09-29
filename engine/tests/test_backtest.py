"""Honest tests for the LISA large-scale historical backtest engine.

These tests verify BOTH that the engine is truthful (honest sign conventions,
no fabricated tier, real provenance) AND that it behaves correctly against the
REAL packaged archive. The exact counts below are snapshot values for the
packaged football-data.co.uk archive (seasons 2021/22..2024/25, 5 leagues).
If the archive is extended, the snapshots must be recomputed — that is by
design (a test suite that cannot be silently re-gamed against synthetic data).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from lisa.backtest import (
    BacktestEngine,
    BacktestMatchRecord,
    BacktestReport,
    compute_wilson_ci,
    format_backtest_report,
)
from lisa.cli import _cmd_backtest
from lisa.history import HISTORICAL_ODDS, HISTORICAL_SCORES
from lisa.parsing import parse_scores_payload


def test_wilson_score_interval_math():
    """Verify mathematical properties of the Wilson score confidence interval."""
    low, high = compute_wilson_ci(42, 50, z=1.96)
    assert 0.70 < low < 0.75
    assert 0.90 < high < 0.95
    assert low < 42 / 50 < high

    assert compute_wilson_ci(0, 0) == (0.0, 0.0)


def test_backtest_runs_on_real_archive():
    """Real archive: executed ledger, win rate, and Wilson bounds are consistent."""
    engine = BacktestEngine(initial_bankroll=10000.0, flat_stake_unit=100.0)
    report = engine.run()

    assert isinstance(report, BacktestReport)
    # 7,155 archived matches; 426 lacked the minimum book depth for de-vigging
    # and are honestly excluded from evaluation (not fabricated into the run).
    assert report.total_matches == 6729
    # Shipped gate requires a book to beat the leave-one-out fair price. On this
    # archive that is rare: 30 of 6,729 evaluated matches carry a genuine edge.
    # Under the old certainty-only gate this was 380 bets at 79.7% and -1.74%
    # flat ROI, i.e. a high win rate that lost money -- the exact profile the
    # benchmarked competitor avoids by pricing narrow, thin markets.
    assert report.executed_bets == 30
    assert report.wins == 26
    assert report.losses == 4
    assert report.pushes == 0
    assert report.win_rate == pytest.approx(0.8667, abs=0.005)

    # Wilson interval must contain the realised win rate.
    assert report.wilson_ci_lower <= report.win_rate <= report.wilson_ci_upper
    assert report.wilson_ci_lower == pytest.approx(0.7032, abs=0.01)
    assert report.wilson_ci_upper == pytest.approx(0.9469, abs=0.01)

    # The interval is the point: 30 bets at 86.7% is NOT evidence of an edge.
    # The lower bound sits below the ~82% breakeven this price band requires, so
    # the honest statement is "unproven on this archive", not "profitable".
    assert report.wilson_ci_lower < 0.82 < report.wilson_ci_upper

    # Grade A is the ONLY executed grade now — Grade B was removed because its
    # markets (double-chance / over-1.5 / NBA) cannot be priced from the archive.
    assert report.grade_a_count == 30
    assert report.grade_a_wins == 26
    assert report.grade_b_count == 0

    # Provenance is baked into every report so a report can never claim
    # synthetic data silently.
    prov = report.data_provenance
    assert prov is not None
    assert "football-data.co.uk" in prov["source"]
    assert prov["total_matches"] == 7155
    assert "No synthetic" in prov["statement"]


def test_backtest_is_honest_about_money():
    """Money is reported as measured, in whichever direction it falls.

    The edge requirement changed this report from -1.74% to +5.20% flat ROI. The
    test must follow the measurement, not pin the direction -- what it still
    enforces is that the report never flatters itself: figures come from the run,
    and the sample is too small to support a profitability claim.
    """
    engine = BacktestEngine(initial_bankroll=10000.0, flat_stake_unit=100.0)
    report = engine.run()

    # Figures are whatever was actually realised, not forced positive.
    assert report.net_profit == pytest.approx(130.5, abs=20.0)
    assert report.roi_pct == pytest.approx(4.5, abs=0.2)
    assert report.flat_profit == pytest.approx(156.0, abs=20.0)
    assert report.flat_roi_pct == pytest.approx(5.2, abs=0.2)
    assert report.ending_bankroll == pytest.approx(10130.5, abs=25.0)

    # ...but a positive mean on 30 bets is not a finding. The Wilson lower
    # bound sits below the breakeven win rate for this price band, so the
    # defensible claim is "unproven", and the report must not imply otherwise.
    assert report.executed_bets == 30
    assert report.wilson_ci_lower < 0.82

    # Drawdown / risk ratios reflect the real losing-streak size.
    assert report.max_drawdown_pct == pytest.approx(1.67, abs=0.5)
    assert report.max_drawdown_dollars == pytest.approx(168.0, abs=25.0)
    assert report.profit_factor > 1.0


def test_backtest_level_clv_and_calibration():
    """CLV is measured against the real closing line, and calibration empirically."""
    engine = BacktestEngine()
    report = engine.run()

    # CLV is now a genuine late-to-close measure: executed early price vs the
    # later recorded closing line (football-data.co.uk *C columns). The old,
    # trivially-true 1.0 rate is gone — real markets move against the bettor.
    assert 0.0 < report.positive_clv_rate < 1.0
    assert report.mean_clv is not None
    assert report.mean_clv < 0.0  # on average the early line did NOT beat the close

    assert report.brier_score == pytest.approx(0.1124, abs=0.01)
    # ECE/MCE move with the executed set. On 30 picks they are larger than they
    # were on 380, which is the correct behaviour: less evidence, coarser
    # calibration. A shrinking sample must not flatter the calibration numbers.
    assert report.ece == pytest.approx(0.0779, abs=0.01)
    assert report.mce < 0.20
    assert report.reliability < 0.015
    assert report.resolution > 0.0


def test_backtest_grade_c_counterfactual():
    """Grade C pass advisories produce a sincere counterfactual ledger."""
    engine = BacktestEngine()
    report = engine.run()

    assert report.grade_c_traps_avoided == 6699
    assert report.grade_c_traps_that_lost == 3105
    assert report.grade_c_traps_that_won == 3594
    assert report.capital_preserved_dollars == 3105 * 100.0
    # The net figure is derived from the archived prices of the avoided winners,
    # not from a hardcoded 80.0-per-win constant. The avoided-winner side comes
    # to $306,052 of foregone profit, so avoiding them is net NEGATIVE -- which
    # is the honest finding and the reason the gross figure is never shown alone.
    assert report.net_counterfactual_value == pytest.approx(5448.0, abs=1.0)
    assert report.net_counterfactual_value < report.capital_preserved_dollars

    for rec in report.records:
        if rec.grade == "GRADE_C":
            assert rec.result in ("PASS_TRAP_AVOIDED", "PASS_ADVISORY")
            assert rec.stake_amount == 0.0
            assert rec.pnl == 0.0
            if rec.result == "PASS_TRAP_AVOIDED":
                assert rec.capital_saved == 100.0
            else:
                assert rec.capital_saved == 0.0


def test_backtest_filter_by_sport_without_nba():
    """Filtering to a soccer league works; NBA simply has no archive data."""
    engine = BacktestEngine()
    report = engine.run(sport_keys=["soccer_epl"])

    assert report.total_matches == 1429  # 1520 archived EPL, 91 lacked book depth
    # EPL is the most efficient league in the archive, so the edge requirement
    # finds almost nothing here: 9 executed bets at 66.7%. This is the honest
    # shape of the result and the reason the archive cannot be sold as proof.
    assert report.executed_bets == 9
    assert report.wins == 6
    assert report.losses == 3
    assert report.win_rate == pytest.approx(0.6667, abs=0.01)
    assert "soccer_epl" in report.sport_breakdown
    assert len(report.sport_breakdown) == 1
    assert report.data_provenance["filtered_sport_keys"] == ["soccer_epl"]

    nba = engine.run(sport_keys=["basketball_nba"])
    assert nba.executed_bets == 0
    assert "no" not in nba.data_provenance  # provenance still real archive


def test_format_backtest_report_no_misleading_labels():
    """The formatted report omits any Grade B / fabricated-tier language."""
    engine = BacktestEngine()
    report = engine.run()

    formatted = format_backtest_report(report, verbose=True)
    assert "LISA QUANTITATIVE HISTORICAL BACKTEST" in formatted
    assert "EXECUTIVE ACCURACY & PREDICTION METRICS" in formatted
    assert "STATISTICAL CALIBRATION & PROBABILITY BENCHMARKS" in formatted
    assert "FINANCIAL & RISK-ADJUSTED PERFORMANCE" in formatted
    assert "SPORT & LEAGUE BREAKDOWN" in formatted
    assert "DETAILED MATCH-BY-MATCH AUDIT TRAIL" in formatted
    assert "Data Source:" in formatted and "football-data.co.uk" in formatted
    assert "Grade B" not in formatted
    assert "(Beats Pinnacle/Closing Line)" not in formatted


def test_cli_backtest_execution(tmp_path: Path):
    """CLI backtest exports JSON and runs on real leagues."""
    export_file = tmp_path / "backtest_test.json"
    args = argparse.Namespace(
        sports="soccer_epl,soccer_germany_bundesliga",
        export_json=str(export_file),
        json=False,
        verbose=False,
    )

    exit_code = _cmd_backtest(args)
    assert exit_code == 0
    assert export_file.exists()
    assert export_file.stat().st_size > 0


def test_multi_strategy_profiles_are_real_or_derived():
    """Every strategy row is computed from REAL records; derived rows are labelled."""
    engine = BacktestEngine()
    report = engine.run()

    matrix = report.strategy_comparison_matrix
    ids = [s["strategy_id"] for s in matrix]
    assert ids == [
        "conservative",
        "high_yield_pivots",
        "always_home",
        "smart_parlays",
        "hybrid_portfolio",
    ]

    cons = report.strategies["conservative"]["summary"]
    assert cons["win_rate"] == pytest.approx(0.8667, abs=0.01)
    assert cons["net_profit"] == pytest.approx(130.5, abs=20.0)
    assert cons["roi_pct"] == pytest.approx(4.5, abs=0.2)
    assert cons["max_drawdown_pct"] == pytest.approx(1.67, abs=1.0)
    assert "realised" in cons["description"].lower()

    # Baselines are the naive "no gate" benchmarks.
    fav = report.strategies["high_yield_pivots"]["summary"]
    assert fav["name"] == "Market Favourites (Baseline)"
    assert fav["executed_bets"] == 6729
    assert fav["win_rate"] == pytest.approx(0.5377, abs=0.01)
    assert fav["roi_pct"] < 0.0  # blind favourite-chasing loses money

    home = report.strategies["always_home"]["summary"]
    assert home["name"] == "Always Home (Baseline)"
    assert home["roi_pct"] < 0.0

    # Parlay / hybrid are explicitly DERIVED scenarios, not independent markets.
    par = report.strategies["smart_parlays"]["summary"]
    assert "derived" in par["name"].lower() or "derived" in par["description"].lower()
    assert "Derived" in par["badge"]

    hyb = report.strategies["hybrid_portfolio"]["summary"]
    assert "derived" in hyb["description"].lower()

    # No strategy may ever claim a win rate far above what the archive supports.
    for s in matrix:
        assert s["win_rate"] <= (report.win_rate + 0.2)


def test_no_hardcoded_grade_b_markets_in_ledger():
    """Ledger must never contain a fake Grade B pivot or fabricated odds."""
    engine = BacktestEngine()
    report = engine.run()
    grades = {r.grade for r in report.records}
    assert grades <= {"GRADE_A", "GRADE_C"}

    odds_seen = {round(r.best_odds, 2) for r in report.records if r.result in ("WIN", "LOSS")}
    # The old fabricated engine emitted ONLY two distinct odds (1.20 / 1.22).
    # A real ledger spans hundreds of distinct prices.
    assert len(odds_seen) > 10
    assert odds_seen != {1.20, 1.22}


def test_backtest_validation_binds_to_archived_scores():
    """REAL DATA ONLY: every prediction result and scoreline in the ledger must
    be exactly what the packaged archive's official final score implies.

    Any synthetic result, generated outcome, or fabricated scoreline would show
    up as a mismatch against ``HISTORICAL_SCORES`` and fail this test."""
    archived = {s.match_id: s for s in parse_scores_payload(HISTORICAL_SCORES)}
    assert len(archived) == len(HISTORICAL_SCORES)  # every archived score parses

    closing_by_id = {g["id"]: g.get("closing_odds") for g in HISTORICAL_ODDS}

    engine = BacktestEngine(initial_bankroll=10000.0, flat_stake_unit=100.0)
    report = engine.run()

    assert len(report.records) == report.grade_c_traps_avoided + report.grade_a_count

    # Every executed (Grade A) record must grade EXACTLY against the archive.
    grade_a = [r for r in report.records if r.grade == "GRADE_A"]
    for rec in grade_a:
        score = archived.get(rec.match_id)
        assert score is not None and score.completed, rec.match_id
        assert rec.actual_score == f"{score.home_score}-{score.away_score}"
        expected = score.grade_pick(rec.market, rec.outcome_name)
        assert rec.result == expected, (
            f"result for {rec.match_id} ({rec.outcome_name}) contradicted by "
            f"archived official score {rec.actual_score}"
        )
        # CLV reference is the archived closing line for that outcome.
        assert closing_by_id.get(rec.match_id) is not None, rec.match_id
        assert rec.closing_odds == closing_by_id[rec.match_id][rec.outcome_name], rec.match_id
        assert rec.beat_clv == (rec.best_odds > rec.closing_odds), rec.match_id

    # Grade C pass advisories must carry the same real scoreline, and their
    # counterfactual prices must be genuine archive prices (never fabricated).
    for rec in report.records:
        if rec.grade != "GRADE_C":
            continue
        score = archived.get(rec.match_id)
        assert score is not None and score.completed, rec.match_id
        assert rec.actual_score == f"{score.home_score}-{score.away_score}"
        assert rec.result in ("PASS_TRAP_AVOIDED", "PASS_ADVISORY")
        assert rec.best_odds >= 1.0  # real published price
        assert 0.0 < rec.p_true < 1.0
        assert rec.stake_amount == 0.0 and rec.pnl == 0.0


def test_backtest_grade_c_counterfactual_matches_archive_favorite():
    """The Grade C trap classification must follow the archived outcome for the
    real de-vigged favourite, not an invented one.

    For a sample of pass advisories, refit the consensus, take the favourite,
    and require the trap type to match the archived scoreline."""
    from lisa.backtest import best_available_price, evaluate_gate
    from lisa.history import HISTORICAL_ODDS
    from lisa.consensus import refine
    from lisa.parsing import parse_odds_payload

    engine = BacktestEngine(initial_bankroll=10000.0, flat_stake_unit=100.0)
    report = engine.run()

    archived = {s.match_id: s for s in parse_scores_payload(HISTORICAL_SCORES)}
    matches_by_id = {m.id: m for m in parse_odds_payload(HISTORICAL_ODDS)}
    grade_c = [r for r in report.records if r.grade == "GRADE_C"]
    checked = 0
    for rec in grade_c:
        match = matches_by_id.get(rec.match_id)
        if match is None:
            continue
        consensus = refine(match, now=match.commence_time,
                           min_books=engine.settings.min_books_telemetry)
        if consensus is None:
            continue
        gate = evaluate_gate(consensus, threshold=engine.settings.gate_threshold,
                             min_books=engine.settings.min_books_alert,
                             max_cv=engine.settings.max_cv,
                             require_positive_ev=engine.settings.require_positive_ev)
        if gate.pick is not None:
            continue  # this match did not pass in the sampled run
        p_home = consensus.p.get(match.home_team, 0.0)
        p_away = consensus.p.get(match.away_team, 0.0)
        p_fav = max(p_home, p_away)
        fav = match.home_team if p_home >= p_away else match.away_team
        score = archived[rec.match_id]
        expectation = (
            "PASS_ADVISORY" if score.winner() == fav else "PASS_TRAP_AVOIDED"
        )
        assert rec.result == expectation, rec.match_id
        assert rec.grade == "GRADE_C"
        checked += 1
        if checked >= 20:
            break
    assert checked >= 20