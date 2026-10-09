"""Builds the live provider set from settings, and the end-to-end feed.

This is the seam between configuration and the board. Two jobs:

``build_providers``
    Turn a :class:`~lisa.config.Settings` object into a set of live, usable
    adapters -- and be honest about which ones will not be built, so a missing
    key is reported at startup rather than discovered as an empty board three
    hours later.

``run_feed``
    The whole cycle in one call: fetch bulk history and routed verified
    calendars, dedupe, fit the model, price available markets, build the
    board. This is what a scheduler or a CLI command actually calls.

Honest degradation, which is the whole design constraint here
------------------------------------------------------------
With free tiers, *some* source is always down, throttled, truncated or
unconfigured. The feed is built so that each of those is a normal, logged,
non-fatal condition:

* a source that is not configured is skipped before any request is spent;
* a source that fails is recorded in the report and the cycle continues;
* a source that returns a clipped season is labelled degraded;
* the model is only fitted if there are real results, and the board is marked
  unproven if there are too few.

The cycle therefore always produces a report, and the report always says exactly
how much of it is trustworthy.
"""
from __future__ import annotations

import logging
import hashlib
import json
import math
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from .board import Board, Fixture, MarketPrice, OpportunityBoard
from .dixon_coles import DixonColesModel, ScoredMatch
from .providers.base import (
    ApiError,
    FixturesResult,
    HttpTransport,
    ProviderError,
    ProviderQuota,
    SourceTier,
    utcnow_ts,
)
from .providers.calendar import LEAGUES, LeagueSpec, dedupe_fixtures
from .providers.football_data import FootballDataProvider
from .providers.openligadb import OpenLigaDbProvider
from .providers.sportsdb import SportsDbProvider
from .providers.sharpapi import SharpApiOddsProvider

logger = logging.getLogger("lisa.feed")

#: Wall-clock ceiling on the fetch phase. Four free hosts, each throttled, must
#: not be able to hold the cycle open indefinitely.
FETCH_TIMEOUT_SEC = 90.0

#: Ceiling on the price fetch alone. Kept separate from, and shorter than, the
#: calendar budget: prices are the only part of the cycle whose absence still
#: leaves a usable board (the winning ladder works unpriced), so it must never
#: be what pushes the fixtures past their own deadline.
PRICE_FETCH_TIMEOUT_SEC = 30.0

@dataclass
class ProviderStatus:
    """Per-source health for one cycle. Surfaced, never swallowed."""

    name: str
    configured: bool
    used: bool
    leagues: tuple[str, ...] = ()
    fixtures: int = 0
    error: str = ""
    degraded: bool = False
    tier: str = SourceTier.OFFICIAL.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "configured": self.configured, "used": self.used,
            "leagues": list(self.leagues), "fixtures": self.fixtures,
            "error": self.error, "degraded": self.degraded, "tier": self.tier,
        }


@dataclass
class FeedReport:
    """Everything one cycle produced, including what it could not do."""

    began: datetime
    finished: Optional[datetime] = None
    window_hours: float = 48.0
    providers: list[ProviderStatus] = field(default_factory=list)
    leagues: dict[str, int] = field(default_factory=dict)
    results_collected: int = 0
    fixtures_in_window: int = 0
    duplicates_removed: int = 0
    model: dict[str, Any] = field(default_factory=dict)
    board: Optional[Board] = None
    #: What the price source delivered and what it could not use. Kept whole so
    #: an operator can tell "the books are not offering this" from "the adapter
    #: is not reading the feed correctly" -- identical from the board.
    prices: dict[str, Any] = field(default_factory=dict)
    price_match: dict[str, Any] = field(default_factory=dict)
    #: Decisions this cycle took about its own behaviour, as opposed to faults
    #: it hit. Kept apart from ``errors`` on purpose: an error means something
    #: failed and an operator may need to fix it, whereas a note records a
    #: choice that was made and can be disagreed with.
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)
    fixture_updates: list[dict[str, Any]] = field(default_factory=list)
    window_selection: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_sec(self) -> float:
        end = self.finished or datetime.now(timezone.utc)
        return (end - self.began).total_seconds()

    # Internal settlement evidence; never serialize entire season histories to visitors.
    active_model: Any = None
    results: list[dict[str, Any]] = field(default_factory=list)
    forecast: dict[str, Any] = field(default_factory=dict)

    def health(self) -> str:
        """One word, for a dashboard tile."""
        if self.errors and not self.board:
            return "broken"
        if any(p.degraded for p in self.providers):
            return "degraded"
        if self.errors:
            return "partial"
        if self.board and self.board.unproven:
            return "unproven"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "began": self.began.isoformat(),
            "finished": self.finished.isoformat() if self.finished else None,
            "duration_sec": round(self.duration_sec, 2),
            "timings_ms": dict(self.timings_ms),
            "window_hours": self.window_hours,
            "window_selection": dict(self.window_selection),
            "health": self.health(),
            "providers": [p.to_dict() for p in self.providers],
            "leagues": dict(self.leagues),
            "results_collected": self.results_collected,
            "fixtures_in_window": self.fixtures_in_window,
            "duplicates_removed": self.duplicates_removed,
            "model": dict(self.model),
            "prices": dict(self.prices),
            "price_match": dict(self.price_match),
            "notes": list(self.notes),
            "errors": list(self.errors),
            "board": self.board.to_dict() if self.board else None,
        }

    def summary(self) -> str:
        """One paragraph, suitable for a log line or a chat alert."""
        parts = [
            f"health={self.health()}",
            f"leagues={len(self.leagues)}",
            f"in-window={self.fixtures_in_window}",
            f"results={self.results_collected}",
            f"{self.duration_sec:.1f}s",
        ]
        if self.board:
            parts.append(f"unproven={self.board.unproven}")
            parts.append(f"winning={len(self.board.winning)}")
            parts.append(f"earning={len(self.board.earning)}")
            parts.append(f"micro={len(self.board.micro_bets)}")
            parts.append(f"accas={len(self.board.accumulators)}")
        if self.prices:
            parts.append(f"priced={self.prices.get('quotes', 0)}")
            parts.append(f"matched={self.price_match.get('matched_fixtures', 0)}")
        if self.errors:
            parts.append(f"errors={len(self.errors)}")
        return " ".join(parts)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class ProviderSet:
    calendar: list[Any] = field(default_factory=list)
    statuses: list[ProviderStatus] = field(default_factory=list)
    transport: Optional[HttpTransport] = None
    #: Price sources. Held apart from ``calendar`` because a price source can
    #: answer for a league the calendar sources do not cover, and must never be
    #: asked for fixtures -- it has no fixture endpoint at all.
    prices: list[Any] = field(default_factory=list)

    def named(self, name: str) -> Optional[Any]:
        for provider in self.calendar:
            if getattr(provider, "name", None) == name:
                return provider
        return None


