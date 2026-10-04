"""The opportunity board: model-first picks, micro-bets and accumulators.

What changed and why
--------------------
The engine used to *refine* a market consensus and surface execution
discrepancies. On free data that is impossible -- there is no consensus to
refine -- and even with paid data it caps out at roughly 1%, because a
de-vigged price is by construction a good estimate of its own probability.

This board inverts the relationship. ``DixonColesModel`` produces a probability
from real results alone; the board then asks a single question of every market
price it can see: *is this price better than what I independently believe?*
That comparison is where edge lives, and it is only meaningful because the two
sides never touched each other.

Two ranked ladders, because they answer different questions
-----------------------------------------------------------
``winning`` ladder
    Nearest kickoff first, then model probability. Each fixture contributes
    its strongest eligible selection.

``earning`` ladder
    Positive expected value is required. Nearest kickoff first, then winning
    probability and expected value. Empty when no usable market price exists.

Conflating the two is the most common way a prediction product misleads: a
60% accumulator and a 45% sharp-money bet are both "picks", and only one of
them is an opportunity.

The honesty rules this module enforces
--------------------------------------
Every one of these is a bug class that has to be prevented, not documented:

* **Unpriced means unpriced.** With no market quote there is no edge claim, so
  the row carries ``priced=False`` and ``ev=None``. A fair price from the model
  is *not* an offer, and is never presented as one.
* **Thin data is labelled, not hidden.** If the fit is short of
  ``SUFFICIENT_GAMES`` the whole board is stamped ``unproven`` and the ladders
  are advisory only. A model with two matches per team is a prior with a
  scorer attached.
* **Accumulator legs are correlated.** Multiplying leg probabilities assumes
  independence. Same-league or same-day legs are *not* independent -- two teams
  from one matchday cannot all win, and a parlay of them is structurally
  worse than its arithmetic joint probability suggests. Every accumulator is
  scored with an explicit correlation penalty and carries the leg mix that
  triggered it.
* **Fractional Kelly, always.** Full Kelly on a modelled edge with estimation
  error is how a correct model still loses money. Stake is quarter-Kelly and
  hard-capped, and is zero when the edge is not real.

No third-party dependencies -- standard library only.
"""
from __future__ import annotations

import itertools
import logging
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from .dixon_coles import DixonColesModel, SUFFICIENT_GAMES

logger = logging.getLogger("lisa.board")

# ---------------------------------------------------------------------------
# Thresholds -- all conservative, and all named so they can be argued with
# ---------------------------------------------------------------------------

MIN_EV: float = 0.03
"""Below 3% expected value there is no edge worth surfacing.

The real reason is not caution but arithmetic: at 3% EV the model has to be
right about a probability that it estimated, and any ordinary estimation error
swallows the edge. Publishing a 1% edge would mean publishing noise."""

MIN_MODEL_PROB: float = 0.12
"""Refuse to recommend a side the model itself does not believe in.

Below ~12% no staking system can be shown to work and the numbers are inside
the fit's error bars, so a "pick" there is a coin flip dressed as analysis."""

MIN_FAIR_ODDS: float = 1.05
"""Smallest fair price worth publishing as an opportunity.

The economic criterion, not a taste one: a selection priced at 1.00 is a bet
that cannot clear any bookmaker's margin, and one at 1.02 needs a model accurate
to within a percentage point to profit. Filtering on *fair* price rather than on
a probability threshold is what removes the degenerate rows -- "Under 6.5" at
p=0.999 is mathematically real and practically unplayable, and it would
otherwise fill the entire winning ladder with certainty that pays nothing.
"""

MAX_TOTAL_LINE: float = 5.5
"""Highest totals line offered as a recommendation. Above this the model is
extrapolating into a truncated grid and no top-flight book quotes it."""

#: Preference order when two selections are equally likely. The moneyline is the
#: main bet, so it leads the winning ladder and the micro markets follow.
MARKET_PRIORITY: dict[str, int] = {
    "h2h": 0, "totals": 1, "btts": 2, "correct_score": 3,
    "double_chance": 4, "home_team_totals": 5, "away_team_totals": 6,
}

MAX_WINNING_LADDER: int = 25
MAX_EARNING_LADDER: int = 25
MAX_MICRO_BETS: int = 40
MAX_ACCUMULATORS: int = 12

KELLY_FRACTION: float = 0.25
"""Quarter Kelly. Full Kelly is optimal only for a probability that is exactly
right; on an estimate, it is the most aggressive stake that maximises ruin."""

