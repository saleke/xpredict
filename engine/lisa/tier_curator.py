"""Tier Curator — per-tier pick selection logic.

Selects the best picks for each subscription tier based on:
- Odds range (Tier 1: 1.50-2.00, Tier 2: 2.00-30, Tier 3: 2.00-100+)
- Conviction score (model confidence)
- Weekly/monthly quotas

The curator ensures no duplicate picks across tiers and respects
the tier-specific rules defined in tiers.py.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import tiers as tier_config
from .gate import Pick
from .odds import utcnow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TierRule:
    """Rules for a single tier's pick selection."""
    tier: str
    min_odds: float
    max_odds: float
    picks_per_week: int
    min_conviction: float = 0.0
    description: str = ""


# Tier rules based on the product spec
TIER_RULES: dict[str, TierRule] = {
    "tier1": TierRule(
        tier="tier1",
        min_odds=1.50,
        max_odds=2.00,
        picks_per_week=4,
        min_conviction=0.5,
        description="Safe picks: 1.50-2.00 odds, 4 per week",
    ),
    "tier2": TierRule(
        tier="tier2",
        min_odds=2.00,
        max_odds=30.00,
        picks_per_week=8,
        min_conviction=0.6,
        description="Best picks: 2.00-30.00 odds, 8 per week",
    ),
    "tier3": TierRule(
        tier="tier3",
        min_odds=2.00,
        max_odds=100.00,
        picks_per_week=12,
        min_conviction=0.7,
        description="All picks: 2.00-100.00 odds, 12 per week + longshot",
    ),
}


@dataclass
class CuratedPick:
    """A pick that has been selected for a specific tier."""
    pick: Pick
    tier: str
    conviction_score: float
    fair_odds: float
    best_odds: float
    selected_at: datetime
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "match_id": self.pick.match_id,
            "sport_key": self.pick.sport_key,
            "home_team": self.pick.home_team,
            "away_team": self.pick.away_team,
            "outcome_name": self.pick.outcome_name,
            "market": self.pick.market,
            "tier": self.tier,
            "conviction_score": self.conviction_score,
            "fair_odds": self.fair_odds,
            "best_odds": self.best_odds,
            "p_true": self.pick.p_true,
            "selected_at": self.selected_at.isoformat(),
            "reason": self.reason,
        }


@dataclass
class TierCuratorResult:
    """Result of tier curation for a set of picks."""
    tier1_picks: list[CuratedPick] = field(default_factory=list)
    tier2_picks: list[CuratedPick] = field(default_factory=list)
    tier3_picks: list[CuratedPick] = field(default_factory=list)
    suppressed: list[tuple[str, str]] = field(default_factory=list)  # (match_id, reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier1": [p.to_dict() for p in self.tier1_picks],
            "tier2": [p.to_dict() for p in self.tier2_picks],
            "tier3": [p.to_dict() for p in self.tier3_picks],
            "suppressed": self.suppressed,
        }


