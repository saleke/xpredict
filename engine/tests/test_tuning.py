"""Tests for the strategy-tuning engine (real archive, honest sweep)."""
from __future__ import annotations

import pytest

from lisa.tuning import simulate_kelly, tune_subsets
from lisa.backtest import BacktestEngine

_QUICK_THRESHOLDS = (0.75, 0.80)
_QUICK_LEAGUES = (None, ("soccer_epl",))


def _quick_archive_slice():
    """A bounded slice of the real archive so the determinism test stays fast."""
    from lisa.history import HISTORICAL_ODDS, HISTORICAL_SCORES
    from lisa.tuning import load_matches
    from lisa.parsing import parse_scores_payload

    matches, scores_by_id, closing_by_id = load_matches()
    keep = {m.id for m in matches[:1200]}
    scores_slice = {
        s.match_id: s
        for s in parse_scores_payload(HISTORICAL_SCORES)
        if s.match_id in keep
    }
    return matches[:1200], scores_slice, {
        g["id"]: g.get("closing_odds")
        for g in HISTORICAL_ODDS if g["id"] in keep
    }


def test_tune_sweep_is_deterministic_and_schemed():
    matches, scores, closing = _quick_archive_slice()
    a = tune_subsets(matches=matches, scores_by_id=scores, closing_by_id=closing,
                     thresholds=_QUICK_THRESHOLDS, league_options=_QUICK_LEAGUES)
    b = tune_subsets(matches=matches, scores_by_id=scores, closing_by_id=closing,
                     thresholds=_QUICK_THRESHOLDS, league_options=_QUICK_LEAGUES)
    assert a["grid"] == b["grid"]
    assert a["best"] == b["best"]

    all_rows = {r["threshold"]: r for r in a["grid"]
                if r["leagues"] is None and r["window"] == "ALL"}
    assert 0.75 in all_rows and 0.80 in all_rows
    for r in all_rows.values():
        assert r["bets"] > 0
        assert 0.0 <= r["win_rate"] <= 1.0
        assert -100.0 <= r["roi_pct"] <= 100.0
        if r["mean_clv"] is not None:
            assert -0.5 < r["mean_clv"] < 0.5

    assert a["meta"]["archive"]["matches_with_closing"] == 7155


def test_tuning_reproduces_backtest_baseline():
    """The 0.75 / all-leagues cell must exactly match the production backtest's
    executed ledger — so tuning can never silently disagree with the audit.

    The absolute counts are pinned to the shipped configuration, which now
    requires a positive-EV execution. Under the previous certainty-only gate the
    cell executed 380 bets; requiring an edge cuts that to 30, and this test
    exists to catch the moment the two drift apart again.
    """
    engine = BacktestEngine(initial_bankroll=10000.0, flat_stake_unit=100.0)
    baseline = engine.run()

    rep = tune_subsets(thresholds=(0.75,), league_options=(None,))
    cell = [r for r in rep["grid"] if r["window"] == "ALL" and r["leagues"] is None][0]

    assert cell["bets"] == baseline.executed_bets == 30
    assert cell["wins"] == baseline.wins == 26
    assert cell["roi_pct"] == pytest.approx(baseline.flat_roi_pct, abs=0.1)


def test_tuning_reflects_the_production_edge_requirement():
    """Tuning must not describe a strategy the product will not actually run.

    ``tune_subsets`` used to hardcode ``require_positive_ev=False`` while the
    engine shipped ``True``, so the whole grid was tuning a certainty-only book
    that production never emitted.
    """
    with_edge = tune_subsets(thresholds=(0.75,), league_options=(None,))
    cell = [r for r in with_edge["grid"]
            if r["window"] == "ALL" and r["leagues"] is None][0]

    without_edge = tune_subsets(thresholds=(0.75,), league_options=(None,),
                                require_positive_ev=False)
    legacy_cell = [r for r in without_edge["grid"]
                   if r["window"] == "ALL" and r["leagues"] is None][0]

    # Requiring an edge can only ever shrink the executed set.
    assert cell["bets"] <= legacy_cell["bets"]
    assert cell["bets"] == 30
    assert legacy_cell["bets"] == 380


def test_kelly_sim_bounds_loss():
    """Risk sim must never blow the bankroll beyond a controlled drawdown path."""
    bets = [{"odds": 1.25, "result": "WIN", "p_true": 0.82},
            {"odds": 1.30, "result": "LOSS", "p_true": 0.78}] * 40
    sim = simulate_kelly(bets, kelly_fraction=0.5, initial_bankroll=10000.0,
                         per_bet_cap_pct=0.05, drawdown_stop_pct=0.25)
    assert sim["final_bankroll"] > 0.0
    assert sim["max_drawdown_pct"] <= 25.0 or sim["stopped"]
    assert 0 < sim["bets_placed"] <= len(bets)
    assert sim["final_bankroll"] > 0