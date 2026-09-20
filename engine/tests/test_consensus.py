"""Stage 2 consensus: blending, agreement metrics, freshness, coverage."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from lisa import consensus
from lisa.fixtures import NBA_ODDS
from lisa.odds import Match, utcnow
from lisa.parsing import parse_odds_payload

NOW = datetime(2026, 9, 20, 15, 0, 0, tzinfo=timezone.utc)


def _match(payload, match_id):
    for m in parse_odds_payload(payload):
        if m.id == match_id:
            return m
    raise AssertionError(f"match {match_id} not found in payload")


def test_consensus_heavy_favourite():
    c = consensus.refine(_match(NBA_ODDS, "nba-a"), now=NOW)
    assert c is not None
    assert c.top_outcome == "Celtics"
    assert 0.80 < c.p_top < 0.85
    assert c.n_books == 5
    assert c.cv < 0.05
    assert c.fair_odds == pytest.approx(1.0 / c.p_top)
    # sharp anchor is weighted hardest (multiplier x2 AND inverse margin)
    assert c.weights["pinnacle"] > c.weights["bet365"]
    assert abs(sum(c.p.values()) - 1.0) < 1e-9


def test_consensus_dispersion_detected():
    c = consensus.refine(_match(NBA_ODDS, "nba-c"), now=NOW)
    assert c is not None
    assert c.cv > 0.10


def test_consensus_insufficient_books_is_none():
    assert consensus.refine(_match(NBA_ODDS, "nba-d"), now=NOW) is None


def test_consensus_stale_books_dropped():
    match = _match(NBA_ODDS, "nba-b")
    # every book's line is ~1h old and we ask for <=60s freshness
    assert consensus.refine(match, now=NOW, max_book_age_sec=60) is None


def test_consensus_without_any_books_is_none():
    empty = Match(id="x", sport_key="basketball_nba", commence_time=utcnow(),
                  home_team="A", away_team="B", completed=False)
    assert consensus.refine(empty, now=NOW) is None


def test_single_book_true_probs_rejects_junk():
    from lisa.odds import Book
    warm = Book(key="x", title="X", last_update=None, outcomes={"A": 1.6, "B": 2.4})
    btp = consensus.single_book_true_probs(warm)
    assert btp is not None
    assert abs(sum(btp.probs.values()) - 1.0) < 1e-9
    junk = Book(key="x", title="X", last_update=None, outcomes={"A": 0.5, "B": 2.4})
    assert consensus.single_book_true_probs(junk) is None


def test_loo_noop_for_non_member_equals_full_consensus():
    c = consensus.refine(_match(NBA_ODDS, "nba-a"), now=NOW)
    assert c is not None
    # excluding a book that isn't in the set is a no-op: identical reference
    assert c.loo_probability("nonexistent") == pytest.approx(c.p_top, abs=1e-12)


def test_loo_moves_reference_away_from_excluded_soft_book():
    c = consensus.refine(_match(NBA_ODDS, "nba-a"), now=NOW)
    assert c is not None
    # bet365 posts a lagging (higher) price on the favourite -> its own
    # de-vigged probability is *below* consensus. Excluding it must push the
    # reference *up*, and the largest single influence is the sharp pinnacle.
    loo_ref = c.loo_probability("bet365")
    assert loo_ref is not None
    assert loo_ref > c.p_top
    assert abs(c.loo_probability("pinnacle") - c.p_top) < 0.02


def test_loo_practical_for_ev_overlay():
    c = consensus.refine(_match(NBA_ODDS, "nba-a"), now=NOW)
    assert c is not None
    bet365 = next(b for b in c.books if b.book_key == "bet365")
    price = bet365.prices["Celtics"]
    # EV for the lagging book measured against a reference that excludes it
    loo_ev = c.loo_probability("bet365") * price - 1.0
    naive_ev = c.p_top * price - 1.0
    assert loo_ev > naive_ev  # ignoring its own drag increases the edge