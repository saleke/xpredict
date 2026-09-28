"""Multi-provider odds layer for LISA.

The engine must never be pinned to a single odds feed. This package defines a
small, explicit provider contract plus a router that:

* tries providers in a configured order and degrades to the next one on any
  failure (timeouts, rate limits, quota exhaustion, malformed payloads);
* records which source produced every fixture, so provenance survives into the
  cache, the ledger and the board;
* short-circuits a provider that is known-bad instead of paying its timeout on
  every single cycle (the failure mode that actually hurts throughput);
* treats quota as a first-class budget, so a poll is skipped rather than made
  when it cannot help.

Design notes
------------
* Each provider owns its own payload -> domain translation (``parse_odds``).
  There is no global parser registry: adding a feed means adding one class,
  and a provider can never silently be parsed by the wrong adapter.
* The router is defensive by construction. It never lets a provider exception
  escape into the pipeline, and it distinguishes "provider returned nothing"
  from "provider failed" so operators can see which of the two happened.
* Monotonic clocks are used for every cooldown/interval decision; wall clock is
  only used for values that are persisted or displayed.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence, runtime_checkable

logger = logging.getLogger("lisa.providers")

__all__ = [
    "SourceTier",
    "ProviderQuota",
    "ProviderError",
    "ApiError",
    "RateLimitedError",
    "ProviderNotAvailableError",
    "OddsProvider",
    "ProviderRouter",
    "CircuitState",
]


class SourceTier(Enum):
    """How much the feed can be trusted, and how it must be surfaced."""

    OFFICIAL = "official"      # Licensed/ToS-compliant API, free tier is fine.
    UNOFFICIAL = "unofficial"  # Scraped or community-maintained: flagged in UI.


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderQuota:
    """Remaining request budget for a provider.

    ``limit <= 0`` means "not metered" (a free or unmetered feed); such a
    provider must never be treated as exhausted.
    """

    remaining: int
    limit: int
    reset_at: float = 0.0
    tier: SourceTier = SourceTier.OFFICIAL

    @property
    def metered(self) -> bool:
        return self.limit > 0

    @property
    def consumed(self) -> int:
        return max(0, self.limit - self.remaining) if self.metered else 0

    @property
    def pct_remaining(self) -> float:
        if not self.metered:
            return 100.0
        return round(max(0.0, self.remaining / self.limit * 100.0), 2)

    def has_headroom(self, need: int = 1) -> bool:
        """True when at least ``need`` more calls can be made."""
        if not self.metered:
            return True
        return self.remaining >= need


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    """Base class for provider failures.

    ``retry_after`` lets the router honour a server-supplied cooldown instead
    of guessing, which is what keeps a rate-limited feed from eating the
    cycle budget.
    """

    retryable = True

    def __init__(self, message: str, *, provider: str = "", retry_after: Optional[float] = None):
        super().__init__(message)
        self.provider = provider
        self.retry_after = retry_after


class ApiError(ProviderError):
    """Transport failure: timeout, DNS, TLS, or a non-2xx response."""


class ParseError(ProviderError):
    """The provider answered, but the payload could not be trusted.

    Treated as a provider fault (and not as "no fixtures") so a silent schema
    change surfaces as an error instead of as an empty board.
    """


class RateLimitedError(ProviderError):
    """429, or a quota response that would not have been billed."""

    def __init__(self, message: str, *, provider: str = "", retry_after: float = 60.0):
        super().__init__(message, provider=provider, retry_after=retry_after)


class ProviderNotAvailableError(ProviderError):
    """Disabled by config, or deliberately left unconfigured."""

    retryable = False


# ---------------------------------------------------------------------------
# Provider contract
# ---------------------------------------------------------------------------


@runtime_checkable
class OddsProvider(Protocol):
    """The contract every feed adapter implements.

    Kept deliberately narrow: four calls and two status calls. Anything a feed
    needs beyond that belongs inside the adapter, not in the router.
    """

    name: str
    tier: SourceTier

    def get_odds(self, sport_key: str, *, region: str, markets: str) -> list[dict[str, Any]]:
        """Return the raw odds payload for one league."""

    def get_events(self, sport_key: str) -> list[dict[str, Any]]:
        """Return fixtures (ideally a free/discovery call)."""

    def get_scores(self, sport_key: str) -> list[dict[str, Any]]:
        """Return final/live scores used for settlement."""

    def parse_odds(self, raw: Iterable[Mapping[str, Any]], *, market_keys: Sequence[str]) -> list:
        """Translate this provider's payload into LISA ``Match`` objects.

        Owned by the adapter on purpose: the schema knowledge stays next to the
        client that produced it, and a mistyped provider name can never be
        parsed by the wrong adapter.
        """

    def quota_status(self) -> ProviderQuota:
        """Report the budget left, for scheduling decisions."""

    def is_available(self) -> bool:
        """False when unconfigured or administratively disabled."""


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


@dataclass
class CircuitState:
    """Per-provider health, used to stop paying for calls that cannot work."""

    consecutive_failures: int = 0
    open_until: float = 0.0        # monotonic deadline while the breaker is open
    last_success: float = 0.0      # monotonic
    last_error: str = ""
    last_error_at: float = 0.0

    def trip(self, now: float, cooldown: float, error: str) -> None:
        self.consecutive_failures += 1
        self.open_until = max(self.open_until, now + cooldown)
        self.last_error = error
        self.last_error_at = now

    def record_success(self, now: float) -> None:
        self.consecutive_failures = 0
        self.open_until = 0.0
        self.last_success = now

    def is_open(self, now: float) -> bool:
        return now < self.open_until


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class ProviderRouter:
    """Chooses which feed to trust for a league, with a fallback chain.

    Ordering policy (why this matters for throughput):

    1. Skip providers that are administratively unavailable.
    2. Skip providers whose circuit breaker is open (no point paying a timeout
       on every cycle for a feed that is already down).
    3. Skip providers with no quota headroom, so the last few credits are spent
       deliberately rather than by a poll that returns nothing useful.
    4. Ask the primary first; on any failure, walk the fallback chain once.
    5. Never raise. A dead feed must not be able to fail a pipeline cycle.
    """

    #: Failures before a provider is benched for ``base_cooldown``.
    FAILURE_THRESHOLD = 3
    #: Backoff ladder, indexed by consecutive failures past the threshold.
    COOLDOWN_LADDER: tuple[float, ...] = (60.0, 300.0, 900.0, 1800.0)

    def __init__(
        self,
        providers: Sequence[OddsProvider],
        *,
        primary: str,
        fallbacks: Sequence[str] = (),
        min_quota: int = 1,
    ) -> None:
        if not providers:
            raise ValueError("ProviderRouter requires at least one provider")

        self._by_name: dict[str, OddsProvider] = {}
        for p in providers:
            name = getattr(p, "name", "") or type(p).__name__.lower()
            self._by_name[name] = p
            # ``name`` on the instance shadows the class attribute for the
            # router's lookups; keep the canonical form on the object too.
            try:
                p.name = name  # type: ignore[misc]
            except Exception:  # frozen/slots adapter: lookups still work
                pass

        self.primary = primary
        self.fallbacks = tuple(f for f in fallbacks if f != primary)
        self.min_quota = max(1, int(min_quota))

        unknown = {primary, *self.fallbacks} - set(self._by_name)
        if unknown:
            raise ValueError(
                f"Unknown provider(s) {sorted(unknown)}; registered: {sorted(self._by_name)}"
            )

        self._states: dict[str, CircuitState] = {n: CircuitState() for n in self._by_name}
        self._lock = threading.Lock()

    # -- introspection ----------------------------------------------------

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(self._by_name)

    def _cooldown_for(self, failures: int) -> float:
        idx = min(max(failures - self.FAILURE_THRESHOLD, 0), len(self.COOLDOWN_LADDER) - 1)
        return self.COOLDOWN_LADDER[idx]

    def _usable(self, name: str, now: float) -> tuple[bool, str]:
        """Whether ``name`` may be called right now, and why not if it may not."""
        provider = self._by_name[name]
        try:
            if not provider.is_available():
                return False, "unavailable"
        except Exception as exc:  # a broken is_available() must not kill routing
            logger.warning("provider %s is_available() raised: %s", name, exc)
            return False, "is_available_error"

        state = self._states[name]
        if state.is_open(now):
            return False, f"circuit_open({max(0.0, state.open_until - now):.0f}s)"

        try:
            quota = provider.quota_status()
        except Exception as exc:
            logger.warning("provider %s quota_status() raised: %s", name, exc)
            return False, "quota_error"

        if not quota.has_headroom(self.min_quota):
            return False, f"quota_exhausted({quota.remaining}/{quota.limit})"
        return True, ""

    # -- failure bookkeeping ---------------------------------------------

    def _record_ok(self, name: str, now: float) -> None:
        with self._lock:
            self._states[name].record_success(now)

    def _record_fail(self, name: str, now: float, exc: BaseException) -> None:
        with self._lock:
            state = self._states[name]
            state.trip(now, self._cooldown_for(state.consecutive_failures + 1), repr(exc))
            failures = state.consecutive_failures
        logger.warning(
            "provider %s failed (%d in a row), backing off: %s", name, failures, exc
        )

    # -- primary entry point ----------------------------------------------

    def get_odds(
        self,
        sport_key: str,
        *,
        region: str = "eu",
        markets: str = "h2h",
        market_keys: Optional[Sequence[str]] = None,
    ) -> tuple[list, str]:
        """Fetch one league, degrading through the fallback chain.

        Returns ``(matches, source_name)``. ``source_name`` is the provider
        that produced the data, or ``"none"`` when every candidate was skipped
        or failed. An empty list with a real source name means "the feed
        answered and has nothing for this league" -- a meaningful difference
        from a total failure, and the caller can tell them apart.
        """
        keys = tuple(market_keys) if market_keys else tuple(
            m.strip() for m in markets.split(",") if m.strip()
        )
        now = time.monotonic()

        for name in self._chain():
            ok, why = self._usable(name, now)
            if not ok:
                logger.debug("skipping provider %s: %s", name, why)
                continue

            provider = self._by_name[name]
            try:
                raw = provider.get_odds(sport_key, region=region, markets=markets)
            except RateLimitedError as exc:
                now = time.monotonic()
                if exc.retry_after:
                    with self._lock:
                        self._states[name].open_until = max(
                            self._states[name].open_until, now + float(exc.retry_after)
                        )
                self._record_fail(name, now, exc)
                continue
            except ProviderError as exc:
                self._record_fail(name, name and time.monotonic(), exc)
                continue
            except Exception as exc:  # never let a bad adapter break a cycle
                self._record_fail(name, time.monotonic(), exc)
                continue

            try:
                matches = provider.parse_odds(raw or [], market_keys=keys)
            except Exception as exc:
                self._record_fail(name, time.monotonic(), ParseError(f"{name}: {exc}"))
                continue

            self._record_ok(name, time.monotonic())
            return matches, name

        logger.warning("no provider produced odds for %s", sport_key)
        return [], "none"

    def get_odds_cross_source(
        self,
        sport_key: str,
        *,
        region: str = "eu",
        markets: str = "h2h",
        market_keys: Optional[Sequence[str]] = None,
    ) -> dict[str, list]:
        """Poll every usable provider, for cross-source consensus.

        Independent feeds fail in different ways, so blending them raises the
        effective book count without raising latency much: a provider already
        benched by its breaker is skipped instantly, and the rest are queried
        with the caller's own timeout budget.
        """
        keys = tuple(market_keys) if market_keys else tuple(
            m.strip() for m in markets.split(",") if m.strip()
        )
        now = time.monotonic()
        out: dict[str, list] = {}

        for name in self._by_name:
            ok, why = self._usable(name, now)
            if not ok:
                logger.debug("cross-source: skipping %s (%s)", name, why)
                continue
            provider = self._by_name[name]
            try:
                raw = provider.get_odds(sport_key, region=region, markets=markets)
                matches = provider.parse_odds(raw or [], market_keys=keys)
            except Exception as exc:
                self._record_fail(name, time.monotonic(), exc)
                continue
            if matches:
                out[name] = matches
                self._record_ok(name, time.monotonic())

        return out

    # -- health ------------------------------------------------------------

    def health(self) -> dict[str, dict[str, Any]]:
        """Per-provider status for the console/dashboard.

        Built defensively: a provider whose status calls raise is reported as
        degraded rather than allowed to break the health endpoint.
        """
        now = time.monotonic()
        out: dict[str, dict[str, Any]] = {}
        for name, provider in self._by_name.items():
            with self._lock:
                state = self._states[name]
                failures = state.consecutive_failures
                open_for = max(0.0, state.open_until - now)
                last_error = state.last_error
            entry: dict[str, Any] = {
                "tier": getattr(provider, "tier", SourceTier.OFFICIAL).value,
                "consecutive_failures": failures,
                "backing_off_for_sec": round(open_for),
                "last_error": last_error[:200] if last_error else "",
            }
            try:
                entry["quota"] = {
                    "remaining": provider.quota_status().remaining,
                    "limit": provider.quota_status().limit,
                    "pct_remaining": provider.quota_status().pct_remaining,
                }
            except Exception as exc:
                entry["quota"] = {"error": str(exc)[:120]}
            try:
                entry["available"] = bool(provider.is_available())
            except Exception as exc:
                entry["available"] = False
                entry["available_error"] = str(exc)[:120]

            if not entry.get("available"):
                entry["status"] = "disabled"
            elif open_for > 0:
                entry["status"] = "backing_off"
            elif failures:
                entry["status"] = "degraded"
            else:
                entry["status"] = "healthy"
            out[name] = entry
        return out

    def any_usable(self) -> bool:
        """True when at least one provider could serve a request right now."""
        now = time.monotonic()
        return any(self._usable(name, now)[0] for name in self._chain())

    def _chain(self) -> tuple[str, ...]:
        return (self.primary, *self.fallbacks)