def build_providers(settings: Any, *, transport: Optional[HttpTransport] = None,
                    ) -> ProviderSet:
    """Construct every configured calendar source, and report the rest.

    A single shared transport is deliberate: it gives all four hosts one
    connection pool, one cache and one request counter, which is what makes the
    cross-source fan-out in :func:`run_feed` cheap.
    """
    transport = transport or HttpTransport(
        timeout=getattr(settings, "provider_timeout_sec", 15.0),
        time_budget=FETCH_TIMEOUT_SEC,
    )
    statuses: list[ProviderStatus] = []
    providers: list[Any] = []

    scalper_mode = getattr(settings, 'scalper_mode', 'off')
    if scalper_mode not in ('off', 'supporting', 'only'):
        raise ValueError('LISA_SCALPER_MODE must be off, supporting or only')
    scalper = None
    if scalper_mode != 'off':
        from .providers.scalper import ScalperProvider
        scalper = ScalperProvider(leagues=getattr(settings, 'board_leagues', ()),
            fixture_max_age=getattr(settings, 'scalper_fixture_max_age_sec', 900),
            quote_max_age=getattr(settings, 'scalper_quote_max_age_sec', 300))
        providers.append(scalper)
        statuses.append(ProviderStatus(name=scalper.name, configured=True, used=False,
            leagues=scalper.leagues(), tier=scalper.tier.value))
        if scalper_mode == 'only':
            return ProviderSet(calendar=providers, statuses=statuses, transport=transport, prices=[scalper])

    if getattr(settings, 'enable_openfootball', True):
        from .providers.openfootball import OpenFootballProvider
        bulk = OpenFootballProvider(cache_sec=getattr(settings, 'openfootball_cache_sec', 86400))
        providers.append(bulk)
        statuses.append(ProviderStatus(name=bulk.name, configured=True, used=True,
            leagues=bulk.leagues(), tier=bulk.tier.value))

    fdo_token = getattr(settings, "football_data_token", "") or ""
    fdo = FootballDataProvider(fdo_token, transport=transport)
    if fdo_token:
        providers.append(fdo)
    statuses.append(ProviderStatus(
        name=fdo.name, configured=bool(fdo_token), used=bool(fdo_token),
        leagues=tuple(fdo.leagues()),
        tier=fdo.tier.value,
        error="" if fdo_token else
        "no FOOTBALL_DATA_TOKEN -- the 12 tier-one European competitions are "
        "unavailable (register free at football-data.org)"))

    if getattr(settings, "enable_openligadb", True):
        oldb = OpenLigaDbProvider(transport=transport)
        providers.append(oldb)
        statuses.append(ProviderStatus(
            name=oldb.name, configured=True, used=True,
            leagues=tuple(oldb.leagues()), tier=oldb.tier.value))

    if getattr(settings, "enable_sportsdb", False):
        tsdb = SportsDbProvider(getattr(settings, "sportsdb_key", "") or "3",
                                transport=transport)
        providers.append(tsdb)
        statuses.append(ProviderStatus(
            name=tsdb.name, configured=True, used=True,
            leagues=tuple(tsdb.leagues()), degraded=tsdb.degraded,
            tier=tsdb.tier.value,
            error="free key is clipped to a handful of rows per season; "
                  "supplementary cross-check only, not a model backbone"))

    price_sources: list[Any] = []
    if scalper is not None:
        price_sources.append(scalper)
    if getattr(settings, 'the_odds_enabled', False) and getattr(settings, 'odds_api_key', ''):
        from .providers.the_odds_api import TheOddsApiProvider
        odds = TheOddsApiProvider(settings.odds_api_key, monthly_limit=settings.the_odds_monthly_limit,
            reserve=settings.the_odds_reserve, daily_limit=settings.the_odds_daily_limit,
            regions=settings.the_odds_regions, markets=settings.the_odds_markets, ttl=settings.the_odds_cache_sec)
        price_sources.append(odds)
        statuses.append(ProviderStatus(name=odds.name, configured=True, used=True,
            leagues=tuple(odds.leagues()), tier=odds.tier.value))
    if getattr(settings, 'oddspapi_key', '') and getattr(settings, 'oddspapi_enabled', True):
        from .providers.oddspapi import OddsPapiProvider
        odds = OddsPapiProvider(settings.oddspapi_key, monthly_limit=settings.oddspapi_monthly_limit,
            reserve=settings.oddspapi_reserve, poll_interval_sec=settings.oddspapi_poll_interval_sec,
            bookmakers=settings.oddspapi_bookmakers)
        price_sources.append(odds)
        statuses.append(ProviderStatus(name=odds.name, configured=True, used=True,
            leagues=tuple(odds.leagues()), tier=odds.tier.value))
    if getattr(settings, 'allsports_api_key', ''):
        from .providers.allsports import AllSportsProvider
        supporting = AllSportsProvider(settings.allsports_api_key,
            hourly_limit=settings.allsports_hourly_limit, odds_enabled=settings.allsports_odds_enabled,
            corner_stat_type=settings.allsports_corner_stat_type, bookmakers=settings.allsports_bookmakers)
        providers.append(supporting)
        if supporting.odds_enabled:
            price_sources.append(supporting)
        statuses.append(ProviderStatus(name=supporting.name, configured=True, used=True,
            leagues=tuple(supporting.leagues()), tier=supporting.tier.value))
    if getattr(settings, 'api_football_key', ''):
        from .providers.api_football import ApiFootballProvider
        api = ApiFootballProvider(settings.api_football_key,
            daily_limit=settings.api_football_daily_limit, bookmakers=settings.api_football_bookmakers)
        providers.append(api)
        price_sources.append(api)
        statuses.append(ProviderStatus(name=api.name, configured=True, used=True,
            leagues=tuple(api.leagues()), tier=api.tier.value))
    sharp_key = getattr(settings, "sharpapi_key", "") or ""
    if getattr(settings, "enable_sharpapi", True) and sharp_key:
        sharp = SharpApiOddsProvider(sharp_key, transport=transport)
        price_sources.append(sharp)
        statuses.append(ProviderStatus(
            name=sharp.name, configured=bool(sharp_key), used=bool(sharp_key),
            leagues=tuple(sharp.leagues()), tier=sharp.tier.value,
            error=""))

    return ProviderSet(calendar=providers, statuses=statuses, transport=transport,
                       prices=price_sources)


