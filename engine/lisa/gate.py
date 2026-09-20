"""Stage 3 of the refinery — the precision quality gate.

Decisions made with the product owner (architecture review):
  * the certainty gate is P_true >= 75% (85% was shown to be unreachable
    post-de-vig on real markets);
  * an EV overlay selects which book to recommend: only books whose price
    clears the *leave-one-out* fair price (EV > 0) get an execution link.
    Each book's EV is measured against a reference consensus that excludes
    that book, so a lagging soft book can't dilute the reference it is
    judged against (self-inclusion confound). The engine itself emits a
    pick on certainty alone unless the product demands a positive-EV
    execution (``require_positive_ev``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .consensus import Consensus
from .odds import utcnow

MIN_EXEC_PRICE = 1.01


@dataclass(frozen=True)
class Execution:
    book_key: str
    book_title: str
    odds: float
    ev: float


@dataclass(frozen=True)
class Pick:
    match_id: str
    sport_key: str
    home_team: str
    away_team: str
    commence_time: datetime
    market: str
    outcome_name: str
    p_true: float
    fair_odds: float
    n_books: int
    stdev: float
    cv: float
    best_execution: Optional[Execution]
    state: str
    created_at: datetime
    line: Optional[float] = None


@dataclass(frozen=True)
class GateResult:
    pick: Optional[Pick]
    reason: str  # "ok" or a suppression reason


def evaluate(consensus: Consensus, *, threshold: float = 0.75,
             min_books: int = 5, max_cv: float = 0.10,
             ev_min: float = 0.0,
             require_positive_ev: bool = False) -> GateResult:
    if consensus.n_books < min_books:
        return GateResult(None, "insufficient_books")
    # Plausibility before certainty: a market the books disagree on is
    # untrustworthy no matter how "high" its blended probability looks.
    if not math.isfinite(consensus.cv) or consensus.cv > max_cv:
        return GateResult(None, "high_dispersion")
    if consensus.p_top < threshold:
        return GateResult(None, "below_threshold")

    best: Optional[Execution] = None
    for btp in consensus.books:
        price = btp.prices.get(consensus.top_outcome)
        if price is None or price < MIN_EXEC_PRICE:
            continue
        # EV against the leave-one-out reference: the book's own de-vigged
        # probabilities are excluded so a lagging line can't dilute the
        # consensus it is compared to.
        p_ref = consensus.loo_probability(btp.book_key)
        if p_ref is None:
            continue
        ev = p_ref * price - 1.0
        if ev >= ev_min and (best is None or ev > best.ev):
            best = Execution(book_key=btp.book_key, book_title=btp.book_title,
                             odds=price, ev=ev)

    if require_positive_ev and best is None:
        return GateResult(None, "no_positive_ev")

    pick_line = consensus.line
    if consensus.match.market == "spreads" and consensus.line is not None:
        if consensus.top_outcome == consensus.match.away_team:
            pick_line = -consensus.line

    pick = Pick(
        match_id=consensus.match.id,
        sport_key=consensus.match.sport_key,
        home_team=consensus.match.home_team,
        away_team=consensus.match.away_team,
        commence_time=consensus.match.commence_time,
        market=consensus.match.market,
        outcome_name=consensus.top_outcome,
        line=pick_line,
        p_true=consensus.p_top,
        fair_odds=consensus.fair_odds,
        n_books=consensus.n_books,
        stdev=consensus.stdev,
        cv=consensus.cv,
        best_execution=best,
        state="TRIGGER_ALERT",
        created_at=utcnow(),
    )
    return GateResult(pick, "ok")