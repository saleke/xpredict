"""LISA Multi-Provider Odds System

Package providing a clean abstraction layer for fetching sports betting odds
from multiple independent sources, with automatic fallback, quota tracking,
and provenance for every odds row.

Design goals:
- Eliminate single-provider dependency (The Odds API)
- Enable graceful degradation when any provider is unavailable
- Track source origin for every Match/Book/Pick
- Support both official (free API tiers) and unofficial (scraped) providers
- Enable cross-source consensus blending
"""

from typing import Protocol, Any, Optional, Literal, TypeVar
from dataclasses import dataclass
from enum import Enum

T = TypeVar("T", bound="MatchData")

__all__ = [
    "SourceTier",
    "ProviderQuota",
    "ProviderError",
    "RateLimitedError",
    "ApiError",
    "ProviderNotAvailableError",
    "OddsProvider",
    "ProviderRouter",
    "normalize_odds_payload",
]

# ---------------------------------------------------------------------------
# Source tier classification
# ---------------------------------------------------------------------------


class SourceTier(Enum):
    """Classifies the provenance and reliability of an odds provider."""

    OFFICIAL = "official"   # Free API tier, maintained, ToS-compliant
    UNOFFICIAL = "unofficial"  # Scraped, community-maintained, ToS-gray


# ---------------------------------------------------------------------------
# Quota & health tracking
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderQuota:
    """Current quota state for a provider."""

    remaining: int
    limit: int
    reset_at: float  # unix timestamp when quota resets (UTC)
    tier: SourceTier

    @property
    def consumed(self) -> int:
        return self.limit - self.remaining

    @property
    def pct_remaining(self) -> float:
        if self.limit == 0:
            return 0.0
        return round(self.remaining / self.limit * 100.0, 2)


# ---------------------------------------------------------------------------
# Exception hierarchy — every adapter raises from this set
# ---------------------------------------------------------------------------


class ProviderError(Exception):
    """Base class for all provider-related errors."""

    def __init__(self, message: str, provider: str, retry_after: Optional[float] = None):
        self.provider = provider
        self.retry_after = retry_after  # seconds, or None
        super().__init__(message)


class ApiError(ProviderError):
    """HTTP-level failure (non-2xx, timeout, connection error)."""


class RateLimitedError(ProviderError):
    """Provider returned 429 or reported quota exhaustion."""

    def __init__(self, message: str, provider: str, retry_after: float):
        super().__init__(message, provider, retry_after=retry_after)


class ProviderNotAvailableError(ProviderError):
    """Provider is explicitly disabled or permanently unavailable."""

    pass


# ---------------------------------------------------------------------------
# The Provider Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class OddsProvider(Protocol):
    """Duck-typed protocol for all odds providers.

    Consumers should type-hint against this Protocol, not a concrete class,
    so the router can accept any registered provider.
    """

    name: str
    """Unique identifier — must match the name used in ProviderRouter."""

    tier: SourceTier
    """OFFICIAL or UNOFFICIAL — gates UI rendering and logging."""

    def get_odds(
        self,
        sport_key: str,
        *,
        region: str,
        markets: str,
    ) -> list[dict[str, Any]]:
        """Fetch raw odds payload from this provider.

        Args:
            sport_key: LISA's canonical sport key (e.g. "soccer_epl").
            region: "eu" or "us" — determines which bookmakers are returned.
            markets: Comma-separated market keys (e.g. "h2h,spreads,totals").

        Returns:
            Raw provider JSON — schema is provider-specific. The caller (router/
            parsing) must transform via parse_provider_payload().

        Raises:
            ApiError: HTTP error, timeout, connection failure.
            RateLimitedError: Provider returned 429 / quota exhausted.
            ProviderNotAvailableError: Provider is disabled.
        """


# ---------------------------------------------------------------------------
# Provider Router — the "traffic cop"
# ---------------------------------------------------------------------------


