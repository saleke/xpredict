"""A pool of The Odds API keys with rotation and a per-key spend budget.

A paid odds key is a finite resource: the quota does not refill on its own, and
a tight polling loop can drain it overnight. This module makes that explicit.

``OddsKeyPool`` owns the keys and the per-key accounting, and
``RotatingOddsClient`` is a drop-in replacement for ``OddsApiClient`` that
picks a key for every request. Three rules keep the spend bounded:

  * never use a key whose remaining quota is at or below the floor;
  * never exceed the per-key daily budget;
  * spread load over the healthiest key rather than draining the first one,
    so a second key genuinely doubles the useful runtime.

The pool is deliberately transport-agnostic: it hands out a key and is told the
outcome, which keeps the budget logic testable without any network access.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from .client import (
    DISABLED_HINT,
    ApiError,
    OddsApiClient,
    OddsApiDisabled,
    RateLimited,
)

logger = logging.getLogger(__name__)


class QuotaExhausted(ApiError):
    """No key in the pool can serve another request right now."""


@dataclass
class KeyState:
    """One key's live accounting."""

    key: str
    remaining: Optional[int] = None
    used: int = 0
    spent_today: int = 0
    budget_day: str = ""
    requests: int = 0
    errors: int = 0
    last_used_at: float = 0.0
    disabled_until: float = 0.0

    def label(self) -> str:
        """A non-secret identifier safe to log and to show in the UI."""
        return f"{self.key[:4]}…{self.key[-2:]}" if len(self.key) > 8 else "…"


@dataclass
class PoolStatus:
    """A snapshot of the pool, safe to serialise into the dashboard."""

    keys: list[dict[str, Any]] = field(default_factory=list)
    total_remaining: Optional[int] = None
    total_spent_today: int = 0
    daily_budget: int = 0
    state: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "keys": self.keys,
            "total_remaining": self.total_remaining,
            "total_spent_today": self.total_spent_today,
            "daily_budget": self.daily_budget,
            "state": self.state,
        }


