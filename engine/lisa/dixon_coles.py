"""Dixon-Coles independent model -- LISA's *independent* probability source.

Why this exists
---------------
The engine used to derive ``p_true`` from the books themselves (Shin de-vig,
then a weighted consensus). That is a self-referential estimate: it can only
ever recover a ~1% execution discrepancy, because a market's de-vigged price is
by construction a very good estimate of its own probability. It cannot beat the
market, and it cannot be measured against the market without circularity.

This module is the second, independent arrow. It learns team strength from
**real final scores only** -- no odds, no prices, no parameters fitted on the
window being predicted -- and produces a full score matrix. Everything the
board then does (find value, size a stake, price a micro-bet, build an
accumulator) is a comparison between this model and a market price. That
comparison is where edge lives, and it is only meaningful because the two
sides are independent.

The model
---------
Dixon & Coles (1997), the standard model in association-football literature::

    log lambda_home = mu + attack[home] - defence[away] + home_adv
    log lambda_away = mu + attack[away] - defence[home]

with a low-score correction tau that fixes the well-known independence
miscalibration on 0-0 / 1-0 / 0-1 / 1-1 scorelines::

    tau(0,0) = 1 - lambda*mu*rho      tau(1,0) = 1 + mu*rho
    tau(0,1) = 1 + lambda*rho          tau(1,1) = 1 - rho

Fitted by penalised maximum likelihood, analytically differentiated. Time decay
``exp(-xi * age_years)`` weights recent results more heavily, because squad
strength drifts.

Honesty rules encoded here (not left to the caller)
---------------------------------------------------
* **Shrinkage is mandatory, not optional.** With a handful of results, an
  unregularised fit gives a promoted club an absurd attack rating from two
  matches. Every rating is pulled toward the league mean by
  ``shrinkage / (shrinkage + games)``, so a team with 2 games sits near the
  mean and a team with 30 does not. The model therefore degrades *toward the
  prior* when it has little evidence, which is the only safe direction.
* **Effective sample size is reported.** ``data_sufficiency`` is the mean
  games-behind-a-rating; below ``SUFFICIENT_GAMES`` the board must not describe
  a pick as an edge. A 9-match Bundesliga sample is not an edge, and this
  module says so rather than letting the gate imply otherwise.
* **No look-ahead.** ``fit()`` takes a match list and weights by age relative to
  a caller-supplied ``as_of``; nothing after ``as_of`` may enter. The board
  passes the current time, so a result that has not happened yet cannot train
  the model that predicts it.

No third-party dependencies -- standard library only.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence
from .job_budget import check_budget
from .providers.calendar import team_identity

# ---------------------------------------------------------------------------
# Defaults (literature priors, deliberately not tuned per-league)
# ---------------------------------------------------------------------------

BASE_MU: float = 1.35
"""League-average goals per team per match (global football prior)."""

HOME_ADV: float = 0.24
"""Home advantage on the log scale (~+27% goals at home)."""

RHO_INIT: float = -0.03
"""Dixon-Coles low-score correlation. Small and negative by convention."""

RHO_BOUNDS: tuple[float, float] = (-0.30, 0.30)
"""rho outside this makes tau negative on some scoreline and breaks the fit."""

SHRINKAGE: float = 8.0
"""Pseudo-games of league-average prior, in the empirical-Bayes sense.

A team's rating is reported shrunk by ``games / (games + SHRINKAGE)``, so a
club with 8 real matches sits half-way to the league mean and a club with 40
does not move. This is the single most important number in the module: without
it a promoted team that won 7-0 once is rated as the best side in the country.
"""

RIDGE: float = 0.05
"""Weak L2 penalty used only to keep the optimiser well-conditioned.

Deliberately small. The real shrinkage toward the league mean is
``SHRINKAGE`` above, applied by games count; a heavy ridge would shrink every
rating a second time and the well-sampled clubs would be pulled toward the mean
they have already earned the right to leave.
"""

XI: float = 0.55
"""Time decay per year. exp(-0.55) ~= 0.58 weight a year later."""

LEARNING_RATE: float = 0.12
MAX_ITERATIONS: int = 220
TOLERANCE: float = 1e-6
"""Convergence threshold on the *relative* objective gain, so the same value
works for a 10-match and an 800-match league. 1e-6 relative is far finer than
the fit's own estimation error, so tightening it further only costs time."""

