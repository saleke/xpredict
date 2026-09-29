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

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from .consensus import Consensus, refine
from .derived import derived_btts_probability, derived_total_probability
from .history import HISTORICAL_ODDS
from .model import EloPoissonModel
from .odds import H2H, Book, Match
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


#: Trained-on-archive model, built once per process. The archive is strictly
#: historical, so using it as prior information for *upcoming* fixtures
#: introduces no look-ahead.
_TRAINED_MODEL: Optional[EloPoissonModel] = None


def trained_model() -> EloPoissonModel:
    """Elo/Poisson model fitted on real settled results, in kickoff order."""
    global _TRAINED_MODEL
    if _TRAINED_MODEL is not None:
        return _TRAINED_MODEL
    model = EloPoissonModel()
    history = sorted(build_matches(), key=lambda m: str(m.get("commence_time") or ""))
    for m in history:
        hs, as_ = m.get("home_score"), m.get("away_score")
        if hs is None or as_ is None:
            continue
        model.observe(m["league"], m["home"], m["away"], int(hs), int(as_))
    _TRAINED_MODEL = model
    return model


def _opportunity(consensus: Consensus) -> dict[str, Any]:
    """One priced, per-market opportunity derived from a real consensus.

    Works for any market ``refine`` understands: point-less 1X2 (``h2h``) and
    line markets (``spreads``/``totals``) where the quoted line is carried on
    the consensus. The best price is taken across every book that quoted the
    winning outcome, so the EV shown is achievable rather than a consensus
    average nobody can bet.
    """
    best_odds: Optional[float] = None
    best_book: Optional[str] = None
    for book in consensus.books:
        price = book.prices.get(consensus.top_outcome)
        if price is None or price <= 1.0:
            continue
        if best_odds is None or price > best_odds:
            best_odds, best_book = float(price), book.book_key

    ev: Optional[float] = None
    if best_odds is not None:
        # EV per unit staked: what the fair price pays versus the best quote.
        ev = round((consensus.p_top * best_odds) - 1.0, 4)

    return {
        "market": consensus.match.market,
        "line": consensus.line,
        "top_outcome": consensus.top_outcome,
        "p_top": round(consensus.p_top, 4),
        "fair_odds": round(consensus.fair_odds, 3),
        "cv": round(consensus.cv, 4),
        "n_books": consensus.n_books,
        "best_odds": round(best_odds, 3) if best_odds is not None else None,
        "best_book": best_book,
        "ev": ev,
    }


def _poisson_tail(home_goals: float, away_goals: float, line: float,
                  *, over: bool) -> Optional[float]:
    """P(total goals over/under ``line``) from two independent Poissons.

    This is what turns the goal model into an actual micro market rather than a
    decorative number: a real totals line can be priced against a probability
    the books never published.
    """
    if home_goals <= 0 or away_goals <= 0 or line < 0:
        return None
    return derived_total_probability(home_goals, away_goals, line, over=over)


def _btts_probability(home_goals: float, away_goals: float) -> Optional[float]:
    """P(both teams score at least once) from the model goal rates."""
    if home_goals <= 0 or away_goals <= 0:
        return None
    return derived_btts_probability(home_goals, away_goals)


