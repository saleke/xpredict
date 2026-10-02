"""Dashboard card freshness must never claim decay on missing data.

The card label is a *claim about the market price*. Asserting "Decayed /
Slippage" on a pick that was never priced fabricates an observation that never
happened, which violates the project's rule that unbacked values are null and
flagged. These tests pin the four real states and, most importantly, the
boundary between "the price fell" and "there is no price".
"""
from __future__ import annotations

import pytest

from lisa.dashboard import _pick_row_to_card


def _card(**overrides):
    row = {
        "dedupe_key": "k1",
        "match_id": "m1",
        "home_team": "Home",
        "away_team": "Away",
        "fair_odds": 2.00,
        "best_odds": 2.10,
        "best_ev": 0.05,
    }
    row.update(overrides)
    return _pick_row_to_card(row)


# -- the real states ---------------------------------------------------------

def test_price_above_fair_is_fresh():
    card = _card(fair_odds=2.00, best_odds=2.10)
    assert card["freshness"] == "FRESH"
    assert card["badge_color"] == "emerald"


def test_price_level_with_fair_is_fair():
    # The FRESH test runs first, so the FAIR band is only reachable *below*
    # fair odds and within 0.01. That ordering is pre-existing and deliberate
    # (anything strictly above fair is an entry), so it is preserved here.
    card = _card(fair_odds=2.00, best_odds=1.995)
    assert card["freshness"] == "FAIR"
    assert card["badge_color"] == "amber"


def test_price_below_fair_is_decayed():
    card = _card(fair_odds=2.00, best_odds=1.80)
    assert card["freshness"] == "DECAYED"
    assert card["badge_color"] == "rose"


# -- the boundary that was wrong --------------------------------------------

def test_missing_price_is_unpriced_not_decayed():
    """A model probability with no executable price is missing data.

    This is the case that made 19 of 22 live cards read "Decayed / Slippage":
    the old bare ``else`` swallowed NULLs and reported decay for a market that
    was never observed.
    """
    card = _card(fair_odds=1.2753, best_odds=None)
    assert card["freshness"] == "UNPRICED"
    assert card["badge_color"] == "cyan"
    # The value itself stays null -- we do not substitute a guess.
    assert card["best_odds"] is None
    assert card["fair_odds"] == pytest.approx(1.2753)


def test_missing_fair_odds_is_unpriced_not_decayed():
    card = _card(fair_odds=None, best_odds=2.10)
    assert card["freshness"] == "UNPRICED"


def test_both_missing_is_unpriced():
    card = _card(fair_odds=None, best_odds=None)
    assert card["freshness"] == "UNPRICED"
    assert card["fair_odds"] is None
    assert card["best_odds"] is None


def test_unpriced_badge_colour_exists_in_the_stylesheet():
    """The class is rendered into a ``pick-cue-<colour>`` class name."""
    from pathlib import Path

    css = Path(__file__).resolve().parents[2] / "web" / "css" / "index.css"
    if not css.exists():
        pytest.skip("web assets not present in this checkout")
    for colour in ("emerald", "amber", "rose", "cyan"):
        assert f".pick-cue-{colour}" in css.read_text(encoding="utf-8")
