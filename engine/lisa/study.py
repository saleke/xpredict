"""Honest market-efficiency study across every market the archive holds.

Replicates the classic betting-market findings on our own real data — with
nothing invented:

* for each market (1X2, Asian Handicap, Over/Under 2.5) the EARLY implied
  probabilities are bucketed and compared against the REAL outcome frequency
  (a calibration/efficiency curve);
* per market we chase the early *favourite* and measure true late-to-close CLV,
  win rate, and whether early→closing line MOVEMENT (steam) predicts the
  outcome better than the opening line alone (the open-vs-close efficiency
  insight of Kuypers 2000);
* the transparency layer reports score-derived analytics (BTTS frequency,
  correct-score distribution) plus a Poisson-model BTTS calibration check.

Everything derives from ``HISTORICAL_ODDS`` (early + closing snapshots) and
``HISTORICAL_SCORES`` (official results). Deterministic, stdlib-only.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Optional

from .history import HISTORICAL_ODDS, HISTORICAL_PROVENANCE, HISTORICAL_SCORES
from .markets import grade_asian_handicap, grade_total
from .model import EloPoissonModel
from .parsing import parse_scores_payload

SEASONS = ("2122", "2223", "2324", "2425")

P_BUCKETS: tuple[tuple[float, float], ...] = (
    (0.00, 0.10), (0.10, 0.20), (0.20, 0.30),
    (0.30, 0.40), (0.40, 0.50), (0.50, 1.01),
)

MOVEMENT_CUTS: tuple[tuple[str, float], ...] = (
    ("steam_in", 0.97), ("slight_in", 1.00),
    ("slight_out", 1.03), ("drift_out", 2.0),
)


def _season_of(match_id: str) -> str:
    try:
        return match_id.split("-")[1]
    except IndexError:
        return "????"


def _implied(price: float) -> float:
    return 1.0 / price if price and price > 0 else 0.0


def _normalize(pairs: dict[str, float]) -> dict[str, float]:
    total = sum(pairs.values())
    if total <= 0.0:
        return {k: 1.0 / len(pairs) for k in pairs}
    return {k: v / total for k, v in pairs.items()}


class MarketStudyEngine:
    """Replay the archive market-by-market; output aggregate findings only."""

    def __init__(self) -> None:
        self._scores = {
            s.match_id: s
            for s in parse_scores_payload(HISTORICAL_SCORES)
        }

    # -- iteration ----------------------------------------------------------

    def _rows_for(self, game: dict, market: str) -> Optional[list[dict[str, Any]]]:
        """Return per-outcome study rows for one market, or None when absent."""
        score = self._scores.get(game["id"])
        if not score or not score.completed or score.home_score is None or score.away_score is None:
            return None
        early = (game.get("markets_summary") or {}).get(market)
        if not early:
            return None
        close = (game.get("closing_markets") or {}).get(market)
        move = (game.get("movement") or {}).get(market)
        home, away = game["home_team"], game["away_team"]

        rows: list[dict[str, Any]] = []
        if market == "h2h":
            implied = _normalize({k: _implied(v) for k, v in early.items() if k != "line"})
            for outcome, p in implied.items():
                won = score.winner() == outcome
                rows.append({
                    "outcome": outcome, "probs": p, "won": won,
                    "decided": True,
                    "clv": _clv_of(early, close, outcome),
                    "movement": _movement_of(move, outcome),
                })
        elif market == "asian_handicap":
            if "line" not in early:
                return None
            implied = _normalize({
                home: _implied(early[home]), away: _implied(early[away])})
            for side, p in implied.items():
                g = grade_asian_handicap(score.home_score, score.away_score,
                                         float(early["line"]), side)
                rows.append({
                    "outcome": side, "probs": p,
                    "won": g["won_fraction"] > 0.0,
                    "decided": bool(g["decided"]),
                    "won_fraction": g["won_fraction"],
                    "clv": _clv_of(early, close, side),
                    "movement": _movement_of(move, side),
                })
        elif market == "total_over_under":
            implied = _normalize({"Over": _implied(early["Over"]),
                                  "Under": _implied(early["Under"])})
            for side, p in implied.items():
                g = grade_total(score.home_score, score.away_score,
                                float(early["line"]), side.lower())
                rows.append({
                    "outcome": side, "probs": p,
                    "won": g["won_fraction"] > 0.0,
                    "decided": bool(g["decided"]),
                    "won_fraction": g["won_fraction"],
                    "clv": _clv_of(early, close, side),
                    "movement": _movement_of(move, side),
                })
        else:
            return None
        return rows

    # -- aggregation --------------------------------------------------------

    def run(self) -> dict[str, Any]:
        calibration: dict[str, list[list[dict[str, Any]]]] = {m: [[] for _ in P_BUCKETS]
                                                              for m in ("h2h", "asian_handicap", "total_over_under")}
        favorites: dict[str, list[dict[str, Any]]] = {m: [] for m in calibration}
        btts_by_season: dict[str, list[bool]] = {}
        scorelines: list[tuple[int, int]] = []

        for game in HISTORICAL_ODDS:
            season = _season_of(game["id"])
            score = self._scores.get(game["id"])
            if not score or not score.completed:
                continue
            btts_by_season.setdefault(season, []).append(
                bool(score.home_score and score.away_score))
            scorelines.append((score.home_score or 0, score.away_score or 0))
            if score.home_score is None or score.away_score is None:
                continue

            for market in calibration:
                rows = self._rows_for(game, market)
                if not rows:
                    continue
                fav = max(rows, key=lambda r: r["probs"])
                favorites[market].append(fav)
                for row in rows:
                    if not row["decided"] and market != "h2h":
                        continue
                    bucket = _bucket_of(row["probs"])
                    calibration[market][bucket].append(row)

        markets_out: dict[str, dict[str, Any]] = {}
        for market in calibration:
            markets_out[market] = {
                "calibration": [
                    _bucketed(rows, P_BUCKETS[i])
                    for i, rows in enumerate(calibration[market])
                ],
                "favorite": {
                    "overview": _overview(favorites[market]),
                    "by_movement": _movement_buckets(favorites[market]),
                },
            }

        model_buckets = self._model_btts_calibration()
        btts_total = [b for lst in btts_by_season.values() for b in lst]

        return {
            "meta": {
                "evaluator": "lisa.study.MarketStudyEngine",
                "archive": _archive_provenance_slim(),
                "method": (
                    "Early implied probabilities (normalized best-price lines) "
                    "bucketed against real outcomes; the early favourite is "
                    "measured for late-to-close CLV and movement direction; "
                    "movement = closing/early price ratio. Derived BTTS and "
                    "correct-score analytics come from the official scores and "
                    "an independent no-look-ahead Poisson model."
                ),
            },
            "markets": markets_out,
            "btts": {
                "empirical_rate": round(sum(btts_total) / len(btts_total), 4) if btts_total else None,
                "matches": len(btts_total),
                "seasons": [
                    {"season": s, "n": len(lst),
                     "btts": sum(lst), "rate": round(sum(lst) / len(lst), 4)}
                    for s, lst in sorted(btts_by_season.items())
                ],
                "model_buckets": model_buckets,
                "correct_scores": _correct_scores(scorelines),
            },
        }

    def _model_btts_calibration(self) -> list[dict[str, Any]]:
        """Independent Poisson-model BTTS probability vs reality (no odds used)."""
        ordered = [
            {"league": g["sport_key"], "home": g["home_team"], "away": g["away_team"],
             **({"home_score": s.home_score, "away_score": s.away_score,
                  "match_id": g["id"]} if
                 (s := self._scores.get(g["id"])) and s.completed else {})}
            for g in HISTORICAL_ODDS
        ]
        model = EloPoissonModel()
        buckets: dict[int, list[tuple[float, bool, bool]]] = {}
        for m in ordered:
            if "home_score" not in m:
                continue
            overview = model.predict_score_matrix(m["league"], m["home"], m["away"])
            ready = model.ready(m["league"], m["home"], m["away"])
            actual = bool(m["home_score"] and m["away_score"])
            idx = _bucket_of(overview["p_btts"])
            buckets.setdefault(idx, []).append(
                (overview["p_btts"], actual, ready))
            model.observe(m["league"], m["home"], m["away"],
                          m["home_score"], m["away_score"])

        out: list[dict[str, Any]] = []
        for idx in sorted(buckets):
            rows = buckets[idx]
            used = [(p, a) for p, a, ready in rows if ready]
            pb, pb_diff = P_BUCKETS[idx]
            if not used:
                continue
            mean_p = sum(p for p, _ in used) / len(used)
            rate = sum(1 for _, a in used if a) / len(used)
            out.append({"prob_lo": pb, "prob_hi": pb_diff,
                        "n": len(used), "model_mean_p": round(mean_p, 4),
                        "empirical_rate": round(rate, 4)})
        return out


# -- helpers ---------------------------------------------------------------

def _archive_provenance_slim() -> dict[str, Any]:
    return {
        "source": HISTORICAL_PROVENANCE["source"],
        "total_matches": HISTORICAL_PROVENANCE["total_matches"],
        "seasons": HISTORICAL_PROVENANCE["seasons"],
        "markets_covered": HISTORICAL_PROVENANCE["markets_covered"],
        "matches_with_ah": HISTORICAL_PROVENANCE["matches_with_ah"],
        "matches_with_ou": HISTORICAL_PROVENANCE["matches_with_ou"],
        "matches_with_movement": HISTORICAL_PROVENANCE["matches_with_movement"],
        "statement": HISTORICAL_PROVENANCE["statement"],
    }


def _bucket_of(prob: float) -> int:
    for i, (lo, hi) in enumerate(P_BUCKETS):
        if lo <= prob < hi:
            return i
    return len(P_BUCKETS) - 1


def _clv_of(early: dict, close: Optional[dict], outcome: str) -> Optional[float]:
    if not early or not close or not early.get(outcome) or not close.get(outcome):
        return None
    return early[outcome] / close[outcome] - 1.0


def _movement_of(move: Optional[dict], outcome: str) -> Optional[float]:
    if not move:
        return None
    return move.get(outcome)


def _bucketed(rows: list[dict[str, Any]], bounds: tuple[float, float]) -> dict[str, Any]:
    decided = [r for r in rows if r["decided"]]
    n = len(decided)
    weights = [r.get("won_fraction", 1.0 if r["won"] else 0.0) for r in decided]
    wins = sum(weights)
    clvs = [r["clv"] for r in decided if r["clv"] is not None]
    return {
        "prob_lo": bounds[0],
        "prob_hi": bounds[1],
        "n": n,
        "total_rows": len(rows),
        "wins": round(wins, 3),
        "empirical_rate": round(wins / n, 4) if n else None,
        "mean_implied": round(sum(r["probs"] for r in decided) / n, 4) if n else None,
        "mean_clv": round(sum(clvs) / len(clvs), 5) if clvs else None,
        "clv_beat_rate": round(sum(1 for v in clvs if v > 0) / len(clvs), 4) if clvs else None,
    }


def _overview(rows: list[dict[str, Any]]) -> dict[str, Any]:
    decided = [r for r in rows if r["decided"]]
    n = len(decided)
    weights = [r.get("won_fraction", 1.0 if r["won"] else 0.0) for r in decided]
    wins = sum(weights)
    clvs = [r["clv"] for r in decided if r["clv"] is not None]
    return {
        "n": n,
        "wins": round(wins, 3),
        "win_rate": round(wins / n, 4) if n else None,
        "mean_implied": round(sum(r["probs"] for r in decided) / n, 4) if n else None,
        "mean_clv": round(sum(clvs) / len(clvs), 5) if clvs else None,
        "clv_beat_rate": round(sum(1 for v in clvs if v > 0) / len(clvs), 4) if clvs else None,
    }


def _movement_buckets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {label: [] for label, _ in MOVEMENT_CUTS}
    for r in rows:
        mv = r.get("movement")
        if mv is None:
            continue
        grouped[_cut_label(mv)].append(r)
    out = []
    for label, _ in MOVEMENT_CUTS:
        seg = grouped[label]
        if not seg:
            continue
        overview = _overview(seg)
        overview["bucket"] = label
        out.append(overview)
    return out


def _cut_label(mv: float) -> str:
    for label, bound in MOVEMENT_CUTS:
        if mv < bound:
            return label
    return "drift_out"


def _correct_scores(scorelines: list[tuple[int, int]], top: int = 12) -> list[dict[str, Any]]:
    counts: Counter[tuple[int, int]] = Counter(scorelines)
    total = len(scorelines) or 1
    return [
        {"home_goals": s[0], "away_goals": s[1],
         "count": c, "fraction": round(c / total, 5)}
        for s, c in counts.most_common(top)
    ]


# -- rendering -------------------------------------------------------------

def format_study(report: dict[str, Any], width: int = 92) -> str:
    w = width
    lines = ["=" * w]
    lines.append(" LISA MARKET-EFFICIENCY STUDY — all markets, real archive ".center(w, "="))
    lines.append("=" * w)
    meta = report["meta"]
    lines.append(f"  archive: {meta['archive']['source']}")
    lines.append(f"  matches: {meta['archive']['total_matches']}  |  AH: {meta['archive']['matches_with_ah']}  |  "
                 f"O/U: {meta['archive']['matches_with_ou']}  |  movement: {meta['archive']['matches_with_movement']}")

    for label, market in (("1X2 (home/draw/away)", "h2h"),
                          ("ASIAN HANDICAP", "asian_handicap"),
                          ("OVER/UNDER 2.5", "total_over_under")):
        section = report["markets"][market]
        fav = section["favorite"]["overview"]
        lines.append("\n" + "-" * w)
        lines.append(f" {label} ".center(w, "="))
        lines.append("-" * w)
        lines.append(" EARLY-IMPLIED vs REALITY (calibration buckets)     "
                     "movement = closing/early price of the favourite")
        lines.append(f"{'p bucket':<18}{'n':>7}{'implied':>9}{'actual%':>9}"
                     f"{'CLV%':>9}{'beatCL%':>9}")
        lines.append("-" * w)
        for row in section["calibration"]:
            if not row["n"]:
                continue
            imp = f"{row['mean_implied']*100:.1f}" if row["mean_implied"] is not None else "n/a"
            act = f"{row['empirical_rate']*100:.1f}" if row["empirical_rate"] is not None else "n/a"
            clv = f"{row['mean_clv']*100:+.2f}" if row["mean_clv"] is not None else "  n/a"
            beats = f"{row['clv_beat_rate']*100:.0f}" if row["clv_beat_rate"] is not None else "n/a"
            lines.append(f"[{row['prob_lo']:.2f},{row['prob_hi']:.2f}){'':<10}{row['n']:>7}"
                         f"{imp:>9}{act:>9}{clv:>9}{beats:>9}")
        lines.append(f"\n EARLY FAVOURITE CHASER  (n={fav['n']}, implied {fav['mean_implied']*100:.1f}%)")
        lines.append(f"   win rate: {fav['win_rate']*100:.1f}%  |  mean CLV {fav['mean_clv']*100:+.2f}%  |  "
                     f"beat close {fav['clv_beat_rate']*100:.0f}%")
        for seg in section["favorite"]["by_movement"]:
            lines.append(f"   {seg['bucket']:<12} n={seg['n']:>5}  win {seg['win_rate']*100:5.1f}%  "
                         f"CLV {seg['mean_clv']*100:+6.2f}%")

    b = report["btts"]
    lines.append("\n" + "-" * w)
    lines.append(" BTTS & CORRECT SCORE — score-derived transparency layer ".center(w, "="))
    lines.append("-" * w)
    if b["empirical_rate"] is not None:
        lines.append(f" BTTS occurred in {b['empirical_rate']*100:.1f}% of matches ({b['matches']})")
        lines.append(" " + " | ".join(f"{s['season']}: {s['rate']*100:.1f}% ({s['n']})"
                                       for s in b["seasons"]))
        lines.append(f"\n INDEPENDENT POISSON MODEL BTTS CALIBRATION (no odds used)")
        lines.append(f"{'p btts':<18}{'n':>7}{'model%':>9}{'actual%':>9}")
        for r in b["model_buckets"]:
            lines.append(f"[{r['prob_lo']:.2f},{r['prob_hi']:.2f}){'':<10}{r['n']:>7}"
                         f"{r['model_mean_p']*100:>9.1f}{r['empirical_rate']*100:>9.1f}")
    if b["correct_scores"]:
        lines.append(" MOST COMMON SCORELINES: " + ", ".join(
            f"{r['home_goals']}-{r['away_goals']} {r['fraction']*100:.1f}%"
            for r in b["correct_scores"][:6]))

    lines.append("\nMethodology: every aggregate is graded against archived official results; "
                 "no odds are invented.".center(w))
    lines.append("=" * w)
    return "\n".join(lines)