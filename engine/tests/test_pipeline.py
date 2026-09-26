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