# ---------------------------------------------------------------------------
# Fetch + fit
# ---------------------------------------------------------------------------


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def calendar_routes(providers, leagues):
    """One verified calendar per league, with existing sources as fallbacks."""
    by_name = {p.name: p for p in providers if p.name != 'openfootball'}
    priority = ('scalper', 'openligadb', 'football_data', 'allsports', 'api_football', 'sportsdb')
    ordered = [name for name in priority if name in by_name]
    ordered += sorted(set(by_name) - set(ordered))
    return {league: [name for name in ordered if league in by_name[name].leagues()]
            for league in leagues}


def fetch_calendar(providers: Sequence[Any], leagues: Sequence[str],
                   statuses: list[ProviderStatus], *, max_workers: int = 4,
                   purpose: str = 'ordinary') -> dict[str, list[dict[str, Any]]]:
    """Fetch a primary calendar, then only failed/empty leagues' fallbacks.

    Hosts remain independent and leagues within each host remain serial. Bulk
    CC0 history is supplementary; it never satisfies the verified-calendar slot.
    Settlement does not download these provisional files at all.
    """
    results = {key: [] for key in leagues}
    by_name = {p.name: p for p in providers}
    routes = calendar_routes(providers, leagues)
    unresolved = {league for league, sources in routes.items() if sources}
    deadline = time.monotonic() + FETCH_TIMEOUT_SEC
    from .job_budget import current_deadline, execution_budget
    from .providers.diagnostics import safe_error_summary
    job_deadline = current_deadline()
    status_by_name = {s.name: s for s in statuses}
    for name in by_name:
        if name in status_by_name:
            status_by_name[name].used = False

    def run_source(item):
        source, source_leagues = item
        provider = by_name[source]
        rows = []
        with execution_budget(deadline=min(deadline, job_deadline) if job_deadline else deadline):
            for league in source_leagues:
                if time.monotonic() >= deadline:
                    rows.append((source, league, None, 'skipped: calendar time budget exhausted'))
                    continue
                try:
                    rows.append((source, league, provider.get_fixtures(league), ''))
                except Exception as exc:
                    rows.append((source, league, None, safe_error_summary(exc)))
        return rows

    rounds = max((len(route) for route in routes.values()), default=0)
    bulk = by_name.get('openfootball') if purpose != 'settlement' else None
    for index in range(max(rounds, 1 if bulk else 0)):
        jobs = {}
        if index == 0 and bulk:
            supported = [league for league in leagues if league in bulk.leagues()]
            if supported:
                jobs[bulk.name] = supported
        for league in leagues:
            if league in unresolved and index < len(routes[league]):
                jobs.setdefault(routes[league][index], []).append(league)
        if not jobs:
            continue
        # Retain the existing budget rotation without requesting every source.
        for source, keys in jobs.items():
            rotation = getattr(by_name[source], 'coverage_rotation', 0)
            if type(rotation) is int and rotation and keys:
                offset = rotation % len(keys)
                jobs[source] = keys[offset:] + keys[:offset]
        with ThreadPoolExecutor(max_workers=min(max(1, max_workers), len(jobs))) as pool:
            for batch in pool.map(run_source, sorted(jobs.items())):
                for source, league, result, error in batch:
                    status = status_by_name.get(source)
                    if error:
                        if status:
                            status.error = (status.error + '; ' + error).strip('; ')
                            status.degraded = True
                        logger.warning('calendar %s/%s: %s', source, league, error)
                        continue
                    if result is None:
                        continue
                    results[league].extend(result.fixtures)
                    if status:
                        status.used = True
                        status.fixtures += len(result.fixtures)
                        if getattr(result, 'warnings', ()):
                            status.degraded = True
                            status.error = (status.error + '; ' + '; '.join(result.warnings)).strip('; ')
                    usable = bool(result.fixtures)
                    if source == 'scalper':
                        usable = any(not r.get('discovery_only') and r.get('kickoff_time_known') is not False and
                            ((r.get('settlement_eligible') is not False and
                              (r.get('completed') or r.get('status') == 'CANCELED')) if purpose == 'settlement'
                             else r.get('status') in ('SCHEDULED', 'LIVE', 'HT')) for r in result.fixtures)
                    if source != 'openfootball' and usable:
                        unresolved.discard(league)
    return results


