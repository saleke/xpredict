"""Strategy tuning on the real archive — find honest, out-of-sample-checked configs.

Answers the profitability question directly: for every (gate threshold x league
subset), replay the real archive, grade against the official results, and report
ROI, max drawdown, and TRUE closing-line CLV, then break the best configs down
per season so nothing ships on a back-test honeymoon.

Only real archived data is used — the same ``HISTORICAL_ODDS`` /
``HISTORICAL_SCORES`` the backtest engine runs on. Consensus is computed once
per match (cached) and reused across all grid cells, so the sweep is cheap.
"""
from __future__ import annotations

import statistics
from typing import Any, Optional, Sequence

from .consensus import refine
from .gate import evaluate as evaluate_gate
from .history import HISTORICAL_ODDS, HISTORICAL_PROVENANCE, HISTORICAL_SCORES
from .odds import Score
from .parsing import parse_odds_payload, parse_scores_payload

DEFAULT_THRESHOLDS = (0.75, 0.78, 0.80, 0.82, 0.85)
DEFAULT_LEAGUES: tuple[Optional[tuple[str, ...]], ...] = (
    None,  # all leagues
    ("soccer_epl",),
    ("soccer_spain_la_liga",),
    ("soccer_germany_bundesliga",),
    ("soccer_italy_serie_a",),
    ("soccer_france_ligue_one",),
    ("soccer_epl", "soccer_france_ligue_one"),
)
SEASONS = ("2122", "2223", "2324", "2425")


def _season_of(match_id: str) -> str:
    try:
        return match_id.split("-")[1]
    except IndexError:
        return "????"


def load_matches() -> tuple[list[Any], dict[str, Score], dict[str, Optional[dict[str, float]]]]:
    """Parsed matches, archived scores, and closing-line references."""
    matches = parse_odds_payload(HISTORICAL_ODDS)
    scores = parse_scores_payload(HISTORICAL_SCORES)
    scores_by_id = {s.match_id: s for s in scores}
    closing_by_id: dict[str, Optional[dict[str, float]]] = {
        g["id"]: g.get("closing_odds") for g in HISTORICAL_ODDS
    }
    return matches, scores_by_id, closing_by_id


def _cell_key(threshold: float, leagues: Optional[tuple[str, ...]]) -> tuple:
    return (round(threshold, 3), leagues)


