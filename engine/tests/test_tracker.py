"""Tracker — JSONL validation trail and weekly summary aggregation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lisa.pipeline import CycleReport
from lisa.settle import SettlementReport
from lisa.tracker import Tracker

T = datetime(2026, 9, 20, 15, 0, 0, tzinfo=timezone.utc)


def test_cycle_and_pick_recording(tmp_path, pipeline) -> None:
    track = Tracker(tmp_path / "m.jsonl", storage=pipeline.storage)
    reports = pipeline.run_cycle(now=T)
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
    pipeline.run_cycle(now=T)
    track.record_picks()
    track.record_picks()

    assert track.summarize()["picks"]["count"] == 5


def test_settlement_recording(tmp_path) -> None:
    track = Tracker(tmp_path / "m.jsonl")
    settled_items = [
        {"dedupe_key": "m1::h2h::A", "match_id": "m1", "sport_key": "basketball_nba",
         "market": "h2h", "outcome_name": "A", "p_true": 0.80, "result": "WIN", "state": "SETTLED"},
        {"dedupe_key": "m2::h2h::B", "match_id": "m2", "sport_key": "basketball_nba",
         "market": "h2h", "outcome_name": "B", "p_true": 0.75, "result": "LOSS", "state": "SETTLED"},
        {"dedupe_key": "m3::totals::Over", "match_id": "m3", "sport_key": "basketball_nba",
         "market": "totals", "outcome_name": "Over", "p_true": 0.82, "line": 220.0, "result": "VOID", "state": "VOID"},
    ]
    track.record_settlement(
        SettlementReport(won=1, lost=1, void=1, settled=2, pending=1, settled_picks=settled_items))

    s = track.summarize()["settlement"]
    assert s["runs"] == 1
    assert s["won"] == 1
    assert s["lost"] == 1
    assert s["void"] == 1
    assert s["settled"] == 2
    assert s["pending_last"] == 1

    summary = track.summarize()
    cal = summary["calibration"]
    assert cal is not None
    assert cal["total_evaluated"] == 2
    assert cal["n_won"] == 1
    assert cal["n_lost"] == 1
    assert cal["n_void"] == 1
    assert cal["brier_score"] is not None
    assert cal["ece"] is not None

    by_m = summary["calibration_by_market"]
    assert "h2h" in by_m
    assert by_m["h2h"]["total_evaluated"] == 2
    assert "totals" in by_m
    assert by_m["totals"]["total_evaluated"] == 0
    assert by_m["totals"]["n_void"] == 1


def test_record_settled_picks_from_storage(tmp_path) -> None:
    from lisa.gate import Execution, Pick
    from lisa.odds import utcnow
    from lisa.storage import InMemoryStorage, pick_key

    s = InMemoryStorage()
    p = Pick(
        match_id="m1", sport_key="basketball_nba",
        home_team="A", away_team="B", commence_time=utcnow(), market="h2h",
        outcome_name="A", p_true=0.85, fair_odds=1.18, n_books=5,
        stdev=0.01, cv=0.0125,
        best_execution=Execution("pinnacle", "Pinnacle", 1.20, 0.02),
        state="TRIGGER_ALERT", created_at=utcnow(),
    )
    s.insert_pick(p)
    s.settle_pick(pick_key("m1", "h2h", "A"), "WIN", utcnow(), state="SETTLED")

    track = Tracker(tmp_path / "m.jsonl", storage=s)
    track.record_settled_picks()
    track.record_settled_picks()  # dedupes

    summary = track.summarize()
    assert summary["calibration"]["total_evaluated"] == 1
    assert summary["calibration"]["win_rate"] == 1.0



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


def test_restart_resilience_dedupes_picks_and_suppression(tmp_path) -> None:
    """A daemon restart re-records the same matches; the report must not
    double-count pick volume or suppression."""
    track = Tracker(tmp_path / "m.jsonl")
    # two "pre-restart" cycle records for the same match
    track.record_cycles([
        _report_with_suppressed(["m1:below_threshold"]),
    ], ts=T)
    # simulate a restart: same match re-suppressed, pick logged twice
    rep = _report_with_suppressed(["m1:below_threshold"])
    rep.picks_emitted = 1
    track.record_cycles([rep], ts=T + timedelta(hours=1))
    track.record_picks()

    # inject the duplicated pick lines directly (as a fresh daemon would)
    from lisa.pipeline import CycleReport
    track.record_cycles([CycleReport(sport_key="basketball_nba", began=T,
                                     matches_seen=1, picks_emitted=1)],
                        ts=T + timedelta(hours=2))
    _write_pick(track.path, "m1::h2h::TeamA", p_true=0.8, ts=T + timedelta(hours=2))
    _write_pick(track.path, "m1::h2h::TeamA", p_true=0.8, ts=T + timedelta(hours=3))

    summary = track.summarize()
    assert summary["picks"]["count"] == 1          # deduped by key
    assert summary["suppression"]["below_threshold"] == 1  # deduped by match+reason


def _write_pick(path, key: str, p_true: float, ts) -> None:
    import json
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "kind": "pick", "dedupe_key": key, "match_id": "m1",
            "sport": "basketball_nba", "outcome": "TeamA",
            "p_true": p_true, "fair_odds": 1.2, "cv": 0.01,
            "best_book": "pinnacle", "best_ev": 0.01,
            "ts": ts.isoformat(),
        }, sort_keys=True) + "\n")


def _report_with_suppressed(suppressed: list[str]) -> CycleReport:
    return CycleReport(sport_key="basketball_nba", began=T,
                       suppressed=suppressed)