def to_scored(rows: Iterable[Mapping[str, Any]], league: str, *, as_of=None
              ) -> list[ScoredMatch]:
    """Normalised finished rows the model can train on.

    Rows without both scores, or with a missing kickoff, are dropped rather than
    defaulted: a zero-goal placeholder would be a fabricated result, and one
    wrong goal corrupts every rating for both clubs involved.
    """
    out: list[ScoredMatch] = []
    as_of = as_of or datetime.now(timezone.utc)
    for row in rows:
        if not row.get("completed"):
            continue
        home_score, away_score = row.get("home_score"), row.get("away_score")
        if type(home_score) is not int or type(away_score) is not int:
            continue
        if home_score < 0 or away_score < 0:
            continue
        kickoff = _parse_iso(row.get("kickoff", ""))
        if kickoff is None:
            continue
        if row.get('result_available_after'):
            available = _parse_iso(row['result_available_after'])
            if available is None or available > as_of:
                continue
        out.append(ScoredMatch(league, kickoff, str(row.get("home_team", "")),
                               str(row.get("away_team", "")), home_score, away_score))
    return out


def to_fixtures(rows: Iterable[Mapping[str, Any]]) -> list[Fixture]:
    """Upcoming rows in the window, as board inputs."""
    out: list[Fixture] = []
    for row in rows:
        if (row.get("completed") or row.get('discovery_only')
                or row.get('kickoff_time_known') is False
                or str(row.get('status', '')).upper() in
                ('CANCELED', 'CANCELLED', 'POSTPONED', 'SUSPENDED', 'ABANDONED',
                 'FINISHED', 'FT', 'AET', 'PEN', 'LIVE', 'IN_PLAY', '1H', '2H', 'HT', 'ET')):
            continue
        kickoff = _parse_iso(row.get("kickoff", ""))
        if kickoff is None:
            continue
        home = str(row.get("home_team", "")).strip()
        away = str(row.get("away_team", "")).strip()
        if not home or not away:
            continue
        out.append(Fixture(str(row.get("match_id", "")),
                           str(row.get("sport_key", "")),
                           kickoff, home, away,
                           str(row.get("provider", "unknown"))))
    return out