def _micro_opportunities(match_id: str, matrix: dict[str, Any],
                         totals: list[dict[str, Any]],
                         btts_price: Optional[tuple[float, str]] = None
                         ) -> list[dict[str, Any]]:
    """Goal-market (micro) opportunities priced against the real book lines.

    ``totals`` holds the priced totals opportunities already derived from the
    books. Where the model and a real line exist together the edge is genuine;
    with no book price the entry is still shown but flagged as unpriced, so the
    UI never implies an edge it cannot back.
    """
    exp = (matrix.get("expected_goals") or {})
    home_goals, away_goals = exp.get("home"), exp.get("away")
    if home_goals is None or away_goals is None:
        return []

    out: list[dict[str, Any]] = []
    for opp in totals:
        line = opp.get("line")
        if line is None:
            continue
        for side, over in (("Over", True), ("Under", False)):
            # The consensus top outcome for a totals market is the priced side.
            if opp.get("top_outcome", "").lower().startswith(side.lower()) is not over:
                continue
            p = _poisson_tail(home_goals, away_goals, line, over=over)
            if p is None or p <= 0 or p >= 1:
                continue
            fair = round(1.0 / p, 3)
            best_odds = opp.get("best_odds")
            ev = round((p * best_odds) - 1.0, 4) if best_odds else None
            out.append({
                "market": "model_totals",
                "line": line,
                "side": side,
                "p_model": round(p, 4),
                "fair_odds": fair,
                "best_odds": best_odds,
                "best_book": opp.get("best_book"),
                "ev": ev,
                "match_id": match_id,
            })

    p_btts = _btts_probability(home_goals, away_goals)
    if p_btts is not None and 0 < p_btts < 1:
        for side, p in (("Yes", p_btts), ("No", 1.0 - p_btts)):
            best_odds, best_book = btts_price if btts_price else (None, None)
            out.append({
                "market": "model_btts",
                "line": None,
                "side": side,
                "p_model": round(p, 4),
                "fair_odds": round(1.0 / p, 3),
                "best_odds": round(best_odds, 3) if best_odds else None,
                "best_book": best_book,
                "ev": round((p * best_odds) - 1.0, 4) if best_odds else None,
                "match_id": match_id,
            })
    return out


def _row_from_matches(matches: list[Match], model: EloPoissonModel, *,
                      in_play: bool = False) -> Optional[dict[str, Any]]:
    """One forecast row for a real fixture, covering every market priced for it.

    The odds parser emits one ``Match`` per market, so a fixture can arrive with
    1X2, a handicap and a total. Grouping them here keeps one row per fixture
    while still surfacing every market as its own priced opportunity.
    """
    anchor = matches[0]
    league = str(getattr(anchor, "sport_key", "") or "unknown")
    home, away = anchor.home_team, anchor.away_team

    opportunities: list[dict[str, Any]] = []
    by_market: dict[str, dict[str, Any]] = {}
    for m in matches:
        consensus = refine(m, min_books=2)
        if not consensus:
            continue
        opp = _opportunity(consensus)
        opportunities.append(opp)
        by_market[opp["market"]] = opp

    # Keep the historical top-level shape: 1X2 when present, else the first
    # market that did resolve, so existing consumers keep working.
    market = by_market.get(H2H) or (opportunities[0] if opportunities else None)

    matrix = model.predict_score_matrix(league, home, away)
    pred = model.predict_log(league, home, away)
    model_view = {
        "p_home": round(pred["p_home"], 4),
        "p_draw": round(pred["p_draw"], 4),
        "p_away": round(pred["p_away"], 4),
        "ready": bool(pred["model_ready"]),
    }
    micro = {
        "p_btts": matrix.get("p_btts"),
        "p_over_2_5": matrix.get("p_over_2_5"),
        "expected_goals_home": matrix.get("expected_goals", {}).get("home"),
        "expected_goals_away": matrix.get("expected_goals", {}).get("away"),
        "most_likely_scores": matrix.get("most_likely_scores", []),
    }

    totals = [o for o in opportunities if o.get("market") == "totals"]
    btts_price = None
    b = by_market.get("btts")
    if b and b.get("best_odds"):
        btts_price = (float(b["best_odds"]), str(b.get("best_book")))
    micro_opps = _micro_opportunities(anchor.id, matrix, totals, btts_price)

    reasons = _uncertainty_flags(market, model_view)
    if in_play:
        reasons = reasons + [{
            "key": "in_play",
            "label": "Match already under way: prices move continuously, "
                     "so this quote can be stale within seconds.",
        }]
    return {
        "match_id": anchor.id,
        "league": league,
        "home": home,
        "away": away,
        "commence_at": (anchor.commence_time.isoformat() if anchor.commence_time else None),
        "in_play": in_play,
        "market": market,
        "markets": opportunities,
        "model": model_view,
        "micro": micro,
        "micro_markets": micro_opps,
        "movement": None,
        "uncertainty": {
            "level": _uncertainty_level(reasons),
            "reasons": reasons,
        },
        "is_top_pick": False,
        "marquee": False,
        "locks": _locks(["micro_pack", "top_pick", "diamond_picks", "steam_radar"]),
    }



