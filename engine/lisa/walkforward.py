"""Honest walk-forward evaluation of LISA vs. an independent model vs. baselines.

Motivation
----------
The static backtest (``backtest.py``) grades the quality-gated consensus
through the historical archive. It cannot answer one central question:

    Does the market-gated signal add value over an independently calibrated
    model that has never seen any odds?

This module answers it with a strictly chronological walk forward. Every
match is processed in real time order: predictions are produced from state
that existed *before* the match, then scored, then state is updated. There is
no look-ahead and no hindsight re-weighting.

SCOPE LIMIT (be precise about what this does not test)
------------------------------------------------------
The strategies compared here are ``market_follower`` and ``model_value``.
``gate.evaluate`` is NOT invoked in this module, so the LISA quality gate is
not one of the arms. This answers "does the market's favourite beat the model,
and does the model find positive-EV value bets" -- it does not yet answer
"does the gate add value", which is what the first paragraph above implies.
Adding the gated strategy as a third arm is the outstanding work.

The three competing strategies

    1. Market Follower  — bet the strongest implied favourite at the best
       recorded price (a naive, no-model baseline).
    2. Model Value      — bet the model's highest positive-EV outcome at its
       best recorded price.
    3. (Reported too) Average-market ROI for both, which is the realistic
       price a bettor typically receives, vs. the optimistic best price.

All probabilities (Brier, LogLoss, ECE, reliability/resolution, Wilson CI on
win rate) are computed on the model's own settled predictions, betting or not.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from typing import Any, Optional

from . import config as cfg
from .calibration import CalibrationReport, evaluate_calibration
from .history import HISTORICAL_ODDS, HISTORICAL_PROVENANCE, HISTORICAL_SCORES
from .model import EloPoissonModel

OUTCOMES = ("home", "draw", "away")


def _best_prices(odds_payload: dict[str, Any]) -> tuple[dict[str, float], dict[str, float], int]:
    """Best and mean recorded price per 1X2 outcome across the archive's books.

    Returns (best, mean, n_books_with_h2h). Only books quoting a full valid 1X2
    line contribute. Official outcome names are mapped to the canonical
    "home" / "draw" / "away" keys via the payload's team names.
    """
    best: dict[str, float] = {}
    means: dict[str, list[float]] = {o: [] for o in OUTCOMES}
    n = 0
    for book in odds_payload.get("bookmakers", []):
        for market in book.get("markets", []):
            if market.get("key", "").lower() != "h2h":
                continue
            prices: dict[str, float] = {}
            for oc in market.get("outcomes", []):
                name = str(oc.get("name", "")).strip()
                if name == odds_payload.get("home_team"):
                    canonical = "home"
                elif name == odds_payload.get("away_team"):
                    canonical = "away"
                else:
                    canonical = "draw"
                try:
                    p = float(oc.get("price"))
                except (TypeError, ValueError):
                    continue
                if p < 1.01:
                    continue
                prices[canonical] = p
            if len(prices) < 3:
                continue
            n += 1
            for o in OUTCOMES:
                p = prices.get(o)
                if p is None:
                    continue
                if p > best.get(o, 0.0):
                    best[o] = p
                means[o].append(p)
    avg = {o: (statistics.mean(means[o]) if means[o] else 0.0) for o in OUTCOMES}
    return best, avg, n


def build_matches() -> list[dict[str, Any]]:
    """Chronologically sorted list of real matches with scores and prices."""
    scores_by_id = {s["id"]: s for s in HISTORICAL_SCORES}
    matches: list[dict[str, Any]] = []
    for odds_payload in HISTORICAL_ODDS:
        sc = scores_by_id.get(odds_payload["id"])
        if not sc:
            continue
        scores = {s.get("name"): s.get("score") for s in sc.get("scores", [])}
        home_score = int(float(scores.get(odds_payload["home_team"], -1)))
        away_score = int(float(scores.get(odds_payload["away_team"], -1)))
        if home_score < 0 or away_score < 0:
            continue
        best, avg, n_books = _best_prices(odds_payload)
        if n_books == 0:
            continue
        matches.append({
            "match_id": odds_payload["id"],
            "league": odds_payload["sport_key"],
            "home": odds_payload["home_team"],
            "away": odds_payload["away_team"],
            "commence_time": odds_payload.get("commence_time", ""),
            "home_score": home_score,
            "away_score": away_score,
            "best": best,
            "avg": avg,
            "n_books": n_books,
        })
    matches.sort(key=lambda m: (m["commence_time"], m["match_id"]))
    return matches


def _settled_outcome(match: dict[str, Any]) -> str:
    hs, as_ = match["home_score"], match["away_score"]
    if hs > as_:
        return "home"
    if hs < as_:
        return "away"
    return "draw"


def _model_value_pick(match: dict[str, Any], probs: dict[str, float],
                      pred: dict[str, Any], min_edge: float, min_prob: float
                      ) -> Optional[tuple[str, float]]:
    """Highest positive-EV outcome per the model, or None if none qualifies.

    EV = p_model * price - 1, evaluated at the recorded best price. Requires
    both clubs to have enough prior history (``model_ready``).
    """
    if not pred["model_ready"]:
        return None
    best = match["best"]
    best_ev = -math.inf
    pick: Optional[str] = None
    for o in OUTCOMES:
        p = probs[o]
        price = best.get(o, 0.0)
        if price <= 0.0:
            continue
        if p < min_prob:
            continue
        ev = p * price - 1.0
        if ev > best_ev:
            best_ev = ev
            pick = o
    if pick is None or best_ev < min_edge:
        return None
    return pick, best_ev


def walk_forward(
    matches: Optional[list[dict[str, Any]]] = None,
    model: Optional[EloPoissonModel] = None,
    min_edge: float = 0.0,
    min_prob: float = 0.30,
    league_filter: Optional[set[str]] = None,
) -> dict[str, Any]:
    """Run the full walk-forward evaluation and return an honest report dict."""
    if matches is None:
        matches = build_matches()
    if league_filter is not None:
        matches = [m for m in matches if m["league"] in league_filter]
    model = model or EloPoissonModel()

    market_bets: list[dict[str, Any]] = []
    model_bets: list[dict[str, Any]] = []
    settled_predictions: list[dict[str, Any]] = []
    multiclass_briers: list[float] = []

    for match in matches:
        pred = model.predict_log(match["league"], match["home"], match["away"])
        probs = {"home": pred["p_home"], "draw": pred["p_draw"], "away": pred["p_away"]}
        actual = _settled_outcome(match)
        # `p_true` is the probability assigned to the outcome that ACTUALLY
        # happened and `result` is that outcome's real result. The previous
        # version stored the probability of the correct answer alongside a
        # hardcoded "WIN", so every record scored as a confident, correct
        # prediction: Brier became mean((1 - p_actual)^2) and ECE was measured
        # against a 100% observed win rate. That is a confidence-on-matches-it-
        # got-right measure, not a calibration, and it was being printed under
        # the heading "INDEPENDENT MODEL CALIBRATION".
        settled_predictions.append({
            "match_id": match["match_id"],
            "league": match["league"],
            "p_true": probs[actual],
            "result": "WIN" if actual == "home" else "LOSS",
        })
        # Multi-class Brier over the full 1X2 distribution.
        brier_i = sum((probs[o] - (1.0 if o == actual else 0.0)) ** 2 for o in OUTCOMES)
        multiclass_briers.append(brier_i)

        # Market follower baseline: strongest implied favourite, always.
        implied = {o: (1.0 / match["best"][o]) if match["best"].get(o, 0.0) > 1.0 else 0.0
                   for o in OUTCOMES}
        fav = max(OUTCOMES, key=lambda o: implied[o])
        if fav == actual:
            mr = "WIN"
            payoff = match["best"][fav] - 1.0
        else:
            mr = "LOSS"
            payoff = -1.0
        market_bets.append({
            "match_id": match["match_id"],
            "league": match["league"],
            "outcome": fav,
            "price_best": match["best"][fav],
            "price_avg": match["avg"][fav],
            "result": mr,
            "pnl_best": payoff,
            "pnl_avg": (match["avg"][fav] - 1.0) if mr == "WIN" else -1.0,
        })

        # Model value pick.
        pick = _model_value_pick(match, probs, pred, min_edge, min_prob)
        if pick is not None:
            outcome, ev = pick
            price_best = match["best"][outcome]
            price_avg = match["avg"][outcome]
            if outcome == actual:
                model_bets.append({
                    "match_id": match["match_id"],
                    "league": match["league"],
                    "outcome": outcome,
                    "p_model": probs[outcome],
                    "ev": ev,
                    "price_best": price_best,
                    "price_avg": price_avg,
                    "result": "WIN",
                    "pnl_best": price_best - 1.0,
                    "pnl_avg": (price_avg - 1.0) if price_avg > 0.0 else -1.0,
                })
            else:
                model_bets.append({
                    "match_id": match["match_id"],
                    "league": match["league"],
                    "outcome": outcome,
                    "p_model": probs[outcome],
                    "ev": ev,
                    "price_best": price_best,
                    "price_avg": price_avg,
                    "result": "LOSS",
                    "pnl_best": -1.0,
                    "pnl_avg": -1.0,
                })

        # Observe AFTER prediction — this is the no-look-ahead invariant.
        model.observe(match["league"], match["home"], match["away"],
                      match["home_score"], match["away_score"])

    def summarize(bets: list[dict[str, Any]], price_key: str) -> dict[str, Any]:
        n = len(bets)
        wins = sum(1 for b in bets if b["result"] == "WIN")
        wr = wins / n if n else 0.0
        lo, hi = (0.0, 0.0) if n == 0 else win_ci(wins, n)
        pnl = sum(b[f"pnl_{price_key}"] for b in bets)
        return {
            "bets": n,
            "wins": wins,
            "losses": n - wins,
            "win_rate": wr,
            "wilson_ci_lower": lo,
            "wilson_ci_upper": hi,
            f"roi_pct_{price_key}": (pnl / n * 100.0) if n else 0.0,
            f"net_pnl_{price_key}": pnl,
        }

    cal: CalibrationReport = evaluate_calibration(settled_predictions)

    def per_league(bets: list[dict[str, Any]], price_key: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for b in bets:
            d = out.setdefault(b["league"], {"bets": 0, "wins": 0, "pnl": 0.0})
            d["bets"] += 1
            if b["result"] == "WIN":
                d["wins"] += 1
            d["pnl"] += b[f"pnl_{price_key}"]
        for league, d in out.items():
            d["win_rate"] = d["wins"] / d["bets"] if d["bets"] else 0.0
            d["roi_pct"] = d["pnl"] / d["bets"] * 100.0 if d["bets"] else 0.0
        return out

    return {
        "meta": {
            "evaluator": "lisa.walkforward",
            "method": (
                "Strict chronological walk forward over {0} real matches. "
                "Predictions use only state from strictly earlier matches; "
                "no odds and no results from the match being predicted or later. "
                "Model learned exclusively from scores, never from market prices."
            ).format(len(matches)),
            "total_matches": len(matches),
            "data_provenance": dict(HISTORICAL_PROVENANCE),
        },
        "model": {
            "class": type(model).__name__,
            "ratings": model.state_size(),
            "warmup_games": model.min_prior_games,
        },
        "calibration": {
            "brier_score": cal.brier_score,
            "multiclass_brier_score": (
                statistics.mean(multiclass_briers) if multiclass_briers else None
            ),
            "log_loss": cal.log_loss,
            "ece": cal.ece,
            "mce": cal.mce,
            "reliability": cal.reliability,
            "resolution": cal.resolution,
            "uncertainty": cal.uncertainty,
            "n_settled_predictions": len(settled_predictions),
        },
        "strategies": {
            "market_follower_best": summarize(market_bets, "best"),
            "market_follower_avg": summarize(market_bets, "avg"),
            "model_value_best": summarize(model_bets, "best"),
            "model_value_avg": summarize(model_bets, "avg"),
        },
        "per_league": {
            "market_follower_best": per_league(market_bets, "best"),
            "model_value_best": per_league(model_bets, "best"),
            "model_value_avg": per_league(model_bets, "avg"),
        },
        "model_value_bets": model_bets,
        "market_follower_bets": market_bets,
    }


def win_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% confidence interval for a win rate."""
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = p + z2 / (2.0 * n)
    radius = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * n)) / n)
    lo = max(0.0, (centre - radius) / denom)
    hi = min(1.0, (centre + radius) / denom)
    return (lo, hi)


