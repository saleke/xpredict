"""Daily Match Forecast Board — the volume product: 10+ probability forecasts
per matchday with honest uncertainty flags.

Decouples two things the previous design conflated:

    * Forecasts  — a probability for EVERY fixture (informational, high volume).
    * Staked bets — only the tiny subset that clears strict certainty gates.

The bulletin serves the first. It is deliberately cheap, daily, and honest:

    * 1X2 probabilities come from the market consensus (``consensus.refine``).
    * BTTS, over/under and most-likely scorelines come from the independent
      Poisson score model, evaluated strictly before the match (no look-ahead).
    * Every row carries an UNCERTAINTY flag: low / medium / high, with reasons.
      Popular fixtures are surfaced separately (``marquee``), and the day's
      strongest legitimate forecast is the ``top_pick`` — or none, if nothing
      clears the bar (we never manufacture a pick).

In this repository's offline mode the fixtures come from the real packaged
archive (the most recent matchday). In production the same engine receives
live odds from the ingestion daemon — the output schema is identical, which is
what makes the refund-on-integrity pledge enforceable.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from .consensus import refine
from .history import HISTORICAL_ODDS
from .model import EloPoissonModel
from .odds import Book, Match
from .tiers import lock_state
from .walkforward import build_matches

#: A (hypothetical) free user's view. Rows list features they cannot see yet —
#: the UI renders those as locked, gated teasers with an upgrade path.
DEFAULT_TIER = "free"

#: Coefficients of variation above this mean the books materially disagree.
MARKET_DISAGREE_CV = 0.08

#: A close/early price ratio outside these bounds counts as steam / drift.
STEAM_LO, STEAM_HI = 0.97, 1.03

#: A consensus pick must exceed this to be "Pick of the Day" material.
TOP_PICK_MIN_PROB = 0.55


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _match_from_payload(payload: dict[str, Any]) -> Optional[Match]:
    """Rebuild an h2h Match (books with line=None) from an archive payload."""
    books: list[Book] = []
    home = payload.get("home_team", "")
    away = payload.get("away_team", "")
    for b in payload.get("bookmakers", []):
        for mkt in b.get("markets", []):
            if str(mkt.get("key", "")).lower() != "h2h":
                continue
            outcomes: dict[str, float] = {}
            for oc in mkt.get("outcomes", []):
                name = str(oc.get("name", ""))
                if name == home:
                    key = "home"
                elif name == away:
                    key = "away"
                else:
                    key = "draw"
                try:
                    price = float(oc.get("price"))
                except (TypeError, ValueError):
                    continue
                if price >= 1.01:
                    outcomes[key] = price
            if len(outcomes) >= 3:
                books.append(Book(
                    key=str(b.get("key", "")),
                    title=str(b.get("title", "")),
                    last_update=None,
                    outcomes=outcomes,
                    line=None,
                ))
    if not books:
        return None
    return Match(
        id=str(payload.get("id", "")),
        sport_key=str(payload.get("sport_key", "")),
        commence_time=_parse_dt(payload.get("commence_time")) or datetime.now(timezone.utc),
        home_team=home,
        away_team=away,
        completed=bool(payload.get("completed", False)),
        market="h2h",
        bookmakers=tuple(books),
    )


def _steam_direction(ratio: Optional[float]) -> Optional[str]:
    if ratio is None:
        return None
    if ratio < STEAM_LO:
        return "steam_in"
    if ratio > STEAM_HI:
        return "drift_out"
    return "flat"


def _uncertainty_flags(market: Optional[dict[str, Any]],
                       model: dict[str, Any]) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    if market is None:
        reasons.append({"key": "thin_market", "label": "Too few books quoted a full 1X2 line — no reliable consensus."})
        return reasons
    if market["n_books"] < 3:
        reasons.append({"key": "thin_market", "label": f"Only {market['n_books']} book(s) — consensus is fragile."})
    if market["cv"] > MARKET_DISAGREE_CV:
        reasons.append({
            "key": "market_disagreement",
            "label": f"Books disagree (CV {market['cv'] * 100:.0f}% across {market['n_books']} books).",
        })
    model_top = max(("home", "draw", "away"), key=lambda o: model.get(f"p_{o}", 0.0))
    if market["top_outcome"] != model_top:
        reasons.append({
            "key": "model_disagreement",
            "label": f"Market leans {market['top_outcome']}, the independent model leans {model_top}.",
        })
    if not model.get("ready", False):
        reasons.append({"key": "low_model_history", "label": "Short model history for these clubs."})
    if market["p_top"] > 0.65 and any(r["key"] == "model_disagreement" for r in reasons):
        reasons.append({
            "key": "caution_favourite",
            "label": "Heavy favourite with market/model misalignment — classic sucker spot.",
        })
    return reasons


def _uncertainty_level(reasons: list[dict[str, Any]]) -> str:
    if any(r["key"] == "thin_market" for r in reasons):
        return "high"
    n = len(reasons)
    if n >= 2:
        return "high"
    if n == 1:
        return "medium"
    return "low"


def _locks(row_feature_keys: list[str]) -> list[dict[str, Any]]:
    out = []
    for feature in row_feature_keys:
        state = lock_state(DEFAULT_TIER, feature)
        if not state["unlocked"]:
            out.append({
                "feature": feature,
                "required_tier": state["required_tier"],
                "teaser": state["teaser"],
            })
    return out


def build_bulletin(*, mode: str = "archive", min_matches: int = 8,
                   max_matches: int = 40) -> dict[str, Any]:
    """Build the day's forecast board from real fixtures.

    ``mode="archive"`` uses the packaged real archive's most recent matchday(s)
    so the board is reproducible offline. The output schema is identical to the
    live board the ingestion daemon produces against current odds.

    Fixtures are collected from the latest well-covered matchday backwards
    until at least ``min_matches`` rows are present (then capped at
    ``max_matches``), so the volume promise holds even on thin days.
    """
    matches = build_matches()
    if not matches:
        return {"kind": "match_forecast_bulletin", "mode": mode, "count": 0, "matches": []}

    payload_by_id = {g.get("id"): g for g in HISTORICAL_ODDS}

    # Pick the most recent matchday(s) — this is "today" in the archive.
    # Prefer the latest matchday with decent book coverage (>=3 books on most
    # fixtures), so the board headline is not wall-to-wall thin flags; the
    # truly-thin latest day is still shown verbatim when nothing else exists.
    def _day_covered(day: str) -> bool:
        mt = [m for m in matches
              if (_parse_dt(m.get("commence_time")) or datetime.min).date().isoformat() == day]
        good = sum(1 for m in mt
                   if len(payload_by_id.get(m["match_id"], {}).get("bookmakers", [])) >= 3)
        return good >= max(3, int(0.6 * len(mt))) if mt else False

    dates: list[str] = []
    for m in matches:
        dt = _parse_dt(m.get("commence_time"))
        if dt is None:
            continue
        day = dt.date().isoformat()
        if day not in dates:
            dates.append(day)
    dates.sort(reverse=True)

    start_at = 0
    for i, day in enumerate(dates):
        if _day_covered(day):
            start_at = i
            break

    # Collect from the well-covered day backwards until the volume bar is met
    # (auto-extends across adjacent days when a single day is thin), then cap.
    target_ids: list[str] = []
    for day in dates[start_at:]:
        for m in matches:
            dt = _parse_dt(m.get("commence_time"))
            if dt is not None and dt.date().isoformat() == day:
                target_ids.append(m["match_id"])
        if len(target_ids) >= min_matches:
            break
    target_ids = target_ids[:max_matches]
    target_set = set(target_ids)

    model = EloPoissonModel()
    model_probs: dict[str, dict[str, Any]] = {}
    for m in matches:
        if m["match_id"] in target_set:
            matrix = model.predict_score_matrix(m["league"], m["home"], m["away"])
            pred = model.predict_log(m["league"], m["home"], m["away"])
            model_probs[m["match_id"]] = {
                "p_home": pred["p_home"],
                "p_draw": pred["p_draw"],
                "p_away": pred["p_away"],
                "ready": pred["model_ready"],
                "matrix": matrix,
            }
        model.observe(m["league"], m["home"], m["away"], m["home_score"], m["away_score"])

    rows: list[dict[str, Any]] = []
    for m in matches:
        if m["match_id"] not in target_set:
            continue
        payload = payload_by_id.get(m["match_id"])
        consensus = None
        market: Optional[dict[str, Any]] = None
        if payload:
            match_obj = _match_from_payload(payload)
            if match_obj:
                consensus = refine(match_obj, min_books=2)
        if consensus:
            market = {
                "present": True,
                "top_outcome": consensus.top_outcome,
                "p_top": consensus.p_top,
                "p_home": consensus.p.get("home", 0.0),
                "p_draw": consensus.p.get("draw", 0.0),
                "p_away": consensus.p.get("away", 0.0),
                "fair_odds": consensus.fair_odds,
                "cv": consensus.cv,
                "n_books": consensus.n_books,
            }
        mp = model_probs.get(m["match_id"], {})
        matrix = mp.get("matrix", {})
        model_view = {
            "p_home": round(mp.get("p_home", 0.0), 4),
            "p_draw": round(mp.get("p_draw", 0.0), 4),
            "p_away": round(mp.get("p_away", 0.0), 4),
            "ready": bool(mp.get("ready")),
        }
        micro = {
            "p_btts": matrix.get("p_btts"),
            "p_over_2_5": matrix.get("p_over_2_5"),
            "expected_goals_home": matrix.get("expected_goals", {}).get("home"),
            "expected_goals_away": matrix.get("expected_goals", {}).get("away"),
            "most_likely_scores": matrix.get("most_likely_scores", []),
        }

        # Steam direction on the consensus favourite (close/early price ratio).
        movement: Optional[dict[str, str]] = None
        if market and payload:
            fav_key = {
                "home": payload.get("home_team"),
                "draw": "Draw",
                "away": payload.get("away_team"),
            }.get(market["top_outcome"])
            ratios = (payload.get("movement") or {}).get("h2h") or {}
            if fav_key in ratios:
                direction = _steam_direction(ratios[fav_key])
                if direction:
                    movement = {"direction": direction, "ratio": ratios[fav_key]}

        reasons = _uncertainty_flags(market, model_view)
        rows.append({
            "match_id": m["match_id"],
            "league": m["league"],
            "home": m["home"],
            "away": m["away"],
            "commence_at": m["commence_time"],
            "market": market,
            "model": model_view,
            "micro": micro,
            "movement": movement,
            "uncertainty": {
                "level": _uncertainty_level(reasons),
                "reasons": reasons,
            },
            "is_top_pick": False,
            "marquee": False,
            "locks": _locks(["micro_pack", "top_pick", "diamond_picks", "steam_radar"]),
        })

    # Deterministic ordering: kickoff then match id.
    rows.sort(key=lambda r: (r["commence_at"], r["match_id"]))

    # The board's headline day = the most common fixture date actually shown.
    from collections import Counter
    day_counts = Counter(
        (_parse_dt(r["commence_at"]) or datetime.min).date().isoformat() for r in rows
    )
    day_label = day_counts.most_common(1)[0][0] if day_counts else None

    # Marquee = the most-covered fixtures of the day (coverage proxy for
    # popularity: more books price it, more money chases it).
    def _coverage(r: dict[str, Any]) -> tuple[int, str]:
        n = r["market"]["n_books"] if r["market"] else 0
        return (-n, r["commence_at"])

    top_three = sorted(rows, key=_coverage)[:3]
    for r in top_three:
        r["marquee"] = True

    # Pick of the Day = strongest genuine confidence, or none.
    candidates = [r for r in rows if r["market"] and r["market"]["p_top"] >= TOP_PICK_MIN_PROB]
    if candidates:
        pick = max(candidates, key=lambda r: r["market"]["p_top"])
        pick["is_top_pick"] = True

    return {
        "kind": "match_forecast_bulletin",
        "mode": mode,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "day": day_label,
        "count": len(rows),
        "top_pick": next((r["match_id"] for r in rows if r["is_top_pick"]), None),
        "marquee_count": sum(1 for r in rows if r["marquee"]),
        "high_uncertainty_count": sum(
            1 for r in rows if r["uncertainty"]["level"] == "high"
        ),
        "disclaimer": (
            f"[{mode}] Demonstration board built from the real packaged archive. "
            "Probabilities use only information available before kickoff; they are "
            "forecasts, not guarantees. In production this exact schema is served "
            "from live odds."
        ),
        "matches": rows,
    }


def format_bulletin(bulletin: dict[str, Any]) -> str:
    """Human-readable board for ``lisa forecast``."""
    meta = bulletin.get("matches", [])
    lines: list[str] = []
    w = 94
    lines.append(" LISA DAILY MATCH FORECAST BOARD ".center(w, "="))
    lines.append("=" * w)
    lines.append(f"  Day: {bulletin.get('day')}   Fixtures: {bulletin.get('count')}   "
                 f"Mode: {bulletin.get('mode')}")
    lines.append(f"  Marquee (❤ popular): {bulletin.get('marquee_count')}   "
                 f"⚠ high-uncertainty flags: {bulletin.get('high_uncertainty_count')}   "
                 f"Pick of the Day: {bulletin.get('top_pick') or '(none — honest bar)'}")
    lines.append("-" * w)

    for r in meta:
        tags = []
        if r["marquee"]:
            tags.append("❤POPULAR")
        if r["is_top_pick"]:
            tags.append("★PICK")
        if r["movement"]:
            tags.append(f"{r['movement']['direction'].upper()}"
                        f"({r['movement']['ratio']:.3f})")
        u = r["uncertainty"]
        head = "  {:<34} vs {:<34}".format(r["home"], r["away"])
        lines.append(head)
        lines.append(f"  {(' | '.join(tags) if tags else '-'):<30}  "
                     f"league={r['league']}  ko={r['commence_at']}")
        if r["market"]:
            mk = r["market"]
            lines.append(
                f"  consensus: {mk['top_outcome']} {mk['p_top'] * 100:.0f}%  "
                f"(H {mk['p_home'] * 100:.0f} D {mk['p_draw'] * 100:.0f} "
                f"A {mk['p_away'] * 100:.0f})  cv={mk['cv'] * 100:.1f}%  "
                f"{mk['n_books']} books"
            )
        else:
            lines.append("  consensus: unavailable")
        mv = r["model"]
        lines.append(
            f"  model:    (H {mv['p_home'] * 100:.0f} D {mv['p_draw'] * 100:.0f} "
            f"A {mv['p_away'] * 100:.0f})  ready={'Y' if mv['ready'] else 'N'}"
        )
        mi = r["micro"]
        if None not in (mi["p_btts"], mi["p_over_2_5"]):
            lines.append(
                f"  micro:    BTTS {mi['p_btts'] * 100:.0f}%  O2.5 {mi['p_over_2_5'] * 100:.0f}%  "
                f"xG {mi['expected_goals_home']}-{mi['expected_goals_away']}  "
                f"most likely: {_scores_text(mi['most_likely_scores'])}"
            )
        lines.append(
            f"  uncertainty: {u['level'].upper()}  {'; '.join(x['label'] for x in u['reasons']) or 'books & model aligned'}"
        )
        if u["level"] == "high":
            lines.append("    ↑ high uncertainty = do NOT stake this. It is a forecast, not a bet.")
        lines.append("-" * w)

    lines.append(f"\n{bulletin.get('disclaimer')}")
    return "\n".join(lines)


def _scores_text(scores: list[dict[str, Any]]) -> str:
    parts = []
    for s in scores[:4]:
        parts.append(f"{s['home_goals']}-{s['away_goals']} ({s['p'] * 100:.0f}%)")
    return ", ".join(parts) if parts else "-"