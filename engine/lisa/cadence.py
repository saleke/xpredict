"""Cadence logic — pure decision functions for *when* the next cycle runs.

State classes, derived from the latest snapshot of not-completed matches:

  live     — at least one match in progress
  spike    — a kickoff is within the spike window (default 90 min)
  prematch — matches are upcoming within the horizon
  idle     — nothing upcoming within the horizon

Priority: live > spike > prematch > idle.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from .odds import Match


@dataclass(frozen=True)
class CadenceDecision:
    mode: str          # "live" | "spike" | "prematch" | "idle"
    interval_sec: int
    reason: str


def not_completed_starts(matches: Iterable[Match], *, now: datetime,
                         live_window: timedelta,
                         horizon: timedelta) -> tuple[datetime, ...]:
    """Commence times worth remembering for cadence decisions: starts of
    not-completed matches between (now - live window) and (now + horizon)."""
    lo = now - live_window
    hi = now + horizon
    out = [m.commence_time for m in matches
           if not m.completed and lo <= m.commence_time <= hi]
    out.sort()
    return tuple(out)


def any_live(starts: Iterable[datetime], now: datetime,
             live_window: timedelta) -> bool:
    return any(s <= now < s + live_window for s in starts)


def decide_cadence(starts: Iterable[datetime], now: datetime, *,
                   live: bool,
                   spike_window: timedelta,
                   pre_match_sec: int, spike_sec: int,
                   live_sec: int, idle_sec: int) -> CadenceDecision:
    """Pick the cadence mode + interval for the next cycle."""
    starts = [s for s in starts]
    if live:
        return CadenceDecision("live", live_sec,
                               "match(es) in progress")
    if any(now <= s <= now + spike_window for s in starts):
        return CadenceDecision("spike", spike_sec,
                               "kickoff within spike window")
    if any(s > now for s in starts):
        return CadenceDecision("prematch", pre_match_sec,
                               "match(es) upcoming")
    return CadenceDecision("idle", idle_sec,
                           "nothing upcoming in horizon")