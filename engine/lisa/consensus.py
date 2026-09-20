"""Stage 2 of the refinery — the quantitative math node.

Multi-book de-vig (Shin) -> sharp/margin weighted consensus -> agreement
metrics (stdev / CV) over the isolated top outcome.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from . import shin
from .odds import Book, Match, utcnow

MIN_POOL_MARGIN = 0.001


@dataclass(frozen=True)
class BookTrueProbs:
    """A single book's de-vigged view of the market."""

    book_key: str
    book_title: str
    margin: float  # bookmaker margin (overround - 1)
    z: float       # Shin insider-money fraction
    probs: dict[str, float]   # outcome -> true probability (Shin)
    prices: dict[str, float]  # outcome -> original decimal odds


@dataclass(frozen=True)
class Consensus:
    match: Match
    books: tuple[BookTrueProbs, ...]
    p: dict[str, float]        # outcome -> consensus true probability
    top_outcome: str
    p_top: float
    n_books: int
    stdev: float               # sample stdev across books for the top outcome
    cv: float                  # coefficient of variation (agreement check)
    fair_odds: float
    weights: dict[str, float]  # book_key -> applied weight
    # blend parameters used to build this consensus — needed by the EV
    # overlay to recompute a reference that excludes a single book
    sharp_keys: tuple[str, ...] = ()
    sharp_multiplier: float = 2.0
    margin_weighted: bool = True
    min_margin_floor: float = MIN_POOL_MARGIN
    line: Optional[float] = None

    def loo_probability(self, exclude_key: str) -> Optional[float]:
        """Leave-one-out consensus probability for the top outcome.

        The reference consensus recomputed over every book *except*
        ``exclude_key`` — this is what the EV overlay compares a book's
        price against, so the book's own (possibly biased) probabilities
        don't dilute the reference it is judged on.
        """
        return leave_one_out_probability(
            self.books, exclude_key, self.top_outcome,
            sharp_keys=self.sharp_keys,
            sharp_multiplier=self.sharp_multiplier,
            margin_weighted=self.margin_weighted,
            min_margin_floor=self.min_margin_floor,
        )


def is_sharp(book_key: str, sharp_keys) -> bool:
    return book_key.lower() in {k.lower() for k in sharp_keys}


def single_book_true_probs(book: Book, lo: float = 1.01,
                           hi: float = 1001.0) -> Optional[BookTrueProbs]:
    """De-vig a single book. Returns None when the line is unusable."""
    if len(book.outcomes) < 2:
        return None
    names = list(book.outcomes.keys())
    prices = [book.outcomes[n] for n in names]
    try:
        res = shin.shin_probabilities(prices, lo=lo, hi=hi)
    except shin.ShinValidationError:
        return None
    return BookTrueProbs(
        book_key=book.key,
        book_title=book.title,
        margin=sum(1.0 / p for p in prices) - 1.0,
        z=res.z,
        probs=dict(zip(names, res.probabilities)),
        prices={n: book.outcomes[n] for n in names},
    )


def _fresh(book: Book, now: datetime, max_age_sec: float) -> bool:
    if book.last_update is None:
        return True  # unknown freshness -> do not penalise
    return (now - book.last_update).total_seconds() <= max_age_sec


def _book_weights(btps: tuple[BookTrueProbs, ...], *, sharp_keys,
                  sharp_multiplier: float, margin_weighted: bool,
                  min_margin_floor: float) -> dict[str, float]:
    """Marginal weights: sharp anchors doubled, optionally scaled by 1/margin
    so tight books (Pinnacle-like) influence the blend more than inflated
    retail lines."""
    weights: dict[str, float] = {}
    for btp in btps:
        w = sharp_multiplier if is_sharp(btp.book_key, sharp_keys) else 1.0
        if margin_weighted:
            w *= 1.0 / max(btp.margin, min_margin_floor)
        weights[btp.book_key] = w
    return weights


