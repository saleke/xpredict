"""Quote lookup on the board: every market the books quote must be reachable.

The bug these pin down was invisible from the board. ``_fixture_opportunities``
built one best-price map filtered to the 1X2 market and then asked that same map
about totals, BTTS and correct-score selections. Every lookup missed, so those
markets rendered as ``priced=False`` with ``ev=None`` -- which reads as "the
books are not offering this", when in fact the books were offering plenty and
the lookup was asking the wrong question. The winning ladder looked perfectly
healthy, because it only ever used 1X2.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lisa.board import (  # noqa: E402
    LINE_PRECISION,
    MarketPrice,
    _line_key,
    _side_key,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


class _StubModel:
    """Just enough model for the lookup path, with no calibration to defend."""

    sufficient = True

    class _Report:
        sufficient = True
        mean_games_behind = 30.0
        prior_dominance = 0.1

    report = _Report()

    def strength(self, team):
        from lisa.dixon_coles import TeamStrength
        return TeamStrength(games=30)

    def predict(self, home, away):
        return {
            "p_home": 0.45, "p_draw": 0.28, "p_away": 0.27,
            "p_btts": 0.52,
            "over": {"0.5": 0.95, "1.5": 0.80, "2.5": 0.55, "3.5": 0.30},
            "most_likely_scores": [
                {"home_goals": 1, "away_goals": 1, "p": 0.14},
                {"home_goals": 1, "away_goals": 0, "p": 0.13},
                {"home_goals": 0, "away_goals": 1, "p": 0.12},
            ],
        }


def _board():
    from lisa.board import OpportunityBoard
    board = OpportunityBoard(_StubModel())
    board._as_of = NOW
    return board


def _price(selection, odds, *, market="h2h", line=None, book="draftkings") -> MarketPrice:
    return MarketPrice(match_id="m1", selection=selection, odds=odds,
                       book_key=book, book_title=book.title(), source="test",
                       market=market, line=line, updated_at=NOW)


def _fixture():
    from lisa.board import Fixture
    return Fixture(match_id="m1", sport_key="soccer_epl", kickoff=NOW,
                   home="Arsenal", away="Chelsea", source="test")


# ---------------------------------------------------------------------------
# Key normalisation
# ---------------------------------------------------------------------------


def test_a_side_key_strips_only_a_trailing_number() -> None:
    assert _side_key("Over 2.5") == "Over"
    assert _side_key("Under 3.5") == "Under"
    assert _side_key("Home") == "Home"
    assert _side_key("BTTS Yes") == "BTTS Yes"
    assert _side_key("1-1") == "1-1", "a scoreline is not a number to strip"
    assert _side_key("") == ""


def test_a_line_key_rounds_so_formatting_cannot_split_a_price() -> None:
    """2.5 and 2.50 are one line, not two."""
    assert _line_key(2.5) == _line_key(2.50)
    assert _line_key(None) is None
    assert _line_key(2.25) != _line_key(2.75)


# ---------------------------------------------------------------------------
# The lookup itself
# ---------------------------------------------------------------------------


def test_1x2_is_priced() -> None:
    quotes = [_price("Home", 2.05), _price("Draw", 3.35), _price("Away", 3.55)]

    found = _board()._fixture_opportunities(_fixture(), quotes)

    h2h = {o.selection: o for o in found if o.market == "h2h"}
    assert all(o.priced for o in h2h.values())
    assert h2h["Home"].best_odds == 2.05


def test_totals_are_priced_at_every_line() -> None:
    """The regression. Totals used to be looked up in a 1X2-only map."""
    quotes = [_price("Over 2.5", 2.00, market="totals", line=2.5),
              _price("Under 2.5", 1.741, market="totals", line=2.5)]

    found = _board()._fixture_opportunities(_fixture(), quotes)

    over = [o for o in found if o.market == "totals" and o.line == 2.5
            and o.selection == "Over"][0]
    under = [o for o in found if o.market == "totals" and o.line == 2.5
             and o.selection == "Under"][0]
    assert over.priced and over.best_odds == 2.00
    assert under.priced and under.best_odds == 1.741
    assert under.ev == pytest.approx(0.45 * 1.741 - 1.0)


def test_a_total_price_does_not_leak_onto_a_different_line() -> None:
    """A book quoting only the 2.5 line prices that line and nothing else."""
    quotes = [_price("Over 2.5", 2.00, market="totals", line=2.5)]

    found = _board()._fixture_opportunities(_fixture(), quotes)

    priced = [o for o in found if o.priced]
    assert [(o.market, o.selection, o.line) for o in priced] == [
        ("totals", "Over", 2.5)]
    # The other side of the same line is a separate selection the book did not
    # quote, and must not become a market candidate or inherit the Over price.
    assert not any(o.market == 'totals' and o.line == 2.5 and o.selection == 'Under' for o in found)


def test_btts_is_priced() -> None:
    quotes = [_price("Yes", 1.95, market="btts"), _price("No", 1.90, market="btts")]

    found = _board()._fixture_opportunities(_fixture(), quotes)

    btts = {o.selection: o for o in found if o.market == "btts"}
    assert btts["Yes"].priced and btts["Yes"].best_odds == 1.95
    assert btts["No"].priced and btts["No"].best_odds == 1.90


def test_correct_score_is_priced() -> None:
    quotes = [_price("1-1", 7.50, market="correct_score")]

    found = _board()._fixture_opportunities(_fixture(), quotes)

    scores = {o.selection: o for o in found if o.market == "correct_score"}
    assert scores["1-1"].priced and scores["1-1"].best_odds == 7.50
    assert "1-0" not in scores, "an unoffered scoreline became a bet candidate"


def test_a_line_formatted_differently_still_matches() -> None:
    """The adapter prints the line; the model computes it. They must agree."""
    quotes = [_price("Over 2.500", 2.00, market="totals", line=2.5)]

    found = _board()._fixture_opportunities(_fixture(), quotes)

    over = [o for o in found if o.market == "totals" and o.line == 2.5][0]
    assert over.priced, "2.500 and 2.5 were treated as different lines"


def test_the_highest_price_across_books_wins() -> None:
    """A bettor wants the best price, so this must be a max and not a mean."""
    quotes = [_price("Home", 1.90, book="draftkings"),
              _price("Home", 2.10, book="fanduel")]

    found = _board()._fixture_opportunities(_fixture(), quotes)

    home = [o for o in found if o.market == "h2h" and o.selection == "Home"][0]
    assert home.best_odds == 2.10
    assert home.best_book == "Fanduel"


def test_no_quotes_leaves_everything_unpriced_rather_than_zero() -> None:
    found = _board()._fixture_opportunities(_fixture(), [])

    assert len(found) == 3 and all(o.market == 'h2h' for o in found)
    assert all(not o.priced for o in found)
    assert all(o.ev is None for o in found)
    assert all(o.best_odds is None for o in found)
    assert all(o.basis == "model_only" for o in found)


def test_a_quote_at_or_below_even_money_is_refused() -> None:
    """1.00 or lower cannot win, so it must not become the 'best' price."""
    quotes = [_price("Home", 1.0), _price("Draw", 3.4), _price("Away", 3.5)]

    found = _board()._fixture_opportunities(_fixture(), quotes)

    home = [o for o in found if o.market == "h2h" and o.selection == "Home"][0]
    assert not home.priced, "a 1.00 price was accepted as the best quote"


def test_line_precision_is_fine_enough_to_keep_quarter_lines_apart() -> None:
    """Asian lines differ by 0.25; rounding must not merge them."""
    assert LINE_PRECISION >= 2
    assert _line_key(2.25) != _line_key(2.5)
    assert _line_key(2.75) != _line_key(3.0)


def test_the_single_market_helper_still_answers_one_market() -> None:
    """The accumulators use this and genuinely want h2h only."""
    quotes = [_price("Home", 2.05), _price("Away", 3.55),
              _price("Over 2.5", 2.0, market="totals", line=2.5)]

    best = _board()._best_quotes(quotes, ("h2h", None))

    assert set(best) == {"Home", "Away"}
    assert best["Home"].odds == 2.05


def test_verified_offer_confirmation_prices_an_unchanged_quote() -> None:
    from dataclasses import replace
    current = replace(_price('Over', 2.0, market='totals', line=2.5),
        updated_at=NOW - timedelta(days=1), confirmed_at=NOW, freshness_basis='publisher_snapshot')
    found = _board()._fixture_opportunities(_fixture(), [current])
    assert next(o for o in found if o.market == 'totals' and o.line == 2.5 and o.selection == 'Over').priced
    stale = replace(current, confirmed_at=NOW - timedelta(hours=1))
    assert not any(o.priced for o in _board()._fixture_opportunities(_fixture(), [stale]))
