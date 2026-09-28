"""Tests for the daily match forecast board (bulletin.py)."""
from __future__ import annotations

import pytest
from dataclasses import replace

from lisa.bulletin import (
    TOP_PICK_MIN_PROB,
    build_bulletin,
    build_live_bulletin,
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

# --- short-horizon board -----------------------------------------------------
#
# The board used to filter out only matches that had already kicked off, with no
# upper bound, so it padded itself to 40 rows with fixtures weeks away. On a day
# with no European football it showed 40 matches starting 13 days out.

def _fixture(sport="soccer_epl", match_id="m1", hours=3, home="A", away="B"):
    from datetime import datetime, timedelta, timezone
    from lisa.odds import Book, Match
    kickoff = datetime.now(timezone.utc) + timedelta(hours=hours)
    # Consensus needs two independent books, as it would in production.
    book = Book(key="bk1", title="Book1", last_update=kickoff,
                outcomes={home: 2.10, away: 2.05}, line=None)
    book2 = Book(key="bk2", title="Book2", last_update=kickoff,
                 outcomes={home: 2.12, away: 2.00}, line=None)
    return Match(id=match_id, sport_key=sport, commence_time=kickoff,
                 home_team=home, away_team=away, completed=False,
                 market="h2h", bookmakers=(book, book2))


def test_board_never_shows_fixtures_beyond_the_horizon():
    near = [_fixture("soccer_epl", "near", hours=5)]
    far = [_fixture("soccer_epl", "far", hours=200)]
    board = build_live_bulletin(near + far, horizon_hours=24)
    ids = [m["match_id"] for m in board["matches"]]
    assert "near" in ids
    assert "far" not in ids, "a fixture 8 days out must not reach a 24h board"
    assert board["horizon_hours"] == 24
    assert board["window_end"]


def test_board_reports_a_shortfall_instead_of_padding():
    rows = [_fixture("soccer_epl", f"m{i}", hours=2 + i) for i in range(3)]
    board = build_live_bulletin(rows, horizon_hours=24, min_matches=12)
    assert board["count"] == 3
    assert board["shortfall"] == 9, "the volume promise must be reported, not faked"


def test_board_reaches_its_volume_promise_when_fixtures_allow():
    rows = [_fixture("soccer_epl", f"m{i}", hours=1 + i * 0.5) for i in range(12)]
    board = build_live_bulletin(rows, horizon_hours=24, min_matches=12)
    assert board["count"] == 12
    assert board["shortfall"] == 0


def test_in_play_is_suppressed_unless_explicitly_enabled():
    started = _fixture("soccer_epl", "started", hours=-1)
    off = build_live_bulletin([started], horizon_hours=24, include_in_play=False)
    assert off["in_play_count"] == 0
    assert off["in_play"] == []
    on = build_live_bulletin([started], horizon_hours=24, include_in_play=True)
    assert on["in_play_count"] == 1
    assert on["in_play"][0]["in_play"] is True
    # An in-play row must say so in its uncertainty reasons.
    keys = {r["key"] for r in on["in_play"][0]["uncertainty"]["reasons"]}
    assert "in_play" in keys
    # Every reason must carry the label the formatter renders, and the level
    # must be computable; a malformed reason crashes _uncertainty_level.
    for r in on["in_play"][0]["uncertainty"]["reasons"]:
        assert r["label"]
    assert on["in_play"][0]["uncertainty"]["level"] in ("low", "medium", "high")


def test_multi_market_fixture_is_one_row_with_several_opportunities():
    """The parser emits one Match per market; the board must not triple-count."""
    base = _fixture("soccer_epl", "multi", hours=6)
    tb = base.bookmakers[0]
    totals = replace(base, market="totals",
                     bookmakers=tuple(
                         replace(b, outcomes=("Over", 1.90) if False else {"Over": 1.90 + i * 0.01,
                                                                          "Under": 1.95},
                                 line=2.5) for i, b in enumerate(base.bookmakers)))
    board = build_live_bulletin([base, totals], horizon_hours=24)
    assert board["count"] == 1, "one fixture must produce exactly one row"
    markets = [o["market"] for o in board["matches"][0]["markets"]]
    assert set(markets) == {"h2h", "totals"}


def test_micro_markets_are_only_shown_when_priced():
    board = build_live_bulletin([_fixture("soccer_epl", "m", hours=6)],
                                include_micro_markets=False)
    assert "micro_markets" not in board["matches"][0]
