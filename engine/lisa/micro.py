"""Sport-aware micro-bet derivation with honest pricing.

The old board applied the soccer Poisson (BTTS, over/under 2.5 goals, expected
goals) to *every* sport, so NBA/MLB/NFL rows showed ``expected_goals≈1.5`` and
a "most likely scoreline" — user-visible nonsense. This module replaces that:

  * **soccer** — reuse the independent Poisson score matrix for BTTS, alternate
    over/under lines and most-likely scorelines. These are *derived* prices:
    each row is flagged ``priced`` when a real book beats it, ``unpriced``
    otherwise, so the UI never implies an edge it cannot back.
  * **everything else** — no goal model exists and none is invented. The micro
    list for a non-soccer fixture is exactly the *priced, real-book* markets it
    was polled on (moneyline, handicap, over/under). No fabricated scorelines.

This is the honest-floor rule: a probability without a pricing path is labelled
as such; a market without a model is simply not offered as an "edge".
"""
from __future__ import annotations

import math
from typing import Any, Optional

from . import config as cfg
from .model import EloPoissonModel

#: Family-keyed by sport-key prefix. Order matters (americanfootball before
#: football, so soccer_* matches first).
_FAMILY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("soccer", "soccer"),
    ("basketball", "basketball"),
    ("americanfootball", "american-football"),
    ("baseball", "baseball"),
    ("icehockey", "ice-hockey"),
    ("tennis", "tennis"),
)


def sport_family(sport_key: str) -> str:
    for prefix, family in _FAMILY_PREFIXES:
        if sport_key.startswith(prefix):
            return family
    return "other"


def is_goals_sport(sport_key: str) -> bool:
    """Only soccer has a Poisson goal model in this codebase."""
    return sport_family(sport_key) == "soccer"


def _poisson_tail(home_goals: float, away_goals: float, line: float, *,
                  over: bool) -> Optional[float]:
    """P(total goals over/under ``line``) from two independent Poissons."""
    if home_goals <= 0 or away_goals <= 0 or line < 0:
        return None
    target = int(line) + 1  # "over 2.5" means 3+ goals
    total = home_goals + away_goals
    cumulative = 0.0
    term = math.exp(-total)
    for k in range(0, target):
        cumulative += term
        term = term * (total / (k + 1))
    over_p = max(0.0, min(1.0, 1.0 - cumulative))
    return over_p if over else 1.0 - over_p


def _btts_probability(home_goals: float, away_goals: float) -> Optional[float]:
    if home_goals <= 0 or away_goals <= 0:
        return None
    return (1.0 - math.exp(-home_goals)) * (1.0 - math.exp(-away_goals))


def _best_price(opportunity: dict[str, Any], side: str) -> Optional[tuple[float, str]]:
    """Best real book quote for ``side`` on an opportunity, or None."""
    top = opportunity.get("top_outcome")
    if top is None:
        return None
    want = str(side).lower()
    if str(top).lower() != want:
        return None
    odds = opportunity.get("best_odds")
    book = opportunity.get("best_book")
    if not isinstance(odds, (int, float)) or odds <= 1.0:
        return None
    return (float(odds), str(book or ""))


def _ev(p: float, best_odds: Optional[float]) -> Optional[float]:
    if best_odds is None:
        return None
    return round((p * best_odds) - 1.0, 4)


