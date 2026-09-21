"""Honest grading and derived analytics for the markets the archive holds.

This module is deliberately small and auditable. It turns the archive's raw
odds + real final scores into *measured* facts:

- ``grade_asian_handicap`` resolves an Asian Handicap bet against a real final
  scoreline, correctly handling quarter balls (half-win / half-loss), half
  balls (pure win/loss) and whole-ball pushes (stake refunded / void).
- ``grade_total`` resolves an Over/Under bet against a final scoreline.
- ``derive_btts`` / ``correct_score_distribution`` produce score-derived
  analytics (BTTS yes/no, most common scorelines) for the transparency layer.

Nothing here invents odds. All probabilities/payouts are derived strictly from
archived prices and the archived official scores.
"""
from __future__ import annotations

from typing import Iterator

from .odds import Score

# Asian Handicap line convention (football-data.co.uk "AHh"): the HOME team's
# starting line. home +line - away = diff; betting home wins when diff > 0.
QUARTER_STEP = 0.25


def _resolve_line(diff: float) -> str:
    """Resolve one whole/half-ball sub-line from the home-perspective diff."""
    d = round(diff, 6)
    if d > 0:
        return "WIN"
    if d < 0:
        return "LOSS"
    return "PUSH"


def _split_result_pair(combined: str) -> str:
    """Map (r1, r2) to the standard AH outcome labels."""
    wins = combined.count("WIN")
    losses = combined.count("LOSS")
    pushes = combined.count("PUSH")
    if wins == 1 and pushes == 1:
        return "HALF_WIN"
    if losses == 1 and pushes == 1:
        return "HALF_LOSS"
    if wins == 1 and losses == 1:
        return "PUSH"
    if wins == 2:
        return "WIN"
    if losses == 2:
        return "LOSS"
    return "PUSH"


def grade_asian_handicap(home_goals: int, away_goals: int, line: float,
                         side: str) -> dict[str, object]:
    """Grade an Asian Handicap wager against a final score.

    ``side`` is ``"home"`` or ``"away"`` — the team receiving the nominal
    handicap (the column owner). Returns:

        result          WIN | LOSS | PUSH | HALF_WIN | HALF_LOSS
        decided         False when no stake is at risk (whole-ball PUSH)
        stake_fraction  1.0 / 0.5 / 0.0  — portion of the unit at risk
        won_fraction    1.0 / 0.5 / 0.0  — portion that won (0 on loss/push)

    Given the betting price ``odds``, net P&L per unit staked is therefore
    ``won_fraction * odds - stake_fraction`` (0 for a pure push) — e.g. a
    half-win priced at 2.00 nets 0.5*2.0 - 0.5 = +0.5 units.
    """
    line = float(line)
    home_delta = home_goals + line - away_goals
    if side == "away":
        home_delta = -home_delta

    quarter_count = int(round(line / QUARTER_STEP))
    is_quarter = (quarter_count % 2) == 1

    if is_quarter:
        resolved = [
            _resolve_line(home_delta - QUARTER_STEP),
            _resolve_line(home_delta + QUARTER_STEP),
        ]
        result = _split_result_pair("".join(resolved))
    else:
        result = _resolve_line(home_delta)

    if result == "WIN":
        decided, stake, won = True, 1.0, 1.0
    elif result == "HALF_WIN":
        decided, stake, won = True, 0.5, 0.5
    elif result == "LOSS":
        decided, stake, won = True, 1.0, 0.0
    elif result == "HALF_LOSS":
        decided, stake, won = True, 0.5, 0.0
    else:  # PUSH / void
        decided, stake, won = False, 0.0, 0.0

    return {
        "result": result,
        "decided": decided,
        "stake_fraction": stake,
        "won_fraction": won,
        "line": line,
    }


def grade_total(home_goals: int, away_goals: int, line: float,
                side: str) -> dict[str, object]:
    """Grade an Over/Under total against a final scoreline.

    ``side`` is ``"over"`` or ``"under"``. Integer lines may push (void)
    when the total lands exactly on the line.
    """
    total = home_goals + away_goals
    diff = total - float(line)
    if abs(round(diff, 6)) < 1e-6:
        return {"result": "PUSH", "decided": False, "stake_fraction": 0.0,
                "won_fraction": 0.0, "line": line}
    won = diff > 0
    if side == "over":
        result = "WIN" if won else "LOSS"
    else:
        result = "WIN" if not won else "LOSS"
    return {"result": result, "decided": True,
            "stake_fraction": 1.0, "won_fraction": 1.0 if result == "WIN" else 0.0,
            "line": line}


def derive_btts(score: Score) -> bool:
    """Both Teams To Score — directly and only from the archived scoreline."""
    if score.home_score is None or score.away_score is None:
        raise ValueError("derive_btts requires a final scoreline")
    return score.home_score > 0 and score.away_score > 0


def derived_outcomes(score: Score) -> dict[str, object]:
    """Score-derived facts about a finished match (transparency layer only)."""
    return {
        "match_id": score.match_id,
        "result": score.winner(),
        "home_goals": score.home_score,
        "away_goals": score.away_score,
        "btts": derive_btts(score) if score.completed else None,
        "total_goals": (score.home_score or 0) + (score.away_score or 0),
        "home_win_margin": (score.home_score or 0) - (score.away_score or 0),
        "over_2_5": (score.home_score or 0) + (score.away_score or 0) > 2.5,
    }


def correct_score_distribution(scorelines: Iterator[tuple[int, int]],
                               top_n: int = 12) -> list[dict[str, object]]:
    """Most common final scorelines in the archive (empirical reference)."""
    from collections import Counter

    counts: Counter[tuple[int, int]] = Counter()
    for home, away in scorelines:
        counts[(home, away)] += 1
    total = sum(counts.values())
    return [
        {"home_goals": s[0], "away_goals": s[1],
         "count": c, "fraction": round(c / total, 5)}
        for s, c in counts.most_common(top_n)
    ]


__all__ = [
    "grade_asian_handicap",
    "grade_total",
    "derive_btts",
    "derived_outcomes",
    "correct_score_distribution",
]