def leave_one_out_probability(
        btps: tuple[BookTrueProbs, ...], exclude_key: str, outcome: str, *,
        sharp_keys, sharp_multiplier: float, margin_weighted: bool,
        min_margin_floor: float) -> Optional[float]:
    """Blended probability of ``outcome`` over every book except
    ``exclude_key``, mirroring ``refine``'s weighting and normalisation.

    Returns ``None`` when no usable reference remains (all books excluded).
    """
    remaining = tuple(b for b in btps if b.book_key != exclude_key)
    if not remaining:
        return None
    weights = _book_weights(remaining, sharp_keys=sharp_keys,
                            sharp_multiplier=sharp_multiplier,
                            margin_weighted=margin_weighted,
                            min_margin_floor=min_margin_floor)
    wsum = sum(weights.values())
    if wsum <= 0.0 or not math.isfinite(wsum):
        return None
    outcomes = remaining[0].probs.keys()
    if outcome not in outcomes:
        return None
    p = {o: (sum(weights[b.book_key] * b.probs.get(o, 0.0) for b in remaining)
              / wsum) for o in outcomes}
    total = sum(p.values())
    if total <= 0.0:
        return None
    return p[outcome] / total


def refine(match: Match, *, now: Optional[datetime] = None,
           min_books: int = 3,
           sharp_keys=("pinnacle", "circa"),
           sharp_multiplier: float = 2.0,
           margin_weighted: bool = True,
           min_margin_floor: float = MIN_POOL_MARGIN,
           max_book_age_sec: Optional[float] = None,
           lo: float = 1.01, hi: float = 1001.0) -> Optional[Consensus]:
    """Blend usable books for a match into one consensus.

    Books are bucketed by their exact quoted point line (``Book.line``).
    The densest fresh complete bucket is selected (tiebreak: lower CV,
    then lower line value). For point-less markets like h2h, all books
    share ``line = None``, maintaining byte-identical single-bucket behavior.

    Returns ``None`` when there aren't enough fresh, complete books in any
    line bucket — the caller should treat that as "no signal this cycle"
    rather than an error.
    """
    if now is None:
        now = utcnow()
    if not match.bookmakers:
        return None

    usable: list[Book] = []
    for book in match.bookmakers:
        if max_book_age_sec is not None and not _fresh(book, now, max_book_age_sec):
            continue
        if len(book.outcomes) < 2:
            continue
        usable.append(book)

    if not usable:
        return None

    # Group usable books into buckets by exact quoted line.
    # For h2h, book.line is None (single bucket).
    buckets: dict[Optional[float], list[Book]] = {}
    for book in usable:
        buckets.setdefault(book.line, []).append(book)

    candidates: list[Consensus] = []
    for line, books in buckets.items():
        outcomes: set[str] = set()
        for b in books:
            outcomes.update(b.outcomes.keys())
        if not outcomes:
            continue

        # completeness: only keep books that price every outcome in the bucket's union
        complete = [b for b in books if set(b.outcomes.keys()) == outcomes]

        refined: list[BookTrueProbs] = []
        for book in complete:
            btp = single_book_true_probs(book, lo=lo, hi=hi)
            if btp is not None:
                refined.append(btp)

        if len(refined) < min_books:
            continue

        weights = _book_weights(refined, sharp_keys=sharp_keys,
                                sharp_multiplier=sharp_multiplier,
                                margin_weighted=margin_weighted,
                                min_margin_floor=min_margin_floor)
        wsum = sum(weights.values())
        if wsum <= 0.0 or not math.isfinite(wsum):
            continue

        p: dict[str, float] = {}
        for outcome in outcomes:
            acc = 0.0
            for btp in refined:
                acc += weights[btp.book_key] * btp.probs[outcome]
            p[outcome] = acc / wsum

        total = sum(p.values())
        if total <= 0.0:
            continue
        p = {k: v / total for k, v in p.items()}

        top_outcome = max(p, key=p.get)
        p_top = p[top_outcome]

        # Agreement: sample stdev across books for the top outcome.
        stdev = (
            statistics.stdev([b.probs[top_outcome] for b in refined])
            if len(refined) >= 2 else 0.0
        )
        cv = stdev / p_top if p_top > 0.0 else float("inf")

        candidates.append(Consensus(
            match=match,
            books=tuple(refined),
            p=p,
            top_outcome=top_outcome,
            p_top=p_top,
            n_books=len(refined),
            stdev=stdev,
            cv=cv,
            fair_odds=1.0 / p_top,
            weights=weights,
            sharp_keys=tuple(sharp_keys),
            sharp_multiplier=sharp_multiplier,
            margin_weighted=margin_weighted,
            min_margin_floor=min_margin_floor,
            line=line,
        ))

    if not candidates:
        return None

    # Densest bucket wins, tie -> lower CV -> lower line
    return min(candidates, key=lambda c: (
        -c.n_books,
        float("inf") if not math.isfinite(c.cv) else c.cv,
        float("-inf") if c.line is None else c.line,
    ))