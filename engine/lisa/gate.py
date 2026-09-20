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
from datetime import datetime, timezone
from typing import Optional, Union

from .consensus import Consensus
from .odds import Match, utcnow

MIN_EXEC_PRICE = 1.01


def compute_conviction_score(p_true: float, threshold: float = 0.75,
                             cv: float = 0.05, ev: float = 0.0) -> float:
    """Calculate institutional conviction score.

    Formula:
        Conviction = ((p_true - threshold) / max(cv, 0.005)) * (1.0 + max(0.0, ev))
    Higher certainty, tighter consensus (lower CV), and positive expected value increase conviction.
    """
    denom = max(cv, 0.005) if (math.isfinite(cv) and cv > 0) else 0.05
    excess = max(0.0, p_true - threshold)
    ev_multiplier = 1.0 + max(0.0, ev)
    return round((excess / denom) * ev_multiplier, 4)


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
    conviction_score: float = 0.0
    recommended_stake_pct: float = 0.0
    recommended_units: float = 0.0


@dataclass(frozen=True)
class GateResult:
    pick: Optional[Pick]
    reason: str  # "ok" or a suppression reason


@dataclass(frozen=True)
class PickFreshness:
    status: str  # "FRESH" | "SLIPPED_POSITIVE_EV" | "DECAYED_NEGATIVE_EV" | "VOLATILITY_SPIKE" | "EXPIRED" | "LINE_REMOVED"
    current_odds: Optional[float]
    emit_odds: float
    fair_odds: float
    current_ev: Optional[float]
    current_cv: Optional[float]
    warning: Optional[str] = None


def evaluate_pick_freshness(pick: Union[Pick, dict], current_match: Match,
                            now: datetime, max_cv_drift: float = 0.03) -> PickFreshness:
    """Evaluate live odds decay and consensus volatility for an emitted pick."""
    commence = current_match.commence_time
    if commence.tzinfo is None and now.tzinfo is not None:
        commence = commence.replace(tzinfo=timezone.utc)
    elif commence.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    emit_odds = float(pick.best_execution.odds if isinstance(pick, Pick) and pick.best_execution else (pick.get("best_odds") or pick.get("fair_odds", 1.0)))
    fair_odds = float(pick.fair_odds if isinstance(pick, Pick) else pick["fair_odds"])
    emit_cv = float(pick.cv if isinstance(pick, Pick) else pick.get("cv", 0.05))
    target_outcome = pick.outcome_name if isinstance(pick, Pick) else pick["outcome_name"]
    exec_book = (pick.best_execution.book_key if pick.best_execution else None) if isinstance(pick, Pick) else pick.get("best_book")
    target_line = pick.line if isinstance(pick, Pick) else pick.get("line")

    if now > commence:
        return PickFreshness(
            status="EXPIRED",
            current_odds=None,
            emit_odds=emit_odds,
            fair_odds=fair_odds,
            current_ev=None,
            current_cv=None,
            warning="Match has already commenced",
        )

    current_odds = None
    if exec_book:
        for b in current_match.bookmakers:
            if target_line is not None and b.line is not None and abs(b.line - target_line) > 1e-4:
                continue
            if b.key == exec_book and target_outcome in b.outcomes:
                current_odds = b.outcomes[target_outcome]
                break

    # Fallback to candidate books in line bucket if exec_book pulled the line
    candidate_odds: list[float] = []
    for b in current_match.bookmakers:
        if target_line is not None and b.line is not None and abs(b.line - target_line) > 1e-4:
            continue
        if target_outcome in b.outcomes:
            candidate_odds.append(b.outcomes[target_outcome])

    if current_odds is None and candidate_odds:
        current_odds = max(candidate_odds)

    if current_odds is None or current_odds <= 0:
        return PickFreshness(
            status="LINE_REMOVED",
            current_odds=None,
            emit_odds=emit_odds,
            fair_odds=fair_odds,
            current_ev=None,
            current_cv=None,
            warning="Market line removed by bookmakers",
        )

    # Compute current CV across quoting books in bucket
    current_cv = emit_cv
    if len(candidate_odds) >= 2:
        mean_p = sum(1.0 / o for o in candidate_odds) / len(candidate_odds)
        var_p = sum(((1.0 / o) - mean_p) ** 2 for o in candidate_odds) / (len(candidate_odds) - 1)
        stdev_p = math.sqrt(var_p)
        current_cv = round(stdev_p / mean_p, 4) if mean_p > 0 else emit_cv

    p_true = float(pick.p_true if isinstance(pick, Pick) else pick["p_true"])
    current_ev = round((p_true * current_odds) - 1.0, 4)

    if current_cv - emit_cv > max_cv_drift:
        return PickFreshness(
            status="VOLATILITY_SPIKE",
            current_odds=current_odds,
            emit_odds=emit_odds,
            fair_odds=fair_odds,
            current_ev=current_ev,
            current_cv=current_cv,
            warning=f"Consensus volatility spiked: CV rose from {emit_cv*100:.1f}% to {current_cv*100:.1f}%",
        )

    if current_odds < fair_odds:
        return PickFreshness(
            status="DECAYED_NEGATIVE_EV",
            current_odds=current_odds,
            emit_odds=emit_odds,
            fair_odds=fair_odds,
            current_ev=current_ev,
            current_cv=current_cv,
            warning=f"Odds decayed to {current_odds:.2f} below fair odds {fair_odds:.2f} (EV {current_ev*100:+.1f}%). Edge extinguished.",
        )

    if current_odds < emit_odds:
        return PickFreshness(
            status="SLIPPED_POSITIVE_EV",
            current_odds=current_odds,
            emit_odds=emit_odds,
            fair_odds=fair_odds,
            current_ev=current_ev,
            current_cv=current_cv,
            warning=f"Price slippage from {emit_odds:.2f} to {current_odds:.2f}; EV remains positive ({current_ev*100:+.1f}%).",
        )

    return PickFreshness(
        status="FRESH",
        current_odds=current_odds,
        emit_odds=emit_odds,
        fair_odds=fair_odds,
        current_ev=current_ev,
        current_cv=current_cv,
        warning=None,
    )


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

    best_ev = best.ev if best else 0.0
    conviction = compute_conviction_score(consensus.p_top, threshold, consensus.cv, best_ev)

    from .staking import compute_kelly_stake
    exec_odds = best.odds if best else consensus.fair_odds
    kelly = compute_kelly_stake(
        p_true=consensus.p_top,
        odds=exec_odds,
        cv=consensus.cv,
        max_cv=max_cv,
    )

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
        conviction_score=conviction,
        recommended_stake_pct=round(kelly.stake_fraction * 100.0, 2),
        recommended_units=kelly.recommended_units,
    )
    return GateResult(pick, "ok")