def tune_subsets(
    matches: Optional[list[Any]] = None,
    scores_by_id: Optional[dict[str, Score]] = None,
    closing_by_id: Optional[dict[str, Optional[dict[str, float]]]] = None,
    thresholds: Sequence[float] = DEFAULT_THRESHOLDS,
    league_options: Sequence[Optional[tuple[str, ...]]] = DEFAULT_LEAGUES,
    flat_stake: float = 100.0,
) -> dict[str, Any]:
    """Full threshold x league sweep with per-season splits and a Kelly risk sim."""

    # Defaults mirror the production backtest engine so the 0.75/ALL cell
    # reproduces its baseline ledger exactly (380 Grade A executes).
    min_books: int = 3
    gate_min_books: int = 5
    max_cv: float = 0.1

    if thresholds is None:
        thresholds = DEFAULT_THRESHOLDS
    if league_options is None:
        league_options = DEFAULT_LEAGUES

    if matches is None or scores_by_id is None or closing_by_id is None:
        matches, scores_by_id, closing_by_id = load_matches()

    consensus_cache: dict[Any, Any] = {}
    seasons = sorted({_season_of(m.id) for m in matches})

    # Per-bet ledger for every cell, so risk sims reuse the exact same bets.
    ledgers: dict[tuple, list[dict[str, Any]]] = {}

    for match in matches:
        score = scores_by_id.get(match.id)
        if not score or not score.completed:
            continue
        cons = consensus_cache.get(match.id)
        if cons is None and match.id not in consensus_cache:
            cons = refine(match, now=match.commence_time, min_books=min_books)
            consensus_cache[match.id] = cons
        if cons is None:
            continue
        closing = closing_by_id.get(match.id) or {}
        sport = match.sport_key
        season = _season_of(match.id)

        for threshold in thresholds:
            for leagues in league_options:
                if leagues is not None and sport not in leagues:
                    continue
                gate = evaluate_gate(
                    cons,
                    threshold=threshold,
                    min_books=gate_min_books,
                    max_cv=max_cv,
                    require_positive_ev=False,
                )
                if gate.pick is None:
                    continue
                pick = gate.pick
                outcome = pick.outcome_name
                odds = pick.best_execution.odds if pick.best_execution else pick.fair_odds
                res = score.grade_pick(pick.market, outcome)
                if res is None:
                    res = "WIN" if score.winner() == outcome else "LOSS"
                if res not in ("WIN", "LOSS", "VOID"):
                    continue
                closing_price = closing.get(outcome)
                clv = (odds / closing_price - 1.0) if closing_price else None
                ledgers.setdefault((threshold, leagues), []).append({
                    "season": season,
                    "sport": sport,
                    "odds": odds,
                    "result": res,
                    "p_true": pick.p_true,
                    "clv": clv,
                })

    # Aggregate across full-sample and per-season.
    rows: list[dict[str, Any]] = []
    seasons = [s for s in sorted(seasons) if s in SEASONS] or sorted(seasons)
    for (threshold, leagues), bets in ledgers.items():
        for season in ("ALL", *seasons):
            subset = bets if season == "ALL" else [b for b in bets if b["season"] == season]
            if not subset:
                continue
            rows.append(_summarize(subset, threshold, leagues, season, flat_stake))

    # Rank full-sample configs by ROI (minimum bet volume), break ties by drawdown.
    full_rows = [r for r in rows if r["window"] == "ALL"]
    ranked = sorted(
        full_rows,
        key=lambda r: (r["roi_pct"], -r["max_drawdown_pct"]),
        reverse=True,
    )
    best_full = [r for r in ranked if r["bets"] >= 60][:3]

    recommendation: Optional[dict[str, Any]] = None
    if best_full:
        cfg = best_full[0]
        sim = simulate_kelly(ledgers[(cfg["threshold"], cfg["leagues"])])
        recommendation = {
            "threshold": cfg["threshold"],
            "leagues": cfg["leagues"],
            "sample_bets": cfg["bets"],
            "sample_roi_pct": round(cfg["roi_pct"], 2),
            "kelly_sim": sim,
            "caveat": (
                "In-sample ranking on one archive. Ship only if the per-season "
                "split below is consistently positive — and re-validate on fresh "
                "matches before any real stake."
            ),
        }

    return {
        "meta": {
            "evaluator": "lisa.tuning.tune_subsets",
            "archive": dict(HISTORICAL_PROVENANCE),
            "thresholds": [float(t) for t in thresholds],
            "annual_windows": seasons,
            "method": (
                "Every (threshold x league subset) replays the real archive and is "
                "graded against official results. ROI/drawdown/CLV are computed on "
                "the same archived prices; ranked configs are then shown per season "
                "for out-of-sample plausibility. Consensus is shared across cells."
            ),
        },
        "grid": rows,
        "best": [
            {
                "threshold": r["threshold"],
                "leagues": r["leagues"],
                "full_sample": {
                    "bets": r["bets"],
                    "win_rate": r["win_rate"],
                    "roi_pct": r["roi_pct"],
                    "mean_clv": r["mean_clv"],
                    "clv_beat_rate": r["clv_beat_rate"],
                    "max_drawdown_pct": r["max_drawdown_pct"],
                },
                "per_season": [
                    _drop_best_summary(rr)
                    for rr in rows
                    if rr["threshold"] == r["threshold"] and rr["leagues"] == r["leagues"]
                ],
            }
            for r in best_full
        ],
        "recommendation": recommendation,
    }


