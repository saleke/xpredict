"""Shin's method: correctness against known values and structural invariants.

The known-value case [1.16, 5.20] was derived by hand from the published
algorithm (see docs/DESIGN.md) — independent of this implementation.
"""
from __future__ import annotations

import pytest

from lisa.shin import ShinValidationError, shin_probabilities


def test_two_way_known_values():
    res = shin_probabilities([1.16, 5.20])
    assert res.method == "closed"
    assert abs(res.probabilities[0] - 0.8349) < 1e-3
    assert abs(res.probabilities[1] - 0.1651) < 1e-3
    assert abs(res.z - 0.0567) < 1e-3
    assert abs(sum(res.probabilities) - 1.0) < 1e-9


def test_favourite_longshot_bias_correction():
    # Relative to *proportional* de-vig (the no-bias baseline), Shin's method
    # pushes the favourite's probability UP and the longshot's DOWN — that is
    # the bias correction. Raw implied odds include the vig, so they are not
    # the right baseline (that was the trap in the first version of this test).
    odds = [1.16, 5.20]
    res = shin_probabilities(odds)
    inv = [1.0 / o for o in odds]
    prop = [v / sum(inv) for v in inv]
    assert res.probabilities[0] > prop[0]   # favourite above proportional
    assert res.probabilities[1] < prop[1]   # longshot below proportional


def test_two_way_sums_to_one():
    for odds in ([1.91, 1.95], [1.05, 12.0], [1.5, 2.75], [1.99, 2.02]):
        res = shin_probabilities(odds)
        assert abs(sum(res.probabilities) - 1.0) < 1e-9
        assert all(0.0 < p < 1.0 for p in res.probabilities)


def test_flat_two_way_market():
    res = shin_probabilities([2.0, 2.0])
    assert abs(res.probabilities[0] - 0.5) < 1e-9
    assert abs(res.probabilities[1] - 0.5) < 1e-9


def test_three_way_invariants():
    res = shin_probabilities([1.25, 5.80, 10.00])
    assert res.method == "iterative"
    assert res.converged
    assert abs(sum(res.probabilities) - 1.0) < 1e-9
    assert all(0.0 < p < 1.0 for p in res.probabilities)
    assert 0.0 <= res.z <= 0.5
    # favourite corrected down, ordering preserved
    assert res.probabilities[0] < 1.0 / 1.25
    assert res.probabilities[0] > res.probabilities[1] > res.probabilities[2]


def test_three_way_sums_to_one_for_many_markets():
    for odds in ([1.30, 5.00, 9.00], [1.90, 3.40, 4.20], [2.60, 3.10, 2.80]):
        res = shin_probabilities(odds)
        assert abs(sum(res.probabilities) - 1.0) < 1e-9


def test_absurd_margin_falls_back_to_proportional():
    res = shin_probabilities([1.01, 1.01, 1.01])
    assert res.method == "proportional"
    assert all(abs(p - 1.0 / 3.0) < 1e-9 for p in res.probabilities)


def test_validation_errors():
    with pytest.raises(ShinValidationError):
        shin_probabilities([1.5])                     # too few outcomes
    with pytest.raises(ShinValidationError):
        shin_probabilities([0.95, 3.0])               # below sane floor
    with pytest.raises(ShinValidationError):
        shin_probabilities([float("inf"), 2.0])       # non-finite
    with pytest.raises(ShinValidationError):
        shin_probabilities([2.0, 99999.0])            # above sane ceiling