def _finalize(rows: list[dict[str, Any]], *, mode: str, disclaimer: str,
              day_label: Optional[str] = None) -> dict[str, Any]:
    """Sort, mark marquee/top-pick and package the board."""
    rows.sort(key=lambda r: (str(r.get("commence_at") or ""), r["match_id"]))

    from collections import Counter
    if day_label is None:
        day_counts = Counter(
            (_parse_dt(r["commence_at"]) or datetime.min).date().isoformat() for r in rows
        )
        day_label = day_counts.most_common(1)[0][0] if day_counts else None

    def _coverage(r: dict[str, Any]) -> tuple[int, str]:
        n = r["market"]["n_books"] if r["market"] else 0
        return (-n, str(r.get("commence_at") or ""))

    for r in sorted(rows, key=_coverage)[:3]:
        r["marquee"] = True

    candidates = [r for r in rows if r["market"] and r["market"]["p_top"] >= TOP_PICK_MIN_PROB]
    if candidates:
        max(candidates, key=lambda r: r["market"]["p_top"])["is_top_pick"] = True

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
        "disclaimer": disclaimer,
        "matches": rows,
    }


LIVE_DISCLAIMER = (
    "Live board: every fixture below comes from the odds feed right now. "
    "Probabilities are forecasts, not guarantees."
)

NO_LIVE_DATA = {
    "kind": "match_forecast_bulletin",
    "mode": "no_live_data",
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "day": None,
    "count": 0,
    "top_pick": None,
    "marquee_count": 0,
    "high_uncertainty_count": 0,
    "in_play_count": 0,
    "in_play": [],
    "disclaimer": (
        "No live fixture snapshot is available yet, so no board is shown. "
        "The odds poller has not completed a cycle, or the feed is out of quota."
    ),
    "matches": [],
}


#: The board is a short-horizon product: everything on it must be playable
#: within this window. Padding the board with fixtures weeks out to hit a
#: volume target is worse than showing fewer matches, so the cap is absolute.
FORECAST_HORIZON_HOURS = 24.0

#: Volume promise the board aims for inside the horizon. Reported honestly as a
#: shortfall when the day's real fixtures cannot supply it.
FORECAST_MIN_MATCHES = 12


def _group_by_fixture(matches: list[Match]) -> list[list[Match]]:
    """Group the parser's per-market ``Match`` objects back into fixtures.

    ``parse_odds_payload`` emits one ``Match`` per market, so a fixture priced
    on 1X2, handicap and total arrives as three objects sharing an id. Without
    this the board would list the same match three times and inflate its count.
    """
    grouped: dict[str, list[Match]] = {}
    order: list[str] = []
    for m in matches:
        key = f"{m.sport_key}:{m.id}"
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(m)
    # 1X2 first so the anchor row object is the one the top-level block uses.
    return [sorted(grouped[k], key=lambda m: (m.market != H2H, m.market)) for k in order]


