"""End-to-end pipeline: ingestion -> refine -> gate -> persist/notify."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

NOW = datetime(2026, 9, 20, 15, 0, 0, tzinfo=timezone.utc)


def test_cycle_counts_and_suppression_reasons(pipeline):
    reports = pipeline.run_cycle(now=NOW)

    assert len(reports) == 3
    by_sport = {r.sport_key: r for r in reports}

    nba = by_sport["basketball_nba"]
    assert nba.matches_seen == 6
    assert nba.picks_emitted == 2          # nba-a, nba-e (nba-g already kicked off)
    assert "nba-b:below_threshold" in nba.suppressed
    assert "nba-c:high_dispersion" in nba.suppressed
    assert "nba-d:insufficient_books" in nba.suppressed
    assert "nba-g:kickoff_passed" in nba.suppressed

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


def test_run_cycle_rejects_out_of_scope_sport(pipeline):
    with pytest.raises(ValueError, match="chess_open"):
        pipeline.run_cycle(("chess_open",))


def test_notifier_fires_exactly_once_per_pick(pipeline, notifier):
    pipeline.run_cycle(now=NOW)
    assert len(notifier.messages) == 4     # 2 NBA + 1 La Liga + 1 Bundesliga
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
    baseline = len(notifier.messages)  # 4

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


def test_no_pick_after_kickoff(pipeline, storage, notifier):
    """A match that already commenced must never receive a pre-game pick."""
    reports = pipeline.run_cycle(now=NOW)
    nba = next(r for r in reports if r.sport_key == "basketball_nba")
    assert "nba-g:kickoff_passed" in nba.suppressed
    assert nba.matches_refined == 4          # nba-g is skipped before refinement
    assert not any("Spurs" in m for m in notifier.messages)
    assert not [p for p in storage.list_pending_picks() if p["match_id"] == "nba-g"]


def test_failed_alert_is_retried_not_lost(fixture_client, storage, settings):
    """Ledger-before-dispatch means delivery must be retried, never dropped."""
    from lisa.notify import Notifier
    from lisa.pipeline import Pipeline

    class Flaky(Notifier):
        def __init__(self, failures: int):
            self.remaining_failures = failures
            self.attempts = 0
            self.delivered = []

        def send(self, text):
            self.attempts += 1
            if self.remaining_failures > 0:
                self.remaining_failures -= 1
                raise RuntimeError("telegram 502")
            self.delivered.append(text)

    # Four picks in the fixture set; the notifier fails the first three sends.
    flaky = Flaky(failures=3)
    pipe = Pipeline(fixture_client, storage, settings, notifier=flaky)
    reports = pipe.run_cycle(now=NOW)

    assert sum(r.notifications_failed for r in reports) == 3
    assert any("pending delivery" in e for r in reports for e in r.errors)

    # Every alert is delivered exactly once and nothing is left queued: a
    # transport failure delays delivery, it never drops the alert.
    pipe.run_cycle(now=NOW)
    assert len(flaky.delivered) == 4
    assert len(set(flaky.delivered)) == 4
    assert storage.list_pending_notifications() == []


def test_storage_without_outbox_still_dispatches(fixture_client, settings):
    from lisa.notify import Notifier
    from lisa.pipeline import Pipeline
    from lisa.storage import InMemoryStorage

    class Bare(InMemoryStorage):
        enqueue_notification = None
        list_pending_notifications = None
        mark_notification_sent = None
        mark_notification_failed = None

    class Collect(Notifier):
        def __init__(self):
            self.messages = []

        def send(self, text):
            self.messages.append(text)

    notifier = Collect()
    pipe = Pipeline(fixture_client, Bare(), settings, notifier=notifier)
    pipe.run_cycle(now=NOW)
    assert len(notifier.messages) == 4


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

def test_cycle_caches_raw_payloads_for_the_forecast_board(pipeline, storage):
    """Every ingestion path must leave the cache the /api/forecast handler reads.

    The payload cache used to live in the live-ingest daemon, so the scheduler
    path fetched odds but left the forecast board empty. Caching now happens
    where the payload is parsed, and this guards the wrapper shape that
    ``server._handle_forecast`` depends on.
    """
    from lisa.dashboard import LIVE_ODDS_PREFIX

    pipeline.run_cycle(now=NOW)

    keys = {k for k in storage.scan_live_keys() if k.startswith(LIVE_ODDS_PREFIX)}
    assert keys == {
        f"{LIVE_ODDS_PREFIX}soccer_spain_la_liga",
        f"{LIVE_ODDS_PREFIX}soccer_germany_bundesliga",
        f"{LIVE_ODDS_PREFIX}basketball_nba",
    }

    entry = storage.get_live(f"{LIVE_ODDS_PREFIX}basketball_nba")
    # The reader requires a dict wrapper with a truthy "payload" list.
    assert isinstance(entry, dict)
    assert entry["sport_key"] == "basketball_nba"
    assert isinstance(entry["payload"], list) and entry["payload"]
    assert entry["observed_at"] > 0


def test_cache_write_failure_does_not_block_picks(pipeline):
    """The cache only backs the UI, so a storage fault must not stop picks."""
    from lisa.dashboard import LIVE_ODDS_PREFIX

    real_upsert = pipeline.storage.upsert_live

    def explode_if_forecast(key, *args, **kwargs):
        # Only the forecast cache fails; telemetry writes still succeed.
        if str(key).startswith(LIVE_ODDS_PREFIX):
            raise RuntimeError("cache backend down")
        return real_upsert(key, *args, **kwargs)

    pipeline.storage.upsert_live = explode_if_forecast
    reports = pipeline.run_cycle(now=NOW)

    # Same picks as a healthy cycle: nba 2 + la_liga 1 + bundesliga 1.
    assert sum(r.picks_emitted for r in reports) == 4
    assert not any("cache" in e for r in reports for e in r.errors)


def test_cycle_publishes_the_board_rollup(pipeline, storage):
    """The dashboard's live snapshot must be written by every ingestion path."""
    from lisa.dashboard import LIVE_SNAPSHOT_KEY

    pipeline.run_cycle(now=NOW)
    snap = storage.get_live(LIVE_SNAPSHOT_KEY)

    assert snap is not None, "board rollup missing after a cycle"
    assert snap["matches_observed"] == 10   # nba 6 + la_liga 2 + bundesliga 2
    assert set(snap["sports"]) == {
        "basketball_nba", "soccer_spain_la_liga", "soccer_germany_bundesliga",
    }
    assert snap["sports"]["basketball_nba"]["n_matches"] == 6
    assert snap["observed_at"] > 0


