"""Tests for the daily match forecast board (bulletin.py)."""
from __future__ import annotations

import pytest

from lisa.bulletin import (
    TOP_PICK_MIN_PROB,
    build_bulletin,
    format_bulletin,
)


class TestBulletin:
    def test_deterministic(self):
        a = build_bulletin()
        b = build_bulletin()
        # Only the timestamps may differ.
        for key in ("kind", "mode", "day", "count", "matches", "top_pick",
                    "marquee_count", "high_uncertainty_count"):
            if key == "matches":
                assert a[key] == b[key], "bulletin board must be bit-identical"
            else:
                assert a[key] == b[key], key

    def test_volume_reaches_min_matches(self):
        b = build_bulletin(min_matches=8)
        assert b["count"] >= 8, "board must deliver 8+ fixtures"

    def test_board_schema(self):
        b = build_bulletin()
        assert b["kind"] == "match_forecast_bulletin"
        assert b["mode"] == "archive"
        assert b["day"]
        for m in b["matches"]:
            assert m["match_id"] and m["home"] and m["away"]
            assert m["league"] and m["commence_at"]
            for p in ("p_home", "p_draw", "p_away"):
                val = m["model"][p]
                assert 0.0 <= val <= 1.0
                assert m["model"]["ready"] is True
            assert m["uncertainty"]["level"] in ("low", "medium", "high")
            for r in m["uncertainty"]["reasons"]:
                assert r["key"] and r["label"]
            assert set(m.keys()) >= {"market", "model", "micro", "movement",
                                     "uncertainty", "is_top_pick", "marquee", "locks"}
            for lock in m["locks"]:
                assert lock["feature"] and lock["required_tier"] and lock["teaser"]

    def test_marquee_and_top_pick_present_or_absent_consistently(self):
        b = build_bulletin()
        assert b["marquee_count"] == 3
        marquee = [m for m in b["matches"] if m["marquee"]]
        assert len(marquee) == 3
        top = b["top_pick"]
        picks = [m for m in b["matches"] if m["is_top_pick"]]
        if top is None:
            assert not picks
        else:
            assert len(picks) == 1
            assert picks[0]["match_id"] == top
            assert picks[0]["market"]["p_top"] >= TOP_PICK_MIN_PROB

    def test_high_uncertainty_rows_have_reasons(self):
        b = build_bulletin()
        flagged = [m for m in b["matches"] if m["uncertainty"]["level"] == "high"]
        for m in flagged:
            assert m["uncertainty"]["reasons"], "high uncertainty must explain itself"

    def test_disclaimer_is_honest(self):
        b = build_bulletin()
        assert "not guarantees" in b["disclaimer"]
        assert "live odds" in b["disclaimer"]

    def test_format_bulletin_readable(self):
        text = format_bulletin(build_bulletin())
        assert "FORECAST BOARD" in text
        assert "uncertainty:" in text.lower()
        assert "Pick of the Day" in text or "pick" in text.lower()