"""Tests for the subscription tier catalog (tiers.py)."""
from __future__ import annotations

import pytest

from lisa import tiers
from lisa.tiers import (
    VALID_TIERS,
    clean_tier,
    entitled,
    feature_keys,
    features_for,
    features_gained_by_upgrade,
    format_tiers,
    lock_state,
    reveal_minutes,
    tier_rank,
    upgrade_path,
)


def test_clean_tier_normalises_and_falls_back():
    assert clean_tier(" FREE ") == "free"
    assert clean_tier("Tier3") == "tier3"
    assert clean_tier("") == "free"
    assert clean_tier("nonexistent") == "free"
    assert clean_tier("TiEr 2") == "tier2"


def test_tier_ranks_ordered():
    assert [tier_rank(t) for t in VALID_TIERS] == [0, 1, 2, 3]


def test_entitlements_are_monotonic_across_tiers():
    for feature in feature_keys():
        prev = entitled("free", feature)
        for t in ("tier1", "tier2", "tier3"):
            now = entitled(t, feature)
            assert now >= prev, f"entitlement not monotonic for {feature}"
            prev = now


def test_every_feature_belongs_to_some_tier():
    for feature in feature_keys():
        assert entitled("tier3", feature), f"{feature} must exist somewhere"
        assert any(entitled(t, feature) for t in VALID_TIERS)


def test_every_tier_has_features():
    for t in VALID_TIERS:
        assert features_for(t), f"{t} should unlock at least one feature"


def test_upgrade_steps_add_value():
    assert features_gained_by_upgrade("free"), "T1 must add something over free"
    assert features_gained_by_upgrade("tier1"), "T2 must add something over T1"
    assert features_gained_by_upgrade("tier2"), "T3 must add something over T2"
    assert features_gained_by_upgrade("tier3") == []


def test_reveal_timing_rewards_higher_tiers_no_later():
    for feature in feature_keys():
        times = {t: reveal_minutes(feature, t) for t in VALID_TIERS if entitled(t, feature)}
        if len(times) < 2:
            continue
        # "earlier" = a LARGER minutes-before-kickoff value.
        vals = [v for v in times.values() if v is not None]
        assert len(vals) == len(times)
        for t in VALID_TIERS:
            if tier_rank(t) >= 1 and entitled(t, feature):
                below = reveal_minutes(feature, VALID_TIERS[tier_rank(t) - 1])
                if below is None:
                    continue
                assert (reveal_minutes(feature, t) or 0) >= (below or 0), feature


def test_reveal_minutes_none_when_not_entitled():
    assert reveal_minutes("api_feed", "free") is None
    assert reveal_minutes("diamond_picks", "tier1") is None
    assert reveal_minutes("bulletin", "free") == 0


def test_lock_state_unlocked_and_teaser():
    ok = lock_state("tier1", "top_pick")
    assert ok["unlocked"] is True
    assert ok["reveal_minutes_before_kickoff"] is not None

    locked = lock_state("free", "top_pick")
    assert locked["unlocked"] is False
    assert locked["required_tier"] == "tier1"
    assert "🔒" in locked["teaser"]

    deep = lock_state("free", "diamond_picks")
    assert deep["required_tier"] == "tier2"


def test_upgrade_path_serialisable_and_shapely():
    path = upgrade_path()
    assert list(path["tiers"]) == ["free", "tier1", "tier2", "tier3"]
    by_key = {f["key"]: f for f in path["features"]}
    assert set(by_key) == set(feature_keys())
    for f in path["features"]:
        assert f["label"] and f["grant"] in VALID_TIERS
        assert set(f["reveal_minutes"]) <= set(VALID_TIERS)
    for t in VALID_TIERS:
        assert "unlocked" in path["ladder"][t]


def test_highest_tier_feature_emphasis():
    # Tier 3 must own the earliness/speed features — the honest premium.
    for f in ("api_feed", "early_bird", "arbitrage_stream", "portfolio"):
        assert tiers.FEATURES[f]["grant"] == "tier3"


def test_format_tiers_lists_ladder():
    text = format_tiers()
    assert "SUBSCRIPTION VALUE LADDER" in text
    assert "earliest" in text.lower()
    assert "360 min" in text
    for t in VALID_TIERS:
        assert tiers.TIER_LABELS[t] in text