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