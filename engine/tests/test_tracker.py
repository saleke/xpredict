"""Tracker — JSONL validation trail and weekly summary aggregation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lisa.pipeline import CycleReport
from lisa.settle import SettlementReport
from lisa.tracker import Tracker

T = datetime(2026, 9, 20, 15, 0, 0, tzinfo=timezone.utc)


def test_cycle_and_pick_recording(tmp_path, pipeline) -> None:
    track = Tracker(tmp_path / "m.jsonl", storage=pipeline.storage)
    reports = pipeline.run_cycle()
    track.record_cycles(reports)
    track.record_picks()

    summary = track.summarize()
    assert summary["cycles"] == 3
    assert summary["matches_seen"] == 10  # 6 NBA + 2 La Liga + 2 Bundesliga
    assert summary["picks"]["count"] == 5
    assert summary["suppression"]["below_threshold"] == 3
    assert summary["suppression"]["insufficient_books"] == 1
    assert summary["suppression"]["high_dispersion"] == 1
    assert summary["picks"]["p_true_mean"] is not None
    assert summary["errors"] == 0


def test_pick_recording_dedupes_across_cycles(tmp_path, pipeline) -> None:
    track = Tracker(tmp_path / "m.jsonl", storage=pipeline.storage)
    pipeline.run_cycle()
    track.record_picks()
    track.record_picks()

    assert track.summarize()["picks"]["count"] == 5


def test_settlement_recording(tmp_path) -> None:
    track = Tracker(tmp_path / "m.jsonl")
    track.record_settlement(
        SettlementReport(won=2, lost=1, void=1, settled=4, pending=3))

    s = track.summarize()["settlement"]
    assert s["runs"] == 1
    assert s["won"] == 2
    assert s["lost"] == 1
    assert s["void"] == 1
    assert s["settled"] == 4
    assert s["pending_last"] == 3


def test_explicit_timestamps_drive_span(tmp_path) -> None:
    track = Tracker(tmp_path / "m.jsonl", storage=None)
    track.record_cycles([
        CycleReport(sport_key="basketball_nba", began=T, matches_seen=5),
    ], ts=T)
    track.record_cycles([
        CycleReport(sport_key="basketball_nba", began=T, matches_seen=5),
    ], ts=T + timedelta(days=7))

    summary = track.summarize()
    assert summary["cycles"] == 2
    assert summary["span_hours"] == 7 * 24


def test_suppression_reason_parsing(tmp_path) -> None:
    track = Tracker(tmp_path / "m.jsonl")
    track.record_cycles([
        _report_with_suppressed(["m1:below_threshold", "m2:high_dispersion"]),
        _report_with_suppressed(["m3:insufficient_books", "m4:below_threshold"]),
    ])

    summary = track.summarize()
    assert summary["suppression"] == {
        "below_threshold": 2,
        "high_dispersion": 1,
        "insufficient_books": 1,
    }


def test_summarize_empty_and_missing_file(tmp_path) -> None:
    track = Tracker(tmp_path / "nope.jsonl")
    summary = track.summarize()
    assert summary["cycles"] == 0
    assert summary["picks"]["count"] == 0
    assert summary["picks_per_week"] is None
    assert summary["settlement"]["runs"] == 0


def _report_with_suppressed(suppressed: list[str]) -> CycleReport:
    return CycleReport(sport_key="basketball_nba", began=T,
                       suppressed=suppressed)