_MAX_STEP: float = 2.0
"""Ceiling on the line-search step.

Deliberately far above the initial step. A cap equal to the initial step would
make growth impossible and the fit would crawl; the line search shortens the
step on its own whenever the landscape turns, so a generous ceiling is safe and
a tight one is not.
"""

_MAX_LINE_SEARCH: int = 24
"""Backtracking halvings before a step is declared unacceptable. 24 halvings
is ~1e-7 of the original step, so "no acceptable step" really does mean the
gradient is exhausted rather than merely impatient."""

GOAL_TAIL: int = 12
"""Minimum score-matrix support and training-score sanity bound.
Prediction support expands until remaining Poisson mass is below 1e-10."""

MAX_PUBLISHED_LINE: int = 7
"""Highest over/under line published as a market. 7.5 is above any plausible
top-flight total; beyond it the model is extrapolating into a truncated grid."""

SUFFICIENT_GAMES: float = 10.0
"""Average games-behind-a-rating below which the model refuses to claim edge."""

_MIN_PROB = 1e-9

_LOG_LAMBDA_MAX = 4.0
"""Hard ceiling on log(expected goals), i.e. lambda <= ~55.

Above roughly 8 expected goals the Poisson likelihood is dominated by its
``-lambda`` term and the fit is chasing nonsense, so clamping the exponent is
both a numerical guard and a modelling statement."""


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass
class ScoredMatch:
    """One finished match, normalised."""

    league: str
    kickoff: datetime
    home: str
    away: str
    home_score: int
    away_score: int
    weight: float = 1.0


@dataclass
class TeamStrength:
    attack: float = 0.0
    defence: float = 0.0
    games: int = 0

    @property
    def effective_games(self) -> float:
        return self.games


@dataclass
class FitReport:
    """Diagnostics. A caller that ignores these can still misuse the model."""

    matches_used: int
    teams: int
    objective: float
    iterations: int
    converged: bool
    mean_games_behind: float
    rho: float
    #: Share of the *prior* still present in an average rating, in [0, 1].
    #: 1.0 means the fit learned nothing.
    prior_dominance: float

    @property
    def data_sufficiency(self) -> float:
        """Normalised evidence score in [0, 1]."""
        return max(0.0, min(1.0, self.mean_games_behind / SUFFICIENT_GAMES))

    @property
    def sufficient(self) -> bool:
        return self.mean_games_behind >= SUFFICIENT_GAMES

    def to_dict(self) -> dict[str, Any]:
        return {
            "matches_used": self.matches_used,
            "teams": self.teams,
            "objective": round(self.objective, 6),
            "iterations": self.iterations,
            "converged": self.converged,
            "mean_games_behind": round(self.mean_games_behind, 3),
            "data_sufficiency": round(self.data_sufficiency, 3),
            "sufficient": self.sufficient,
            "prior_dominance": round(self.prior_dominance, 4),
            "rho": round(self.rho, 5),
        }


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


