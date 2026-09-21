"""Independent, strictly chronological Elo + Poisson 1X2 model.

Purpose
-------
Layer 3 of the honest evaluation stack. This model is an *independent arrow*:
it learns ONLY from real final scores (no odds, no market consensus, no model
parameters fitted on the window being evaluated). It lets the walk-forward
engine compare "does LISA's market-gated consensus add value over an
independently calibrated model?" without circularity.

No-look-ahead invariant
-----------------------
``predict()`` returns probabilities built exclusively from ratings that were
updated on matches strictly BEFORE the match being predicted. ``observe()``
must be called AFTER ``predict()`` for every real match, in chronological
order, and it must never be called for a match before predicting it. The
walk-forward engine and the tests enforce this invariant.

No third-party dependencies — pure Python standard library.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Iterator, Optional

# ---------------------------------------------------------------------------
# Tunable constants (documented priors, NOT fitted to the evaluation window)
# ---------------------------------------------------------------------------

START_RATING: float = 1500.0
"""Starting Elo rating for every brand-new team."""

DV: float = 400.0
"""Elo divisor controlling how much a rating gap moves win expectancy."""

K_BASE: float = 24.0
"""Base Elo K-factor applied per result."""

HOME_ADV: float = 60.0
"""Home advantage expressed in rating points."""

DRAW_SCALE: float = 0.6
"""Elo adjustment applied to a draw result (draws update ratings weakly)."""

K_MARGIN_WIN: float = 0.5
"""Each extra goal of margin adds this fraction of K for a decisive result."""

GOAL_REACT: float = 0.35
"""Rating gap -> expected-goal responsiveness (log-space slope)."""

HOME_MU: float = 1.5
"""Prior expected home goals per match at a neutral rating gap."""

AWAY_MU: float = 1.2
"""Prior expected away goals per match at a neutral rating gap."""

XI_CAP: float = 2.4
"""Caps the rating-gap term so goal expectations stay in a sane range."""

POISSON_TAIL: int = 9
"""Poisson probabilities for goal counts are summed up to this bound."""

MIN_PRIOR_GAMES: int = 2
"""Clubs need this many historical results before the model will bet on them."""

SCALE: float = 1.0 / DV


def _elo_expectation(delta: float) -> float:
    """Win-expectancy from a rating delta, using the standard Elo formula."""
    return 1.0 / (1.0 + math.pow(10.0, -delta * SCALE))


def _poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0.0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _poisson_1x2(lam_home: float, lam_away: float) -> tuple[float, float, float]:
    """(p_home, p_draw, p_away) implied by two independent Poisson goals."""
    tail = max(POISSON_TAIL, int(math.ceil(max(lam_home, lam_away) * 2.0)) + 3)
    grid = [
        [(_poisson_pmf(hi, lam_home), _poisson_pmf(aj, lam_away))
         for aj in range(tail)]
        for hi in range(tail)
    ]
    p_home = 0.0
    p_draw = 0.0
    for hi in range(tail):
        for aj in range(tail):
            w = grid[hi][aj][0] * grid[hi][aj][1]
            if hi > aj:
                p_home += w
            elif hi == aj:
                p_draw += w
    p_away = 1.0 - p_home - p_draw
    # Guard tiny floating-point drift.
    p_away = max(0.0, p_away)
    total = p_home + p_draw + p_away
    if total <= 0.0:
        return (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0)
    return (p_home / total, p_draw / total, p_away / total)


class EloPoissonModel:
    """Elo ratings per (league, club) mapped to 1X2 via a Poisson expectancy.

    Threading / determinism: all state lives in ``self._ratings``; predicting
    and observing mutate only that dict, so two identical run sequences
    produce bit-identical results.
    """

    def __init__(self, **overrides: float) -> None:
        self._ratings: dict[tuple[str, str], float] = {}
        self._games: dict[tuple[str, str], int] = {}
        params = {
            "start_rating": START_RATING,
            "k_base": K_BASE,
            "home_adv": HOME_ADV,
            "draw_scale": DRAW_SCALE,
            "k_margin_win": K_MARGIN_WIN,
            "goal_react": GOAL_REACT,
            "home_mu": HOME_MU,
            "away_mu": AWAY_MU,
            "xi_cap": XI_CAP,
            "min_prior_games": MIN_PRIOR_GAMES,
        }
        params.update({k: float(v) for k, v in overrides.items() if k in params})
        self.start_rating = params["start_rating"]
        self.k_base = params["k_base"]
        self.home_adv = params["home_adv"]
        self.draw_scale = params["draw_scale"]
        self.k_margin_win = params["k_margin_win"]
        self.goal_react = params["goal_react"]
        self.home_mu = params["home_mu"]
        self.away_mu = params["away_mu"]
        self.xi_cap = params["xi_cap"]
        self.min_prior_games = int(params["min_prior_games"])

    # -- public API ---------------------------------------------------------

    def rating(self, league: str, team: str) -> float:
        return self._ratings.get((league, team), self.start_rating)

    def games_seen(self, league: str, team: str) -> int:
        return self._games.get((league, team), 0)

    def ready(self, league: str, home: str, away: str) -> bool:
        """True only when BOTH clubs have enough real history to model."""
        return (self.games_seen(league, home) >= self.min_prior_games
                and self.games_seen(league, away) >= self.min_prior_games)

    def predict(self, league: str, home: str, away: str) -> tuple[float, float, float]:
        """(p_home, p_draw, p_away) from current ratings; no side effects."""
        lam_home, lam_away = self._lambdas(league, home, away)
        return _poisson_1x2(lam_home, lam_away)

    def _lambdas(self, league: str, home: str, away: str) -> tuple[float, float]:
        rh = self.rating(league, home)
        ra = self.rating(league, away)
        delta = rh + self.home_adv - ra
        xi = max(-self.xi_cap, min(self.xi_cap, delta / 100.0))
        lam_home = self.home_mu * math.exp(0.5 * xi * self.goal_react)
        lam_away = self.away_mu * math.exp(-0.5 * xi * self.goal_react)
        return lam_home, lam_away

    def predict_score_matrix(self, league: str, home: str, away: str) -> dict[str, Any]:
        """Full score distribution from the Poisson model (no side effects).

        Exactly the same lambdas that drive ``predict`` are used to build the
        joint goal grid, so every derived probability is consistent with the
        1X2 line the walk-forward evaluation audits.
        """
        lam_home, lam_away = self._lambdas(league, home, away)
        tail = max(POISSON_TAIL, int(math.ceil(max(lam_home, lam_away) * 2.0)) + 3)
        grid = [
            [(_poisson_pmf(hi, lam_home), _poisson_pmf(aj, lam_away))
             for aj in range(tail)]
            for hi in range(tail)
        ]

        p_home = p_draw = p_btts = p_over = 0.0
        scores: list[dict[str, Any]] = []
        p_no_home = math.exp(-lam_home)
        p_no_away = math.exp(-lam_away)
        p_btts = 1.0 - p_no_home - p_no_away + p_no_home * p_no_away
        for hi in range(tail):
            for aj in range(tail):
                w = grid[hi][aj][0] * grid[hi][aj][1]
                if hi > aj:
                    p_home += w
                elif hi == aj:
                    p_draw += w
                if hi + aj > 2.5:
                    p_over += w
                scores.append((w, hi, aj))
        p_away = max(0.0, 1.0 - p_home - p_draw)
        total_1x2 = p_home + p_draw + p_away
        if total_1x2 <= 0.0:
            p_home = p_draw = p_away = 1.0 / 3.0
        else:
            p_home /= total_1x2
            p_draw /= total_1x2
            p_away /= total_1x2

        scores.sort(reverse=True)
        return {
            "league": league,
            "home": home,
            "away": away,
            "p_home": round(p_home, 4),
            "p_draw": round(p_draw, 4),
            "p_away": round(p_away, 4),
            "p_btts": round(min(1.0, p_btts), 4),
            "p_over_2_5": round(min(1.0, p_over), 4),
            "expected_goals": {
                "home": round(lam_home, 2),
                "away": round(lam_away, 2),
            },
            "most_likely_scores": [
                {"home_goals": hi, "away_goals": aj, "p": round(w, 5)}
                for w, hi, aj in scores[:8]
            ],
        }

    def predict_home_win(self, league: str, home: str, away: str) -> float:
        return self.predict(league, home, away)[0]

    def observe(self, league: str, home: str, away: str,
                home_score: int, away_score: int) -> float:
        """Update ratings from a final scoreline. Returns the (home, away)
        Elo result marker (1/0.5/0) for callers that want to track it."""
        rh = self.rating(league, home)
        ra = self.rating(league, away)
        delta = rh + self.home_adv - ra
        expected = _elo_expectation(delta)

        if home_score > away_score:
            actual = 1.0
            margin = home_score - away_score
            k = self.k_base * (1.0 + self.k_margin_win * min(margin, 4.0))
        elif home_score == away_score:
            actual = 0.5
            margin = 0
            k = self.k_base * self.draw_scale
        else:
            actual = 0.0
            margin = away_score - home_score
            k = self.k_base * (1.0 + self.k_margin_win * min(margin, 4.0))

        shift = k * (actual - expected)
        key_h = (league, home)
        key_a = (league, away)
        self._ratings[key_h] = rh + shift
        self._ratings[key_a] = ra - shift
        self._games[key_h] = self.games_seen(league, home) + 1
        self._games[key_a] = self.games_seen(league, away) + 1
        return actual

    # -- walking ------------------------------------------------------------

    def predict_log(self, league: str, home: str, away: str) -> dict[str, Any]:
        """Prediction record used by the walk-forward evaluator and tests."""
        ph, pd, pa = self.predict(league, home, away)
        return {
            "league": league,
            "home": home,
            "away": away,
            "p_home": ph,
            "p_draw": pd,
            "p_away": pa,
            "model_ready": self.ready(league, home, away),
        }

    def walk_forward(
        self,
        matches: list[dict[str, Any]],
    ) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        """Yield (match, prediction) strictly BEFORE observing that match.

        ``matches`` must be a list of dicts:
            {"league": sport_key, "home": ..., "away": ...,
             "home_score": int, "away_score": int, "match_id": str}
        sorted in chronological order. The engine sorts internally.
        """
        for m in matches:
            pred = self.predict_log(m["league"], m["home"], m["away"])
            yield m, pred
            self.observe(m["league"], m["home"], m["away"],
                         m["home_score"], m["away_score"])

    def state_size(self) -> int:
        return len(self._ratings)


__all__ = [
    "EloPoissonModel",
    "_elo_expectation",
    "_poisson_1x2",
    "START_RATING",
    "DV",
    "K_BASE",
    "HOME_ADV",
    "DRAW_SCALE",
    "K_MARGIN_WIN",
    "GOAL_REACT",
    "HOME_MU",
    "AWAY_MU",
    "XI_CAP",
    "MIN_PRIOR_GAMES",
    "SCALE",
]