def build_live_bulletin(matches: list[Match], *, max_matches: int = 40,
                        horizon_hours: float = FORECAST_HORIZON_HOURS,
                        min_matches: int = FORECAST_MIN_MATCHES,
                        include_micro_markets: bool = False,
                        include_in_play: bool = False,
                        now: Optional[datetime] = None) -> dict[str, Any]:
    """Forecast board for real fixtures inside the horizon window.

    ``horizon_hours`` is a hard ceiling: a fixture further out than that is
    excluded rather than used to pad the board to ``max_matches``.
    """
    now = now or datetime.now(timezone.utc)
    model = trained_model()

    window_end = now + timedelta(hours=horizon_hours)
    upcoming: list[list[Match]] = []
    in_play: list[list[Match]] = []
    for group in _group_by_fixture(matches):
        anchor = group[0]
        if anchor.commence_time is None:
            continue
        if anchor.commence_time <= now:
            in_play.append(group)     # already under way
        elif anchor.commence_time <= window_end:
            upcoming.append(group)

    upcoming.sort(key=lambda g: (g[0].commence_time, g[0].id))
    in_play.sort(key=lambda g: (g[0].commence_time, g[0].id))

    def _rows(groups: list[list[Match]], *, playing: bool) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for group in groups[:max_matches]:
            try:
                row = _row_from_matches(group, model, in_play=playing)
            except Exception:  # a single bad fixture must not blank the board
                continue
            if row is not None:
                out.append(row)
        return out

    rows = _rows(upcoming, playing=False)
    if include_in_play:
        live_rows = _rows(in_play, playing=True)
    else:
        # Without live polling the cached prices for a started match are the
        # pre-match quote, so presenting them as an in-play opportunity would be
        # showing a stale price as if it were tradeable now.
        live_rows = []

    if not rows and not live_rows:
        empty = dict(NO_LIVE_DATA)
        empty["horizon_hours"] = horizon_hours
        empty["window_end"] = window_end.isoformat()
        return empty

    board = _finalize(rows, mode="live", disclaimer=LIVE_DISCLAIMER)
    board["horizon_hours"] = horizon_hours
    board["window_end"] = window_end.isoformat()
    board["min_matches"] = min_matches
    board["shortfall"] = max(0, min_matches - len(rows))
    board["in_play_count"] = len(live_rows)
    board["in_play"] = live_rows
    if not include_micro_markets:
        # Priced micro bets need real totals lines, which cost extra credits.
        # Strip them rather than showing an edge with no price behind it.
        for row in rows + live_rows:
            row.pop("micro_markets", None)
    return board


def build_live_bulletin_from_payloads(payloads: list[tuple[str, list[dict[str, Any]]]],
                                      *, now: Optional[datetime] = None,
                                      max_matches: int = 40,
                                      horizon_hours: float = FORECAST_HORIZON_HOURS,
                                      min_matches: int = FORECAST_MIN_MATCHES,
                                      include_micro_markets: bool = False,
                                      include_in_play: bool = False,
                                      market_keys: Optional[Iterable[str]] = None
                                      ) -> dict[str, Any]:
    """Build the live board from cached raw odds payloads ``(sport_key, data)``."""
    from .parsing import parse_odds_payload

    now = now or datetime.now(timezone.utc)
    keys = tuple(market_keys) if market_keys else (H2H,)
    matches: list[Match] = []
    for sport_key, data in payloads:
        if not data:
            continue
        try:
            matches.extend(parse_odds_payload(data, market_keys=keys))
        except Exception:
            continue
    return build_live_bulletin(
        matches,
        max_matches=max_matches,
        horizon_hours=horizon_hours,
        min_matches=min_matches,
        include_micro_markets=include_micro_markets,
        include_in_play=include_in_play,
        now=now,
    )



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

    from collections import Counter
    day_counts = Counter(
        (_parse_dt(r["commence_at"]) or datetime.min).date().isoformat() for r in rows
    )
    day_label = day_counts.most_common(1)[0][0] if day_counts else None

    return _finalize(
        rows,
        mode=mode,
        day_label=day_label,
        disclaimer=(
            f"[{mode}] Demonstration board built from the real packaged archive. "
            "Probabilities use only information available before kickoff; they are "
            "forecasts, not guarantees. In production this exact schema is served "
            "from live odds."
        ),
    )


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