def run_feed(settings: Any, *, providers: Optional[ProviderSet] = None,
             now: Optional[datetime] = None,
             window_hours: Optional[float] = None,
             prices: Optional[Mapping[str, Sequence[MarketPrice]]] = None,
             model: Optional[DixonColesModel] = None, history=None) -> FeedReport:
    """The full cycle. Always returns a report; never raises for a data fault."""
    from .config import paper_settings
    settings = paper_settings(settings)
    now = now or datetime.now(timezone.utc)
    window = window_hours if window_hours is not None else \
        float(getattr(settings, "board_window_hours", 48.0))
    report = FeedReport(began=now, window_hours=window)
    began_clock = time.perf_counter()
    owned_store = None
    def finish():
        report.finished = datetime.now(timezone.utc)
        report.timings_ms['total'] = (time.perf_counter()-began_clock)*1000
        if owned_store is not None:
            owned_store.close()
        return report

    try:
        provider_set = providers or build_providers(settings)
        if getattr(settings, 'scalper_mode', 'off') != 'off' and history is None:
            from .cli import _make_storage
            from .match_history import HistoryRepository
            owned_store = _make_storage(settings)
            history = HistoryRepository(owned_store)
        if history is not None:
            for source in (*provider_set.calendar, *provider_set.prices):
                source.storage = history.storage
    except Exception as exc:
        report.errors.append(f"could not build providers: {exc}")
        return finish()

    report.providers = [replace(s) for s in provider_set.statuses]
    leagues = list(getattr(settings, "board_leagues", []) or _default_leagues(settings))
    if not leagues:
        report.errors.append(
            "no leagues configured; set LISA_BOARD_LEAGUES (comma-separated "
            "canonical keys such as soccer_epl)")
        return finish()

    try:
        phase = time.perf_counter()
        raw = fetch_calendar(provider_set.calendar, leagues, report.providers)
    except Exception as exc:
        report.errors.append(f"calendar fetch failed: {exc}")
        return finish()
    finally:
        report.timings_ms['calendar'] = (time.perf_counter()-phase)*1000

    report.fixture_updates = [dict(row) for rows in raw.values() for row in rows]
    phase = time.perf_counter()

    report.results = [dict(r) for rows in raw.values() for r in rows
                      if r.get("completed") or str(r.get("status", "")).upper() in ("CANCELED", "CANCELLED")]
    from .data_quality import reconcile_results
    _, conflicts = reconcile_results(report.results)
    if conflicts:
        report.notes.append(f'{len(conflicts)} cross-source result disagreements; affected results withheld from training/settlement')

    for league, rows in raw.items():
        unique = dedupe_fixtures(rows)
        raw[league] = unique
        report.duplicates_removed += len(rows) - len(unique)
        report.results_collected += sum(1 for r in unique if r.get("completed"))
    initial_fixtures = [f for f in to_fixtures(
        r for league in leagues for r in raw.get(league, ()))
        if now < f.kickoff <= now + timedelta(hours=window)]
    for fixture in initial_fixtures:
        report.leagues[fixture.sport_key] = report.leagues.get(fixture.sport_key, 0) + 1
    report.fixtures_in_window = len(initial_fixtures)

    # Recover persisted results, reconcile source conflicts, and exclude recent
    # kickoffs. The default model fits each competition independently; sparse
    # leagues retain their priors rather than borrowing another league's scale.
    if history is not None:
        history.ingest(report.results, observed_at=now)
        historical = history.results(leagues, as_of=now)
        training = [m for league in leagues for m in to_scored(
            [r for r in historical if r.get('sport_key') == league], league, as_of=now)]
    else:
        accepted, _ = reconcile_results(report.results)
        training = [m for league in leagues for m in to_scored(
            dedupe_fixtures(r for r in accepted if r.get('sport_key') == league), league, as_of=now)]
    training = [m for m in training if m.kickoff + timedelta(hours=3) < now]
    from .league_model import LeagueGoalModel
    active = model or LeagueGoalModel(
        shrinkage=float(getattr(settings, "model_shrinkage", 8.0)),
        xi=float(getattr(settings, "model_xi", 0.55)),
        base_mu=float(getattr(settings, "model_base_mu", 1.35)),
        home_adv=float(getattr(settings, "model_home_adv", 0.24)),
    )
    if isinstance(active, LeagueGoalModel) and history is not None:
        active.cache_storage = history.storage
    if training:
        fingerprint = hashlib.sha256(json.dumps([
            now.date().isoformat(),
            sorted((m.league, m.kickoff.isoformat(), m.home, m.away, m.home_score, m.away_score)
                   for m in training),
            [getattr(settings, k) for k in ("model_base_mu", "model_home_adv", "model_shrinkage", "model_xi")],
        ]).encode()).hexdigest()
        if getattr(active, "_training_fingerprint", None) != fingerprint:
            active.fit(training, as_of=now)
            active._training_fingerprint = fingerprint
        report.active_model = active
        if active.report is not None:
            report.model = active.report.to_dict()
            report.model["training_fingerprint"] = fingerprint
            if hasattr(active, 'diagnostics'):
                report.model['leagues'] = active.diagnostics()
                from .corners import CornerTotalModel
                corner_rows = historical if history is not None else report.results
                corner_key = hashlib.sha256(json.dumps(sorted(
                    (r.get('sport_key'), r.get('match_id'), r.get('home_corners'), r.get('away_corners'))
                    for r in corner_rows), default=str).encode()).hexdigest()
                if getattr(active, '_corner_fingerprint', None) != (corner_key, now.date()):
                    active.corners = {league: CornerTotalModel().fit(
                        [r for r in corner_rows if r.get('sport_key') == league], as_of=now) for league in leagues}
                    active._corner_fingerprint = (corner_key, now.date())
                report.model['corner_matches'] = {league: m.matches for league, m in active.corners.items()}
    else:
        # The provider failures have to be surfaced *here*, not only on the
        # success path below. This return used to skip that collection
        # entirely, so a cycle where every fetch failed reported only "model
        # not fitted" -- which reads as a modelling problem and is in fact a
        # network or credentials problem upstream. The operator was told the
        # symptom and never the cause.
        _collect_provider_errors(report)
        report.errors.append("no finished results available; model not fitted")
        report.timings_ms['history_and_model'] = (time.perf_counter()-phase)*1000
        return finish()

    report.timings_ms['history_and_model'] = (time.perf_counter()-phase)*1000

    # Choose the final horizon before prices or predictions are computed. Only
    # verified, future fixtures with ratings can justify extending it. This
    # avoids both a fixed 48h ceiling and spending quota twice after widening.
    upcoming = sorted((f for f in to_fixtures(
        r for league in leagues for r in raw.get(league, ())) if f.kickoff > now),
        key=lambda f: (f.kickoff, f.match_id))
    modelled = []
    for fixture in upcoming:
        fixture_model = active.for_league(fixture.sport_key) if hasattr(active, 'for_league') else active
        if fixture_model.knows(fixture.home) and fixture_model.knows(fixture.away):
            modelled.append(fixture)
    target = max(1, int(getattr(settings, "board_volume_target", 12)))
    configured_window = window
    initial_end = now + timedelta(hours=window)
    initial_count = sum(f.kickoff <= initial_end for f in modelled)
    if initial_count < target and modelled:
        nearest_end = modelled[min(target, len(modelled)) - 1].kickoff
        window = max(window, float(math.ceil((nearest_end - now).total_seconds() / 3600)))
    end = now + timedelta(hours=window)
    fixtures = [f for f in upcoming if f.kickoff <= end]
    report.window_hours = window
    report.window_selection = {
        'mode': 'nearest_upcoming', 'configured_hours': configured_window,
        'effective_hours': window, 'expanded': window > configured_window,
        'available_verified': len(upcoming), 'available_modelled': len(modelled),
        'next_kickoff': upcoming[0].kickoff.isoformat() if upcoming else None,
        'next_forecast_kickoff': modelled[0].kickoff.isoformat() if modelled else None,
        'volume_target': target,
    }
    if window > configured_window:
        report.notes.append(
            f"Widened the look-ahead from {configured_window:g}h to {window:g}h "
            f"to include the nearest forecastable fixtures: {initial_count} "
            f"fixture(s) against a target of {target}; the selected window holds "
            f"{sum(f.kickoff <= end for f in modelled)}. Calendar fetched once; "
            "one price-fetch pass for the final window.")
    report.leagues = {}
    for fixture in fixtures:
        report.leagues[fixture.sport_key] = report.leagues.get(fixture.sport_key, 0) + 1
    report.fixtures_in_window = len(fixtures)

    # Prices are fetched only for the fixtures actually in the window. Fetching
    # first and matching afterwards would spend the free tier's twelve requests
    # a minute reading prices for matches that were never going to be on the
    # board.
    prices = dict(prices or {})
    phase = time.perf_counter()
    if not prices:
        prices = fetch_prices(provider_set, fixtures, settings, report,
                             window=window, now=now)
    report.timings_ms['prices'] = (time.perf_counter()-phase)*1000

    phase = time.perf_counter()
    board = build_board(active, settings).build(
        fixtures, prices, now=now, window_hours=window)
    board_time = (time.perf_counter()-phase)*1000

    report.timings_ms['board'] = board_time
    report.board = board
    forecast_rows = []
    for fixture in fixtures:
        fixture_model = active.for_league(fixture.sport_key) if hasattr(active, 'for_league') else active
        if not fixture_model.knows(fixture.home) or not fixture_model.knows(fixture.away):
            continue
        pred = fixture_model.predict(fixture.home, fixture.away)
        forecast_rows.append({
            "match_id": fixture.match_id, "home": fixture.home, "away": fixture.away,
            "sport_key": fixture.sport_key, "league": fixture.sport_key, "commence_at": fixture.kickoff.isoformat(),
            "market": None, "model": {"ready": not board.unproven,
                "p_home": pred["p_home"], "p_draw": pred["p_draw"], "p_away": pred["p_away"]},
            "micro": {"double_chance": pred.get("double_chance", {}),
                "home_team_over": pred.get("home_team_over", {}),
                "away_team_over": pred.get("away_team_over", {}),
                "p_btts": pred["p_btts"], "p_over_2_5": pred["over"].get("2.5"),
                "expected_goals_home": pred["expected_goals"]["home"],
                "expected_goals_away": pred["expected_goals"]["away"],
                "most_likely_scores": [{"score": f"{r['home_goals']}-{r['away_goals']}",
                    "p": r["p"]} for r in pred["most_likely_scores"][:5]]},
            "uncertainty": {"level": "high" if board.unproven else "medium",
                "reasons": ["Independent model forecast; not a bookmaker consensus"]},
        })
    forecast_rows.sort(key=lambda r: (datetime.fromisoformat(r['commence_at']),
        -max(r['model']['p_home'], r['model']['p_draw'], r['model']['p_away']), r['match_id']))
    report.forecast = {"kind": "match_forecast_bulletin", "mode": "live_model",
        "generated_at": now.isoformat(), "day": now.date().isoformat(),
        "window_hours": window, "window_selection": dict(report.window_selection),
        "count": len(forecast_rows), "matches": forecast_rows,
        "disclaimer": "Model forecasts, not guaranteed outcomes. Unpriced selections have no measured EV."}
    _collect_provider_errors(report)
    return finish()


