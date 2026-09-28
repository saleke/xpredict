"""Scheduler — cadence adaptation, credit guard, error resilience."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lisa import config as cfg
from lisa.fixtures import FIXTURE_SPORTS
from lisa.scheduler import Scheduler

NOW = datetime(2026, 9, 20, 15, 0, 0, tzinfo=timezone.utc)
KICKOFF = datetime(2026, 9, 21, 0, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def _scheduler(client, storage, now_fn, **kw) -> Scheduler:
    return Scheduler(client, storage, cfg.Settings(sports=FIXTURE_SPORTS),
                     now_fn=now_fn, **kw)


def test_first_tick_runs_cycle_and_settlement(fixture_client, storage) -> None:
    clock = FakeClock(NOW)
    sched = _scheduler(fixture_client, storage, clock)
    summary = sched.tick()

    assert summary.ran_cycle is True
    assert summary.ran_settlement is True
    assert summary.skipped_reason is None
    # nba kickoffs are ~9h out, well outside the 90-min spike window
    assert summary.mode == "prematch"
    assert sched.next_cycle_at == NOW + timedelta(hours=1)
    assert sched.next_settle_at == NOW + timedelta(hours=1)
    assert sched.stats.cycles_run == 1
    assert sched.stats.settlements_run == 1
    assert len(summary.cycle_reports) == 3  # one per configured sport


def test_tick_skips_work_when_nothing_due(fixture_client, storage) -> None:
    clock = FakeClock(NOW)
    sched = _scheduler(fixture_client, storage, clock)
    sched.tick()
    summary = sched.tick()

    assert summary.ran_cycle is False
    assert summary.ran_settlement is False
    assert sched.stats.cycles_run == 1
    assert sched.stats.settlements_run == 1


def test_spike_cadence_near_kickoff(fixture_client, storage) -> None:
    clock = FakeClock(KICKOFF - timedelta(hours=1))  # 60 min to kickoff
    sched = _scheduler(fixture_client, storage, clock)
    summary = sched.tick()

    assert summary.mode == "spike"
    assert sched.next_cycle_at == clock.now + timedelta(minutes=15)


def test_live_cadence_and_live_flag_after_kickoff(fixture_client, storage) -> None:
    clock = FakeClock(KICKOFF)
    # Live cadence is opt-in: with in-play off, a running match must not by
    # itself move the scheduler to 15 minute polling.
    sched = Scheduler(fixture_client, storage,
                      cfg.Settings(sports=FIXTURE_SPORTS, enable_inplay=True),
                      now_fn=clock)
    first = sched.tick()

    assert first.mode == "live"          # nba-a kicks off at this instant
    assert sched.next_cycle_at == KICKOFF + timedelta(minutes=15)

    # one live interval later the next cycle polls in live mode
    clock.now = KICKOFF + timedelta(minutes=16)
    second = sched.tick()
    assert second.ran_cycle is True
    assert second.mode == "live"
    assert sched.stats.cycles_run == 2


def test_credit_stop_blocks_cycle_but_runs_settlement(fixture_client, storage) -> None:
    fixture_client.last_remaining = 10
    clock = FakeClock(NOW)
    sched = _scheduler(fixture_client, storage, clock)
    summary = sched.tick()

    assert summary.ran_cycle is False
    assert summary.skipped_reason == "credit stop (10 left)"
    assert summary.ran_settlement is True  # grading stays essential
    # hard backoff until credits recover
    assert sched.next_cycle_at == NOW + timedelta(seconds=21600)
    assert sched.stats.cycles_skipped == 1


def test_credit_warning_degrades_quiet_modes_to_idle(fixture_client, storage) -> None:
    fixture_client.last_remaining = 50  # below warn (100), above stop (20)
    clock = FakeClock(NOW)
    sched = _scheduler(fixture_client, storage, clock)
    summary = sched.tick()

    assert summary.ran_cycle is False
    assert summary.skipped_reason == "credit warning (50 left)"


class _FlakyClient:
    def get_odds(self, *args, **kwargs):  # noqa: ARG002
        raise RuntimeError("upstream boom")

    def get_scores(self, *args, **kwargs):  # noqa: ARG002
        raise RuntimeError("upstream boom")


def test_upstream_failures_recorded_without_crash(storage) -> None:
    clock = FakeClock(NOW)
    sched = _scheduler(_FlakyClient(), storage, clock)
    summary = sched.tick()

    assert summary.ran_cycle is True
    assert len(summary.errors) == 3  # one per sport
    assert sched.next_cycle_at is not None  # loop stays alive
    assert summary.ran_settlement is True


def test_run_forever_honours_max_ticks(fixture_client, storage) -> None:
    clock = FakeClock(NOW)
    sleeps: list[float] = []
    sched = _scheduler(fixture_client, storage, clock)
    sched.run_forever(max_ticks=2, sleep_fn=sleeps.append)

    assert sched.stats.ticks == 2
    assert sched.stats.cycles_run == 1
    assert sched.stats.settlements_run == 1
    assert sleeps and all(sec >= 0 for sec in sleeps)

# --- in-play cadence gating --------------------------------------------------
#
# A match that has already kicked off used to drop the scheduler into the 15
# minute live cadence purely on timing. With in-play switched off there is no
# live poll refreshing that price, so a tradeable-looking in-play quote would be
# whatever was last cached. The cadence has to stay off, not just the display.

def _live_settings(**overrides):
    base = dict(sports=FIXTURE_SPORTS, cadence_prematch_sec=3600,
                cadence_live_sec=900, cadence_idle_sec=6 * 3600,
                enable_inplay=False, inplay_tail_hours=2.0)
    base.update(overrides)
    return cfg.Settings(**base)


def test_a_running_match_does_not_engage_live_cadence_when_inplay_is_off(
        fixture_client, storage) -> None:
    """Fixture client serves a match that kicked off an hour ago."""
    clock = FakeClock(NOW)
    sched = Scheduler(fixture_client, storage, _live_settings(), now_fn=clock)
    sched._commences = [NOW - timedelta(minutes=30)]   # kicked off half an hour ago
    assert sched._live_now(clock.now) is False


def test_the_same_match_does_engage_live_cadence_when_inplay_is_on(
        fixture_client, storage) -> None:
    clock = FakeClock(NOW)
    sched = Scheduler(fixture_client, storage,
                      _live_settings(enable_inplay=True), now_fn=clock)
    sched._commences = [NOW - timedelta(minutes=30)]
    assert sched._live_now(clock.now) is True


def test_in_play_tail_bounds_how_long_a_match_stays_live(
        fixture_client, storage) -> None:
    """A match that ended hours ago must not keep the live cadence running."""
    clock = FakeClock(NOW)
    sched = Scheduler(fixture_client, storage,
                      _live_settings(enable_inplay=True), now_fn=clock)
    long_done = NOW - timedelta(hours=9)
    sched._commences = [long_done]
    assert sched._live_now(clock.now) is False