MAX_STAKE_FRACTION: float = 0.02
"""Hard cap of bankroll per single bet (2%).

Quarter Kelly on a 10% edge asks for ~1% of bankroll, so this binds rarely --
it exists to catch a pathological EV estimate rather than to tune the normal
case."""

MIN_ACCUMULATOR_PROB: float = 0.02
"""A parlay under 2% is not a bet, it is a donation. Dropped rather than
shown, because a long shot with a computable price is still a long shot."""

ACCUMULATOR_SIZES: tuple[int, ...] = (2, 3, 4, 5)
CORRELATION_PENALTY_SAME_LEAGUE: float = 0.90
CORRELATION_PENALTY_SAME_DAY: float = 0.94
"""Multiplicative haircuts on the naive joint probability. Deliberately mild
and visible rather than a black box -- the penalty is reported alongside the
number so a user can disagree with it."""


#: Quote lines are compared at this many decimals. Two sources that print the
#: same line differently are still the same line, and rounding to a fixed
#: precision makes that a property of the comparison rather than of whichever
#: adapter happened to run.
LINE_PRECISION: int = 4


def _line_key(line: Optional[float]) -> Optional[float]:
    """Comparable form of a handicap/total line."""
    return None if line is None else round(float(line), LINE_PRECISION)


def _side_key(selection: str) -> str:
    """The side of a market, with any trailing number removed.

    Adapters name a totals selection ``"Over 2.5"`` because that is how the
    board's own lookup key is spelled elsewhere in this file; the *side* is
    ``"Over"``. Splitting the number off here means the match does not depend on
    both ends formatting the float the same way -- ``2.5`` against ``2.50`` used
    to be two different selections, so a genuine price was looked up under a key
    nobody had written.
    """
    parts = str(selection).strip().split()
    if len(parts) > 1:
        try:
            float(parts[-1])
        except ValueError:
            return " ".join(parts)
        return " ".join(parts[:-1])
    return parts[0] if parts else ""


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketPrice:
    """One bookmaker's price for one selection."""

    match_id: str
    selection: str
    odds: float
    book_key: str = "unknown"
    book_title: str = ""
    source: str = "unknown"
    market: str = "h2h"
    line: Optional[float] = None
    updated_at: Optional[datetime] = None

    @property
    def stake_name(self) -> str:
        return f"{self.book_key}:{self.market}:{self.line}" if self.line is not None \
            else f"{self.book_key}:{self.market}"


@dataclass(frozen=True)
class Fixture:
    """An upcoming match in the product window."""

    match_id: str
    sport_key: str
    kickoff: datetime
    home: str
    away: str
    source: str = "unknown"


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Opportunity:
    """One priced-or-unpriced selection on one fixture."""

    match_id: str
    sport_key: str
    kickoff: datetime
    home: str
    away: str
    market: str                    # h2h | totals | btts | correct_score
    selection: str                 # outcome name, e.g. "Home" / "Over 2.5"
    p_model: float
    fair_odds: float
    line: Optional[float] = None
    best_odds: Optional[float] = None
    best_book: Optional[str] = None
    best_source: Optional[str] = None
    ev: Optional[float] = None     # None => unpriced, no edge claim
    stake_fraction: float = 0.0
    priced: bool = False
    #: "model_vs_market" when a real quote was compared, "model_only" otherwise.
    basis: str = "model_only"
    reason: str = ""
    payout_probabilities: Optional[dict[str, float]] = None

    @property
    def outcome_key(self) -> str:
        if self.market == "h2h":
            return self.selection
        if self.market == "totals" and self.line is not None:
            return f"{self.selection} {self.line}"
        return self.selection

    def to_dict(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id, "sport_key": self.sport_key,
            "kickoff": self.kickoff.isoformat(), "home": self.home, "away": self.away,
            "market": self.market, "selection": self.selection,
            "line": self.line, "p_model": round(self.p_model, 4),
            "fair_odds": self.fair_odds, "best_odds": self.best_odds,
            "best_book": self.best_book, "best_source": self.best_source,
            "ev": round(self.ev, 4) if self.ev is not None else None,
            "stake_fraction": round(self.stake_fraction, 5),
            "priced": self.priced, "basis": self.basis, "reason": self.reason,
            "payout_probabilities": self.payout_probabilities,
        }


