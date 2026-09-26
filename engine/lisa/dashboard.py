"""Assemble the dashboard payload from real data only.

Every number served here is derived from the durable pick ledger, the live
cache written by the ingestion daemon, or the upstream quota headers. Nothing
is invented: when a value cannot be computed it is reported as ``None`` and
the UI renders an explicit "no data" state instead of a placeholder number.
"""

from __future__ import annotations

import time
from typing import Any, Optional

LIVE_ODDS_PREFIX = "live:odds:"
LIVE_SNAPSHOT_KEY = "live:odds_snapshot"
PENDING_LIMIT = 50
SETTLED_LIMIT = 200


def live_odds_key(sport_key: str) -> str:
    """Cache key holding the raw odds payload last seen for a league."""
    return f"{LIVE_ODDS_PREFIX}{sport_key}"


def _live_snapshot(storage: Any) -> Optional[dict]:
    """Last real upstream observation, even if its TTL has lapsed."""
    if storage is None:
        return None
    reader = getattr(storage, "get_live_stale", None)
    if not callable(reader):
        return None
    try:
        return reader(LIVE_SNAPSHOT_KEY)
    except Exception:
        return None


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

    if best_odds is not None and fair_odds is not None and best_odds > fair_odds:
        freshness, badge, gauge = "FRESH", "emerald", "Optimal Entry"
    elif best_odds is not None and fair_odds is not None and abs(best_odds - fair_odds) < 0.01:
        freshness, badge, gauge = "FAIR", "amber", "Fair Value Entry"
    else:
        freshness, badge, gauge = "SLIPPED", "rose", "Decayed / Slippage"

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
    observed_sports = sorted(
        {str(m.get("sport_key")) for m in matches if m.get("sport_key")}
    )

    live_state = "live" if snapshot_age_sec is not None and snapshot_age_sec <= 300 else (
        "stale" if snapshot_age_sec is not None else "never"
    )

    counts = {"total": 0, "pending": len(pending), "settled": 0, "won": 0, "lost": 0, "void": 0}
    if storage is not None and hasattr(storage, "count_picks"):
        try:
            counts = storage.count_picks()
        except Exception:
            pass

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
            "matches_observed": len(matches),
            "credits_remaining": remaining,
            "quota_state": _quota_state(settings, remaining),
            "last_error": (snapshot or {}).get("last_error"),
        },
        "active_picks": [_pick_row_to_card(p) for p in pending],
        "settled_ledger": settled,
        "calibration": cal_rep,
        "clv": clv_rep,
    }
