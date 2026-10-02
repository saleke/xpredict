"""Assemble the dashboard payload from real data only.

Every number served here is derived from the durable pick ledger, the live
cache written by the ingestion daemon, or the upstream quota headers. Nothing
is invented: when a value cannot be computed it is reported as ``None`` and
the UI renders an explicit "no data" state instead of a placeholder number.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

LIVE_ODDS_PREFIX = "live:odds:"
LIVE_SNAPSHOT_KEY = "live:odds_snapshot"
TRAPS_KEY = "live:traps"
PENDING_LIMIT = 50
SETTLED_LIMIT = 200
#: Books quoted per match in the pipeline explainer.
QUOTE_LIMIT = 4


def live_odds_key(sport_key: str) -> str:
    """Cache key holding the raw odds payload last seen for a league."""
    return f"{LIVE_ODDS_PREFIX}{sport_key}"


def _live_json(storage: Any, key: str) -> Optional[Any]:
    """Read a cached live value, even if its TTL has lapsed."""
    if storage is None:
        return None
    reader = getattr(storage, "get_live_stale", None)
    if not callable(reader):
        return None
    try:
        return reader(key)
    except Exception:
        return None


def _live_snapshot(storage: Any) -> Optional[dict]:
    """Last real upstream observation, even if its TTL has lapsed."""
    return _live_json(storage, LIVE_SNAPSHOT_KEY)


def _record_traps(storage: Any, limit: int = 10) -> list[dict]:
    """Recorded trap advisories, newest first, from the operations history."""
    raw = _live_json(storage, TRAPS_KEY)
    rows: list[dict] = []
    if isinstance(raw, dict):
        raw = raw.get("traps") or []
    if not isinstance(raw, list):
        return rows
    for row in raw:
        if not isinstance(row, dict):
            continue
        # A record is only useful with both fixtures and the metric behind it.
        if not row.get("home_team") or row.get("cv") is None:
            continue
        rows.append({
            "home_team": row.get("home_team"),
            "away_team": row.get("away_team"),
            "public_favorite": row.get("public_favorite"),
            "cv": row.get("cv"),
            "fair_odds": row.get("fair_odds"),
            "public_odds": row.get("public_odds"),
            "detected_at": row.get("detected_at"),
        })
    rows.sort(key=lambda r: str(r.get("detected_at") or ""), reverse=True)
    return rows[:limit]


def _quote_index(storage: Any, match_ids: set[str]) -> dict[str, dict]:
    """Real per-book prices and vig for the requested fixtures.

    Built from the raw payloads already in the live cache, so the explainer
    costs no upstream credit. A fixture that is not in the cache simply has no
    entry: the UI then says the quotes are unavailable.
    """
    if storage is None or not match_ids:
        return {}
    keys: list[str] = []
    scanner = getattr(storage, "scan_live_keys", None)
    if callable(scanner):
        try:
            keys = [k for k in scanner() if str(k).startswith(LIVE_ODDS_PREFIX)]
        except Exception:
            keys = []
    if not keys:
        return {}

    from .parsing import parse_odds_payload

    out: dict[str, dict] = {}
    for key in keys:
        entry = _live_json(storage, key)
        payload = entry.get("payload") if isinstance(entry, dict) else entry
        if not payload:
            continue
        try:
            matches = parse_odds_payload(payload)
        except Exception as exc:  # a malformed payload must not blank the board
            logger.warning("dashboard: cannot parse cached payload %s: %r", key, exc)
            continue
        for match in matches:
            if match.id not in match_ids or match.id in out:
                continue
            books = []
            margins: list[float] = []
            for book in match.bookmakers:
                prices = [
                    p for p in book.outcomes.values()
                    if isinstance(p, (int, float)) and p > 1
                ]
                if not prices:
                    continue
                if len(prices) > 1:
                    margins.append(sum(1.0 / p for p in prices) - 1.0)
                books.append({
                    "book_key": book.key,
                    "book_title": book.title,
                    "prices": dict(book.outcomes),
                    "margin": (sum(1.0 / p for p in prices) - 1.0) if len(prices) > 1 else None,
                })
            if not books:
                continue
            out[match.id] = {
                "home_team": match.home_team,
                "away_team": match.away_team,
                "sport_key": match.sport_key,
                "margin": (sum(margins) / len(margins)) if margins else None,
                "n_books": len(books),
                "books": books[:QUOTE_LIMIT],
            }
    return out


def _upcoming_matches(snapshot: Optional[dict]) -> list[dict]:
    if not snapshot:
        return []
    matches = snapshot.get("matches")
    return matches if isinstance(matches, list) else []


def _quota_state(settings: Any, remaining: Optional[int]) -> str:
    if remaining is None:
        return "unknown"
    warn = int(getattr(settings, "credit_warn", 100) or 100)
    stop = int(getattr(settings, "credit_stop", 20) or 20)
    if remaining <= stop:
        return "exhausted"
    if remaining <= warn:
        return "constrained"
    return "ok"


def _pick_row_to_card(p: dict) -> dict:
    """Presentation-only shaping of a ledger row. No values are invented."""
    best_odds = p.get("best_odds")
    fair_odds = p.get("fair_odds")
    ev = p.get("best_ev")
    commence = p.get("commence_time")

    # Freshness is a claim about the *price*, so it can only be made when a
    # price exists. The previous version used a bare `else`, which conflated
    # two very different situations:
    #
    #   1. the market price really did fall below fair value (decay), and
    #   2. there is no executable price at all (best_odds is NULL).
    #
    # Case 2 is not decay -- it is missing data. Asserting "Decayed /
    # Slippage" on a pick that was never priced invents a market observation
    # that was never made, which is exactly what this project must not do. In
    # this deployment 19 of 22 ledger rows have best_odds = NULL (a fair odds
    # was computed, but no book produced a price), so nearly every card
    # rendered as a confident-looking "Decayed / Slippage" claim about a
    # market that was never observed. Missing inputs are now surfaced as
    # UNPRICED and left visibly unpriced.
    if best_odds is None or fair_odds is None:
        freshness, badge, gauge = "UNPRICED", "cyan", "No Price Yet"
    elif best_odds > fair_odds:
        freshness, badge, gauge = "FRESH", "emerald", "Optimal Entry"
    elif abs(best_odds - fair_odds) < 0.01:
        freshness, badge, gauge = "FAIR", "amber", "Fair Value Entry"
    else:
        # best_odds < fair_odds: the price is genuinely below the model's fair
        # value, so the edge is gone. This is the only real decay case.
        freshness, badge, gauge = "DECAYED", "rose", "Decayed (Below Fair)"

    return {
        "dedupe_key": p.get("dedupe_key"),
        "match_id": p.get("match_id"),
        "sport_key": p.get("sport_key"),
        "home_team": p.get("home_team"),
        "away_team": p.get("away_team"),
        "commence_time": commence,
        "market": p.get("market"),
        "outcome_name": p.get("outcome_name"),
        "line": p.get("line"),
        "p_true": p.get("p_true"),
        "fair_odds": fair_odds,
        "best_book": p.get("best_book"),
        "best_odds": best_odds,
        "best_ev": ev,
        "n_books": p.get("n_books"),
        "stdev": p.get("stdev"),
        "cv": p.get("cv"),
        "conviction_score": p.get("conviction_score"),
        "recommended_stake_pct": p.get("recommended_stake_pct"),
        "recommended_units": p.get("recommended_units"),
        "freshness": freshness,
        "badge_color": badge,
        "gauge_text": gauge,
    }


def build_dashboard(
    storage: Any,
    settings: Any = None,
    *,
    pending_limit: int = PENDING_LIMIT,
    settled_limit: int = SETTLED_LIMIT,
) -> dict:
    """Build the dashboard payload from the ledger and live cache.

    ``summary`` fields are ``None`` until there is enough settled history to
    compute them honestly; they are never defaulted to a target number.
    """
    from . import config as cfg
    from .calibration import compute_clv_metrics, evaluate_calibration
    from .odds import utcnow

    settings = settings or cfg.load_settings()

    pending: list[dict] = []
    settled: list[dict] = []
    if storage is not None:
        try:
            pending = list(storage.list_pending_picks())[:pending_limit]
        except Exception:
            pending = []
        try:
            settled = list(storage.list_settled_picks())[:settled_limit]
        except Exception:
            settled = []

    cal_rep = evaluate_calibration(settled).to_dict() if settled else None
    clv_rep = compute_clv_metrics(settled).to_dict() if settled else None

    snapshot = _live_snapshot(storage)
    snapshot_at = None
    snapshot_age_sec = None
    if snapshot:
        raw_at = snapshot.get("observed_at")
        if isinstance(raw_at, (int, float)):
            snapshot_age_sec = max(0, int(time.time() - float(raw_at)))
            snapshot_at = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(raw_at)))
        else:
            snapshot_at = str(raw_at) if raw_at else None

    remaining = snapshot.get("credits_remaining") if snapshot else None
    matches = _upcoming_matches(snapshot)
    # The rollup counts what upstream actually returned; its ``matches`` list is
    # intentionally empty (the raw payloads are cached separately), so the
    # per-league tally is the only honest source for these two numbers.
    sport_rows = (snapshot or {}).get("sports") or {}
    observed_sports = sorted(
        {str(k) for k, v in sport_rows.items() if isinstance(v, dict) and not v.get("error")}
    )
    matches_observed = int((snapshot or {}).get("matches_observed") or 0)

    live_state = "live" if snapshot_age_sec is not None and snapshot_age_sec <= 300 else (
        "stale" if snapshot_age_sec is not None else "never"
    )

    counts = {
        "total": len(pending) + len(settled),
        "pending": len(pending),
        "settled": len(settled),
        "won": sum(1 for p in settled if p.get("result") == "WIN"),
        "lost": sum(1 for p in settled if p.get("result") == "LOSS"),
        "void": sum(1 for p in settled if p.get("result") == "VOID"),
    }
    if storage is not None and hasattr(storage, "count_picks"):
        try:
            counts = storage.count_picks()
        except Exception:
            pass

    traps = _record_traps(storage)
    quotes = _quote_index(
        storage, {str(p.get("match_id")) for p in pending if p.get("match_id")})
    cards = []
    for row in pending:
        card = _pick_row_to_card(row)
        card["quotes"] = quotes.get(str(row.get("match_id")))
        cards.append(card)

    return {
        "meta": {
            "generated_at": utcnow().isoformat(),
            "sports_scope": list(getattr(settings, "sports", ()) or ()),
            "data_provenance": {
                "synthetic": False,
                "source": "live_odds_api" if live_state == "live" else (
                    "ledger_and_last_observation" if live_state == "stale" else "ledger_only"
                ),
                "statement": (
                    "Every figure is computed from the pick ledger and the odds "
                    "feed. Values with insufficient history are shown as n/a."
                ),
            },
        },
        "summary": {
            "active_picks_count": counts.get("pending", len(pending)),
            "settled_picks_count": counts.get("settled", len(settled)),
            "total_picks_count": counts.get("total", len(pending) + len(settled)),
            "total_matches_evaluated": int((snapshot or {}).get("matches_observed") or 0),
            "won_count": counts.get("won"),
            "lost_count": counts.get("lost"),
            "win_rate": cal_rep.get("win_rate") if cal_rep else None,
            "brier_score": cal_rep.get("brier_score") if cal_rep else None,
            "ece": cal_rep.get("ece") if cal_rep else None,
            "mean_clv": clv_rep.get("mean_clv") if clv_rep else None,
            "positive_clv_share": clv_rep.get("positive_clv_share") if clv_rep else None,
        },
        "live": {
            "state": live_state,
            "observed_at": snapshot_at,
            "age_sec": snapshot_age_sec,
            "sports_observed": observed_sports,
            "matches_observed": matches_observed,
            "credits_remaining": remaining,
            "quota_state": _quota_state(settings, remaining),
            "last_error": (snapshot or {}).get("last_error"),
        },
        "active_picks": cards,
        "traps": traps,
        "settled_ledger": settled,
        "calibration": cal_rep,
        "clv": clv_rep,
    }
