"""Unit tests for the LISA Closing Line Value (CLV) Engine."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest

from lisa.calibration import (
    CLVReport,
    compute_clv_metrics,
    evaluate_clv_by_market,
    format_clv_report,
)
from lisa.gate import Execution, Pick
from lisa.notify import LogNotifier
from lisa.odds import Book, Match, utcnow
from lisa.pipeline import Pipeline
from lisa.storage import InMemoryStorage, pick_key
import lisa.config as cfg


def test_clv_computation_analytical() -> None:
    # Positive CLV: emit at 1.45, closed at 1.25 -> (1.45 / 1.25) - 1.0 = +16%
    records = [
        {"clv": (1.45 / 1.25) - 1.0, "result": "WIN"},
        {"clv": (1.20 / 1.25) - 1.0, "result": "LOSS"},
        {"clv": 0.0, "result": "WIN"},
    ]
    rep = compute_clv_metrics(records)

    assert rep.count == 3
    assert pytest.approx(rep.min_clv) == (1.20 / 1.25) - 1.0  # -0.04
    assert pytest.approx(rep.max_clv) == (1.45 / 1.25) - 1.0  # +0.16
    assert pytest.approx(rep.positive_clv_share) == 1.0 / 3.0


def test_clv_report_empty_and_invalid() -> None:
    # Empty collection
    rep_empty = compute_clv_metrics([])
    assert rep_empty.count == 0
    assert rep_empty.mean_clv is None
    assert rep_empty.median_clv is None
    assert rep_empty.positive_clv_share is None

    # Invalid / non-float values
    records = [
        {"clv": None},
        {"clv": "corrupted"},
        {"other_key": 123},
    ]
    rep_invalid = compute_clv_metrics(records)
    assert rep_invalid.count == 0


def test_clv_win_rate_correlation() -> None:
    records = [
        {"clv": 0.10, "result": "WIN"},
        {"clv": 0.05, "result": "WIN"},
        {"clv": -0.02, "result": "LOSS"},
        {"clv": -0.05, "result": "LOSS"},
        {"clv": 0.08, "result": "VOID"},  # void excluded from win rate correlation
    ]
    rep = compute_clv_metrics(records)

    assert rep.count == 5
    assert rep.win_rate_positive_clv == 1.0  # 2 wins out of 2 decided
    assert rep.win_rate_negative_clv == 0.0  # 0 wins out of 2 decided


def test_evaluate_clv_by_market() -> None:
    records = [
        {"market": "h2h", "clv": 0.08},
        {"market": "h2h", "clv": 0.04},
        {"market": "totals", "clv": -0.02},
        {"market": "spreads", "clv": 0.05},
    ]
    by_market = evaluate_clv_by_market(records)

    assert "overall" in by_market
    assert by_market["overall"].count == 4
    assert "h2h" in by_market
    assert by_market["h2h"].count == 2
    assert pytest.approx(by_market["h2h"].mean_clv) == 0.06
    assert "totals" in by_market
    assert by_market["totals"].count == 1
    assert "spreads" in by_market
    assert by_market["spreads"].count == 1


def test_format_clv_report_output() -> None:
    rep = CLVReport(
        count=10,
        mean_clv=0.035,
        median_clv=0.030,
        positive_clv_share=0.70,
        min_clv=-0.04,
        max_clv=0.12,
        win_rate_positive_clv=0.85,
        win_rate_negative_clv=0.60,
    )
    text = format_clv_report(rep, title="Test CLV")

    assert "--- Test CLV ---" in text
    assert "Picks Evaluated: 10" in text
    assert "+3.50%" in text
    assert "70.0%" in text
    assert "85.0%" in text


def test_pipeline_prekickoff_clv_snapshot_and_locking() -> None:
    """Verify that:
    1. A pick emitted at T-2h starts with initial emission closing odds (CLV = 0.0).
    2. A subsequent pre-kickoff cycle (T-30m) updates closing odds to the latest price and computes CLV.
    3. A post-kickoff cycle (T+15m) locks the pre-kickoff snapshot and does not overwrite it.
    """
    commence = datetime(2026, 9, 20, 20, 0, 0, tzinfo=timezone.utc)
    t_emit = datetime(2026, 9, 20, 18, 0, 0, tzinfo=timezone.utc)   # T-2h
    t_spike = datetime(2026, 9, 20, 19, 30, 0, tzinfo=timezone.utc)  # T-30m
    t_live = datetime(2026, 9, 20, 20, 15, 0, tzinfo=timezone.utc)   # T+15m (live)

    # 5 books all heavily favoring Team A:
    # Initial odds at T-2h: best book offers 1.45
    b_emit = [
        Book("pinnacle", "Pinnacle", t_emit, {"TeamA": 1.45, "TeamB": 4.50}),
        Book("book2", "Book 2", t_emit, {"TeamA": 1.40, "TeamB": 4.60}),
        Book("book3", "Book 3", t_emit, {"TeamA": 1.42, "TeamB": 4.40}),
        Book("book4", "Book 4", t_emit, {"TeamA": 1.41, "TeamB": 4.50}),
        Book("book5", "Book 5", t_emit, {"TeamA": 1.40, "TeamB": 4.70}),
    ]
    m_emit = Match(
        id="m-clv", sport_key="basketball_nba", commence_time=commence,
        home_team="TeamA", away_team="TeamB", completed=False,
        market="h2h", bookmakers=tuple(b_emit),
    )

    storage = InMemoryStorage()
    settings = cfg.Settings(sports=("basketball_nba",), gate_threshold=0.65)
    pipeline = Pipeline(client=None, storage=storage, settings=settings, notifier=LogNotifier())

    # Cycle 1: Emission at T-2h
    from lisa.consensus import refine as refine_match
    from lisa.gate import evaluate as evaluate_gate
    cons_emit = refine_match(m_emit, now=t_emit, min_books=3)
    gate_emit = evaluate_gate(cons_emit, threshold=0.65, min_books=3)
    assert gate_emit.pick is not None
    assert storage.insert_pick(gate_emit.pick) is True

    key = pick_key("m-clv", "h2h", "TeamA")
    row_1 = storage._picks[key]
    assert row_1["best_odds"] == 1.45
    # At emission there is no observed close yet. Seeding closing_odds from
    # best_odds made an unrepriced pick look like it had exactly matched the
    # close (clv == 0.0), which reads as "beat the close perfectly" in every
    # mean that folds it in. Unobserved stays NULL until a close is seen.
    assert row_1["closing_odds"] is None
    assert row_1["clv"] is None

    # Cycle 2: T-30m before kickoff, Pinnacle line moves down to 1.25!
    b_spike = [
        Book("pinnacle", "Pinnacle", t_spike, {"TeamA": 1.25, "TeamB": 5.50}),
        Book("book2", "Book 2", t_spike, {"TeamA": 1.24, "TeamB": 5.60}),
        Book("book3", "Book 3", t_spike, {"TeamA": 1.23, "TeamB": 5.40}),
        Book("book4", "Book 4", t_spike, {"TeamA": 1.25, "TeamB": 5.50}),
        Book("book5", "Book 5", t_spike, {"TeamA": 1.22, "TeamB": 5.70}),
    ]
    m_spike = Match(
        id="m-clv", sport_key="basketball_nba", commence_time=commence,
        home_team="TeamA", away_team="TeamB", completed=False,
        market="h2h", bookmakers=tuple(b_spike),
    )
    cons_spike = refine_match(m_spike, now=t_spike, min_books=3)
    gate_spike = evaluate_gate(cons_spike, threshold=0.65, min_books=3)

    # Calling _maybe_update_closing directly or via pipeline logic
    pipeline._maybe_update_closing(gate_spike.pick, m_spike, cons_spike, now=t_spike)

    row_2 = storage._picks[key]
    assert row_2["closing_odds"] == 1.25
    # CLV = (1.45 / 1.25) - 1.0 = +16.0%
    assert pytest.approx(row_2["clv"]) == (1.45 / 1.25) - 1.0

    # Cycle 3: T+15m (live in-play). In-play odds fluctuate to 1.05.
    b_live = [
        Book("pinnacle", "Pinnacle", t_live, {"TeamA": 1.05, "TeamB": 12.0}),
        Book("book2", "Book 2", t_live, {"TeamA": 1.05, "TeamB": 12.0}),
        Book("book3", "Book 3", t_live, {"TeamA": 1.04, "TeamB": 13.0}),
        Book("book4", "Book 4", t_live, {"TeamA": 1.05, "TeamB": 12.0}),
        Book("book5", "Book 5", t_live, {"TeamA": 1.06, "TeamB": 11.0}),
    ]
    m_live = Match(
        id="m-clv", sport_key="basketball_nba", commence_time=commence,
        home_team="TeamA", away_team="TeamB", completed=False,
        market="h2h", bookmakers=tuple(b_live),
    )
    cons_live = refine_match(m_live, now=t_live, min_books=3)
    gate_live = evaluate_gate(cons_live, threshold=0.65, min_books=3)


    # Update closing attempted during live play
    pipeline._maybe_update_closing(gate_live.pick, m_live, cons_live, now=t_live)

    # Invariant check: closing_odds MUST REMAIN 1.25 (locked pre-kickoff!)
    row_3 = storage._picks[key]
    assert row_3["closing_odds"] == 1.25
    assert pytest.approx(row_3["clv"]) == (1.45 / 1.25) - 1.0
