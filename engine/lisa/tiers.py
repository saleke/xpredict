"""Subscription tier catalog: what each tier unlocks, when, and why upgrade.

This module is the single source of truth for the product's entitlement model.
The web pricing matrix, the ``lisa tiers`` CLI, the ``/api/tiers`` endpoint and
any dispatch policy all read from here, so the paid promise can never drift
from what the engine actually enforces.

Value ladder (honest and defensible):
    * We never sell "guaranteed wins". Higher tiers buy EARLIER reveals,
      BROADER markets, and DEEPER analysis — the only honest levers.
    * ``minutes_before_kickoff`` per feature encodes the reveal ladder:
      a larger offset means the tier sees the signal earlier in the run-up
      to kickoff (e.g. 360 min = roughly 6h early).
"""
from __future__ import annotations

from typing import Any, Optional

VALID_TIERS = ("free", "tier1", "tier2", "tier3")

TIER_RANK = {"free": 0, "tier1": 1, "tier2": 2, "tier3": 3}

TIER_LABELS = {
    "free": "Free",
    "tier1": "Tier 1 · Sharp Starter",
    "tier2": "Tier 2 · Pro Trader",
    "tier3": "Tier 3 · VIP Syndicate",
}

# feature -> spec. ``grant`` is the minimum tier that may see the feature at
# all; ``reveal_minutes`` is minutes before kickoff when each eligible tier
# receives it (larger = earlier = more valuable). A key already present on a
# lower tier does not appear in the lists of higher-tier-owned features alone.
FEATURES: dict[str, dict[str, Any]] = {
    "bulletin": {
        "label": "Daily Match Forecast Board",
        "grant": "free",
        "reveal_minutes": {"free": 0},
        "blurb": "Probability forecasts for every fixture of the day — 1X2, "
                 "BTTS, over/under and most-likely scoreline, plus popular-game "
                 "highlighting and honest uncertainty flags.",
        "upgrade_hint": "Free users get the full board. The forecasts that "
                        "cross real confidence gates are staked picks reserved "
                        "for paid tiers.",
    },
    "micro_pack": {
        "label": "Micro Forecast Pack (BTTS / O/U / scoreline)",
        "grant": "tier1",
        "reveal_minutes": {"tier1": 120, "tier2": 240, "tier3": 360},
        "blurb": "Per-match BTTS yes/no, over/under 2.5 and the most likely "
                 "scorelines from the Poisson model — high-volume forecasts "
                 "for the daily board.",
        "upgrade_hint": "Tier 3 sees the micro pack two hours earlier than "
                        "Tier 1, when the edge is freshest.",
    },
    "top_pick": {
        "label": "Pick of the Day",
        "grant": "tier1",
        "reveal_minutes": {"tier1": 120, "tier2": 240, "tier3": 360},
        "blurb": "The single highest-confidence forecast of the day, publicly "
                 "explained with its supporting data.",
        "upgrade_hint": "Higher tiers receive the Pick hours earlier; free "
                        "users only ever see it after the matchday settles.",
    },
    "traps": {
        "label": "Trap & Sucker-Bet Avoidance Warnings",
        "grant": "tier1",
        "reveal_minutes": {"tier1": 60, "tier2": 120, "tier3": 240},
        "blurb": "Flags odds that look juicy but where the model and the "
                 "market disagree — the bets bookmakers want you to place.",
        "upgrade_hint": "Avoidance signals arrive earliest on the VIP tier.",
    },
    "booking_codes": {
        "label": "6-Bookmaker Booking Codes",
        "grant": "tier1",
        "reveal_minutes": {"tier1": 0, "tier2": 0, "tier3": 0},
        "blurb": "Ready-to-place booking/reference codes across six books for "
                 "the day's picks.",
        "upgrade_hint": "",
    },
    "diamond_picks": {
        "label": "Certainty-Gated Diamond Picks",
        "grant": "tier2",
        "reveal_minutes": {"tier2": 180, "tier3": 360},
        "blurb": "Only forecasts that clear strict confidence gates, with "
                 "stake sizing advice. Rarity is the point — rarely, never "
                 "daily.",
        "upgrade_hint": "Tier 3 gets the diamonds the moment they clear the "
                        "gate; Tier 2 waits until three hours before kickoff.",
    },
    "ah_ou_picks": {
        "label": "Asian Handicap & Over/Under Picks",
        "grant": "tier2",
        "reveal_minutes": {"tier2": 120, "tier3": 240},
        "blurb": "Handicap and totals markets graded on real spreading rules "
                 "(quarter-ball half win/loss, void pushes).",
        "upgrade_hint": "Breadth markets that casual forecasters cannot reach.",
    },
    "steam_radar": {
        "label": "Line-Movement & Steam Radar",
        "grant": "tier2",
        "reveal_minutes": {"tier2": 60, "tier3": 180},
        "blurb": "Real-time alerts when odds move hard toward a side — the "
                 "sharpest, hardest-to-arbitrage signal in betting.",
        "upgrade_hint": "The steam radar fires minutes-to-hours before kickoff; "
                        "early access is what makes it actionable.",
    },
    "parlay": {
        "label": "Algorithmic Parlay Builder",
        "grant": "tier2",
        "reveal_minutes": {"tier2": 0, "tier3": 0},
        "blurb": "Correlated, size-gated multi-leg combos computed from the "
                 "day's forecast board.",
        "upgrade_hint": "",
    },
    "clv_stats": {
        "label": "CLV & Calibration Analytics",
        "grant": "tier2",
        "reveal_minutes": {"tier2": 0, "tier3": 0},
        "blurb": "Your closed-price value (CLV), model Brier score, calibration "
                 "ECC and the full audited ledger under your own account.",
        "upgrade_hint": "",
    },
    "api_feed": {
        "label": "Direct REST API & Webhook Feed",
        "grant": "tier3",
        "reveal_minutes": {"tier3": 0},
        "blurb": "Machine-readable forecast board with a personal API key and "
                 "outbound webhooks for automation.",
        "upgrade_hint": "Automatically consume every signal before it hits the "
                        "public channel.",
    },
    "portfolio": {
        "label": "Portfolio Correlation & Covariance Engine",
        "grant": "tier3",
        "reveal_minutes": {"tier3": 0},
        "blurb": "See how today's slate overlaps so a correlated day cannot "
                 "wipe you out.",
        "upgrade_hint": "The syndicate layer: staking that survives real "
                        "variance.",
    },
    "arbitrage_stream": {
        "label": "Soft-Book Arbitrage Stream",
        "grant": "tier3",
        "reveal_minutes": {"tier3": 0},
        "blurb": "Cross-book price discordance stream for soft books, "
                 "monitored per match.",
        "upgrade_hint": "Institutional-only depth.",
    },
    "early_bird": {
        "label": "Earliest Alerts & Weekly Audit PDF",
        "grant": "tier3",
        "reveal_minutes": {"tier3": 0},
        "blurb": "Every signal lands earliest on Tier 3, plus a signed weekly "
                 "audit PDF of every pick and settlement under your account.",
        "upgrade_hint": "The exact edge the platform studies: earliness. This "
                        "tier is the reason everything else exists.",
    },
}