def build_micro_markets(anchor: Any,
                        markets: list[dict[str, Any]],
                        model: Optional[EloPoissonModel] = None, *,
                        include_model_markets: bool = True) -> list[dict[str, Any]]:
    """Micro-bet menu for one fixture, priced where possible.

    ``markets`` is the list of priced consensus opportunities already derived
    from the real feed (one entry per market the fixture was polled on).

    Every entry reports the truth about itself:

      * ``priced: True``  — a real book quote backs this line/side.
      * ``priced: False`` — model-derived fair price only; DO NOT stake as an
        edge. The UI renders these as "unpriced model view".

    Soccer-only model extras (BTTS, alternate totals, scorelines) are appended
    only when ``include_model_markets`` is set; the priced real markets are
    always present so "micro bets of different kinds" never means "nothing".
    """
    out: list[dict[str, Any]] = []
    sport_key = str(getattr(anchor, "sport_key", "") or "unknown")

    # 1) Real-book markets this fixture was actually polled on.
    for opp in markets:
        mkt = opp.get("market")
        if mkt == "h2h":
            out.append({
                "market": "h2h",
                "kind": "moneyline",
                "line": None,
                "outcome": opp.get("top_outcome"),
                "p": opp.get("p_top"),
                "fair_odds": opp.get("fair_odds"),
                "best_odds": opp.get("best_odds"),
                "best_book": opp.get("best_book"),
                "ev": opp.get("ev"),
                "priced": opp.get("best_odds") is not None,
            })
        elif mkt in ("spreads", "totals", "btts"):
            out.append({
                "market": mkt,
                "kind": "handicap" if mkt == "spreads" else (
                    "over_under" if mkt == "totals" else "btts"),
                "line": opp.get("line"),
                "outcome": opp.get("top_outcome"),
                "p": opp.get("p_top"),
                "fair_odds": opp.get("fair_odds"),
                "best_odds": opp.get("best_odds"),
                "best_book": opp.get("best_book"),
                "ev": opp.get("ev"),
                "priced": opp.get("best_odds") is not None,
            })

    # 2) Soccer-only model extras (no look-ahead: derived from the same pre-
    #    match odds the market quotes came from).
    if include_model_markets and is_goals_sport(sport_key) and model is not None:
        league = sport_key
        home, away = anchor.home_team, anchor.away_team
        try:
            matrix = model.predict_score_matrix(league, home, away)
        except Exception:
            matrix = {}
        exp = matrix.get("expected_goals") or {}
        hg, ag = exp.get("home"), exp.get("away")
        if hg and ag and hg > 0 and ag > 0:
            totals = {o.get("line"): o for o in markets if o.get("market") == "totals"}
            quoted_lines = sorted(k for k in totals if isinstance(k, (int, float)))
            # Over/under at each line we have a real quote for *and* a couple
            # of adjacent half-lines the model can price fresh.
            lines = set(quoted_lines)
            if quoted_lines:
                base = quoted_lines[0]
                lines.update((base - 1.0, base + 1.0))
            for line in sorted(lines):
                for side, over in (("Over", True), ("Under", False)):
                    p = _poisson_tail(hg, ag, float(line), over=over)
                    if p is None or p <= 0.0 or p >= 1.0:
                        continue
                    quote = totals.get(line)
                    best = _best_price(quote, side) if quote else None
                    out.append({
                        "market": "model_totals",
                        "kind": "over_under_alt",
                        "line": float(line),
                        "outcome": side,
                        "p": round(p, 4),
                        "fair_odds": round(1.0 / p, 3),
                        "best_odds": best[0] if best else None,
                        "best_book": best[1] if best else None,
                        "ev": _ev(p, best[0] if best else None),
                        "priced": best is not None,
                    })

            p_btts = _btts_probability(hg, ag)
            if p_btts is not None and 0 < p_btts < 1:
                btts_opp = next((o for o in markets if o.get("market") == "btts"), None)
                for side, p in (("Yes", p_btts), ("No", 1.0 - p_btts)):
                    best = _best_price(btts_opp, side) if btts_opp else None
                    out.append({
                        "market": "model_btts",
                        "kind": "btts_model",
                        "line": None,
                        "outcome": side,
                        "p": round(p, 4),
                        "fair_odds": round(1.0 / p, 3),
                        "best_odds": best[0] if best else None,
                        "best_book": best[1] if best else None,
                        "ev": _ev(p, best[0] if best else None),
                        "priced": best is not None,
                    })

    return out


def micro_summary(micro_markets: list[dict[str, Any]]) -> dict[str, Any]:
    """Honest per-fixture micro summary (counts, priced coverage)."""
    priced = sum(1 for m in micro_markets if m.get("priced"))
    positive_ev = sum(1 for m in micro_markets
                      if isinstance(m.get("ev"), (int, float)) and m["ev"] > 0)
    kinds = sorted({m.get("kind") for m in micro_markets if m.get("kind")})
    return {
        "offered": len(micro_markets),
        "priced": priced,
        "unpriced": len(micro_markets) - priced,
        "positive_ev": positive_ev,
        "kinds": kinds,
    }