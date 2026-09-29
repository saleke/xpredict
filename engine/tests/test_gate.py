"""Stage 3 gate: threshold, dispersion, EV overlay, execution selection."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from lisa import consensus
from lisa.fixtures import NBA_ODDS
from lisa.gate import evaluate
from lisa.parsing import parse_odds_payload

NOW = datetime(2026, 9, 20, 15, 0, 0, tzinfo=timezone.utc)


def _consensus(match_id: str, **kwargs):
    match = next(m for m in parse_odds_payload(NBA_ODDS) if m.id == match_id)
    return consensus.refine(match, now=NOW, **kwargs)


def test_gate_passes_with_ev_execution():
    res = evaluate(_consensus("nba-a"), threshold=0.75, min_books=5, max_cv=0.10)
    assert res.reason == "ok"
    pick = res.pick
    assert pick.outcome_name == "Celtics"
    assert pick.best_execution is not None
    assert pick.best_execution.book_key == "bet365"  # the lagging book
    assert pick.best_execution.ev > 0.0
    assert pick.state == "TRIGGER_ALERT"


def test_gate_confident_but_no_execution():
    """Opting out of the EV requirement yields a pick with no execution.

    This is the behaviour the production default exists to prevent: a confident
    consensus where no book beats fair price. Kept as an explicit opt-in test so
    the escape hatch stays covered, but nothing in production should reach it.
    """
    res = evaluate(_consensus("nba-e"), threshold=0.75, min_books=5, max_cv=0.10,
                   require_positive_ev=False)
    assert res.reason == "ok"
    assert res.pick.best_execution is None  # no book beats fair price
    # ...and a pick with no execution cannot be sized, because the fallback
    # price is 1/p_top, which makes EV exactly zero by construction.
    assert res.pick.recommended_stake_pct == 0.0


def test_gate_requires_positive_ev_is_the_default():
    """The default gate must demand an edge, not just certainty."""
    res = evaluate(_consensus("nba-e"), threshold=0.75, min_books=5, max_cv=0.10)
    assert res.pick is None
    assert res.reason == "no_positive_ev"


def test_gate_requires_positive_ev():
    res = evaluate(_consensus("nba-e"), threshold=0.75, min_books=5, max_cv=0.10,
                   require_positive_ev=True)
    assert res.pick is None
    assert res.reason == "no_positive_ev"


def test_gate_sizes_on_the_probability_the_ev_was_measured_against():
    """A priced pick must be staked from the same probability that justified it.

    The gate judged the book against the leave-one-out ``p_ref`` but used to
    hand Kelly the self-inclusive ``p_top`` -- two different probabilities for
    one bet, one of which had already been ruled out. On nba-a bet365 is the
    only book with positive EV; the stake must reflect that reference.
    """
    res = evaluate(_consensus("nba-a"), threshold=0.75, min_books=5, max_cv=0.10,
                   require_positive_ev=True)
    assert res.reason == "ok"
    exec_ = res.pick.best_execution
    assert exec_ is not None and exec_.ev > 0.0
    # A positive-EV execution must produce a positive stake, which is only
    # possible if Kelly saw an edge rather than the zero-EV fallback.
    assert res.pick.recommended_stake_pct > 0.0


def test_gate_below_threshold():
    res = evaluate(_consensus("nba-b"), threshold=0.75, min_books=5, max_cv=0.10)
    assert res.pick is None
    assert res.reason == "below_threshold"


def test_gate_high_dispersion():
    res = evaluate(_consensus("nba-c"), threshold=0.75, min_books=5, max_cv=0.10)
    assert res.pick is None
    assert res.reason == "high_dispersion"


def test_gate_min_book_requirement():
    res = evaluate(_consensus("nba-a"), threshold=0.75, min_books=6, max_cv=0.10)
    assert res.pick is None
    assert res.reason == "insufficient_books"


def test_execution_ev_uses_leave_one_out_reference():
    c = _consensus("nba-a")
    res = evaluate(c, threshold=0.75, min_books=5, max_cv=0.10)
    assert res.reason == "ok"
    ex = res.pick.best_execution
    assert ex.book_key == "bet365"
    bet365 = next(b for b in c.books if b.book_key == "bet365")
    price = bet365.prices["Celtics"]
    # execution EV equals the LOO-based value, not the naive all-book EV
    assert ex.ev == pytest.approx(c.loo_probability("bet365") * price - 1.0,
                                  abs=1e-12)
    naive_ev = c.p_top * price - 1.0
    assert ex.ev > naive_ev  # removing the lagging book's drag raises its edge


def test_gate_carries_line_into_pick():
    # h2h: pick.line is None
    res_h2h = evaluate(_consensus("nba-a"), threshold=0.75, min_books=5, max_cv=0.10)
    assert res_h2h.reason == "ok"
    assert res_h2h.pick.line is None

    # totals: pick.line is the consensus point line (220.5)
    from lisa.fixtures import NBA_TOTALS_ODDS
    tot_match = parse_odds_payload(NBA_TOTALS_ODDS, market_keys=("totals",))[0]
    c_tot = consensus.refine(tot_match, now=NOW, min_books=5)
    assert c_tot is not None
    assert c_tot.line == 220.5
    res_tot = evaluate(c_tot, threshold=0.75, min_books=5, max_cv=0.10)
    assert res_tot.reason == "ok"
    assert res_tot.pick is not None
    assert res_tot.pick.line == 220.5