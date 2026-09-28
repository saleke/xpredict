"""Scheduler — decides *when* cycles run and executes them on that cadence.

The cadence adapts to the match schedule seen in the last fetch:

  live     -> tick fastest (polling over live games)
  spike    -> kickoff within the spike window: keep polling hard
  prematch -> matches upcoming within the horizon: poll quietly
  idle     -> nothing upcoming: back off to save credits

Settlement is graded on its own cadence. The credit budget is respected:
below ``credit_warn`` the quiet modes degrade to idle; below ``credit_stop``
only settlement runs.

The loop is driven by an injectable clock (``now_fn``) and an injectable
``sleep_fn`` so the whole thing is testable without sleeping. ``run_forever``
is the long-running entry; ``tick`` is the unit of work.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from . import config as cfg
from .cadence import any_live, CadenceDecision, decide_cadence
from .notify import LogNotifier, Notifier
from .key_pool import QuotaExhausted
from .odds import utcnow
from .pipeline import CycleReport, Pipeline
from .settle import SettlementReport, run_settlement
from .storage import Storage
from .tracker import Tracker


@dataclass
class TickSummary:
    now: datetime
    mode: str
    ran_cycle: bool = False
    ran_settlement: bool = False
    cycle_reports: list[CycleReport] = field(default_factory=list)
    settlement: Optional[SettlementReport] = None
    skipped_reason: Optional[str] = None
    errors: list[str] = field(default_factory=list)


@dataclass
class RunStats:
    ticks: int = 0
    cycles_run: int = 0
    settlements_run: int = 0
    cycles_skipped: int = 0
    total_errors: int = 0


class Scheduler:
    """Owns the tick loop and the schedule state (last seen commence times)."""

    def __init__(self, client, storage: Storage, settings: cfg.Settings, *,
                 pipeline: Optional[Pipeline] = None,
                 notifier: Optional[Notifier] = None,
                 tracker: Optional[Tracker] = None,
                 now_fn: Optional[Callable[[], datetime]] = None):
        self.client = client
        self.storage = storage
        self._settings = settings
        self.notifier = notifier or LogNotifier()
        self.pipeline = pipeline or Pipeline(client, storage, settings,
                                             notifier=self.notifier)
        self.tracker = tracker
        self._now_fn = now_fn or utcnow
        self.next_cycle_at: Optional[datetime] = None   # None == due now
        self.next_settle_at: Optional[datetime] = None
        self._commences: tuple[datetime, ...] = ()      # last snapshot starts
        self.stats = RunStats()

    @property
    def settings(self):
        """Effective settings, re-read on every access.

        May be a ``Settings`` instance or a zero-argument callable returning one.
        The callable form is how the admin panel applies changes to a running
        process: the scheduler and pipeline ask for the current settings each
        time they need one instead of holding a snapshot from construction.
        """
        s = self._settings
        return s() if callable(s) else s

    # -- public ---------------------------------------------------------------

    def tick(self, now: Optional[datetime] = None,
             *, only: Optional[str] = None) -> TickSummary:
        """One unit of scheduler work: run whatever is due at ``now``.

        ``only`` restricts the work to ``"cycle"`` or ``"settlement"``. The
        normal loop leaves it unset and the tick does both when both are due;
        the admin console needs the narrow form, because an operator asking to
        "settle now" should not also trigger a full fixture poll and spend a
        cycle of API budget as a side effect.
        """
        if only is not None and only not in ("cycle", "settlement"):
            raise ValueError(f"only must be 'cycle', 'settlement' or None, got {only!r}")
        now = now or self._now()
        summary = TickSummary(now=now, mode="")

        decision = self._decide(now)
        summary.mode = decision.mode

        if only != "settlement" and (
                self.next_cycle_at is None or now >= self.next_cycle_at):
            blocked = self._credit_block(decision.mode)
            if blocked is not None:
                summary.skipped_reason = blocked
                self.stats.cycles_skipped += 1
                # back off hard until credits recover
                self.next_cycle_at = now + timedelta(
                    seconds=self.settings.cadence_idle_sec)
            else:
                try:
                    self._run_cycle(summary, now, live=decision.mode == "live")
                except QuotaExhausted as exc:
                    # The pool ran dry mid-cycle. Record it like a blocked cycle
                    # and fall through to settlement, so the loop keeps serving
                    # and resumes polling when credits return.
                    summary.errors.append(str(exc))
                    summary.skipped_reason = str(exc)
                    self.stats.cycles_skipped += 1
                    self.next_cycle_at = now + timedelta(
                        seconds=self.settings.cadence_idle_sec)
                else:
                    decision = self._decide(now)  # fresh snapshot -> next cadence
                    summary.mode = decision.mode
                    self.next_cycle_at = now + timedelta(
                        seconds=decision.interval_sec)

        if only != "cycle" and (
                self.next_settle_at is None or now >= self.next_settle_at):
            report = run_settlement(self.client, self.storage, self.settings,
                                    now=now)
            summary.settlement = report
            summary.ran_settlement = True
            self.stats.settlements_run += 1
            self.next_settle_at = now + timedelta(
                seconds=self.settings.cadence_settle_sec)
            if self.tracker is not None:
                self.tracker.record_settlement(report, ts=now)

        return summary

    def run_forever(self, *, stop_at: Optional[datetime] = None,
                    max_ticks: Optional[int] = None,
                    sleep_fn: Callable[[float], None] = time.sleep,
                    tick_interval: float = 2.0) -> None:
        """Long-running loop; returns when ``stop_at`` passes or after
        ``max_ticks`` (the latter is a test/CI guard)."""
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            now = self._now()
            if stop_at is not None and now >= stop_at:
                return
            summary = self.tick(now)
            ticks += 1
            self.stats.ticks = ticks
            if summary.errors:
                self.stats.total_errors += len(summary.errors)
                self.notifier.send(
                    f"[lisa] tick had {len(summary.errors)} error(s): "
                    f"{summary.errors[:3]}")
            due = min(self.next_cycle_at or now, self.next_settle_at or now)
            delay = max(0.0, (due - self._now()).total_seconds())
            if delay > 0:
                sleep_fn(min(delay, tick_interval))
        return

    # -- internals ------------------------------------------------------------

    def _now(self) -> datetime:
        return self._now_fn()

    def _live_now(self, now: datetime) -> bool:
        """True when a match is under way *and* in-play polling is switched on.

        Live cadence costs roughly five times a pre-match poll, so it must never
        engage just because a match happens to be running. With in-play disabled
        a started match is left on the ordinary schedule: the value of an
        in-play price is that it is current, and nothing is refreshing it.
        """
        if not getattr(self.settings, "enable_inplay", False):
            return False
        window = timedelta(hours=getattr(
            self.settings, "inplay_tail_hours", 2.0))
        return any_live(self._commences, now, window)

    def _decide(self, now: datetime) -> CadenceDecision:
        s = self.settings
        return decide_cadence(
            self._commences, now,
            live=self._live_now(now),
            spike_window=timedelta(seconds=s.spike_window_sec),
            pre_match_sec=s.cadence_prematch_sec,
            spike_sec=s.cadence_spike_sec,
            live_sec=s.cadence_live_sec,
            idle_sec=s.cadence_idle_sec,
        )

    def _credit_block(self, mode: str) -> Optional[str]:
        """Reason the cycle must be skipped, or None when it may run.

        A rotating pool reports the *combined* remaining quota, so the per-key
        floor has to come from the pool itself; a key that is spent must not
        stop the whole scheduler when a sibling key still has credits.
        """
        pool = getattr(self.client, "pool", None)
        if pool is not None and callable(getattr(pool, "any_exhausted", None)):
            try:
                if pool.any_exhausted():
                    return "all API keys spent (quota floor or daily budget)"
            except Exception:
                pass

        remaining = getattr(self.client, "last_remaining", None)
        if remaining is None:
            return None
        if remaining <= self.settings.credit_stop and mode != "live":
            return f"credit stop ({remaining} left)"
        if remaining <= self.settings.credit_warn and mode in ("prematch", "idle"):
            return f"credit warning ({remaining} left)"
        return None

    def _run_cycle(self, summary: TickSummary, now: datetime, *,
                   live: bool) -> None:
        reports = self.pipeline.run_cycle(live=live, now=now)
        summary.cycle_reports = reports
        summary.ran_cycle = True
        self.stats.cycles_run += 1

        horizon = timedelta(hours=self.settings.horizon_hours)
        live_win = timedelta(hours=self.settings.live_window_hours)
        fresh: list[datetime] = []
        for r in reports:
            if r.errors:
                summary.errors.extend(f"{r.sport_key}: {e}" for e in r.errors)
            fresh.extend(m.commence_time for m in r.matches if not m.completed)
        lo = now - live_win
        hi = now + horizon
        self._commences = tuple(sorted(
            c for c in fresh if lo <= c <= hi))

        if self.tracker is not None:
            self.tracker.record_cycles(reports, ts=now)
            self.tracker.record_picks(ts=now)