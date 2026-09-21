"""Tests for the independent Elo + Poisson model (Layer 3).

Every test here enforces the two properties that make the model honest:

  1. NO LOOK-AHEAD: predictions use only state from strictly earlier matches.
  2. DETERMINISM: identical input sequences produce identical outputs.
"""
from __future__ import annotations

import pytest

from lisa.model import EloPoissonModel


def _match(mid, home, away, hs, as_, league="soccer_epl"):
    return {
        "match_id": mid,
        "league": league,
        "home": home,
        "away": away,
        "home_score": hs,
        "away_score": as_,
    }


def test_probabilities_sum_to_one():
    m = EloPoissonModel()
    for league in ("soccer_epl", "soccer_bundesliga"):
        for _ in range(20):
            ph, pd, pa = m.predict(league, "Home Team", f"Opp {_ + 1}")
            assert ph + pd + pa == pytest.approx(1.0, abs=1e-9)
            assert all(0.0 <= p <= 1.0 for p in (ph, pd, pa))


def test_no_look_ahead_predictions():
    """Predicting a match twice must be identical, and ratings only change
    when observe() is called — prediction has no side effects."""
    m = EloPoissonModel()
    seq = [
        _match("m1", "A", "B", 3, 0),
        _match("m2", "C", "D", 1, 1),
        _match("m3", "E", "F", 0, 2),
    ]
    before = {k: v for k, v in m._ratings.items()}
    for match in seq:
        p1 = m.predict_log(match["league"], match["home"], match["away"])
        p2 = m.predict_log(match["league"], match["home"], match["away"])
        assert p1 == p2  # prediction is side-effect free
    assert m._ratings == before  # nothing observed yet, nothing changed

    # Observe ONLY m1, then confirm the OTHER teams still have rating 1500.
    m.observe("soccer_epl", "A", "B", 3, 0)
    assert m.rating("soccer_epl", "A") != 1500.0
    assert m.rating("soccer_epl", "B") != 1500.0
    assert m.rating("soccer_epl", "C") == 1500.0  # untouched by future data


def test_chronological_walk_forward_order():
    """walk_forward() yields predictions before observing; ratings for a team
    reflect ONLY matches that chronologically preceded the predicted match."""
    m = EloPoissonModel()
    matches = [
        _match("m1", "A", "B", 2, 1),
        _match("m2", "A", "B", 0, 3),
    ]
    seen = []
    for match, pred in m.walk_forward(matches):
        seen.append(match["match_id"])
        # At prediction time for m1, A has no history; for m2, A has exactly 1.
        games_home = m.games_seen("soccer_epl", "A")
        if match["match_id"] == "m1":
            assert games_home == 0
        else:
            assert games_home == 1
    assert seen == ["m1", "m2"]


def test_deterministic_runs():
    a = EloPoissonModel()
    b = EloPoissonModel()
    seq = [
        _match("m1", "X", "Y", 2, 0),
        _match("m2", "Y", "Z", 1, 1),
        _match("m3", "Z", "X", 0, 3),
        _match("m4", "W", "V", 4, 2),
    ]
    ra = [dict(pred) for _, pred in a.walk_forward(seq)]
    rb = [dict(pred) for _, pred in b.walk_forward(seq)]
    assert ra == rb

    # A different order is a different history, so ratings legitimately differ:
    # the reassigned fixture sequence must not be accidentally symmetric.
    rc = [dict(pred) for _, pred in b.walk_forward(list(reversed(seq)))]
    assert rc != ra


def test_model_remembers_min_prior_games():
    m = EloPoissonModel()
    assert m.min_prior_games == 2
    m.observe("soccer_epl", "A", "C", 1, 0)
    m.observe("soccer_epl", "B", "D", 0, 2)
    m.observe("soccer_epl", "A", "B", 2, 2)
    assert m.ready("soccer_epl", "A", "B") is True
    assert m.ready("soccer_epl", "C", "B") is False  # C never played


def test_home_advantage_shifts_probabilities():
    m = EloPoissonModel()
    ph_a, pd_a, pa_a = m.predict("soccer_epl", "A", "B")
    ph_b, pd_b, pa_b = m.predict("soccer_epl", "B", "A")
    # Home sides must both be favoured (home advantage is baked in).
    assert ph_a > pa_a
    assert ph_b > pa_b