def fetch_prices(provider_set: ProviderSet, fixtures: Sequence[Fixture],
                 settings: Any, report: FeedReport, *, window: float,
                 now: datetime) -> dict[str, Sequence[MarketPrice]]:
    """Read and join book prices for the fixtures in the window.

    Never raises: a price source that fails leaves the board unpriced, which is
    an honest state it already knows how to report, rather than an error that
    costs the whole cycle.
    """
    priced: dict[str, Sequence[MarketPrice]] = {}
    for source in provider_set.prices:
        if not source.is_available():
            continue
        if not fixtures:
            # Nothing in the window, so there is nothing to pay requests for.
            continue
        try:
            # The deadline runs from *this* moment, not from the cycle's start.
            # Anchoring it to the cycle start means the price fetch is already
            # expired by the time the calendar phase finishes -- which is what
            # silently produced zero prices on a 37s cycle, with the board
            # reporting unpriced rather than reporting that it was never asked.
            started = datetime.now(timezone.utc)
            snapshot = source.fetch(
                sport_keys=sorted({f.sport_key for f in fixtures}),
                window_hours=window, now=started,
                max_pages=int(getattr(settings, "sharpapi_max_pages", 6)),
                deadline=started + timedelta(seconds=PRICE_FETCH_TIMEOUT_SEC))
        except Exception as exc:
            from .providers.diagnostics import safe_error_summary
            logger.warning("prices: %s fetch failed: %s", source.name, safe_error_summary(exc))
            report.errors.append(f"{source.name}: price fetch failed: {safe_error_summary(exc)}")
            continue

        if snapshot.error:
            report.errors.append(f"{source.name}: {snapshot.error}")

        if getattr(source, 'storage', None) is not None and source.name != 'oddspapi':
            from .odds_history import OddsHistoryRepository
            observed_at = datetime.now(timezone.utc).isoformat()
            OddsHistoryRepository(source.storage).ingest([{
                'source': source.name, 'event_id': q.event_id, 'bookmaker': q.book_key,
                'observed_at': observed_at, 'odds': q.odds, 'market': q.market,
                'selection': q.selection, 'line': q.line, 'period': 'regulation',
                'provider_reported_update_at': q.updated_at.isoformat() if q.updated_at else None,
                'confirmed_at': q.confirmed_at.isoformat() if q.confirmed_at else None,
                'freshness_basis': q.freshness_basis,
                'detail': 'Publisher price-change and offer-confirmation clocks are separate; retrieval is not freshness.'
            } for q in snapshot.quotes if q.freshness_basis != 'publisher_snapshot'])

        try:
            outcome = source.match(snapshot.quotes, fixtures,
                max_kickoff_gap_h=float(getattr(settings, 'sharpapi_max_kickoff_gap_h', 6.0)))
        except Exception as exc:
            report.errors.append(f'{source.name}: quote matching failed: {type(exc).__name__}; details omitted')
            continue
        for fixture_id, offers in outcome.prices.items():
            priced[fixture_id] = tuple(priced.get(fixture_id, ())) + tuple(offers)
        previous_sources = dict(report.prices.get('sources', {}))
        report.prices = snapshot.to_dict()
        previous_sources[source.name] = snapshot.to_dict()
        report.prices['sources'] = previous_sources
        if len(previous_sources) > 1:
            report.prices['source'] = 'multiple'
            for name in ('quotes', 'rows', 'pages'):
                report.prices[name] = sum(value.get(name, 0) for value in previous_sources.values())
            dropped = Counter()
            for value in previous_sources.values():
                dropped.update(value.get('dropped', {}))
            report.prices['dropped'] = dict(dropped)
            report.prices['truncated'] = any(value.get('truncated') for value in previous_sources.values())
            report.prices['error'] = '; '.join(value['error'] for value in previous_sources.values() if value.get('error'))
            report.prices['quota'] = None  # Budgets remain attached to their individual sources.
            report.prices['stopped_because'] = 'multiple_sources'
        match_sources = dict(report.price_match.get('sources', {}))
        match_sources[source.name] = outcome.to_dict()
        report.price_match = {name: sum(value.get(name, 0) for value in match_sources.values())
            for name in ('matched_events', 'unmatched_events', 'ambiguous_events', 'contested_fixtures')}
        report.price_match.update(matched_fixtures=len(priced), sources=match_sources)
        for status in report.providers:
            if status.name == source.name:
                status.fixtures = len(outcome.prices)
                if snapshot.error:
                    status.error = (status.error+'; '+snapshot.error).strip('; ')
                    status.degraded = True
                status.used = bool(snapshot.quotes or not snapshot.error)
        logger.info(
            "prices: %s rows=%d quotes=%d dropped=%d events=%d matched=%d "
            "ambiguous=%d stopped=%s",
            source.name, snapshot.rows, len(snapshot.quotes),
            snapshot.dropped_total, len({q.event_id for q in snapshot.quotes}),
            outcome.matched_events, outcome.ambiguous_events,
            snapshot.stopped_because)
        if snapshot.dropped:
            for reason, count in sorted(snapshot.dropped.items()):
                logger.info("prices: %s dropped %d -- %s", source.name, count, reason)
    return priced


