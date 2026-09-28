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
from .history import HISTORICAL_ODDS
from .micro import build_micro_markets, is_goals_sport
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
    # Only compare market vs model when the model actually has a view. A
    # not-ready model (or a sport with no model at all) must not manufacture a
    # "model disagreement" flag out of a None probability.
    model_ps = {o: model.get(f"p_{o}") for o in ("home", "draw", "away")}
    model_ready = bool(model.get("ready")) and all(
        isinstance(v, (int, float)) for v in model_ps.values())
    if model_ready:
        model_top = max(model_ps, key=lambda o: float(model_ps[o]))
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
    else:
        # No independent model available for this sport/fixture: say so once.
        reasons.append({
            "key": "low_model_history",
            "label": "No independent model view for this fixture — market consensus only.",
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

    # Sport-aware independent model. Only soccer has the Poisson goal model;
    # every other sport gets an honest blank rather than football-shaped
    # numbers (expected "goals", BTTS, scorelines) the model cannot produce.
    if is_goals_sport(league):
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
    else:
        matrix, pred = {}, {"model_ready": False}
        model_view = {
            "p_home": None,
            "p_draw": None,
            "p_away": None,
            "ready": False,
        }
        micro = {
            "p_btts": None,
            "p_over_2_5": None,
            "expected_goals_home": None,
            "expected_goals_away": None,
            "most_likely_scores": [],
        }

    micro_opps = build_micro_markets(
        anchor, opportunities, model,
        include_model_markets=True,
    )

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



def _win_score(r: dict[str, Any]) -> Optional[float]:
    """Confidence-weighted win score for a forecast row, or None when unrated.

    ``p_top`` alone is not the whole story: a 0.80 favourite quoted by two
    books that disagree is a worse *win opportunity* than a 0.70 favourite
    over ten books at near-zero dispersion. The score is the market's true
    probability discounted by how fragile that probability is:
      * high uncertainty  -> -15%
      * medium uncertainty -> -5%
      * thin market (<5 books) -> -3%, <3 books -> -10%
    The result is a relative ranking key, not an absolute calibrated
    probability, and is always reported alongside ``p_top`` so it cannot
    masquerade as one.
    """
    market = r.get("market")
    if not market or not isinstance(market.get("p_top"), (int, float)):
        return None
    p = market["p_top"]
    if p <= 0.0 or p >= 1.0:
        return None
    level = (r.get("uncertainty") or {}).get("level")
    solidity = 1.0
    if level == "high":
        solidity *= 0.85
    elif level == "medium":
        solidity *= 0.95
    n = market.get("n_books") or 0
    if n < 3:
        solidity *= 0.90
    elif n < 5:
        solidity *= 0.97
    return round(p * solidity, 4)


def _best_earning_market(r: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The best real, *priced*, positive-EV quote for this fixture.

    Scans the headline market and every micro market. Only quotes backed by a
    real book count (``best_odds`` present); model-derived rows with no price
    behind them are views, not earners, and can never win this ranking. Returns
    None when nothing on the fixture is both priced and positive-EV — an honest
    \"no earner here\" rather than a manufactured pick.
    """
    best: Optional[dict[str, Any]] = None

    def _consider(market_name: str, outcome_name: Any, p: Any,
                  best_odds: Any, best_book: Any, fair_odds: Any,
                  ev: Any) -> None:
        nonlocal best
        try:
            p_f = float(p)
            ev_f = float(ev)
            odds_f = float(best_odds)
        except (TypeError, ValueError):
            return
        if p_f <= 0 or ev_f <= 0 or odds_f <= 1.0:
            return
        entry = {
            "market": market_name,
            "outcome_name": str(outcome_name),
            "p": round(p_f, 4),
            "best_odds": round(odds_f, 3),
            "best_book": str(best_book or ""),
            "fair_odds": round(float(fair_odds), 3) if fair_odds else None,
            "ev": round(ev_f, 4),
        }
        if best is None or ev_f > best["ev"]:
            best = entry

    market = r.get("market")
    if market and isinstance(market.get("best_odds"), (int, float)):
        _consider(
            market.get("market", "h2h"),
            market.get("top_outcome"),
            market.get("p_top"),
            market.get("best_odds"),
            market.get("best_book"),
            market.get("fair_odds"),
            market.get("ev"),
        )
    for mm in r.get("micro_markets") or []:
        if not mm.get("priced"):
            continue
        _consider(
            mm.get("market", ""),
            mm.get("outcome") or mm.get("side"),
            mm.get("p"),
            mm.get("best_odds"),
            mm.get("best_book"),
            mm.get("fair_odds"),
            mm.get("ev"),
        )
    return best


def _finalize(rows: list[dict[str, Any]], *, mode: str, disclaimer: str,
              day_label: Optional[str] = None) -> dict[str, Any]:
    """Sort, mark marquee/top-pick, rank win vs earn, package the board."""
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

    # -- dual ranking: best WIN opportunity vs best EARN opportunity ---------
    # A "win opportunity" is the most reliable probability on the board; an
    # "earn opportunity" is the best *priced, positive-EV* quote, across every
    # market (moneyline, handicap, over/under, model micro) of every fixture.
    # Ranking the same board two ways is honest because the two answers are
    # normally different: the safest winner rarely pays enough to earn.
    for r in rows:
        r["win_score"] = _win_score(r)
        r["best_earning_market"] = _best_earning_market(r)

    win_candidates = [r for r in rows
                      if r["win_score"] is not None
                      and r["win_score"] >= TOP_PICK_MIN_PROB]
    earn_candidates = [r for r in rows if r["best_earning_market"] is not None]

    best_win: Optional[dict[str, Any]] = None
    if win_candidates:
        top = max(win_candidates, key=lambda r: float(r["win_score"]))
        top["is_top_pick"] = True
        top["best_win"] = True
        best_win = {
            "match_id": top["match_id"],
            "home": top.get("home"),
            "away": top.get("away"),
            "league": top.get("league"),
            "outcome_name": top["market"]["top_outcome"],
            "p_top": top["market"]["p_top"],
            "win_score": round(float(top["win_score"]), 4),
            "n_books": top["market"]["n_books"],
            "commence_at": top.get("commence_at"),
        }

    best_earning: Optional[dict[str, Any]] = None
    if earn_candidates:
        top = max(earn_candidates,
                  key=lambda r: float(r["best_earning_market"]["ev"]))
        top["best_earning"] = True
        m = top["best_earning_market"]
        best_earning = {
            "match_id": top["match_id"],
            "home": top.get("home"),
            "away": top.get("away"),
            "league": top.get("league"),
            "market": m["market"],
            "outcome_name": m["outcome_name"],
            "p": m["p"],
            "best_odds": m["best_odds"],
            "best_book": m["best_book"],
            "fair_odds": m["fair_odds"],
            "ev": m["ev"],
            "commence_at": top.get("commence_at"),
        }

    return {
        "kind": "match_forecast_bulletin",
        "mode": mode,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "day": day_label,
        "count": len(rows),
        # Back-compat alias: the UI historically highlighted ``top_pick`` to
        # mean the highest-conviction pick. It now means the best WIN
        # opportunity; the board additionally ranks the best EARN opportunity
        # separately, because they are not the same match.
        "top_pick": best_win["match_id"] if best_win else None,
        "best_win": best_win,
        "best_earning": best_earning,
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
    "best_win": None,
    "best_earning": None,
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


#: The board is a short-horizon product (the LISA promise is 48h): everything on
#: it must be playable within this window. Padding the board with fixtures weeks
#: out to hit a volume target is worse than showing fewer matches, so the cap is
#: absolute.
FORECAST_HORIZON_HOURS = 48.0

#: Volume promise the board aims for inside the horizon. Reported honestly as a
#: shortfall when the day's real fixtures cannot supply it.
FORECAST_MIN_MATCHES = 12


def _filter_micro_markets(micro_list: list[dict[str, Any]], *,
                          include_micro_markets: bool,
                          include_micro_predictions: bool) -> list[dict[str, Any]]:
    """Keep only the micro rows the enabled product surfaces may show.

    ``include_micro_markets`` unlocks the real-book (priced) markets, which cost
    extra credits at fetch time and are therefore off by default.
    ``include_micro_predictions`` unlocks the model-derived rows, which are free
    because they reuse the Poisson matrix on already-polled fixtures. A row is
    only ever kept when the surface that funds it is switched on.
    """
    if include_micro_markets:
        return micro_list
    out: list[dict[str, Any]] = []
    for m in micro_list:
        if m.get("market") in ("model_totals", "model_btts"):
            if include_micro_predictions:
                out.append(m)
        elif m.get("market") == "h2h":
            continue    # already the row's headline market; do not duplicate it
        else:
            out.append(m)
    return out


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
                        include_micro_predictions: bool = False,
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
        # Rows that require a paid market (spreads/totals/btts) or that are
        # model-only are stripped unless their surface is switched on. h2h is
        # always polled, so it is always retained.
        for row in rows + live_rows:
            filtered = _filter_micro_markets(
                row.get("micro_markets") or [],
                include_micro_markets=False,
                include_micro_predictions=include_micro_predictions,
            )
            if filtered:
                row["micro_markets"] = filtered
            else:
                row.pop("micro_markets", None)
    return board


def build_live_bulletin_from_payloads(payloads: list[tuple[str, list[dict[str, Any]]]],
                                      *, now: Optional[datetime] = None,
                                      max_matches: int = 40,
                                      horizon_hours: float = FORECAST_HORIZON_HOURS,
                                      min_matches: int = FORECAST_MIN_MATCHES,
                                      include_micro_markets: bool = False,
                                      include_micro_predictions: bool = False,
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
        include_micro_predictions=include_micro_predictions,
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