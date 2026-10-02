"""Match router — resolves a league to the free sources that can actually serve it.

Strategy
--------
Coverage is derived, not declared
    The old router carried a hand-maintained list of ~200 leagues with a
    Flashscore id each, and a tier letter implying which source would answer.
    Those ids were partly fabricated (sequential, colliding at 100), so the
    router could return one league's fixtures labelled as another's. That is
    the worst possible failure for a betting engine: silently wrong data,
    confidently presented.

    Coverage now comes from
    :data:`~lisa.providers.calendar.LEAGUES`, which records, per league, the
    identifier each *real* source uses for it. A league no free source carries
    is genuinely uncovered, and says so, rather than being assigned an id that
    was never verified against the source.

Three tiers, and what each one now means
    A  tier-one competition, multiple free sources can cover it.
    B  covered, but by a single source (no cross-check available).
    C  not covered by any free source. The router will not invent fixtures.

    The letter is now derived from the registry, so it cannot drift from what
    the sources can actually do.

The Odds API is gone from this path
    ``has_odds`` is true only when a price source is actually configured and
    enabled. With the default opt-in switch off it is always false, which is
    the honest answer: no price has been observed, so nothing here is priced.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from . import config as cfg
from .odds import Match, Score

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LeagueTier:
    """What the engine can expect for one league, and from where."""

    sport_key: str
    tier: str  # "A", "B" or "C"
    has_odds: bool
    has_scores: bool
    #: Which free calendar sources can serve this league, cheapest-first.
    #: Empty means no free source covers it.
    sources: tuple[str, ...] = ()


def _odds_available() -> bool:
    """Whether any market price source is configured *and* armed.

    Reads live config rather than caching at import time: the router is a
    module-level singleton created during startup, and an operator flipping
    the switch must not need a restart to see it take effect.
    """
    try:
        settings = cfg.load_settings()
    except Exception as exc:  # config unreadable: report no prices, not a crash
        logger.debug("match_router: config unavailable (%s); assuming unpriced", exc)
        return False
    return bool(getattr(settings, "odds_api_enabled", False)
                and getattr(settings, "odds_api_key", ""))


class MatchRouter:
    """Resolves league coverage and fetches fixtures from the free sources.

    Holds a short TTL cache. The board cycle is authoritative and should not
    read this cache; this exists so the daily-board and settlement paths, which
    are per-request, do not re-fetch a season per request.
    """

    def __init__(self, *, cache_ttl: int = 300, providers: Optional[list[Any]] = None):
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._providers = providers
        self._league_tiers: dict[str, LeagueTier] = {}

    # -- providers ----------------------------------------------------------

    def _provider_set(self) -> list[Any]:
        """The configured calendar providers, built once and reused.

        Built lazily: importing this module must not open sockets, and a host
        with no IPv4 egress should not pay for provider construction at import.
        """
        if self._providers is None:
            from .feed import build_providers

            self._providers = build_providers(cfg.load_settings()).calendar
        return self._providers

    def _provider_for(self, source: str) -> Optional[Any]:
        for provider in self._provider_set():
            if getattr(provider, "name", "") == source:
                return provider
        return None

    # -- cache --------------------------------------------------------------

    def _get_cached(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            expires, value = entry
            if expires < time.time():
                del self._cache[key]
                return None
            return value

    def _set_cached(self, key: str, value: Any) -> None:
        with self._lock:
            self._cache[key] = (time.time() + self.cache_ttl, value)

    # -- coverage -----------------------------------------------------------

    def classify_league(self, sport_key: str) -> LeagueTier:
        """Classify a league from the live registry.

        Cached per key, but ``has_odds`` is resolved fresh each time: whether
        prices exist is a configuration fact that can change, whereas the
        source coverage of a league cannot.
        """
        cached = self._league_tiers.get(sport_key)
        if cached is not None:
            return LeagueTier(sport_key=sport_key, tier=cached.tier,
                              has_odds=_odds_available(),
                              has_scores=cached.has_scores, sources=cached.sources)

        from .providers.calendar import LEAGUES, sources_for

        spec = LEAGUES.get(sport_key)
        if spec is None:
            # Not in the registry. Claiming coverage here is exactly the bug
            # this rewrite removes, so an unknown league is tier C with no
            # sources rather than being guessed into tier B.
            tier = LeagueTier(sport_key=sport_key, tier="C", has_odds=_odds_available(),
                              has_scores=False, sources=())
            self._league_tiers[sport_key] = tier
            return tier

        sources = sources_for(sport_key)
        # A single source is a real but un-cross-checked source: honest, but
        # worth flagging as a weaker guarantee than two agreeing feeds.
        tier = LeagueTier(
            sport_key=sport_key,
            tier="A" if len(sources) >= 2 else ("B" if sources else "C"),
            has_odds=_odds_available(),
            # Every calendar source in the registry also carries results, which
            # is what the model trains on.
            has_scores=bool(sources),
            sources=tuple(sources),
        )
        self._league_tiers[sport_key] = tier
        return tier

    # -- fixtures -----------------------------------------------------------

    @staticmethod
    def _to_match(fixture: dict[str, Any]) -> Match:
        """Normalise a calendar fixture into the engine's Match shape."""
        kickoff = fixture.get("kickoff")
        try:
            commence = datetime.fromisoformat(str(kickoff).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            commence = datetime.now(timezone.utc)
        if commence.tzinfo is None:
            commence = commence.replace(tzinfo=timezone.utc)
        return Match(
            id=str(fixture.get("match_id", "")),
            sport_key=str(fixture.get("sport_key", "")),
            commence_time=commence,
            home_team=str(fixture.get("home_team", "")),
            away_team=str(fixture.get("away_team", "")),
            completed=bool(fixture.get("completed")),
        )

    def get_matches(self, sport_key: str, *, days_ahead: int = 7,
                    source: Optional[str] = None) -> list[Match]:
        """Fixtures for one league, from every free source that covers it.

        Args:
            sport_key: League identifier.
            days_ahead: Only return fixtures kicking off within this many days.
            source: Restrict to one named source. ``None`` walks the
                cheapest-first chain and stops at the first that answers, so a
                metered source is only spent once an unmetered one has failed.

        Returns an empty list for an uncovered league. An empty list here means
        "no source covers this", and callers must say so rather than showing
        an empty schedule as though the league simply has no fixtures.
        """
        tier = self.classify_league(sport_key)
        if not tier.sources:
            return []

        cache_key = f"matches:{sport_key}:{days_ahead}:{source or 'any'}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        chain = (source,) if source else tier.sources
        cutoff = time.time() + days_ahead * 86400
        fixtures: list[dict[str, Any]] = []

        for name in chain:
            provider = self._provider_for(name)
            if provider is None:
                continue
            try:
                result = provider.get_fixtures(sport_key)
            except Exception as exc:
                # One source failing is not a failed request: walk the chain.
                logger.warning("match_router: %s failed for %s: %s", name, sport_key, exc)
                continue
            rows = [dict(f) for f in result.fixtures
                    if f.get("epoch", 0) <= cutoff]
            if rows:
                fixtures = rows
                break

        matches = [self._to_match(f) for f in fixtures]
        # Only cache a non-empty result. Caching emptiness would pin "no
        # fixtures" in place for the whole TTL after a transient source failure.
        if matches:
            self._set_cached(cache_key, matches)
        return matches

    def get_live_matches(self, sport_key: str) -> list[Match]:
        """Fixtures currently in play (started within the last 3 hours)."""
        now = time.time()
        window = 3 * 3600
        return [m for m in self.get_matches(sport_key)
                if not m.completed
                and m.commence_time.timestamp() <= now
                and m.commence_time.timestamp() >= now - window]

    def get_scores(self, match_id: str, sport_key: str = "") -> Optional[Score]:
        """The official result for a match, or None when it is not final.

        Settlement depends on this, so an absent result stays ``None``: it is
        never inferred, and never reported as a 0-0.
        """
        cache_key = f"score:{sport_key}:{match_id}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        if not sport_key:
            logger.debug("match_router: no sport_key for match %s; cannot resolve score",
                         match_id)
            return None

        for name in self.classify_league(sport_key).sources:
            provider = self._provider_for(name)
            if provider is None:
                continue
            try:
                result = provider.get_fixtures(sport_key)
            except Exception as exc:
                logger.warning("match_router: %s score lookup failed for %s: %s",
                               name, match_id, exc)
                continue
            for fixture in result.fixtures:
                if str(fixture.get("match_id")) != match_id:
                    continue
                if not fixture.get("completed"):
                    return None
                kickoff = fixture.get("kickoff")
                try:
                    commence = datetime.fromisoformat(str(kickoff).replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    commence = datetime.now(timezone.utc)
                if commence.tzinfo is None:
                    commence = commence.replace(tzinfo=timezone.utc)
                score = Score(
                    match_id=match_id,
                    sport_key=sport_key,
                    commence_time=commence,
                    completed=True,
                    home_score=fixture.get("home_score"),
                    away_score=fixture.get("away_score"),
                    status=str(fixture.get("status", "final")),
                    home_team=str(fixture.get("home_team", "")),
                    away_team=str(fixture.get("away_team", "")),
                )
                if score.home_score is None or score.away_score is None:
                    # Flagged complete upstream but carrying no scoreline: a
                    # missing goal is missing data, so it cannot settle a pick.
                    return None
                self._set_cached(cache_key, score)
                return score
        return None

    def get_all_leagues(self, tier: Optional[str] = None) -> list[str]:
        """Every league the free stack covers, optionally filtered by tier."""
        from .providers.calendar import LEAGUES

        keys = sorted(LEAGUES.keys())
        if tier is None:
            return keys
        return [k for k in keys if self.classify_league(k).tier == tier.upper()]

    def clear_cache(self) -> None:
        """Drop all cached fixtures and scores."""
        with self._lock:
            self._cache.clear()


# Global router instance
match_router = MatchRouter()