@dataclass(frozen=True)
class Accumulator:
    """A multi-leg parlay with an honest joint probability."""

    legs: tuple[Opportunity, ...]
    p_naive: float
    p_adjusted: float
    fair_odds: float
    best_odds: Optional[float] = None
    best_book: Optional[str] = None
    ev: Optional[float] = None
    stake_fraction: float = 0.0
    priced: bool = False
    correlation_penalty: float = 1.0
    warnings: tuple[str, ...] = ()

    @property
    def size(self) -> int:
        return len(self.legs)

    def describe(self) -> str:
        """Human-readable legs, named by team and kickoff.

        Team names and times rather than bare selections: "A1 v D2" from two
        different fixtures is two distinct bets and must not render as the same
        string, or a user cannot tell which accumulator they are looking at.
        """
        return " + ".join(
            f"{leg.home} v {leg.away} ({leg.kickoff:%d %b %H:%M}) {leg.selection}"
            for leg in self.legs
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "size": self.size, "legs": [leg.to_dict() for leg in self.legs],
            "description": self.describe(),
            "p_naive": round(self.p_naive, 5), "p_adjusted": round(self.p_adjusted, 5),
            "fair_odds": self.fair_odds, "best_odds": self.best_odds,
            "best_book": self.best_book,
            "ev": round(self.ev, 4) if self.ev is not None else None,
            "stake_fraction": round(self.stake_fraction, 5),
            "priced": self.priced,
            "correlation_penalty": round(self.correlation_penalty, 4),
            "warnings": list(self.warnings),
        }


@dataclass
class BoardCoverage:
    """Answers "did we actually deliver the promised volume?" -- not a metric."""

    window_hours: float
    fixtures_seen: int
    fixtures_modelled: int
    fixtures_priced: int
    meets_volume_target: bool
    volume_target: int
    leagues: dict[str, int] = field(default_factory=dict)
    sources: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_hours": self.window_hours,
            "fixtures_seen": self.fixtures_seen,
            "fixtures_modelled": self.fixtures_modelled,
            "fixtures_priced": self.fixtures_priced,
            "meets_volume_target": self.meets_volume_target,
            "volume_target": self.volume_target,
            "leagues": dict(self.leagues),
            "sources": dict(self.sources),
            "notes": list(self.notes),
        }


@dataclass
class Board:
    """Everything the board decided this cycle."""

    generated_at: datetime
    window_hours: float
    unproven: bool
    model_report: dict[str, Any]
    winning: tuple[Opportunity, ...] = ()
    earning: tuple[Opportunity, ...] = ()
    micro_bets: tuple[Opportunity, ...] = ()
    accumulators: tuple[Accumulator, ...] = ()
    coverage: Optional[BoardCoverage] = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "window_hours": self.window_hours,
            "unproven": self.unproven,
            "model": self.model_report,
            "winning": [o.to_dict() for o in self.winning],
            "earning": [o.to_dict() for o in self.earning],
            "micro_bets": [o.to_dict() for o in self.micro_bets],
            "accumulators": [a.to_dict() for a in self.accumulators],
            "coverage": self.coverage.to_dict() if self.coverage else None,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------