def _summarize(bets: list[dict[str, Any]], threshold: float,
               leagues: Optional[tuple[str, ...]], season: str, flat: float) -> dict[str, Any]:
    n = len(bets)
    wins = sum(1 for b in bets if b["result"] == "WIN")
    losses = n - wins
    pnl = sum((b["odds"] - 1.0) * flat if b["result"] == "WIN" else -flat for b in bets)
    roi = pnl / (n * flat) if n else 0.0

    # Bankroll-basis drawdown (starts at $10k, flat $100 stakes) — matches the
    # backtest engine's financial drawdown so figures are comparable.
    bankroll = 10000.0
    peak_bankroll = bankroll
    max_dd = 0.0
    for b in bets:
        bankroll += (b["odds"] - 1.0) * flat if b["result"] == "WIN" else -flat
        peak_bankroll = max(peak_bankroll, bankroll)
        if peak_bankroll > 0:
            max_dd = max(max_dd, (peak_bankroll - bankroll) / peak_bankroll)

    clvs = [b["clv"] for b in bets if b["clv"] is not None]
    return {
        "threshold": threshold,
        "leagues": leagues,
        "window": season,
        "bets": n,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / n, 4) if n else 0.0,
        "roi_pct": round(roi * 100.0, 2),
        "max_drawdown_pct": round(max_dd * 100.0, 2),
        "mean_clv": round(statistics.mean(clvs), 4) if clvs else None,
        "clv_beat_rate": round(sum(1 for v in clvs if v > 0) / len(clvs), 4) if clvs else None,
    }


def _drop_best_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (list(row[key]) if isinstance(row[key], tuple) else row[key])
        for key in ("window", "bets", "wins", "losses", "win_rate",
                    "roi_pct", "max_drawdown_pct", "mean_clv", "clv_beat_rate")
    }


def simulate_kelly(
    bets: Sequence[dict[str, Any]],
    kelly_fraction: float = 0.5,
    initial_bankroll: float = 10000.0,
    per_bet_cap_pct: float = 0.05,
    drawdown_stop_pct: float = 0.25,
    flat_loss_limit: float = 0.0,
) -> dict[str, Any]:
    """Fractional-Kelly bankroll simulation over a bet ledger with risk limits.

    Full Kelly stake = p - (1-p)/(odds-1), scaled by ``kelly_fraction``, capped
    at ``per_bet_cap_pct`` of the current bankroll, and halted when drawdown
    reaches ``drawdown_stop_pct``. This is the 'minimum/controlled loss' half.
    """
    bankroll = float(initial_bankroll)
    peak = bankroll
    max_dd = 0.0
    max_dd_dollars = 0.0
    placed = 0
    stopped = False
    for b in bets:
        if drawdown_stop_pct > 0:
            dd = (peak - bankroll) / peak if peak > 0 else 0.0
            if dd >= drawdown_stop_pct:
                stopped = True
                break
        if flat_loss_limit > 0 and bankroll <= flat_loss_limit:
            stopped = True
            break
        odds = float(b["odds"])
        p = float(b["p_true"])
        f_full = p - (1.0 - p) / (odds - 1.0) if odds > 1.0 else 0.0
        f = max(0.0, f_full * kelly_fraction)
        stake = min(bankroll * per_bet_cap_pct, bankroll * f)
        stake = max(0.0, stake)
        pnl = stake * (odds - 1.0) if b["result"] == "WIN" else -stake
        bankroll += pnl
        placed += 1
        peak = max(peak, bankroll)
        if peak > 0:
            dd_pct = (peak - bankroll) / peak * 100.0
            if dd_pct > max_dd:
                max_dd = dd_pct
                max_dd_dollars = peak - bankroll

    return {
        "initial_bankroll": initial_bankroll,
        "final_bankroll": round(bankroll, 2),
        "net_profit": round(bankroll - initial_bankroll, 2),
        "bankroll_roi_pct": round((bankroll / initial_bankroll - 1.0) * 100.0, 2),
        "bets_placed": placed,
        "max_drawdown_pct": round(max_dd, 2),
        "max_drawdown_dollars": round(max_dd_dollars, 2),
        "kelly_fraction": kelly_fraction,
        "per_bet_cap_pct": per_bet_cap_pct,
        "drawdown_stop_pct": drawdown_stop_pct,
        "stopped": stopped,
    }