class TierCurator:
    """Selects the best picks for each subscription tier.

    The curator:
    1. Filters picks by odds range for each tier
    2. Ranks by conviction score (model confidence)
    3. Selects the best N picks per week per tier
    4. Ensures no duplicate picks across tiers
    """

    def __init__(self, *, rules: Optional[dict[str, TierRule]] = None):
        self.rules = rules or TIER_RULES

    def _get_pick_odds(self, pick: Pick) -> float:
        """Get the best available odds for a pick."""
        if pick.best_execution:
            return pick.best_execution.odds
        return pick.fair_odds

    def _get_conviction(self, pick: Pick) -> float:
        """Get the conviction score for a pick."""
        return getattr(pick, "conviction_score", 0.0)

    def _filter_by_odds(self, picks: list[Pick], rule: TierRule) -> list[Pick]:
        """Filter picks by odds range for a tier."""
        filtered = []
        for pick in picks:
            odds = self._get_pick_odds(pick)
            if rule.min_odds <= odds <= rule.max_odds:
                filtered.append(pick)
        return filtered

    def _filter_by_conviction(self, picks: list[Pick], rule: TierRule) -> list[Pick]:
        """Filter picks by minimum conviction score."""
        filtered = []
        for pick in picks:
            conviction = self._get_conviction(pick)
            if conviction >= rule.min_conviction:
                filtered.append(pick)
        return filtered

    def _rank_by_conviction(self, picks: list[Pick]) -> list[Pick]:
        """Rank picks by conviction score (highest first)."""
        return sorted(picks, key=lambda p: self._get_conviction(p), reverse=True)

    def _deduplicate(self, *pick_lists: list[Pick]) -> tuple[list[Pick], list[tuple[str, str]]]:
        """Remove duplicate picks across tiers (keep highest tier)."""
        seen: dict[str, Pick] = {}
        suppressed: list[tuple[str, str]] = []

        # Process from highest tier to lowest (tier3 > tier2 > tier1)
        for picks in reversed(pick_lists):
            for pick in picks:
                key = f"{pick.match_id}::{pick.market}::{pick.outcome_name}"
                if key in seen:
                    suppressed.append((pick.match_id, "duplicate across tiers"))
                else:
                    seen[key] = pick

        return list(seen.values()), suppressed

    def curate(self, picks: list[Pick], *, now: Optional[datetime] = None) -> TierCuratorResult:
        """Curate picks for all tiers.

        Args:
            picks: List of picks from the pipeline
            now: Reference time (defaults to utcnow())

        Returns:
            TierCuratorResult with picks selected for each tier
        """
        now = now or utcnow()
        result = TierCuratorResult()

        # Track which picks have been assigned to a tier
        assigned: set[str] = set()

        # Process tiers from highest to lowest (tier3 gets first pick)
        for tier_name in ("tier3", "tier2", "tier1"):
            rule = self.rules.get(tier_name)
            if not rule:
                continue

            # Filter by odds range
            candidates = self._filter_by_odds(picks, rule)

            # Filter by conviction
            candidates = self._filter_by_conviction(candidates, rule)

            # Remove already assigned picks
            candidates = [
                p for p in candidates
                if f"{p.match_id}::{p.market}::{p.outcome_name}" not in assigned
            ]

            # Rank by conviction
            candidates = self._rank_by_conviction(candidates)

            # Select top N picks
            selected = candidates[: rule.picks_per_week]

            # Mark as assigned
            for pick in selected:
                assigned.add(f"{pick.match_id}::{pick.market}::{pick.outcome_name}")

            # Create curated picks
            for pick in selected:
                curated = CuratedPick(
                    pick=pick,
                    tier=tier_name,
                    conviction_score=self._get_conviction(pick),
                    fair_odds=pick.fair_odds,
                    best_odds=self._get_pick_odds(pick),
                    selected_at=now,
                    reason=f"Selected for {tier_name}: odds {self._get_pick_odds(pick):.2f}, conviction {self._get_conviction(pick):.2f}",
                )
                if tier_name == "tier1":
                    result.tier1_picks.append(curated)
                elif tier_name == "tier2":
                    result.tier2_picks.append(curated)
                elif tier_name == "tier3":
                    result.tier3_picks.append(curated)

        # Track suppressed picks
        for pick in picks:
            key = f"{pick.match_id}::{pick.market}::{pick.outcome_name}"
            if key not in assigned:
                result.suppressed.append((pick.match_id, "not selected for any tier"))

        return result

    def curate_for_tier(self, picks: list[Pick], tier: str, *,
                        now: Optional[datetime] = None) -> list[CuratedPick]:
        """Curate picks for a single tier.

        Args:
            picks: List of picks from the pipeline
            tier: Tier name ("tier1", "tier2", "tier3")
            now: Reference time

        Returns:
            List of curated picks for the specified tier
        """
        now = now or utcnow()
        rule = self.rules.get(tier)
        if not rule:
            return []

        # Filter by odds range
        candidates = self._filter_by_odds(picks, rule)

        # Filter by conviction
        candidates = self._filter_by_conviction(candidates, rule)

        # Rank by conviction
        candidates = self._rank_by_conviction(candidates)

        # Select top N picks
        selected = candidates[: rule.picks_per_week]

        # Create curated picks
        curated = []
        for pick in selected:
            curated.append(CuratedPick(
                pick=pick,
                tier=tier,
                conviction_score=self._get_conviction(pick),
                fair_odds=pick.fair_odds,
                best_odds=self._get_pick_odds(pick),
                selected_at=now,
                reason=f"Selected for {tier}: odds {self._get_pick_odds(pick):.2f}, conviction {self._get_conviction(pick):.2f}",
            ))

        return curated

    def get_tier_summary(self, result: TierCuratorResult) -> dict[str, Any]:
        """Get a summary of the curation result."""
        return {
            "tier1": {
                "count": len(result.tier1_picks),
                "avg_conviction": round(sum(p.conviction_score for p in result.tier1_picks) / max(1, len(result.tier1_picks)), 3),
                "avg_odds": round(sum(p.best_odds for p in result.tier1_picks) / max(1, len(result.tier1_picks)), 2),
            },
            "tier2": {
                "count": len(result.tier2_picks),
                "avg_conviction": round(sum(p.conviction_score for p in result.tier2_picks) / max(1, len(result.tier2_picks)), 3),
                "avg_odds": round(sum(p.best_odds for p in result.tier2_picks) / max(1, len(result.tier2_picks)), 2),
            },
            "tier3": {
                "count": len(result.tier3_picks),
                "avg_conviction": round(sum(p.conviction_score for p in result.tier3_picks) / max(1, len(result.tier3_picks)), 3),
                "avg_odds": round(sum(p.best_odds for p in result.tier3_picks) / max(1, len(result.tier3_picks)), 2),
            },
            "suppressed": len(result.suppressed),
        }


# Global curator instance
tier_curator = TierCurator()
