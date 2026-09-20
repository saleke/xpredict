"""End-to-end pipeline: ingestion -> refine -> gate -> persist/notify."""
from __future__ import annotations

from datetime import datetime, timezone

NOW = datetime(2026, 9, 20, 15, 0, 0, tzinfo=timezone.utc)


def test_cycle_counts_and_suppression_reasons(pipeline):
    reports = pipeline.run_cycle(now=NOW)

    assert len(reports) == 3
    by_sport = {r.sport_key: r for r in reports}

    nba = by_sport["basketball_nba"]
    assert nba.matches_seen == 6
    assert nba.picks_emitted == 3          # nba-a, nba-e, nba-g
    assert "nba-b:below_threshold" in nba.suppressed
    assert "nba-c:high_dispersion" in nba.suppressed
    assert "nba-d:insufficient_books" in nba.suppressed

    liga = by_sport["soccer_spain_la_liga"]
    assert liga.picks_emitted == 1         # lig-a
    assert "lig-b:below_threshold" in liga.suppressed

    bundes = by_sport["soccer_germany_bundesliga"]
    assert bundes.picks_emitted == 1       # bdl-a
    assert "bdl-b:below_threshold" in bundes.suppressed


def test_cycle_is_idempotent(pipeline):
    pipeline.run_cycle(now=NOW)
    reports = pipeline.run_cycle(now=NOW)
    assert sum(r.picks_emitted for r in reports) == 0
    assert all(any("already_emitted" in s for s in r.suppressed)
               for r in reports)


def test_notifier_fires_exactly_once_per_pick(pipeline, notifier):
    pipeline.run_cycle(now=NOW)
    assert len(notifier.messages) == 5     # 3 NBA + 1 La Liga + 1 Bundesliga
    assert "Celtics" in notifier.messages[0]
    assert "True probability" in notifier.messages[0]


def test_telemetry_hot_layer_written(pipeline, storage):
    pipeline.run_cycle(now=NOW)
    live = storage.get_live("match:nba-a")
    assert live is not None
    assert live["book_count"] == 5
    assert storage.get_live("pick:nba-a::h2h::Celtics") is not None


def test_realert_fires_only_after_move_and_cooldown(pipeline, storage, notifier):
    pipeline.run_cycle(now=NOW)
    baseline = len(notifier.messages)  # 5

    # unchanged data: second cycle must NOT re-alert anything
    reports = pipeline.run_cycle(now=NOW)
    assert sum(r.picks_emitted for r in reports) == 0
    assert len(notifier.messages) == baseline

    # simulate a big consensus move + an elapsed cooldown for one pick
    key = "pick:nba-a::h2h::Celtics"
    hot = storage.get_live(key)
    hot["p_true"] = 0.5                       # large delta vs real 0.824
    hot["emitted_at"] = "2026-09-20T10:00:00+00:00"
    storage.upsert_live(key, hot, pipeline.settings.live_ttl_sec)

    reports = pipeline.run_cycle(now=NOW)
    assert any("nba-a:alert_moved" in s for r in reports for s in r.suppressed)
    assert len(notifier.messages) == baseline + 1


def test_upstream_failure_degrades_per_sport(pipeline, storage):
    class Flaky:
        def __init__(self, inner):
            self.inner = inner
            self.calls = 0
        def get_odds(self, sport, **kwargs):
            if sport == "basketball_nba":
                raise RuntimeError("upstream down")
            return self.inner.get_odds(sport, **kwargs)

    pipeline.client = Flaky(pipeline.client)
    reports = pipeline.run_cycle(now=NOW)
    nba = next(r for r in reports if r.sport_key == "basketball_nba")
    assert nba.errors
    assert nba.matches_seen == 0
    others = [r for r in reports if r.sport_key != "basketball_nba"]
    assert all(r.errors == [] and r.picks_emitted >= 1 for r in others)