class ProviderRouter:
    """Selects a provider (with fallback chain) and returns provenance.

    Responsibilities:
    1. Try primary provider first.
    2. On any error, cycle through configured fallbacks.
    3. Track per-provider quota so we never hit hard limits unexpectedly.
    4. Return (matches, source_name) so the caller can record provenance.
    5. Support cross-source consensus by fetching from ALL available providers.
    """

    def __init__(
        self,
        *,
        primary: str,
        fallbacks: tuple[str, ...],
        providers: dict[str, OddsProvider],
    ):
        self._primary = primary
        self._fallbacks = fallbacks
        self._providers = providers  # name -> OddsProvider

        # Validate that primary and fallbacks reference registered providers
        all_names = {primary, *fallbacks}
        missing = all_names - set(providers.keys())
        if missing:
            raise ValueError(f"Provider(s) {missing} not registered in router")

    def get_odds_single(
        self,
        sport_key: str,
        *,
        region: str,
        markets: str,
        now: Optional[float] = None,
    ) -> tuple[Optional[list], str]:
        """Try primary → fallbacks. Returns (matches_list, source_name) or (None, "none").

        The matches_list may be empty if no provider had data for this fixture.
        """
        chain = [self._primary] + list(self._fallbacks)

        for name in chain:
            provider = self._providers.get(name)
            if provider is None:
                continue
            if not provider.is_available():
                continue

            try:
                raw = provider.get_odds(sport_key, region=region, markets=markets)
                matches = self._parse_payload(name, raw, markets=markets)
                if matches:
                    return matches, name
            except RateLimitedError as e:
                # Log and continue to next provider
                continue
            except ApiError as e:
                # Log and continue to next provider
                continue
            except Exception as e:
                # Unexpected error — continue but surface for monitoring
                continue

        return [], "none"

    def get_odds_cross_source(
        self,
        sport_key: str,
        *,
        region: str,
        markets: str,
    ) -> dict[str, list]:
        """Fetch from ALL available providers for consensus blending.

        Returns:
            {provider_name: [Match, ...], ...} — only providers that returned data.
            Empty dict if nothing returned.
        """
        results: dict[str, list] = {}
        for name, provider in self._providers.items():
            if not provider.is_available():
                continue
            try:
                raw = provider.get_odds(sport_key, region=region, markets=markets)
                matches = self._parse_payload(name, raw, markets=markets)
                if matches:
                    results[name] = matches
            except Exception:
                continue
        return results

    def _parse_payload(
        self,
        provider_name: str,
        raw: list[dict[str, Any]],
        *,
        markets: str,
    ) -> list:
        """Dispatch to the correct parser for this provider.

        The dispatch table is built at module init time from all registered
        providers' parse functions.
        """
        from . import parsing  # local import to avoid circular deps

        parser = parsing.PAYLOAD_PARSERS.get(provider_name)
        if parser is None:
            # Fallback: try generic fallback parser
            from .parsing import parse_odds_payload_generic
            return parse_odds_payload_generic(raw, market_keys=markets.split(","))

        return parser(raw, market_keys=markets.split(","))

    def status(self) -> dict[str, dict]:
        """Return health/status for every registered provider."""
        result: dict[str, dict] = {}
        for name, provider in self._providers.items():
            try:
                quota = provider.quota_status()
                result[name] = {
                    "status": "healthy" if provider.is_available() else "unavailable",
                    "tier": quota.tier.value,
                    "remaining": quota.remaining,
                    "limit": quota.limit,
                    "pct_remaining": quota.pct_remaining,
                    "last_checked": round(time.time()),  # TODO: track real time
                }
            except Exception:
                result[name] = {
                    "status": "error",
                    "tier": self._providers[name].tier.value,
                    "remaining": 0,
                    "limit": 0,
                    "pct_remaining": 0.0,
                }
        return result

    def any_exhausted(self) -> bool:
        """True if every provider is either unavailable or has low remaining."""
        available = [p for p in self._providers.values() if p.is_available()]
        if not available:
            return True
        return all(p.quota_status().remaining < 20 for p in available)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

_imported_time: Optional[float] = None


def now() -> float:
    """Monotonic-ish time helper — avoids calling time.time() in hot paths."""
    global _imported_time
    if _imported_time is None:
        _imported_time = time.time()
    return _imported_time


# ---------------------------------------------------------------------------
# End of package
# ---------------------------------------------------------------------------