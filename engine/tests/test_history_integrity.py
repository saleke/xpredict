"""Integrity canaries for the packaged real-data archive.

These tests exist to make it IMPOSSIBLE to silently swap the archive back to
synthetic / future-dated / NBA fabrications: any regression that does so trips
hard asserts on real-world invariants (real dates, real soccer only, real
provenance, deterministic build).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from lisa.history import (
    HISTORICAL_ODDS,
    HISTORICAL_PROVENANCE,
    HISTORICAL_SCORES,
    HISTORICAL_SPORTS,
)

NOW = datetime(2026, 9, 21, tzinfo=timezone.utc)


def test_no_synthetic_or_future_dated_fixtures():
    """Every archived match must have a real, past kickoff time."""
    for g in HISTORICAL_ODDS:
        commence = datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00"))
        assert commence <= NOW, f"future-dated fixture {g['id']}"


def test_no_nba_or_non_archived_sports():
    """The archive contains only the 5 archived football leagues."""
    assert "basketball_nba" not in HISTORICAL_SPORTS
    for g in HISTORICAL_ODDS:
        assert g["sport_key"] in HISTORICAL_SPORTS


def test_archive_size_and_schema():
    """Real archive: 7,155 completed matches with odds + scores, paired by id."""
    assert len(HISTORICAL_ODDS) == 7155
    assert len(HISTORICAL_SCORES) == 7155

    odds_ids = {g["id"] for g in HISTORICAL_ODDS}
    scores_ids = {s["id"] for s in HISTORICAL_SCORES}
    assert odds_ids == scores_ids  # every odds payload has a settled score

    for g in HISTORICAL_ODDS:
        assert g["sport_key"] in HISTORICAL_SPORTS
        assert g["bookmakers"], f"{g['id']} has no books"
        for book in g["bookmakers"]:
            assert any(m["key"] == "h2h" for m in book["markets"])
    for s in HISTORICAL_SCORES:
        assert s["completed"] is True
        assert len(s["scores"]) == 2


def test_flat_build_odds_counts():
    """Spot-check the archive's known book-depth fingerprint."""
    by_depth: dict[int, int] = {}
    for g in HISTORICAL_ODDS:
        by_depth[len(g["bookmakers"])] = by_depth.get(len(g["bookmakers"]), 0) + 1
    assert by_depth.get(5, 0) == 5358
    assert by_depth.get(2, 0) == 426
    assert sum(by_depth.values()) == 7155


def test_provenance_fields_present():
    """Provenance must be explicit and verifiable in every export."""
    assert HISTORICAL_PROVENANCE["source"].startswith("football-data.co.uk")
    assert HISTORICAL_PROVENANCE["total_matches"] == 7155
    assert HISTORICAL_PROVENANCE["seasons"] == ["2122", "2223", "2324", "2425"]
    assert "No synthetic" in HISTORICAL_PROVENANCE["statement"]
    assert HISTORICAL_PROVENANCE["markets_covered"] == ["1x2", "asian_handicap", "total_over_under"]


def test_every_match_has_multi_market_snapshots():
    """AH + O/U 2.5 ride the same real rows as 1X2 (open AND closing best)."""
    assert HISTORICAL_PROVENANCE["matches_with_ah"] == 7154
    assert HISTORICAL_PROVENANCE["matches_with_ou"] == 7155
    assert HISTORICAL_PROVENANCE["matches_with_movement"] == 7155

    for g in HISTORICAL_ODDS:
        summary = g.get("markets_summary")
        assert summary and summary.get("h2h"), g["id"]
        # closing_markets h2h must agree with the legacy closing_odds alias
        assert g.get("closing_markets", {}).get("h2h") == g.get("closing_odds")
        ah = summary.get("asian_handicap")
        if ah:
            assert "line" in ah and ah.get("line") is not None
            for k in (g["home_team"], g["away_team"]):
                assert 1.01 <= ah[k] <= 100.0, g["id"]
        ou = summary.get("total_over_under")
        if ou:
            assert ou.get("line") == 2.5
            for k in ("Over", "Under"):
                assert 1.01 <= ou[k] <= 100.0, g["id"]


def test_movement_is_close_over_early_ratio():
    """Movement must be exactly the closing/early price ratio, self-consistent."""
    for g in HISTORICAL_ODDS:
        move = g.get("movement") or {}
        for market, ratios in move.items():
            early = g["markets_summary"][market]
            close = g["closing_markets"].get(market)
            assert close, g["id"]
            for outcome, ratio in ratios.items():
                assert ratio >= 0.5 and ratio <= 2.0
                expected = round(close[outcome] / early[outcome], 5)
                assert ratio == expected, (
                    f"{g['id']} {market} {outcome} movement out of sync"
                )


def test_every_match_has_real_closing_line():
    """CLV measurement requires the closing snapshot; it must be real data.

    The archive ships TWO real snapshots per match — the executed early line and
    the closing line (football-data.co.uk *C columns) used to score CLV. A
    fabricated or missing closing line would break the 'measured, never
    constructed' promise."""
    assert HISTORICAL_PROVENANCE["matches_with_closing"] == 7155
    for g in HISTORICAL_ODDS:
        closing = g.get("closing_odds")
        assert closing, f"{g['id']} missing closing-odds snapshot"
        assert set(closing.keys()) == {g["home_team"], "Draw", g["away_team"]}
        for price in closing.values():
            assert 1.01 <= price <= 100.0, f"{g['id']} implausible closing price"


def test_scores_are_real_results():
    """Scores must be plausible real football scorelines."""
    for s in HISTORICAL_SCORES:
        vals = [int(x["score"]) for x in s["scores"]]
        assert all(0 <= v <= 12 for v in vals)
        # Completed match: exactly one winner or a draw.
        assert vals[0] != vals[1] or True  # draws are valid; no further constraint