class OpportunityBoard:
    """Builds the two ladders, the micro-bet list and the accumulators."""

    def __init__(self, model: DixonColesModel, *, min_ev: float = MIN_EV,
                 min_model_prob: float = MIN_MODEL_PROB,
                 min_fair_odds: float = MIN_FAIR_ODDS,
                 min_accumulator_prob: float = MIN_ACCUMULATOR_PROB,
                 kelly_fraction: float = KELLY_FRACTION,
                 max_stake: float = MAX_STAKE_FRACTION,
                 max_total_line: float = MAX_TOTAL_LINE,
                 accumulator_sizes: Sequence[int] = ACCUMULATOR_SIZES,
                 volume_target: int = 12, evidence_gate=None, configuration_hash=None,
                 max_quote_age_sec: float = 1800.) -> None:
        self.model = model
        self.min_ev = min_ev
        self.min_model_prob = min_model_prob
        self.min_fair_odds = min_fair_odds
        self.min_accumulator_prob = min_accumulator_prob
        self.kelly_fraction = kelly_fraction
        self.max_stake = max_stake
        self.max_total_line = max_total_line
        self.accumulator_sizes = tuple(sorted(accumulator_sizes))
        self.volume_target = volume_target
        self.evidence_gate = evidence_gate
        self.configuration_hash = configuration_hash
        self.max_quote_age_sec = max_quote_age_sec
        self._as_of = None

    # -- main entry point ---------------------------------------------------

    def build(self, fixtures: Sequence[Fixture],
              prices: Mapping[str, Sequence[MarketPrice]] | None = None,
              *, now: Optional[datetime] = None,
              window_hours: float = 48.0) -> Board:
        now = now or datetime.now(timezone.utc)
        self._as_of = now
        prices = prices or {}
        report = self.model.report
        unproven = report is None or not report.sufficient
        notes: list[str] = []
        if self.evidence_gate is None or self.evidence_gate.error:
            notes.append('Research forecasts: executable stakes require reviewed out-of-sample validation.')

        if report is None:
            return Board(now, window_hours, True, {}, notes=["model not fitted"],
                         coverage=BoardCoverage(window_hours, len(fixtures), 0, 0,
                                                False, self.volume_target))
        if unproven:
            notes.append(
                f"UNPROVEN: only {report.mean_games_behind:.1f} results behind an "
                f"average rating (need {SUFFICIENT_GAMES:.0f}); "
                f"{report.prior_dominance:.0%} of each rating is still the prior. "
                "Ladders are advisory, not recommendations.")

        in_window = [f for f in fixtures
                     if now <= f.kickoff <= now + _hours(window_hours)]
        modelled = [f for f in in_window
                    if self._fixture_model(f).knows(f.home) and self._fixture_model(f).knows(f.away)]

        priced_count = sum(1 for f in modelled if prices.get(f.match_id))

        candidates: list[Opportunity] = []
        from .job_budget import check_budget
        for fixture in modelled:
            check_budget()
            candidates.extend(self._fixture_opportunities(fixture,
                                                          prices.get(fixture.match_id, ())))
        # All views share the same constrained stakes. Several selections on
        # one fixture cannot each independently risk the full match budget.
        allocated = {}
        total = 0.
        ordering = sorted(enumerate(candidates), key=lambda item: (
            -(item[1].ev or 0), item[1].kickoff, item[1].match_id, item[1].market, item[1].selection))
        for index, opportunity in ordering:
            amount = min(opportunity.stake_fraction, max(0., self.max_stake - allocated.get(opportunity.match_id, 0.)),
                         max(0., .05 - total))
            allocated[opportunity.match_id] = allocated.get(opportunity.match_id, 0.) + amount
            total += amount
            candidates[index] = replace(opportunity, stake_fraction=amount)
        priced_count = len({o.match_id for o in candidates if o.priced})

        winning = self._winning_ladder(candidates)
        earning = self._earning_ladder(candidates)
        micro = self._micro_bets(candidates, excluded={(o.match_id, o.market, o.line, o.selection) for o in earning})
        accumulators = self._accumulators(modelled, prices, candidates=candidates)

        coverage = self._coverage(in_window, modelled, priced_count, window_hours, notes)
        if not coverage.meets_volume_target and not unproven:
            notes.append(
                f"Volume short: {len(modelled)} modelled fixtures against a target "
                f"of {self.volume_target} in a {window_hours:.0f}h window. This is a "
                "data-coverage limit, not a selection limit -- widen the window or "
                "add a source.")

        return Board(generated_at=now, window_hours=window_hours, unproven=unproven,
                     model_report=report.to_dict(), winning=winning, earning=earning,
                     micro_bets=micro, accumulators=accumulators,
                     coverage=coverage, notes=notes)

    # -- per-fixture markets ------------------------------------------------

    def _fixture_opportunities(self, fixture: Fixture,
                               quotes: Sequence[MarketPrice]) -> list[Opportunity]:
        """Every market the model can price for one fixture, consistently.

        All of these come from a single score matrix, so they cannot contradict
        each other: it is impossible to publish an over/under 2.5 price that
        disagrees with the same fixture's 1X2 view.
        """
        pred = self._fixture_model(fixture).predict(fixture.home, fixture.away)
        out: list[Opportunity] = []

        # One index over every market the books quoted, rather than a per-market
        # filter reused across lookups. Filtering to one market and then asking
        # that same dict about another market is silently always empty, which
        # left every totals, BTTS and correct-score row unpriced no matter what
        # the books were actually offering.
        now = self._as_of or datetime.now(timezone.utc)
        usable = [q for q in quotes if q.updated_at is not None and q.updated_at.tzinfo is not None
                  and -60 <= (now - q.updated_at).total_seconds() <= self.max_quote_age_sec]
        best = self._quote_index(usable)

        def quote_for(market: str, side: str,
                      line: Optional[float]) -> Optional[MarketPrice]:
            return best.get((market, _line_key(line), side))

        for selection, p in (("Home", pred["p_home"]),
                             ("Draw", pred["p_draw"]),
                             ("Away", pred["p_away"])):
            out.append(self._opportunity(
                fixture, "h2h", selection, None, p,
                quote_for("h2h", selection, None)))

        for selection, probability in pred.get("double_chance", {}).items():
            out.append(self._opportunity(fixture, "double_chance", selection, None,
                probability, quote_for("double_chance", selection, None)))
        for market, distribution in (("home_team_totals", pred.get("home_team_over", {})),
                                     ("away_team_totals", pred.get("away_team_over", {}))):
            for label, probability in distribution.items():
                line = float(label)
                for selection, p in (("Over", probability), ("Under", 1 - probability)):
                    out.append(self._opportunity(fixture, market, selection, line, p,
                        quote_for(market, selection, line)))

        for line, p_over in sorted(pred["over"].items(), key=lambda kv: float(kv[0])):
            # The 0.5 line is near-certain in one direction and the "Under 0.5"
            # side is a long shot; both are still priced, so nothing is dropped
            # for looking uninteresting.
            out.append(self._opportunity(
                fixture, "totals", "Over", float(line), p_over,
                quote_for("totals", "Over", float(line))))
            out.append(self._opportunity(
                fixture, "totals", "Under", float(line), 1.0 - p_over,
                quote_for("totals", "Under", float(line))))

        out.append(self._opportunity(fixture, "btts", "Yes", None, pred["p_btts"],
                                     quote_for("btts", "Yes", None)))
        out.append(self._opportunity(fixture, "btts", "No", None, 1.0 - pred["p_btts"],
                                     quote_for("btts", "No", None)))

        for score in pred["most_likely_scores"][:5]:
            label = f"{score['home_goals']}-{score['away_goals']}"
            out.append(self._opportunity(
                fixture, "correct_score", label, None, score["p"],
                quote_for("correct_score", label, None)))

        # Refund/split contracts need their full payout distribution. Evaluate
        # offered lines only; do not manufacture hundreds of nonexistent prices.
        from .contracts import MarketContract
        local_model = self._fixture_model(fixture)
        if hasattr(local_model, 'score_matrix'):
            matrix = local_model.score_matrix(fixture.home, fixture.away)
            contracts = {("draw_no_bet", side, None) for side in ("Home", "Away")}
            for (market, line, side), quote in best.items():
                if market == 'asian_handicap' or (market in ('totals', 'home_team_totals', 'away_team_totals')
                                                  and line is not None and line % 1 != .5):
                    contracts.add((market, side, line))
            for market, side, line in sorted(contracts, key=lambda c: (c[0], c[1], c[2] or 0)):
                try:
                    payout = MarketContract(market, side, line).distribution(matrix)
                except ValueError:
                    continue
                if payout.fair_odds is not None:
                    out.append(self._opportunity(fixture, market, side, line,
                        payout.positive_probability, quote_for(market, side, line), payout=payout))

        corner_model = getattr(self.model, 'corners', {}).get(fixture.sport_key)
        corners = corner_model.predict(fixture.home, fixture.away) if corner_model else None
        if corners:
            for label, p_over in corners['over'].items():
                line = float(label)
                for side, probability in (('Over', p_over), ('Under', 1 - p_over)):
                    out.append(self._opportunity(fixture, 'corners', side, line, probability,
                        quote_for('corners', side, line)))

        return out

    @staticmethod
    def _quote_index(quotes: Sequence[MarketPrice]
                     ) -> dict[tuple[str, Optional[float], str], MarketPrice]:
        """Best price per ``(market, line, side)``.

        The *highest* price is the one the bettor wants, so this is a max, not
        an average. Averaging book prices would silently manufacture a price
        nobody is offering to take.

        The line is compared as a rounded value rather than through a formatted
        string. Both ends of this lookup build their own label -- the model
        formats a line it computed, an adapter formats one it read -- and a
        ``2.25`` that one side prints as ``2.25`` and the other as ``2.250`` is
        the same price, not two.
        """
        best: dict[tuple[str, Optional[float], str], MarketPrice] = {}
        for q in quotes:
            if not math.isfinite(q.odds) or q.odds <= 1.0 or (q.line is not None and not math.isfinite(q.line)):
                # A price at or below even money cannot win. Treating it as a
                # quote would let a malformed row set the "best available" odds
                # for a selection and produce a confidently wrong edge.
                continue
            key = (q.market, _line_key(q.line), _side_key(q.selection))
            current = best.get(key)
            if current is None or q.odds > current.odds:
                best[key] = q
        return best

    @staticmethod
    def _best_quotes(quotes: Sequence[MarketPrice],
                     key: tuple[str, Optional[float]]) -> dict[str, MarketPrice]:
        """Best price per selection name, within one market and line.

        Thin wrapper over :meth:`_quote_index` for the callers that genuinely do
        want a single market. Kept because the accumulators iterate one market
        at a time.
        """
        market, line = key
        index = OpportunityBoard._quote_index(quotes)
        want = _line_key(line)
        return {side: q for (m, ln, side), q in index.items()
                if m == market and ln == want}

    def _opportunity(self, fixture: Fixture, market: str, selection: str,
                     line: Optional[float], p_model: float,
                     quote: Optional[MarketPrice], *, payout=None) -> Opportunity:
        fair = round(payout.fair_odds, 3) if payout else _fair_odds(p_model)
        payout_probabilities = dict(payout.probabilities) if payout else None
        if quote is None:
            return Opportunity(
                match_id=fixture.match_id, sport_key=fixture.sport_key,
                kickoff=fixture.kickoff, home=fixture.home, away=fixture.away,
                market=market, selection=selection, line=line,
                p_model=p_model, fair_odds=fair, priced=False, basis="model_only",
                payout_probabilities=payout_probabilities,
                reason="no market quote -- model fair price only, not an offer")

        odds = quote.odds
        ev = payout.ev(odds) if payout else (p_model * odds) - 1.0
        local_model = self._fixture_model(fixture)
        enough_history = min(local_model.strength(fixture.home).games,
                             local_model.strength(fixture.away).games) >= SUFFICIENT_GAMES
        margin = self.evidence_gate.margin(fixture.sport_key, market, selection, line, quote.book_key,
            configuration_hash=self.configuration_hash) if self.evidence_gate else None
        conservative_p = max(0., p_model - (margin or 0.))
        # For split contracts move probability mass from positive payouts to a
        # full loss before EV/Kelly sizing, retaining push semantics.
        conservative_payout = payout
        if payout and margin is not None:
            from .contracts import PayoutDistribution
            probabilities = dict(payout.probabilities)
            remaining = margin
            for grade in ('WIN', 'HALF_WIN'):
                moved = min(probabilities.get(grade, 0), remaining)
                probabilities[grade] = probabilities.get(grade, 0) - moved
                probabilities['LOSS'] = probabilities.get('LOSS', 0) + moved
                remaining -= moved
            conservative_payout = PayoutDistribution(probabilities)
        conservative_ev = conservative_payout.ev(odds) if payout else conservative_p * odds - 1
        stake = (conservative_payout.kelly(odds, fraction=self.kelly_fraction, cap=self.max_stake)
                 if payout else self._kelly(conservative_p, odds)) if (
                     margin is not None and conservative_ev >= self.min_ev and enough_history) else 0.0
        return Opportunity(
            match_id=fixture.match_id, sport_key=fixture.sport_key,
            kickoff=fixture.kickoff, home=fixture.home, away=fixture.away,
            market=market, selection=selection, line=line,
            p_model=p_model, fair_odds=fair, best_odds=odds,
            best_book=quote.book_title or quote.book_key,
            best_source=quote.source, ev=ev, stake_fraction=stake, priced=True,
            basis="model_vs_market",
            payout_probabilities=payout_probabilities,
            reason="Awaiting market/league validation" if margin is None else "" if conservative_ev > self.min_ev else
                   f"edge {ev:+.1%} below the {self.min_ev:.0%} threshold")

    def _kelly(self, p: float, odds: float) -> float:
        """Fractional Kelly, hard-capped. Zero unless the bet is actually +EV."""
        b = odds - 1.0
        if b <= 0.0 or p <= 0.0 or p >= 1.0:
            return 0.0
        full = (p * odds - 1.0) / b
        if full <= 0.0:
            return 0.0
        return min(full * self.kelly_fraction, self.max_stake)

    def _fixture_model(self, fixture):
        return self.model.for_league(fixture.sport_key) if hasattr(self.model, 'for_league') else self.model

    # -- ladders ------------------------------------------------------------

    def _winning_ladder(self, candidates: Sequence[Opportunity]) -> tuple[Opportunity, ...]:
        """Highest eligible probability per fixture across supported markets.

        Minimum fair odds excludes near-certain, negligible-return selections.
        Market priority is only a tie-breaker, never a preference for 1X2.
        """
        by_fixture: dict[str, list[Opportunity]] = {}
        for o in candidates:
            if o.p_model < self.min_model_prob or o.fair_odds < self.min_fair_odds:
                continue
            by_fixture.setdefault(o.match_id, []).append(o)

        picks: list[Opportunity] = []
        for match_id, rows in by_fixture.items():
            rows.sort(key=lambda o: (-o.p_model, MARKET_PRIORITY.get(o.market, 9),
                                     not o.priced, o.outcome_key))
            picks.append(rows[0])

        picks.sort(key=_opportunity_order)
        return tuple(picks[:MAX_WINNING_LADDER])

    def _earning_ladder(self, candidates: Sequence[Opportunity]) -> tuple[Opportunity, ...]:
        """Best money per unit staked. Only priced rows can appear.

        An unpriced row is *not* included with a zero EV. It simply is not an
        earning opportunity, and listing it as one -- even ranked last -- is how
        a board starts implying it can find value where it never saw a price.
        """
        eligible = [o for o in candidates
                    if o.priced and o.ev is not None and o.ev >= self.min_ev
                    and o.p_model >= self.min_model_prob
                    and o.fair_odds >= self.min_fair_odds]
        eligible.sort(key=lambda o: (o.kickoff, -o.p_model, -(o.ev or 0.0),
                                    _opportunity_order(o)))
        return tuple(eligible[:MAX_EARNING_LADDER])

    def _micro_bets(self, candidates: Sequence[Opportunity],
                    excluded: set[tuple[str, str, Optional[float], str]]) -> tuple[Opportunity, ...]:
        """Small, diverse markets: a balanced totals line, BTTS, correct score.

        A micro bet is small in *stake* and small in *conviction*, which means
        the most interesting line rather than the most likely one. For totals
        that is the line whose probability sits nearest 0.5 -- ``Over 1.5`` at
        95% is a certainty that pays nothing, and ``Over 5.5`` at 8% is a
        punt, and neither is a micro bet.

        Excludes the moneyline (that is the main pick, not a micro) and anything
        already on the earning ladder, so the list adds breadth rather than
        repeating the headline.
        """
        by_fixture: dict[str, list[Opportunity]] = {}
        for o in candidates:
            if o.market not in ("totals", "btts", "correct_score", "double_chance", "home_team_totals", "away_team_totals", "draw_no_bet", "asian_handicap", "corners"):
                continue
            if o.p_model < self.min_model_prob or o.fair_odds < self.min_fair_odds:
                continue
            if (o.match_id, o.market, o.line, o.selection) in excluded:
                continue
            if o.market == "totals" and o.line is not None and o.line > self.max_total_line:
                continue
            by_fixture.setdefault(o.match_id, []).append(o)

        picks: list[Opportunity] = []
        for match_id, rows in by_fixture.items():
            # One balanced totals line, the nearer BTTS side, and the single
            # most likely correct score: three genuinely different bets on the
            # same fixture rather than six versions of the same one.
            totals = [o for o in rows if o.market == "totals"]
            for market in ("double_chance", "home_team_totals", "away_team_totals", "draw_no_bet", "asian_handicap", "corners"):
                extra = [o for o in rows if o.market == market]
                if extra:
                    extra.sort(key=lambda o: (-o.p_model, o.line or 0, o.selection))
                    picks.append(extra[0])
            if totals:
                totals.sort(key=lambda o: (abs(o.p_model - 0.5), o.line or 0.0))
                picks.append(totals[0])
            btts = [o for o in rows if o.market == "btts"]
            if btts:
                btts.sort(key=lambda o: (abs(o.p_model - 0.5), o.selection))
                picks.append(btts[0])
            scores = [o for o in rows if o.market == "correct_score"]
            if scores:
                scores.sort(key=lambda o: (-o.p_model, o.selection))
                picks.append(scores[0])

        picks.sort(key=_opportunity_order)
        return tuple(picks[:MAX_MICRO_BETS])

    # -- accumulators -------------------------------------------------------

    def _accumulators(self, fixtures: Sequence[Fixture],
                      prices: Mapping[str, Sequence[MarketPrice]], *,
                      candidates: Optional[Sequence[Opportunity]] = None) -> tuple[Accumulator, ...]:
        """Multi-leg combos, scored with an explicit correlation haircut.

        A parlay's naive joint probability is the product of its legs, which
        assumes the legs are independent. Ours frequently are not: two fixtures
        from the same league on the same day share team-strength uncertainty, so
        if the model's read on the league is wrong, several legs fail at once
        and the true hit rate is *below* the product. The naive number is
        therefore reported next to the adjusted one and never in place of it.
        """
        # Candidate legs: the strongest single selection per fixture, priced
        # where possible, so a parlay is a set of distinct decisions rather than
        # the same bet repeated.
        if candidates is None:
            candidates = [opportunity for fixture in fixtures
                for opportunity in self._fixture_opportunities(fixture, prices.get(fixture.match_id, ()))]
        binary = [o for o in candidates if not o.payout_probabilities or not any(
            o.payout_probabilities.get(grade, 0) > 0 for grade in ('VOID', 'HALF_WIN', 'HALF_LOSS'))]
        legs = list(self._winning_ladder(binary))
        if len(legs) < 2:
            return ()

        legs.sort(key=_opportunity_order)
        pool = legs[:14]
        results: list[Accumulator] = []
        seen: set[tuple[str, ...]] = set()
        for size in self.accumulator_sizes:
            if size > len(pool):
                break
            for combo in itertools.combinations(pool, size):
                # One entry per set of fixtures. A fixture's strongest side is
                # already the only candidate, but the guard makes that a
                # property of the builder rather than a coincidence of it.
                key = tuple(sorted(leg.match_id for leg in combo))
                if key in seen:
                    continue
                acc = self._score_accumulator(combo)
                if acc is not None:
                    seen.add(key)
                    results.append(acc)
        if not results:
            return ()

        # Respect kickoff urgency before comparing winning probability. All
        # current combinations are research slips without a verified offer.
        results.sort(key=lambda a: (min(o.kickoff for o in a.legs), -a.p_adjusted,
            max(o.kickoff for o in a.legs), tuple(o.match_id for o in a.legs)))
        return tuple(results[:MAX_ACCUMULATORS])

    def _score_accumulator(self, combo: Sequence[Opportunity]) -> Optional[Accumulator]:
        p_naive = 1.0
        for leg in combo:
            p_naive *= leg.p_model
        if p_naive < self.min_accumulator_prob:
            return None

        penalty, warnings = self._correlation(combo)
        p_adj = max(1e-6, p_naive * penalty)
        if p_adj < self.min_accumulator_prob:
            return None
        fair = _fair_odds(p_adj)

        # Straight-leg quotes do not prove a combined offer, even at one book.
        # Keep these combinations advisory until a sportsbook confirms its slip.
        books = {leg.best_book for leg in combo if leg.priced}
        explanation = "theoretical combination: no confirmed bookmaker parlay offer"
        if len(books) > 1:
            explanation += "; best individual leg prices come from different books"
        return Accumulator(legs=tuple(combo), p_naive=p_naive, p_adjusted=p_adj,
            fair_odds=fair, correlation_penalty=penalty,
            warnings=warnings + (explanation,))

    @staticmethod
    def _correlation(combo: Sequence[Opportunity]) -> tuple[float, tuple[str, ...]]:
        """Haircut for shared uncertainty across legs."""
        penalty = 1.0
        warnings: list[str] = []

        leagues: dict[str, int] = {}
        days: set[str] = set()
        for leg in combo:
            leagues[leg.sport_key] = leagues.get(leg.sport_key, 0) + 1
            days.add(leg.kickoff.date().isoformat())

        for league, count in leagues.items():
            if count > 1:
                penalty *= CORRELATION_PENALTY_SAME_LEAGUE ** (count - 1)
                warnings.append(
                    f"{count} legs share {league}: leg outcomes are not independent")
        if len(days) == 1 and len(combo) > 1:
            penalty *= CORRELATION_PENALTY_SAME_DAY
            warnings.append(
                "all legs kick off the same day, so one slate of news or one "
                "round of rotation can break several legs at once")

        return round(penalty, 6), tuple(warnings)

    # -- coverage -----------------------------------------------------------

    def _coverage(self, in_window: Sequence[Fixture], modelled: Sequence[Fixture],
                  priced: int, window_hours: float,
                  notes: list[str]) -> BoardCoverage:
        leagues: dict[str, int] = {}
        sources: dict[str, int] = {}
        for f in in_window:
            leagues[f.sport_key] = leagues.get(f.sport_key, 0) + 1
            sources[f.source] = sources.get(f.source, 0) + 1
        return BoardCoverage(
            window_hours=window_hours,
            fixtures_seen=len(in_window),
            fixtures_modelled=len(modelled),
            fixtures_priced=priced,
            meets_volume_target=len(modelled) >= self.volume_target,
            volume_target=self.volume_target,
            leagues=leagues, sources=sources, notes=list(notes),
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _opportunity_order(o: Opportunity) -> tuple:
    return (o.kickoff, -o.p_model, MARKET_PRIORITY.get(o.market, 9),
            not o.priced, o.match_id, o.market, o.line or 0.0, o.selection)


def _fair_odds(p: float) -> float:
    p = min(max(p, 1e-6), 1.0 - 1e-6)
    return round(1.0 / p, 4)


def _hours(value: float) -> "datetime":
    from datetime import timedelta
    return timedelta(hours=value)


__all__ = [
    "OpportunityBoard", "Board", "Opportunity", "Accumulator", "Fixture",
    "MarketPrice", "BoardCoverage", "MIN_EV", "MIN_MODEL_PROB", "KELLY_FRACTION",
    "MIN_ACCUMULATOR_PROB",
]