class DixonColesModel:
    """Attack/defence ratings per club, fitted to real scorelines."""

    def __init__(self, *, base_mu: float = BASE_MU, home_adv: float = HOME_ADV,
                 rho: float = RHO_INIT, shrinkage: float = SHRINKAGE,
                 ridge: float = RIDGE, xi: float = XI,
                 learning_rate: float = LEARNING_RATE,
                 max_iterations: int = MAX_ITERATIONS, normalise_identities: bool = False) -> None:
        self.base_mu = base_mu
        self.home_adv = home_adv
        self.rho = rho
        self.shrinkage = max(0.0, shrinkage)
        self.ridge = max(0.0, ridge)
        self.xi = xi
        self.learning_rate = learning_rate
        self.max_iterations = max_iterations
        self.normalise_identities = normalise_identities
        self._attack: dict[str, float] = {}
        self._defence: dict[str, float] = {}
        self._games: dict[str, int] = {}
        self.report: Optional[FitReport] = None

    # -- introspection ------------------------------------------------------

    def _team_key(self, team):
        return team_identity(team) if self.normalise_identities else team

    def strength(self, team: str) -> TeamStrength:
        team = self._team_key(team)
        return TeamStrength(self._attack.get(team, 0.0),
                            self._defence.get(team, 0.0),
                            self._games.get(team, 0))

    def knows(self, team: str) -> bool:
        return self._games.get(self._team_key(team), 0) > 0

    @property
    def fitted(self) -> bool:
        return self.report is not None

    def teams(self) -> tuple[str, ...]:
        return tuple(sorted(self._attack))

    # -- fitting ------------------------------------------------------------

    def fit(self, matches: Sequence[ScoredMatch] | Iterable[Mapping[str, Any]],
            *, as_of: Optional[datetime] = None) -> Optional[FitReport]:
        """Fit ratings from finished matches.

        ``as_of`` is the "now" used for time decay. Any match kicking off after
        it is rejected outright rather than weighted, so a look-ahead bug is a
        dropped row instead of a silently better model.
        """
        rows = list(matches)
        if not rows:
            self.report = FitReport(0, 0, 0.0, 0, False, 0.0, self.rho, 1.0)
            return self.report

        as_of = as_of or datetime.now(timezone.utc)
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)

        usable: list[ScoredMatch] = []
        for m in rows:
            m = replace(m, home=self._team_key(m.home), away=self._team_key(m.away))
            if not m.home or not m.away or m.home == m.away:
                continue
            if m.kickoff > as_of:
                continue
            if m.home_score < 0 or m.away_score < 0:
                continue
            if m.home_score > GOAL_TAIL or m.away_score > GOAL_TAIL:
                # A 9-0 would make the Poisson likelihood explode and would
                # tell us nothing useful. Clipped by rejection, not by capping.
                continue
            age_years = max(0.0, (as_of - m.kickoff).total_seconds() / 31_557_600.0)
            m.weight = math.exp(-self.xi * age_years)
            usable.append(m)

        if len(usable) < 2:
            self.report = FitReport(0, 0, 0.0, 0, False, 0.0, self.rho, 1.0)
            return self.report

        self._attack = {t: 0.0 for m in usable for t in (m.home, m.away)}
        self._defence = {t: 0.0 for m in usable for t in (m.home, m.away)}
        # Game counts come straight from the data and do not depend on the
        # parameters, so they are fixed before the first iteration. They are
        # used only for the shrinkage *report*, never inside the objective --
        # see _objective_and_grad.
        self._games = {t: 0 for m in usable for t in (m.home, m.away)}
        for m in usable:
            self._games[m.home] += 1
            self._games[m.away] += 1

        objective, grad_a, grad_d, grad_rho = self._objective_and_grad(usable)
        if not math.isfinite(objective):
            self.report = FitReport(0, 0, 0.0, 0, False, 0.0, self.rho, 1.0)
            return self.report

        # Backtracking line search on the ascent direction.
        #
        # A fixed learning rate is not viable here: the per-team gradients scale
        # with each team's match count, so one step size cannot serve both a
        # 20-game side and a 2-game newcomer, and an aggressive one sends a
        # rating to exp(+800) within two iterations. Every step is therefore
        # accepted only if it *improves* the penalised likelihood, and rejected
        # steps restore the previous parameters and halve the step. That makes
        # the fit monotone by construction, so it can never diverge -- the worst
        # case is a slow fit, which is a cost problem, not a correctness one.
        step = self.learning_rate
        iterations = 0
        converged = False
        best_objective = objective
        stall = 0

        for iterations in range(1, self.max_iterations + 1):
            check_budget()
            snapshot = (dict(self._attack), dict(self._defence), self.rho)
            gain = 0.0
            accepted = False

            for _ in range(_MAX_LINE_SEARCH):
                self._apply_gradient(snapshot, grad_a, grad_d, grad_rho, step)
                candidate, _, _, _ = self._objective_and_grad(usable)
                if math.isfinite(candidate) and candidate > objective:
                    gain = candidate - objective
                    accepted = True
                    break
                # Roll back and shorten the step.
                self._attack, self._defence, self.rho = (
                    dict(snapshot[0]), dict(snapshot[1]), snapshot[2])
                step *= 0.5
                if step < 1e-9:
                    break

            if not accepted:
                # Full backtracking found no improvement. That is the definition
                # of a stationary point for this objective, not a failure, so it
                # is recorded as convergence rather than as a truncated fit.
                self._attack, self._defence, self.rho = (
                    dict(snapshot[0]), dict(snapshot[1]), snapshot[2])
                converged = True
                break

            self._center()
            objective, grad_a, grad_d, grad_rho = self._objective_and_grad(usable)
            # Grow the step back for the next iteration; the search will shorten
            # it again if the landscape turns.
            step = min(step * 1.6, _MAX_STEP)

            # Converged when the objective stops improving relative to the best
            # value seen, and it must stall twice: near a flat optimum the
            # step can alternate around the solution and a single small gain
            # does not mean the search is finished.
            if objective > best_objective:
                best_objective = objective
            if best_objective - objective < TOLERANCE * (1.0 + abs(best_objective)):
                stall += 1
                if stall >= 2:
                    converged = True
                    break
            else:
                stall = 0

        self._center()
        objective = max(objective, best_objective)
        n_teams = max(1, len(self._attack))
        mean_games = sum(self._games.values()) / n_teams
        # The share of the prior still present in an average rating. This is the
        # number the board must surface: at 0.8 the model is mostly guessing,
        # however confident the fitted numbers look.
        prior_dom = (self.shrinkage / (self.shrinkage + mean_games)
                     if mean_games and self.shrinkage > 0 else 1.0)
        self.report = FitReport(
            matches_used=len(usable), teams=len(self._attack),
            objective=float(objective), iterations=iterations,
            converged=converged, mean_games_behind=mean_games,
            rho=self.rho, prior_dominance=prior_dom,
        )
        if not self.report.sufficient:
            logger_warning(
                "Dixon-Coles fit on %d matches gives only %.1f games behind an "
                "average rating; below the %.0f needed to claim edge. Board "
                "must label these outputs unproven.",
                self.report.matches_used, mean_games, SUFFICIENT_GAMES)
        return self.report

    # -- likelihood ---------------------------------------------------------

    def _lambdas(self, home: str, away: str) -> tuple[float, float]:
        """Expected goals for a fixture, with empirical-Bayes shrinkage applied.

        The fit is unshrunk (the ridge in ``_objective_and_grad`` only keeps the
        optimiser well-conditioned), and shrinkage happens here, where the games
        count is available. Shrinking once, at the point of use, is what keeps a
        one-match team from producing a 94% win probability; shrinking twice --
        here *and* by a heavy ridge -- would flatten the well-sampled clubs that
        have genuinely earned their rating.
        """
        home, away = self._team_key(home), self._team_key(away)
        lam = (self.base_mu * math.exp(self.home_adv)
               * math.exp(self._shrunk_attack(home) - self._shrunk_defence(away)))
        mu = (self.base_mu
              * math.exp(self._shrunk_attack(away) - self._shrunk_defence(home)))
        return _safe_lambda(lam), _safe_lambda(mu)

    def _shrink_factor(self, team: str) -> float:
        """``games / (games + shrinkage)`` -- the weight of evidence for a team."""
        if self.shrinkage <= 0.0:
            return 1.0
        return self._games.get(team, 0) / (self._games.get(team, 0) + self.shrinkage)

    def _shrunk_attack(self, team: str) -> float:
        return self._attack.get(team, 0.0) * self._shrink_factor(team)

    def _shrunk_defence(self, team: str) -> float:
        return self._defence.get(team, 0.0) * self._shrink_factor(team)

    def _tau(self, x: int, y: int, lam: float, mu: float) -> float:
        r = self.rho
        if x == 0 and y == 0:
            value = 1.0 - lam * mu * r
        elif x == 0 and y == 1:
            value = 1.0 + lam * r
        elif x == 1 and y == 0:
            value = 1.0 + mu * r
        elif x == 1 and y == 1:
            value = 1.0 - r
        else:
            return 1.0
        return value if value > _MIN_PROB else _MIN_PROB

    def _objective_and_grad(self, rows: Sequence[ScoredMatch]):
        """Penalised log-likelihood and its analytic gradient.

        The Poisson part is linear in the parameters once ``log lambda`` is the
        coordinate system, so its gradient is ``(goals - lambda)`` -- no finite
        differencing anywhere, which keeps a full-season fit in the low
        milliseconds and the result exact.
        """
        grad_a: dict[str, float] = {t: 0.0 for t in self._attack}
        grad_d: dict[str, float] = {t: 0.0 for t in self._attack}
        grad_rho = 0.0
        total = 0.0

        for m in rows:
            ah = self._attack.get(m.home, 0.0)
            aa = self._attack.get(m.away, 0.0)
            dh = self._defence.get(m.home, 0.0)
            da = self._defence.get(m.away, 0.0)

            log_lam = math.log(self.base_mu) + self.home_adv + ah - da
            log_mu = math.log(self.base_mu) + aa - dh
            # Clamp before exponentiating. A rejected trial step can leave a
            # rating large enough that exp() overflows, and an OverflowError
            # here would abort the whole fit rather than merely scoring that
            # trial point as bad.
            lam = math.exp(min(log_lam, _LOG_LAMBDA_MAX))
            mu = math.exp(min(log_mu, _LOG_LAMBDA_MAX))

            w = m.weight
            x, y = float(m.home_score), float(m.away_score)
            total += w * (-lam - mu + x * log_lam + y * log_mu)

            # d(penalised Poisson term)/d(rating) via the chain rule through
            # log lambda. Note the two goals enter their own lambdas only, and
            # each rating enters with the sign of the term it is subtracted in.
            grad_a[m.home] += w * (x - lam)
            grad_d[m.away] -= w * (x - lam)
            grad_a[m.away] += w * (y - mu)
            grad_d[m.home] -= w * (y - mu)

            # The Dixon-Coles correction is a genuine part of the likelihood,
            # so it contributes to the rating gradient too:
            #   d(log tau)/d(log lam) = (dtau/dlam) * lam / tau
            tau, dtau_drho, dtau_dlam, dtau_dmu = _tau_gradients(x, y, lam, mu, self.rho)
            total += w * math.log(tau)
            grad_rho += w * dtau_drho / tau
            inv_tau = 1.0 / tau
            g_lam = w * dtau_dlam * lam * inv_tau
            g_mu = w * dtau_dmu * mu * inv_tau
            grad_a[m.home] += g_lam
            grad_d[m.away] -= g_lam
            grad_a[m.away] += g_mu
            grad_d[m.home] -= g_mu

        # Weak ridge, purely to keep the optimisation well-conditioned. The
        # real shrinkage toward the league mean is games-dependent and is
        # applied in _lambdas, where the games count is available.
        penalty = 0.0
        for team in self._attack:
            for theta in (self._attack[team], self._defence[team]):
                penalty += self.ridge * 0.5 * theta * theta
        total -= penalty
        for team in self._attack:
            grad_a[team] -= self.ridge * self._attack[team]
            grad_d[team] -= self.ridge * self._defence[team]

        return total, grad_a, grad_d, grad_rho

    def _apply_gradient(self, snapshot: tuple[dict[str, float], dict[str, float], float],
                        grad_a: Mapping[str, float], grad_d: Mapping[str, float],
                        grad_rho: float, step: float) -> None:
        """One trial step, starting from ``snapshot`` rather than in place.

        Rebuilding from the snapshot each attempt is what makes the line search
        a true search: halving the step must be measured from the same point, not
        from wherever the previous, too-long step happened to land.
        """
        attack, defence, _ = snapshot
        self._attack = {t: attack[t] + step * grad_a.get(t, 0.0) for t in attack}
        self._defence = {t: defence[t] + step * grad_d.get(t, 0.0) for t in defence}
        self.rho = min(RHO_BOUNDS[1], max(RHO_BOUNDS[0], snapshot[2] + step * grad_rho))
        # Centred here rather than after acceptance, so every point the line
        # search scores is the same point that would be kept. Without this the
        # search measures a pre-centring objective and then reports a different
        # one, and the two disagree exactly when the rating mean drifts.
        self._center()

    def _center(self) -> None:
        """Identifiability: attacks and defences are only defined up to a shift.

        Without centring the fit wanders along the degenerate direction and the
        lambdas stay correct but the reported ratings become meaningless.
        """
        if not self._attack:
            return
        n = len(self._attack)
        mean_a = sum(self._attack.values()) / n
        mean_d = sum(self._defence.values()) / n
        for team in self._attack:
            self._attack[team] -= mean_a
            self._defence[team] -= mean_d

    # -- prediction ---------------------------------------------------------

    def score_matrix(self, home: str, away: str) -> list[list[float]]:
        """Joint goal distribution ``[home_goals][away_goals]``, normalised."""
        lam, mu = self._lambdas(home, away)
        vectors = []
        for rate in (lam, mu):
            values = [math.exp(-rate)]
            cumulative = values[0]
            while len(values) < GOAL_TAIL or cumulative < 1 - 1e-10:
                values.append(values[-1] * rate / len(values))
                cumulative += values[-1]
                if len(values) > 100:
                    raise ValueError('goal distribution tail exceeds numerical budget')
            vectors.append(values)
        size = max(map(len, vectors))
        # Complete the shorter vector using the same recurrence, avoiding
        # repeated factorials and tail sums in every board/market forecast.
        for rate, values in zip((lam, mu), vectors):
            while len(values) < size:
                values.append(values[-1] * rate / len(values))
        ph, pa = vectors
        grid = [[ph[i] * pa[j] * self._tau(i, j, lam, mu)
                 for j in range(size)] for i in range(size)]
        total = sum(sum(row) for row in grid)
        if total <= 0.0 or not math.isfinite(total):
            raise ValueError('invalid goal distribution; refusing a fabricated uniform forecast')
        return [[v / total for v in row] for row in grid]

    def predict(self, home: str, away: str) -> dict[str, Any]:
        """Every market the board needs, from one score matrix.

        Deriving 1X2, totals, BTTS and scorelines from a single distribution
        is what makes them mutually consistent: it is impossible for the board
        to publish an over/under price that contradicts its own 1X2 view.
        """
        grid = self.score_matrix(home, away)
        lam, mu = self._lambdas(home, away)

        p_home = p_draw = p_btts = 0.0
        totals: dict[str, float] = {}
        home_totals: dict[str, float] = {}
        away_totals: dict[str, float] = {}
        for x in range(len(grid)):
            for y in range(len(grid[x])):
                w = grid[x][y]
                if x > y:
                    p_home += w
                elif x == y:
                    p_draw += w
                if x >= 1 and y >= 1:
                    p_btts += w
                home_totals[str(x)] = home_totals.get(str(x), 0.0) + w
                away_totals[str(y)] = away_totals.get(str(y), 0.0) + w
                key = str(x + y)
                totals[key] = totals.get(key, 0.0) + w
        p_away = max(0.0, 1.0 - p_home - p_draw)

        sum_1x2 = p_home + p_draw + p_away
        if sum_1x2 > 0:
            p_home, p_draw, p_away = p_home / sum_1x2, p_draw / sum_1x2, p_away / sum_1x2

        over = _over_probabilities(totals)

        scores = sorted(
            ((p, x, y) for x, row in enumerate(grid) for y, p in enumerate(row)),
            reverse=True)

        return {
            "p_home": p_home,
            "p_draw": p_draw,
            "p_away": p_away,
            "p_btts": p_btts,
            "double_chance": {"1X": p_home + p_draw, "X2": p_draw + p_away, "12": p_home + p_away},
            "home_team_over": _over_probabilities(home_totals),
            "away_team_over": _over_probabilities(away_totals),
            "expected_goals": {"home": lam, "away": mu},
            "over": over,
            "most_likely_scores": [
                {"home_goals": x, "away_goals": y, "p": w} for w, x, y in scores[:10]
            ],
            "top_outcome": _argmax_name(home, away, p_home, p_draw, p_away),
        }

    def fair_odds(self, home: str, away: str) -> dict[str, float]:
        p = self.predict(home, away)
        return {
            "home": _fair(p["p_home"]), "draw": _fair(p["p_draw"]),
            "away": _fair(p["p_away"]),
        }

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_mu": self.base_mu, "home_adv": self.home_adv, "rho": self.rho,
            "shrinkage": self.shrinkage, "xi": self.xi,
            'normalise_identities': self.normalise_identities,
            "attack": dict(self._attack), "defence": dict(self._defence),
            "games": dict(self._games),
            "report": self.report.to_dict() if self.report else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DixonColesModel":
        model = cls(
            base_mu=float(data.get("base_mu", BASE_MU)),
            home_adv=float(data.get("home_adv", HOME_ADV)),
            rho=float(data.get("rho", RHO_INIT)),
            shrinkage=float(data.get("shrinkage", SHRINKAGE)),
            xi=float(data.get("xi", XI)),
            normalise_identities=data.get('normalise_identities') is True,
        )
        model._attack = {k: float(v) for k, v in (data.get("attack") or {}).items()}
        model._defence = {k: float(v) for k, v in (data.get("defence") or {}).items()}
        model._games = {k: int(v) for k, v in (data.get("games") or {}).items()}
        raw = data.get("report")
        if raw:
            model.report = FitReport(
                matches_used=int(raw.get("matches_used", 0)),
                teams=int(raw.get("teams", 0)),
                objective=float(raw.get("objective", 0.0)),
                iterations=int(raw.get("iterations", 0)),
                converged=bool(raw.get("converged", False)),
                mean_games_behind=float(raw.get("mean_games_behind", 0.0)),
                rho=float(raw.get("rho", model.rho)),
                prior_dominance=float(raw.get("prior_dominance", 1.0)),
            )
        return model


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _safe_lambda(value: float) -> float:
    if not math.isfinite(value):
        return BASE_MU
    return min(max(value, 0.05), 8.0)