def format_walk_forward(report: dict[str, Any]) -> str:
    w = 80
    lines = ["=" * w]
    lines.append(" LISA WALK-FORWARD: GATED MARKET VS INDEPENDENT MODEL ".center(w, "="))
    lines.append("=" * w)
    meta = report["meta"]
    lines.append(f"  Matches walk-forward:                  {meta['total_matches']}")
    lines.append(f"  Warm-up games before model bets:       {report['model']['warmup_games']}")
    cal = report["calibration"]
    lines.append("\n[1] INDEPENDENT MODEL CALIBRATION (settled predictions)")
    lines.append("-" * w)
    lines.append(f"  Brier Score (MSE, 0=perfect):         {cal['brier_score']:.4f}")
    brier_mc = cal.get("multiclass_brier_score")
    if brier_mc is not None:
        lines.append(f"  Multiclass Brier (full 1X2):         {brier_mc:.4f}")
    lines.append(f"  Log Loss (Cross-Entropy):             {cal['log_loss']:.4f}")
    lines.append(f"  Expected Calibration Error (ECE):     {cal['ece']*100:.2f}%")
    lines.append(f"  Maximum Calibration Error (MCE):      {cal['mce']*100:.2f}%")
    lines.append(f"  Reliability:                          {cal['reliability']:.6f}")
    if cal.get("uncertainty"):
        lines.append(f"  Resolution:                           {cal['resolution']:.6f}")
        lines.append(f"  Uncertainty:                          {cal['uncertainty']:.6f}")

    lines.append("\n[2] STRATEGY HEAD-TO-HEAD (flat 1 unit)")
    lines.append("-" * w)
    lines.append(f"  {'Strategy':<34} {'Bets':<7} {'W-L':<8} {'Win %':<8} {'ROI@best':<10} {'ROI@avg'}")
    for key, label in (
        ("market_follower_best", "Market Follower (no model)"),
        ("model_value_best", "Model Value (indep. model)"),
    ):
        s = report["strategies"][key]
        avg = report["strategies"]["model_value_avg" if "model" in key else "market_follower_avg"]
        lines.append(
            f"  {label:<34} {s['bets']:<7} {s['wins']}-{s['losses']:<6} {s['win_rate']*100:>5.1f}%  "
            f"{s['roi_pct_best']:>6.2f}%   {avg['roi_pct_avg']:>6.2f}%"
        )
    lines.append(f"  ('@best' = best recorded price; '@avg' = mean of the five books — the price a bettor usually gets.)")
    return "\n".join(lines)


__all__ = [
    "build_matches",
    "walk_forward",
    "format_walk_forward",
    "win_ci",
    "OUTCOMES",
]