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


def test_line_bucket_densest_wins():
    from lisa.fixtures import NBA_TOTALS_ODDS
    matches = parse_odds_payload(NBA_TOTALS_ODDS, market_keys=("totals",))
    assert len(matches) == 1
    match = matches[0]
    # NBA_TOTALS_ODDS has 5 books at line 220.5 and 2 books at line 221.5
    c = consensus.refine(match, now=NOW, min_books=2)
    assert c is not None
    assert c.line == 220.5
    assert c.n_books == 5
    assert c.top_outcome == "Over"
    assert c.p_top > 0.75


def test_line_bucket_tiebreak_lower_cv():
    from lisa.odds import Book
    # Two buckets with exactly 3 books each:
    # Bucket 2.5 has tight consensus (low CV)
    # Bucket 3.0 has dispersed prices (high CV)
    b25_1 = Book(key="pinnacle", title="Pinnacle", last_update=NOW,
                 outcomes={"Over": 1.90, "Under": 1.90}, line=2.5)
    b25_2 = Book(key="bet365", title="Bet365", last_update=NOW,
                 outcomes={"Over": 1.91, "Under": 1.89}, line=2.5)
    b25_3 = Book(key="draftkings", title="DraftKings", last_update=NOW,
                 outcomes={"Over": 1.89, "Under": 1.91}, line=2.5)

    b30_1 = Book(key="pinnacle", title="Pinnacle", last_update=NOW,
                 outcomes={"Over": 1.70, "Under": 2.15}, line=3.0)
    b30_2 = Book(key="bet365", title="Bet365", last_update=NOW,
                 outcomes={"Over": 2.10, "Under": 1.75}, line=3.0)
    b30_3 = Book(key="draftkings", title="DraftKings", last_update=NOW,
                 outcomes={"Over": 1.85, "Under": 1.95}, line=3.0)

    match = Match(id="m-tie-cv", sport_key="soccer_spain_la_liga",
                  commence_time=NOW, home_team="A", away_team="B",
                  completed=False, market="totals",
                  bookmakers=(b25_1, b25_2, b25_3, b30_1, b30_2, b30_3))

    c = consensus.refine(match, now=NOW, min_books=3)
    assert c is not None
    assert c.n_books == 3
    # Equal density (3 books each) -> lower CV wins -> line 2.5 selected
    assert c.line == 2.5


def test_line_bucket_tiebreak_lower_line():
    from lisa.odds import Book
    # Two buckets with 3 identical books each (equal density and equal CV):
    # Bucket 2.0 vs Bucket 2.5
    b20_1 = Book(key="pinnacle", title="Pinnacle", last_update=NOW,
                 outcomes={"Over": 1.90, "Under": 1.90}, line=2.0)
    b20_2 = Book(key="bet365", title="Bet365", last_update=NOW,
                 outcomes={"Over": 1.91, "Under": 1.89}, line=2.0)
    b20_3 = Book(key="draftkings", title="DraftKings", last_update=NOW,
                 outcomes={"Over": 1.89, "Under": 1.91}, line=2.0)

    b25_1 = Book(key="pinnacle", title="Pinnacle", last_update=NOW,
                 outcomes={"Over": 1.90, "Under": 1.90}, line=2.5)
    b25_2 = Book(key="bet365", title="Bet365", last_update=NOW,
                 outcomes={"Over": 1.91, "Under": 1.89}, line=2.5)
    b25_3 = Book(key="draftkings", title="DraftKings", last_update=NOW,
                 outcomes={"Over": 1.89, "Under": 1.91}, line=2.5)

    match = Match(id="m-tie-line", sport_key="soccer_spain_la_liga",
                  commence_time=NOW, home_team="A", away_team="B",
                  completed=False, market="totals",
                  bookmakers=(b25_1, b25_2, b25_3, b20_1, b20_2, b20_3))

    c = consensus.refine(match, now=NOW, min_books=3)
    assert c is not None
    assert c.n_books == 3
    # Equal density and equal CV -> lower line wins (2.0 < 2.5)
    assert c.line == 2.0


def test_line_bucket_fragmented_below_min_books_returns_none():
    from lisa.odds import Book
    # 6 books in total, but split 2 + 2 + 2 across three lines
    books = [
        Book(key="b1", title="B1", last_update=NOW, outcomes={"Over": 1.9, "Under": 1.9}, line=2.0),
        Book(key="b2", title="B2", last_update=NOW, outcomes={"Over": 1.9, "Under": 1.9}, line=2.0),
        Book(key="b3", title="B3", last_update=NOW, outcomes={"Over": 1.9, "Under": 1.9}, line=2.5),
        Book(key="b4", title="B4", last_update=NOW, outcomes={"Over": 1.9, "Under": 1.9}, line=2.5),
        Book(key="b5", title="B5", last_update=NOW, outcomes={"Over": 1.9, "Under": 1.9}, line=3.0),
        Book(key="b6", title="B6", last_update=NOW, outcomes={"Over": 1.9, "Under": 1.9}, line=3.0),
    ]
    match = Match(id="m-frag", sport_key="soccer_spain_la_liga",
                  commence_time=NOW, home_team="A", away_team="B",
                  completed=False, market="totals",
                  bookmakers=tuple(books))

    # min_books=3: total books is 6, but no single line bucket has >= 3 books
    assert consensus.refine(match, now=NOW, min_books=3) is None


def test_line_bucket_h2h_byte_identical():
    # All h2h matches have line is None
    for game in NBA_ODDS:
        m = _match([game], game["id"])
        c = consensus.refine(m, now=NOW)
        if c is not None:
            assert c.line is None