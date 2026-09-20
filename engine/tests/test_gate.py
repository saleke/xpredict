"""Stage 3 gate: threshold, dispersion, EV overlay, execution selection."""
from __future__ import annotations

from datetime import datetime, timezone

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
    res = evaluate(_consensus("nba-e"), threshold=0.75, min_books=5, max_cv=0.10)
    assert res.reason == "ok"
    assert res.pick.best_execution is None  # no book beats fair price


def test_gate_requires_positive_ev():
    res = evaluate(_consensus("nba-e"), threshold=0.75, min_books=5, max_cv=0.10,
                   require_positive_ev=True)
    assert res.pick is None
    assert res.reason == "no_positive_ev"


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