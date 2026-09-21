"""Unit tests for the market grading + derived-analytics module."""
from __future__ import annotations

import pytest

from lisa.markets import (
    correct_score_distribution,
    derive_btts,
    derived_outcomes,
    grade_asian_handicap,
    grade_total,
)
from lisa.odds import Score


def _score(h: int, a: int, completed: bool = True) -> Score:
    return Score(match_id="x", sport_key="s", commence_time=None,
                 completed=completed, home_score=h, away_score=a, status="final")


def test_ah_integer_line_win_loss_push():
    # home laying -1: win by 2+ = WIN, win by exactly 1 = PUSH, others = LOSS
    assert grade_asian_handicap(3, 1, -1.0, "home")["result"] == "WIN"
    assert grade_asian_handicap(2, 1, -1.0, "home")["result"] == "PUSH"
    # draw-no-bet at 0: stake fully returned
    assert grade_asian_handicap(1, 1, 0.0, "home")["result"] == "PUSH"
    assert grade_asian_handicap(1, 1, 0.0, "home")["stake_fraction"] == 0.0
    assert grade_asian_handicap(1, 1, 0.0, "home")["decided"] is False
    # home +0 wins outright -> WIN
    assert grade_asian_handicap(2, 1, 0.0, "home")["result"] == "WIN"


def test_ah_half_ball_no_push():
    # home +0.5 covers any draw or win
    assert grade_asian_handicap(1, 1, 0.5, "home")["result"] == "WIN"
    assert grade_asian_handicap(1, 2, 0.5, "home")["result"] == "LOSS"
    assert grade_asian_handicap(1, 2, -0.5, "away")["result"] == "WIN"
    assert grade_asian_handicap(2, 1, -0.5, "home")["result"] == "WIN"


def test_ah_quarter_ball_half_results():
    # -0.25, home wins by 1 -> both halves win (WIN)
    assert grade_asian_handicap(2, 1, -0.25, "home")["result"] == "WIN"
    # -0.25, draw -> -0.5 loses, 0 pushes -> HALF_LOSS
    r = grade_asian_handicap(1, 1, -0.25, "home")
    assert r["result"] == "HALF_LOSS"
    assert r["stake_fraction"] == 0.5 and r["won_fraction"] == 0.0
    # -0.75, home wins by 1 -> -1 pushes, -0.5 wins -> HALF_WIN
    r = grade_asian_handicap(2, 1, -0.75, "home")
    assert r["result"] == "HALF_WIN"
    assert r["stake_fraction"] == 0.5 and r["won_fraction"] == 0.5
    # away side of the same scoreline -> HALF_LOSS
    assert grade_asian_handicap(2, 1, -0.75, "away")["result"] == "HALF_LOSS"


def test_ah_pnl_semantics():
    # half-win at odds 2.00: 0.5*2.0 - 0.5 = +0.5 units
    r = grade_asian_handicap(2, 1, -0.75, "home")
    assert r["won_fraction"] * 2.0 - r["stake_fraction"] == pytest.approx(0.5)


def test_total_2_5():
    assert grade_total(2, 1, 2.5, "over")["result"] == "WIN"
    assert grade_total(2, 1, 2.5, "under")["result"] == "LOSS"
    assert grade_total(1, 1, 2.5, "over")["result"] == "LOSS"
    assert grade_total(1, 1, 2.5, "under")["result"] == "WIN"


def test_total_integer_line_push():
    r = grade_total(2, 1, 3.0, "over")
    assert r["result"] == "PUSH" and r["decided"] is False


def test_btts_derived():
    assert derive_btts(_score(1, 1)) is True
    assert derive_btts(_score(4, 0)) is False
    assert derived_outcomes(_score(3, 0))["btts"] is False
    assert derived_outcomes(_score(2, 2))["over_2_5"] is True
    assert derived_outcomes(_score(1, 1))["over_2_5"] is False


def test_correct_score_distribution():
    scores = [(1, 1), (1, 0), (1, 1), (2, 1), (1, 1)]
    top = correct_score_distribution(iter(scores), top_n=3)
    assert top[0]["home_goals"] == 1 and top[0]["away_goals"] == 1
    assert top[0]["count"] == 3 and top[0]["fraction"] == pytest.approx(0.6)
    assert len(top) == 3