def _poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0.0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam + k * math.log(lam) - math.lgamma(k + 1))


def _tau_gradients(x: float, y: float, lam: float, mu: float,
                   rho: float) -> tuple[float, float, float, float]:
    """(tau, dtau/drho, dtau/dlam, dtau/dmu)."""
    if x == 0 and y == 0:
        tau = 1.0 - lam * mu * rho
        return (max(tau, _MIN_PROB), -lam * mu, -mu * rho, -lam * rho)
    if x == 0 and y == 1:
        tau = 1.0 + lam * rho
        return (max(tau, _MIN_PROB), lam, rho, 0.0)
    if x == 1 and y == 0:
        tau = 1.0 + mu * rho
        return (max(tau, _MIN_PROB), mu, 0.0, rho)
    if x == 1 and y == 1:
        tau = 1.0 - rho
        return (max(tau, _MIN_PROB), -1.0, 0.0, 0.0)
    return (1.0, 0.0, 0.0, 0.0)


def _over_probabilities(totals: Mapping[str, float]) -> dict[str, float]:
    """P(total goals > line) for every half line the grid can express.

    Goals are integers, so the only lines that resolve without a push are the
    half lines ``0.5, 1.5, 2.5, ...`` -- and for those the payoff is exact:
    ``P(> k+0.5) == P(total >= k+1)``. Whole lines (``2.0``) need a push/stake
    rule that a single probability cannot express, so they are deliberately not
    published rather than published as a misleading number.

    Computed as one downward suffix pass, so the whole ladder is O(goals) rather
    than O(lines x goals) -- this runs on every micro-bet for every fixture.
    """
    if not totals:
        return {}
    counts = [0.0] * (len(totals) + 1)
    maximum = 0
    for key, value in totals.items():
        try:
            k = int(key)
        except (TypeError, ValueError):
            continue
        if k < 0 or k > len(totals):
            continue
        counts[k] = value
        maximum = max(maximum, k)

    ladder: dict[str, float] = {}
    running = 0.0
    for k in range(maximum, -1, -1):
        running += counts[k]
        if k >= 1:
            # running now holds P(total >= k), i.e. P(> (k-1) + 0.5)
            ladder[k - 1] = max(0.0, min(1.0, running))

    # Only lines a book actually offers, ascending. The grid's upper region is a
    # truncated tail carrying float noise, and publishing "over 21.5" at p=0.0
    # would be a fabricated market rather than a thin one.
    return {f"{line}.5": ladder[line]
            for line in range(0, min(maximum, MAX_PUBLISHED_LINE + 1))}


def _fair(p: float) -> float:
    p = min(max(p, _MIN_PROB), 1.0 - _MIN_PROB)
    return round(1.0 / p, 4)


def _argmax_name(home: str, away: str, ph: float, pd: float,
                 pa: float) -> str:
    if ph >= pd and ph >= pa:
        return home
    if pa >= ph and pa >= pd:
        return away
    return "Draw"


def logger_warning(msg: str, *args: Any) -> None:
    import logging
    logging.getLogger("lisa.dixon_coles").warning(msg, *args)


__all__ = [
    "DixonColesModel",
    "ScoredMatch",
    "FitReport",
    "TeamStrength",
    "SUFFICIENT_GAMES",
    "GOAL_TAIL",
]