def clean_tier(tier: str) -> str:
    """Normalise a tier string to one of ``VALID_TIERS`` (fallback 'free')."""
    t = (tier or "").strip().lower().replace(" ", "")
    return t if t in VALID_TIERS else "free"


def tier_rank(tier: str) -> int:
    return TIER_RANK.get(clean_tier(tier), 0)


def feature_keys() -> list[str]:
    return sorted(FEATURES.keys())


def feature_label(key: str) -> str:
    spec = FEATURES.get(key)
    return spec["label"] if spec else key


def grant_tier(key: str) -> str:
    """Minimum tier that unlocks ``key``."""
    spec = FEATURES.get(key)
    return spec["grant"] if spec else "free"


def entitled(tier: str, feature: str) -> bool:
    """True when the given tier may see ``feature`` at all."""
    t = clean_tier(tier)
    return tier_rank(t) >= tier_rank(grant_tier(feature))


def features_for(tier: str) -> list[str]:
    t = clean_tier(tier)
    return [k for k in feature_keys() if entitled(t, k)]


def reveal_minutes(feature: str, tier: str) -> Optional[int]:
    """Minutes before kickoff the tier receives ``feature``.

    Entitled tiers listed in ``reveal_minutes`` use their own offset; entitled
    tiers without one (e.g. bulletin is shared by every tier) receive it
    immediately (``0``). ``None`` means the tier never sees it.
    """
    spec = FEATURES.get(feature)
    if not spec or not entitled(tier, feature):
        return None
    t = clean_tier(tier)
    return spec["reveal_minutes"].get(t, 0)


