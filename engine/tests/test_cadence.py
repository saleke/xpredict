"""Cadence logic — pure schedule-state decisions."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lisa.cadence import any_live, decide_cadence, not_completed_starts
from lisa.odds import Match

T = datetime(2026, 9, 20, 15, 0, 0, tzinfo=timezone.utc)
SPIKE = timedelta(minutes=90)
HORIZON = timedelta(hours=36)
LIVE_WIN = timedelta(hours=4)


def _start(hours_ahead: float) -> datetime:
    return T + timedelta(hours=hours_ahead)


def test_idle_when_nothing_upcoming() -> None:
    d = decide_cadence([], T, live=False, spike_window=SPIKE,
                       pre_match_sec=3600, spike_sec=900,
                       live_sec=900, idle_sec=21600)
    assert d.mode == "idle"
    assert d.interval_sec == 21600


def test_prematch_when_matches_upcoming_within_horizon() -> None:
    starts = [_start(9), _start(11)]
    d = decide_cadence(starts, T, live=False, spike_window=SPIKE,
                       pre_match_sec=3600, spike_sec=900,
                       live_sec=900, idle_sec=21600)
    assert d.mode == "prematch"
    assert d.interval_sec == 3600


def test_spike_when_kickoff_within_window() -> None:
    starts = [_start(0.5), _start(9)]
    d = decide_cadence(starts, T, live=False, spike_window=SPIKE,
                       pre_match_sec=3600, spike_sec=900,
                       live_sec=900, idle_sec=21600)
    assert d.mode == "spike"
    assert d.interval_sec == 900


def test_live_outranks_spike_and_prematch() -> None:
    starts = [_start(0.5), _start(9)]
    d = decide_cadence(starts, T, live=True, spike_window=SPIKE,
                       pre_match_sec=3600, spike_sec=900,
                       live_sec=900, idle_sec=21600)
    assert d.mode == "live"
    assert d.interval_sec == 900


def test_any_live_window() -> None:
    now = T
    assert any_live([now - timedelta(minutes=30)], now, LIVE_WIN)
    assert any_live([now - timedelta(hours=1)], now, LIVE_WIN)
    assert not any_live([now - timedelta(hours=5)], now, LIVE_WIN)
    assert not any_live([now + timedelta(minutes=5)], now, LIVE_WIN)


def _match(hours_from_now: float, completed: bool = False,
           match_id: str | None = None) -> Match:
    return Match(
        id=match_id or f"m-{hours_from_now}", sport_key="basketball_nba",
        commence_time=_start(hours_from_now),
        home_team="A", away_team="B", completed=completed)


def test_not_completed_starts_filters_by_window() -> None:
    matches = [
        _match(-5, completed=True),                      # finished, long past
        _match(-2, completed=False),                     # in progress
        _match(1, completed=False),                      # upcoming
        _match(48, completed=False, match_id="far"),     # beyond horizon
    ]
    starts = not_completed_starts(matches, now=T,
                                  live_window=LIVE_WIN, horizon=HORIZON)
    assert starts == (_start(-2), _start(1)), starts


def test_completed_match_dropped_even_within_window() -> None:
    matches = [_match(-1, completed=True)]
    starts = not_completed_starts(matches, now=T,
                                  live_window=LIVE_WIN, horizon=HORIZON)
    assert starts == ()