class OddsKeyPool:
    """Tracks quota and spend across several API keys."""

    def __init__(
        self,
        keys: Iterable[str],
        *,
        budget_daily: int = 50,
        credit_warn: int = 100,
        credit_stop: int = 20,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        cleaned: list[str] = []
        for raw in keys:
            key = (raw or "").strip()
            if key and key not in cleaned:
                cleaned.append(key)
        if not cleaned:
            raise ValueError("OddsKeyPool needs at least one API key")
        self.now_fn = now_fn
        self.budget_daily = max(0, int(budget_daily))
        self.credit_warn = max(0, int(credit_warn))
        self.credit_stop = max(0, int(credit_stop))
        self._states = [KeyState(key=k) for k in cleaned]
        self._lock = threading.Lock()
        self._warned = False

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _day(ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

    def _roll_day(self, state: KeyState, ts: float) -> None:
        """Reset the daily counter when the UTC day turns over."""
        today = self._day(ts)
        if state.budget_day != today:
            state.budget_day = today
            state.spent_today = 0

    def _usable(self, state: KeyState, ts: float) -> tuple[bool, str]:
        if ts < state.disabled_until:
            return False, "cooling down after a rate limit"
        if self.budget_daily and state.spent_today >= self.budget_daily:
            return False, f"daily budget spent ({state.spent_today}/{self.budget_daily})"
        if state.remaining is not None and state.remaining <= self.credit_stop:
            return False, f"quota floor reached ({state.remaining} left)"
        return True, ""

    # -- public --------------------------------------------------------------

    @property
    def size(self) -> int:
        return len(self._states)

    def pick(self) -> KeyState:
        """Choose the healthiest usable key.

        Preference order: a key with unknown quota (never used) first so its
        real balance is learned early, then the one with the most headroom.
        """
        with self._lock:
            ts = self.now_fn()
            usable: list[KeyState] = []
            reasons: list[str] = []
            for state in self._states:
                self._roll_day(state, ts)
                ok, why = self._usable(state, ts)
                if ok:
                    usable.append(state)
                else:
                    reasons.append(f"{state.label()}: {why}")
            if not usable:
                detail = "; ".join(reasons) or "no keys configured"
                raise QuotaExhausted(f"no usable API key ({detail})")

            fresh = [s for s in usable if s.remaining is None]
            if fresh:
                return min(fresh, key=lambda s: s.requests)

            def headroom(s: KeyState) -> tuple[int, int]:
                room = (s.remaining or 0) - self.credit_stop
                budget_left = (
                    (self.budget_daily - s.spent_today) if self.budget_daily else 10**9
                )
                # Spend is bounded by the smaller of quota room and daily budget.
                return (min(room, budget_left), s.remaining or 0)

            chosen = max(usable, key=headroom)
            if (self.budget_daily and chosen.spent_today + 1 > self.budget_daily) or (
                chosen.remaining is not None
                and chosen.remaining - 1 < self.credit_stop
            ):
                # Headroom is under one request: fall back to any usable key.
                chosen = max(usable, key=lambda s: s.remaining or 0)
            return chosen

    def record_use(self, state: KeyState) -> None:
        with self._lock:
            ts = self.now_fn()
            self._roll_day(state, ts)
            state.requests += 1
            state.spent_today += 1
            state.last_used_at = ts

    def record_response(
        self,
        state: KeyState,
        *,
        used: Optional[int] = None,
        remaining: Optional[int] = None,
    ) -> None:
        with self._lock:
            if used is not None:
                state.used = int(used)
            if remaining is not None:
                state.remaining = int(remaining)

    def record_error(self, state: KeyState, *, rate_limited: bool = False) -> None:
        with self._lock:
            state.errors += 1
            if rate_limited:
                # A 429 means the key is being throttled; stop using it for a
                # while rather than burning retries against the same limit.
                state.disabled_until = self.now_fn() + 300.0

    @property
    def last_remaining(self) -> Optional[int]:
        """Combined remaining quota, for the existing single-key callers."""
        known = [s.remaining for s in self._states if s.remaining is not None]
        return sum(known) if known else None

    def any_exhausted(self) -> bool:
        with self._lock:
            ts = self.now_fn()
            for state in self._states:
                self._roll_day(state, ts)
                ok, _ = self._usable(state, ts)
                if ok:
                    return False
            return True

    def status(self) -> PoolStatus:
        with self._lock:
            ts = self.now_fn()
            rows: list[dict[str, Any]] = []
            for state in self._states:
                self._roll_day(state, ts)
                ok, why = self._usable(state, ts)
                rows.append({
                    "label": state.label(),
                    "remaining": state.remaining,
                    "used": state.used or None,
                    "spent_today": state.spent_today,
                    "requests": state.requests,
                    "errors": state.errors,
                    "usable": ok,
                    "reason": why or None,
                })
            known = [s.remaining for s in self._states if s.remaining is not None]
            total = sum(known) if known else None
            spent = sum(s.spent_today for s in self._states)

        if not rows:
            state_name = "unknown"
        elif not any(r["usable"] for r in rows):
            state_name = "exhausted"
        elif total is not None and total <= self.credit_warn * len(rows):
            state_name = "constrained"
        elif spent and self.budget_daily and spent >= self.budget_daily * len(rows):
            state_name = "constrained"
        else:
            state_name = "ok"
        return PoolStatus(
            keys=rows,
            total_remaining=total,
            total_spent_today=spent,
            daily_budget=self.budget_daily * len(rows),
            state=state_name,
        )


class RotatingOddsClient:
    """Drop-in ``OddsApiClient`` replacement that rotates across a key pool."""

    def __init__(
        self,
        keys: Iterable[str],
        *,
        base_url: str = "https://api.the-odds-api.com",
        budget_daily: int = 50,
        credit_warn: int = 100,
        credit_stop: int = 20,
        timeout: float = 15.0,
        max_retries: int = 4,
        backoff_base: float = 2.0,
        now_fn: Callable[[], float] = time.time,
        enabled: bool = True,
    ) -> None:
        self.pool = OddsKeyPool(
            keys,
            budget_daily=budget_daily,
            credit_warn=credit_warn,
            credit_stop=credit_stop,
            now_fn=now_fn,
        )
        # The low-quota warning fires once per process, not once per request.
        self._warned = False
        self.enabled = enabled
        # One transport per key so retries and quota headers stay per key.
        # The kill switch is pushed down into every transport, so a disabled
        # pool cannot reach the network through any of its keys.
        self._transports = [
            OddsApiClient(
                key,
                base_url=base_url,
                timeout=timeout,
                max_retries=max_retries,
                backoff_base=backoff_base,
                enabled=enabled,
            )
            for key in [s.key for s in self.pool._states]
        ]

    # -- quota surface used elsewhere ---------------------------------------

    @property
    def last_remaining(self) -> Optional[int]:
        return self.pool.last_remaining

    def status(self) -> PoolStatus:
        return self.pool.status()

    # -- endpoints (same signatures as OddsApiClient) ------------------------

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Run one request on a pooled key, accounting for it either way."""
        # Refuse before pool.pick()/record_use() so a disabled pool does not
        # write a phantom credit-spend into the ledger, which would make the
        # budget look spent without a single request leaving the process.
        if not self.enabled:
            raise OddsApiDisabled(DISABLED_HINT)
        state = self.pool.pick()
        transport = self._transports[self._index_of(state)]
        self.pool.record_use(state)
        try:
            result = getattr(transport, method)(*args, **kwargs)
        except RateLimited:
            self.pool.record_error(state, rate_limited=True)
            raise
        except Exception:
            self.pool.record_error(state)
            raise
        # Read quota headers defensively: the fetch already succeeded, and a
        # transport without these attributes (or a custom client) must not make
        # us throw away a good response.
        self.pool.record_response(
            state,
            used=getattr(transport, "last_used", None),
            remaining=getattr(transport, "last_remaining", None),
        )
        if not self._warned and state.remaining is not None and state.remaining <= self.pool.credit_warn:
            self._warned = True
            logger.warning(
                "odds quota low on key %s: %s credits left",
                state.label(), state.remaining,
            )
        return result

    def _index_of(self, state: KeyState) -> int:
        for i, candidate in enumerate(self.pool._states):
            if candidate is state:
                return i
        return 0

    def list_sports(self) -> list[dict]:
        return self._call("list_sports")

    def get_odds(self, sport_key: str, *, regions: str = "eu,us",
                 markets: str = "h2h", odds_format: str = "decimal",
                 date_format: str = "iso") -> list[dict]:
        return self._call(
            "get_odds", sport_key, regions=regions, markets=markets,
            odds_format=odds_format, date_format=date_format,
        )

    def fetch_league_odds(self, sport_key: str, region: str = "eu,us") -> list[dict]:
        return self.get_odds(sport_key, regions=region, markets="h2h")

    def get_scores(self, sport_key: str, *, days_from: int = 1) -> list[dict]:
        return self._call("get_scores", sport_key, days_from=days_from)


def pool_from_settings(settings: Any) -> Optional[OddsKeyPool]:
    """Build a pool from whatever keys the environment provided."""
    keys = list(getattr(settings, "odds_api_keys", ()) or ())
    if not keys:
        single = getattr(settings, "odds_api_key", "") or ""
        keys = [single] if single else []
    if not keys:
        return None
    return OddsKeyPool(
        keys,
        budget_daily=int(getattr(settings, "credit_budget_daily", 0) or 0),
        credit_warn=int(getattr(settings, "credit_warn", 100) or 100),
        credit_stop=int(getattr(settings, "credit_stop", 20) or 20),
    )