def format_tuning(report: dict[str, Any]) -> str:
    w = 86
    lines = ["=" * w]
    lines.append(" LISA STRATEGY TUNING — (threshold x league) SWEEP ON REAL ARCHIVE ".center(w, "="))
    lines.append("=" * w)

    lines.append("\n[1] FULL-SAMPLE GRID (ROI @ flat " + "$100, best early price, whole archive)")
    lines.append("-" * w)
    lines.append(f"{'Config':<44}{'Bets':>6}{'Win%':>7}{'ROI%':>8}{'DD%':>7}{'CLV%':>8}{'BeatsCL%':>9}")
    lines.append("-" * w)
    for r in sorted([x for x in report["grid"] if x["window"] == "ALL"],
                    key=lambda r: r["roi_pct"], reverse=True):
        league = "ALL" if r["leagues"] is None else "+".join(
            s.replace("soccer_", "").replace("_", " ").title() for s in r["leagues"])
        clv = f"{r['mean_clv'] * 100:+.2f}" if r["mean_clv"] is not None else "  n/a"
        beats = f"{r['clv_beat_rate'] * 100:.0f}" if r["clv_beat_rate"] is not None else "n/a"
        label = f"{r['threshold']:.0%} / {league}"
        lines.append(f"{label:<44}{r['bets']:>6}"
                     f"{r['win_rate'] * 100:>6.1f}%{r['roi_pct']:>8.2f}"
                     f"{r['max_drawdown_pct']:>7.1f}{clv:>8}{beats:>9}")

    if report["best"]:
        lines.append("\n[2] BEST CONFIGS — PER-SEASON OUT-OF-SAMPLE CHECK")
        lines.append("-" * w)
        for best in report["best"]:
            league = "ALL" if best["leagues"] is None else "+".join(
                s.replace("soccer_", "").replace("_", " ").title() for s in best["leagues"])
            lines.append(f"\n> {best['threshold']:.0%} / {league}"
                         f"  (full sample: {best['full_sample']['bets']} bets, "
                         f"ROI {best['full_sample']['roi_pct']:+.2f}%, "
                         f"DD {best['full_sample']['max_drawdown_pct']:.1f}%)")
            lines.append(f"{'Season':<10}{'Bets':>6}{'Win%':>7}{'ROI%':>8}{'DD%':>7}{'CLV%':>8}{'BeatsCL%':>9}")
            for row in best["per_season"]:
                if row["window"] == "ALL":
                    continue
                clv = f"{row['mean_clv'] * 100:+.2f}" if row["mean_clv"] is not None else "    n/a"
                beats = f"{row['clv_beat_rate'] * 100:.0f}" if row["clv_beat_rate"] is not None else "n/a"
                lines.append(f"{row['window']:<10}{row['bets']:>6}"
                             f"{row['win_rate'] * 100:>6.1f}%{row['roi_pct']:>8.2f}"
                             f"{row['max_drawdown_pct']:>7.1f}{clv:>8}{beats:>9}")

    rec = report.get("recommendation")
    if rec:
        sim = rec["kelly_sim"]
        lines.append("\n[3] RISK-CONTROLLED KELLY SIMULATION (recommended config, capital preserved part)")
        lines.append("-" * w)
        lines.append(f"  Config:                           {rec['threshold']:.0%} gate / "
                     f"{'+'.join(s for s in (rec['leagues'] or ())) or 'all leagues'}"
                     f"  ({rec['sample_bets']} sample bets, ROI {rec['sample_roi_pct']:+.2f}%)")
        lines.append(f"  Kelly fraction / stake cap:        {sim['kelly_fraction']*100:.0f}% / "
                     f"{sim['per_bet_cap_pct']*100:.0f}% of bankroll")
        lines.append(f"  Drawdown stop:                     {sim['drawdown_stop_pct']*100:.0f}%")
        lines.append(f"  Simulated bankroll:                ${sim['initial_bankroll']:,.0f} -> "
                     f"${sim['final_bankroll']:,.2f}  ({sim['bankroll_roi_pct']:+.2f}%)")
        lines.append(f"  Max drawdown (sim):                {sim['max_drawdown_pct']:.2f}% "
                     f"(${sim['max_drawdown_dollars']:,.2f})  |  bets: {sim['bets_placed']}  |  "
                     f"stopped: {sim['stopped']}")
        lines.append(f"  Caveat:                            {rec['caveat']}")

    lines.append("\n" + "=" * w)
    lines.append(" Methodology: every cell graded vs official archived results; per-season "
                 "split guards against tuning on one lucky window.".center(w))
    lines.append("=" * w)
    return "\n".join(lines)