def _collect_provider_errors(report: FeedReport) -> None:
    """Append one line per provider that failed.

    Only for providers that were configured and then failed to contribute: a
    source that was never configured is not an error, it is just absent.
    """
    for status in report.providers:
        if status.error and status.configured and status.used is False:
            message = f"{status.name}: {status.error}"
            if message not in report.errors:
                report.errors.append(message)


def build_board(model: DixonColesModel, settings: Any) -> OpportunityBoard:
    """One configuration seam, shared by narrow and fallback windows."""
    fields = {"min_ev": "board_min_ev", "min_model_prob": "board_min_model_prob",
        "min_offer_odds": "board_min_offer_odds",
        "min_accumulator_prob": "board_min_accumulator_prob",
        "kelly_fraction": "board_kelly_fraction", "max_stake": "board_max_stake",
        "max_total_line": "board_max_total_line", "max_team_total_line": "board_max_team_total_line",
        "accumulator_sizes": "board_accumulator_sizes",
        "volume_target": "board_volume_target"}
    from .model_policy import EvidenceGate, configuration_hash
    options = {k: getattr(settings, v) for k, v in fields.items() if hasattr(settings, v)}
    options['min_model_prob'] = max(options.get('min_model_prob', .12), getattr(settings, 'pick_feed_min_probability', .55))
    options['min_accumulator_prob'] = max(options.get('min_accumulator_prob', .02),
        getattr(settings, 'pick_feed_min_accumulator_probability', .35))
    return OpportunityBoard(model, evidence_gate=EvidenceGate(getattr(settings, 'model_validation_path', '')),
                            configuration_hash=configuration_hash(settings),
                            **options)


def _default_leagues(settings: Any = None) -> list[str]:
    """Leagues served by the enabled bulk inputs or configured live feeds.

    Bulk coverage supplies history/discovery only. Upcoming selections still
    require a timed calendar observation and known teams in the league model.
    """
    from .providers.api_football import COMPETITIONS
    from .providers.openfootball import FILES
    if getattr(settings, 'scalper_mode', 'off') == 'only':
        from .scalper.sources import ESPN_LEAGUES
        return list(ESPN_LEAGUES)
    from .scalper.sources import ESPN_LEAGUES
    return [key for key, spec in LEAGUES.items()
            if (spec.oldb and getattr(settings, "enable_openligadb", True))
            or (spec.fdo and getattr(settings, "football_data_token", ""))
            or (key in FILES and getattr(settings, 'enable_openfootball', True))
            or (key in COMPETITIONS and getattr(settings, 'api_football_key', ''))
            or (key in ESPN_LEAGUES and getattr(settings, 'scalper_mode', 'off') == 'supporting')]


__all__ = [
    "FeedReport", "ProviderStatus", "ProviderSet",
    "build_providers", "run_feed", "fetch_calendar", "fetch_prices",
    "to_scored", "to_fixtures",
]
