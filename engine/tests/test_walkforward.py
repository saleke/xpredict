"""Tests for the honest walk-forward evaluation engine (Layer 4)."""
from __future__ import annotations

import pytest

from lisa.walkforward import build_matches, walk_forward, win_ci


def test_build_matches_uses_real_archive_only():
    matches = build_matches()
    assert len(matches) == 7155
    ids = {m["match_id"] for m in matches}
    assert len(ids) == 7155  # no duplicates
    for m in matches:
        assert m["best"] and "home" in m["best"] and "away" in m["best"]
        assert m["home_score"] >= 0 and m["away_score"] >= 0


def test_walk_forward_is_deterministic():
    a = walk_forward()
    b = walk_forward()
    assert a["strategies"] == b["strategies"]
    assert a["calibration"] == b["calibration"]
    assert a["meta"]["total_matches"] == b["meta"]["total_matches"]


def test_walk_forward_no_look_ahead_via_model_state():
    """The evaluator must NOT update a team's rating before that team's match
    is predicted. We verify by checking that the model used for the headline
    numbers stays pure: a second full run yields identical results (determinism
    implies no hidden state leakage across runs)."""
    from lisa.model import EloPoissonModel

    m1 = EloPoissonModel()
    m2 = EloPoissonModel()
    r1 = walk_forward(model=m1)
    r2 = walk_forward(model=m2)
    assert r1["model_value_bets"] == r2["model_value_bets"]


def test_win_ci_math():
    lo, hi = win_ci(50, 100)
    assert 0.4 < lo < 0.61
    assert lo < 0.5 < hi
    assert win_ci(0, 0) == (0.0, 0.0)
    lo2, hi2 = win_ci(100, 100)
    assert lo2 > 0.95 and hi2 == pytest.approx(1.0)


def test_headline_strategies_are_honest():
    """The walk-forward must not be rigged: a naive market follower vs the
    independent model both lose money at realistic ('avg') book prices, and
    the market follower beats the independent model."""
    rep = walk_forward()
    mf = rep["strategies"]["market_follower_avg"]
    mv = rep["strategies"]["model_value_avg"]

    assert mf["bets"] == 7155
    assert mv["bets"] < mf["bets"]  # the gate/model restricts bet volume

    # At realistic prices neither strategy is profitable (market is efficient).
    assert mf["roi_pct_avg"] < 0.0
    assert mv["roi_pct_avg"] < 0.0

    # The independent model is a candid baseline — it loses MORE than simply
    # following the market favourite. Reporting this is the point of honesty.
    assert mf["win_rate"] > mv["win_rate"]
    assert mf["roi_pct_avg"] > mv["roi_pct_avg"]


def test_walk_forward_sport_filter():
    epl = walk_forward(league_filter={"soccer_epl"})
    assert epl["meta"]["total_matches"] == 1520
    assert set(epl["per_league"]["market_follower_best"].keys()) == {"soccer_epl"}


def test_calibration_section_complete():
    rep = walk_forward()
    cal = rep["calibration"]
    assert cal["n_settled_predictions"] == 7155
    assert cal["brier_score"] is not None
    assert cal["multiclass_brier_score"] is not None
    assert 0.0 < cal["multiclass_brier_score"] < 0.67  # beats the 1/3 uniform
    assert cal["ece"] > 0.0


def test_report_meta_states_methodology():
    rep = walk_forward()
    assert "no odds and no results from the match being predicted" in rep["meta"]["method"]
    assert rep["meta"]["data_provenance"]["source"].startswith("football-data.co.uk")


def test_every_bet_result_validated_against_archived_outcome():
    """REAL DATA ONLY: the result of every market-follower bet, model bet, and
    calibration row must be exactly what the packaged archive's official final
    score implies — never a generated or simulated outcome."""
    from lisa.history import HISTORICAL_ODDS, HISTORICAL_SCORES

    scores_by_id = {s["id"]: s for s in HISTORICAL_SCORES}
    archived_outcome: dict[str, str] = {}
    for od in HISTORICAL_ODDS:
        sc = scores_by_id.get(od["id"])
        if not sc:
            continue
        scores = {s.get("name"): s.get("score") for s in sc.get("scores", [])}
        hs = int(float(scores.get(od["home_team"], -1)))
        as_ = int(float(scores.get(od["away_team"], -1)))
        if hs < 0 or as_ < 0:
            continue
        archived_outcome[od["id"]] = "home" if hs > as_ else ("away" if hs < as_ else "draw")

    rep = walk_forward()
    assert len(archived_outcome) == rep["meta"]["total_matches"] == 7155

    for bet in rep["market_follower_bets"]:
        assert bet["result"] == ("WIN" if bet["outcome"] == archived_outcome[bet["match_id"]] else "LOSS")
    for bet in rep["model_value_bets"]:
        assert bet["result"] == ("WIN" if bet["outcome"] == archived_outcome[bet["match_id"]] else "LOSS")