def test_a_quota_blocked_cycle_keeps_the_last_observation(pipeline, storage):
    """A cycle that polls nothing must never erase what we last saw."""
    from lisa.dashboard import LIVE_SNAPSHOT_KEY

    pipeline.run_cycle(now=NOW)
    first = storage.get_live(LIVE_SNAPSHOT_KEY)
    assert first["matches_observed"] == 10

    # Simulate a fully blocked cycle: no sports polled, everything errored.
    from lisa.pipeline import CycleReport
    blocked = [
        CycleReport(sport_key=sport, began=NOW, errors=["quota floor reached"])
        for sport in first["sports"]
    ]
    pipeline._write_snapshot(blocked, now=NOW, live=False)

    after = storage.get_live(LIVE_SNAPSHOT_KEY)
    assert after["matches_observed"] == 10, "last observation was erased"
    assert after["observed_at"] == first["observed_at"]
    # The reason is still surfaced, so the UI can explain the stale numbers.
    assert "quota floor" in after["last_error"]
    for entry in after["sports"].values():
        assert entry["n_matches"] > 0
        assert "quota floor" in entry["error"]


def test_a_rollup_failure_does_not_break_the_cycle(pipeline):
    from lisa.dashboard import LIVE_SNAPSHOT_KEY

    real_upsert = pipeline.storage.upsert_live

    def explode_if_snapshot(key, *args, **kwargs):
        if str(key) == LIVE_SNAPSHOT_KEY:
            raise RuntimeError("rollup backend down")
        return real_upsert(key, *args, **kwargs)

    pipeline.storage.upsert_live = explode_if_snapshot
    reports = pipeline.run_cycle(now=NOW)
    assert sum(r.picks_emitted for r in reports) == 4
