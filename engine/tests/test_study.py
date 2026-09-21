"""Tests for the market-efficiency study engine (real archive, deterministic)."""
from __future__ import annotations

import pytest

from lisa.study import MOVEMENT_CUTS, MarketStudyEngine


def _report():
    return MarketStudyEngine().run()


def test_study_schema_and_determinism():
    a = _report()
    b = _report()
    assert a == b  # bit-identical reruns

    meta = a["meta"]["archive"]
    assert meta["total_matches"] == 7155
    assert meta["markets_covered"] == ["1x2", "asian_handicap", "total_over_under"]

    assert set(a["markets"].keys()) == {"h2h", "asian_handicap", "total_over_under"}
    for market, section in a["markets"].items():
        calib = section["calibration"]
        assert any(row["n"] for row in calib), market
        for row in calib:
            if row["n"]:
                assert 0.0 <= row["empirical_rate"] <= 1.0
        fav = section["favorite"]["overview"]
        assert 0 < fav["n"] <= 7155
        assert 0.0 <= fav["win_rate"] <= 1.0
        assert len({row["bucket"] for row in section["favorite"]["by_movement"]}) <= len(MOVEMENT_CUTS)


def test_study_h2h_favorite_overview():
    report = _report()
    fav = report["markets"]["h2h"]["favorite"]["overview"]
    # every archived match contributes a favorite row; win rate near the
    # market basement (~54%) — this is the market-efficiency control.
    assert fav["n"] == 7155
    assert 0.45 < fav["win_rate"] < 0.65
    assert fav["mean_clv"] is not None


def test_study_stream_movement_predicts_better_than_drift():
    """The closing line is more efficient than the early line: favourites whose
    price shortened toward the close cover more often than drifted-out ones."""
    by_move = {
        row["bucket"]: row
        for row in _report()["markets"]["h2h"]["favorite"]["by_movement"]
    }
    assert "steam_in" in by_move and "drift_out" in by_move
    assert by_move["steam_in"]["win_rate"] > by_move["drift_out"]["win_rate"]
    assert by_move["steam_in"]["mean_clv"] > by_move["drift_out"]["mean_clv"]


def test_study_btts_and_scores():
    report = _report()
    b = report["btts"]
    assert 0.30 < b["empirical_rate"] < 0.80
    assert sum(s["n"] for s in b["seasons"]) == 7155
    assert b["model_buckets"], "expected at least one model calibration bucket"
    for row in b["model_buckets"]:
        assert row["n"] > 0
        assert 0.0 <= row["empirical_rate"] <= 1.0
        assert 0.0 <= row["model_mean_p"] <= 1.0
    top_total = sum(r["fraction"] for r in b["correct_scores"])
    assert 0.30 < top_total <= 1.0