def features_gained_by_upgrade(tier: str) -> list[str]:
    """Features newly unlocked when moving from ``tier`` to the next tier."""
    t = clean_tier(tier)
    rank = tier_rank(t)
    next_rank = rank + 1
    if next_rank >= len(VALID_TIERS):
        return []
    next_tier = VALID_TIERS[next_rank]
    return [k for k in feature_keys()
            if entitled(next_tier, k) and not entitled(t, k)]


def upgrade_path() -> dict[str, Any]:
    """Whole value ladder as a serialisable dict (for the CLI and web)."""
    return {
        "features": [
            {
                "key": k,
                "label": FEATURES[k]["label"],
                "blurb": FEATURES[k]["blurb"],
                "upgrade_hint": FEATURES[k]["upgrade_hint"],
                "grant": FEATURES[k]["grant"],
                "reveal_minutes": dict(FEATURES[k]["reveal_minutes"]),
            }
            for k in feature_keys()
        ],
        "tiers": {t: TIER_LABELS[t] for t in VALID_TIERS},
        "ladder": {
            t: {
                "unlocked": features_for(t),
                "new_vs_previous": features_gained_by_upgrade(
                    VALID_TIERS[max(0, TIER_RANK[t] - 1)]
                ) if TIER_RANK[t] > 0 else features_for("free"),
            }
            for t in VALID_TIERS
        },
    }


def format_tiers() -> str:
    """Human-readable tier matrix for ``lisa tiers``."""
    lines: list[str] = []
    w = 96
    lines.append(" LISA SUBSCRIPTION VALUE LADDER ".center(w, "="))
    lines.append("=" * w)
    lines.append("  " + "Feature".ljust(42) + "Free  T1    T2    T3    Earliest reveal")
    lines.append("-" * w)
    for key in feature_keys():
        spec = FEATURES[key]
        cells = []
        for t in VALID_TIERS:
            cells.append(" ✔   " if entitled(t, key) else " ·   ")
        reveal = spec["reveal_minutes"]
        earliest_tier = max(reveal, key=lambda t: reveal[t]) if reveal else None
        earliest = (f"{reveal[earliest_tier]} min pre-KO ({earliest_tier})"
                    if earliest_tier else "-")
        lines.append("  " + spec["label"][:40].ljust(42) + "".join(cells) + " " + earliest)
    lines.append("-" * w)
    lines.append("  WHY UPGRADE — what each step newly unlocks:")
    for t in VALID_TIERS:
        if TIER_RANK[t] == 0:
            continue
        gained = features_gained_by_upgrade(VALID_TIERS[TIER_RANK[t] - 1])
        labels = ", ".join(FEATURES[k]["label"] for k in gained) or "(nothing new)"
        lines.append(f"    {TIER_LABELS[t]:<28} {labels}")
    lines.append("-" * w)
    lines.append("  The ladder sells EARLINESS + BREADTH + DEPTH — never promised wins.")
    lines.append("  Highest tier sees every signal first (up to 360 min before kickoff).")
    return "\n".join(lines)


def lock_state(tier: str, feature: str) -> dict[str, Any]:
    """Describe what a user on ``tier`` sees for ``feature``.

    Returns ``unlocked`` plus a teaser (locked but visible, with the required
    tier and the time advantage of upgrading).
    """
    t = clean_tier(tier)
    if entitled(t, feature):
        return {
            "unlocked": True,
            "required_tier": grant_tier(feature),
            "reveal_minutes_before_kickoff": reveal_minutes(feature, t),
            "teaser": "",
        }
    required = grant_tier(feature)
    ahead = reveal_minutes(feature, required) or 0
    return {
        "unlocked": False,
        "required_tier": required,
        "reveal_minutes_before_kickoff": None,
        "teaser": (
            f"🔒 Unlocks on {TIER_LABELS[required]} — sees this "
            f"{ahead} min before kickoff." if ahead else
            f"🔒 Unlocks on {TIER_LABELS[required]}."
        ),
    }