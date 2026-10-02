"""Builds the live provider set from settings, and the end-to-end feed.

This is the seam between configuration and the board. Two jobs:

``build_providers``
    Turn a :class:`~lisa.config.Settings` object into a set of live, usable
    adapters -- and be honest about which ones will not be built, so a missing
    key is reported at startup rather than discovered as an empty board three
    hours later.

``run_feed``
    The whole cycle in one call: fetch the calendar from every source that can
    serve each league, dedupe, fit the model, price what is priced, build the
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
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
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
from .providers.calendar import LEAGUES, LeagueSpec, dedupe_fixtures, sources_for
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

#: Look-ahead ceiling used when the configured window cannot reach the volume
#: goal. 24h is the target; this is the widest the board will look without the
#: operator asking for it. Widening costs no requests -- the fixtures are
#: already fetched, only re-windowed -- so the only cost of a narrow setting is
#: an emptier board, which is not a trade worth making silently.
FALLBACK_WINDOW_HOURS: float = 48.0


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

    @property
    def duration_sec(self) -> float:
        end = self.finished or datetime.now(timezone.utc)
        return (end - self.began).total_seconds()

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
            "window_hours": self.window_hours,
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

    if getattr(settings, "enable_sportsdb", True):
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
    sharp_key = getattr(settings, "sharpapi_key", "") or ""
    if getattr(settings, "enable_sharpapi", True):
        sharp = SharpApiOddsProvider(sharp_key, transport=transport)
        price_sources.append(sharp)
        statuses.append(ProviderStatus(
            name=sharp.name, configured=bool(sharp_key), used=bool(sharp_key),
            leagues=tuple(sharp.leagues()), tier=sharp.tier.value,
            error="" if sharp_key else
            "no SHARPAPI_KEY -- fixtures and the winning ladder still work, but "
            "every pick is unpriced, so the earning ladder, accumulators, "
            "slippage and CLV are structurally empty (free key at sharpapi.io)"))

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


def fetch_calendar(providers: Sequence[Any], leagues: Sequence[str],
                   statuses: list[ProviderStatus],
                   *, max_workers: int = 4) -> dict[str, list[dict[str, Any]]]:
    """Fetch every league from every source that can serve it.

    Cross-source in parallel, but *within* a source serialised: the sources have
    independent rate limits, so four hosts can work concurrently while each
    host's own pacing is respected by the transport's token bucket. Fanning out
    per-league within a single host would defeat that pacing and earn a 429.

    One league's failure never removes another league's data.
    """
    results: dict[str, list[dict[str, Any]]] = {key: [] for key in leagues}
    by_name = {getattr(p, "name", ""): p for p in providers}

    # Group jobs by source and run one thread per source. Within a source the
    # leagues stay serialised, because that host's rate limit is what is being
    # respected: 14 football-data.org leagues at the free tier's 10 req/min is
    # ~85s of serial work, and fanning those out per-league only converts the
    # pacing into 429s.
    by_source: dict[str, list[str]] = {}
    for league in leagues:
        for source in sources_for(league):
            if source in by_name:
                by_source.setdefault(source, []).append(league)

    if not by_source:
        return results

    started = time.monotonic()
    deadline = started + FETCH_TIMEOUT_SEC

    def run_source(item: tuple[str, list[str]]) -> list[tuple[str, str, FixturesResult | None, str]]:
        source, source_leagues = item
        provider = by_name[source]
        rows: list[tuple[str, str, FixturesResult | None, str]] = []
        for league in source_leagues:
            if time.monotonic() > deadline:
                # Budget spent. Say so per league rather than silently
                # returning fewer leagues than were asked for -- a truncated
                # calendar is indistinguishable from a quiet one otherwise.
                rows.append((source, league, None,
                             f"skipped: the {FETCH_TIMEOUT_SEC:.0f}s fetch budget was "
                             f"already spent on this cycle (host rate limit)"))
                continue
            try:
                rows.append((source, league, provider.get_fixtures(league), ""))
            except ProviderError as exc:
                # ProviderError covers every classified failure: auth, rate
                # limit, transport, parse. Anything else is caught below, but
                # the common paths never reach that.
                rows.append((source, league, None, f"{type(exc).__name__}: {exc}"))
            except Exception as exc:  # pragma: no cover - defensive
                rows.append((source, league, None,
                             f"unexpected {type(exc).__name__}: {exc}"))
        return rows

    workers = min(max(1, max_workers), len(by_source))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for batch in pool.map(run_source, sorted(by_source.items())):
            for source, league, result, error in batch:
                status = next((s for s in statuses if s.name == source), None)
                if status is not None and league not in status.leagues:
                    status.leagues = status.leagues + (league,)
                if error:
                    if status is not None:
                        status.error = (status.error + "; " + error).strip("; ")
                        if not error.startswith("skipped:"):
                            status.used = False
                    # A budget skip is expected and self-explaining; logging it
                    # at WARNING would bury the genuine failures underneath a
                    # line the operator already knows about. It is still recorded
                    # on the provider status, so it is never silent.
                    if error.startswith("skipped:"):
                        logger.info("fetch skipped: %s/%s -- %s", source, league, error)
                    else:
                        logger.warning("fetch failed: %s/%s -- %s", source, league, error)
                    continue
                if result is None:
                    continue
                results[league].extend(result.fixtures)
                if status is not None:
                    status.fixtures += len(result.fixtures)

    slow = time.monotonic() - started
    if slow > FETCH_TIMEOUT_SEC:
        logger.warning("fetch phase took %.1fs, over the %.0fs budget",
                       slow, FETCH_TIMEOUT_SEC)
    return results


def to_scored(rows: Iterable[Mapping[str, Any]], league: str
              ) -> list[ScoredMatch]:
    """Normalised finished rows the model can train on.

    Rows without both scores, or with a missing kickoff, are dropped rather than
    defaulted: a zero-goal placeholder would be a fabricated result, and one
    wrong goal corrupts every rating for both clubs involved.
    """
    out: list[ScoredMatch] = []
    for row in rows:
        if not row.get("completed"):
            continue
        home_score, away_score = row.get("home_score"), row.get("away_score")
        if not isinstance(home_score, int) or not isinstance(away_score, int):
            continue
        if home_score < 0 or away_score < 0:
            continue
        kickoff = _parse_iso(row.get("kickoff", ""))
        if kickoff is None:
            continue
        out.append(ScoredMatch(league, kickoff, str(row.get("home_team", "")),
                               str(row.get("away_team", "")), home_score, away_score))
    return out


def to_fixtures(rows: Iterable[Mapping[str, Any]]) -> list[Fixture]:
    """Upcoming rows in the window, as board inputs."""
    out: list[Fixture] = []
    for row in rows:
        if row.get("completed"):
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
             model: Optional[DixonColesModel] = None) -> FeedReport:
    """The full cycle. Always returns a report; never raises for a data fault."""
    now = now or datetime.now(timezone.utc)
    window = window_hours if window_hours is not None else \
        float(getattr(settings, "board_window_hours", 48.0))
    report = FeedReport(began=now, window_hours=window)

    try:
        provider_set = providers or build_providers(settings)
    except Exception as exc:
        report.errors.append(f"could not build providers: {exc}")
        report.finished = datetime.now(timezone.utc)
        return report

    report.providers = list(provider_set.statuses)
    leagues = list(getattr(settings, "board_leagues", []) or _default_leagues())
    if not leagues:
        report.errors.append(
            "no leagues configured; set LISA_BOARD_LEAGUES (comma-separated "
            "canonical keys such as soccer_epl)")
        report.finished = datetime.now(timezone.utc)
        return report

    try:
        raw = fetch_calendar(provider_set.calendar, leagues, report.providers)
    except Exception as exc:
        report.errors.append(f"calendar fetch failed: {exc}")
        report.finished = datetime.now(timezone.utc)
        return report

    cutoff = (now.timestamp(), now.timestamp() + window * 3600.0)
    for league, rows in raw.items():
        unique = dedupe_fixtures(rows)
        report.duplicates_removed += len(rows) - len(unique)
        report.results_collected += sum(1 for r in unique if r.get("completed"))
        in_window = [r for r in unique
                     if not r.get("completed") and cutoff[0] <= r.get("epoch", 0) <= cutoff[1]]
        if in_window:
            report.leagues[league] = len(in_window)
        report.fixtures_in_window += len(in_window)

    # Fit on every finished result we hold, across all leagues. Pooling is
    # deliberate: a league with 9 results cannot fit anything alone, but the
    # *ratings* it contributes are still informative next to other leagues, and
    # a team is a team regardless of which division it plays in. Leagues that do
    # not share a strength scale would need a normalisation step here.
    training = [m for league in leagues for m in to_scored(raw.get(league, ()), league)]
    active = model or DixonColesModel(
        shrinkage=float(getattr(settings, "model_shrinkage", 8.0)),
        xi=float(getattr(settings, "model_xi", 0.55)),
        base_mu=float(getattr(settings, "model_base_mu", 1.35)),
    )
    if training:
        fit_report = active.fit(training, as_of=now)
        if fit_report is not None:
            report.model = fit_report.to_dict()
    else:
        # The provider failures have to be surfaced *here*, not only on the
        # success path below. This return used to skip that collection
        # entirely, so a cycle where every fetch failed reported only "model
        # not fitted" -- which reads as a modelling problem and is in fact a
        # network or credentials problem upstream. The operator was told the
        # symptom and never the cause.
        _collect_provider_errors(report)
        report.errors.append("no finished results available; model not fitted")
        report.finished = datetime.now(timezone.utc)
        return report

    fixtures = to_fixtures(
        [r for league in leagues for r in raw.get(league, ())
         if not r.get("completed") and cutoff[0] <= r.get("epoch", 0) <= cutoff[1]])

    # Prices are fetched only for the fixtures actually in the window. Fetching
    # first and matching afterwards would spend the free tier's twelve requests
    # a minute reading prices for matches that were never going to be on the
    # board.
    prices = dict(prices or {})
    if not prices:
        prices = fetch_prices(provider_set, fixtures, settings, report,
                             window=window, now=now)

    target = int(getattr(settings, "board_volume_target", 12))
    board = OpportunityBoard(active, volume_target=target).build(
        fixtures, prices, now=now, window_hours=window)

    # The configured window is the *target*, not a hard limit. When it cannot
    # reach the volume goal -- an international week with nothing scheduled --
    # widening it costs nothing: the calendar rows are already in `raw`, and
    # only the board build is repeated. Publishing an empty board while the
    # next 24 hours of the same feed hold real fixtures would report a coverage
    # problem that does not exist.
    #
    # The wider window is adopted only if it genuinely finds more fixtures. If
    # it finds the same none, the configured window stands, so the coverage note
    # describes the window the operator actually set.
    if (not board.coverage.meets_volume_target
            and window < FALLBACK_WINDOW_HOURS):
        wide = to_fixtures(
            [r for league in leagues for r in raw.get(league, ())
             if not r.get("completed")
             and now.timestamp() <= r.get("epoch", 0)
             <= now.timestamp() + FALLBACK_WINDOW_HOURS * 3600.0])
        if len(wide) > len(fixtures):
            # Prices were fetched for the narrow window, so a fixture that only
            # the wide window contains is unpriced rather than wrongly priced.
            # That is the safe direction to widen in: it can add fixtures, it
            # can never attach one match's price to another match.
            narrow_count = board.coverage.fixtures_modelled
            narrow_window = window
            window = FALLBACK_WINDOW_HOURS
            fixtures = wide
            if not prices:
                prices = fetch_prices(provider_set, fixtures, settings, report,
                                      window=window, now=now)
            report.window_hours = window
            report.notes.append(
                f"Widened the look-ahead from {narrow_window:.0f}h to "
                f"{window:.0f}h: the shorter window held {narrow_count} "
                f"fixture(s) against a target of {target}, and the wider one "
                f"holds {len(wide)}. No extra requests were spent -- the same "
                f"fetched fixtures were re-windowed."
            )
            board = OpportunityBoard(active, volume_target=target).build(
                fixtures, prices, now=now, window_hours=window)

    report.board = board
    _collect_provider_errors(report)
    report.finished = datetime.now(timezone.utc)
    return report


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
            logger.warning("prices: %s fetch failed: %s", source.name, exc)
            report.errors.append(f"{source.name}: price fetch failed: {exc}")
            continue

        if snapshot.error:
            report.errors.append(f"{source.name}: {snapshot.error}")

        outcome = source.match(
            snapshot.quotes, fixtures,
            max_kickoff_gap_h=float(getattr(settings, "sharpapi_max_kickoff_gap_h",
                                             6.0)))
        priced.update(outcome.prices)
        report.prices = snapshot.to_dict()
        report.price_match = outcome.to_dict()
        for status in report.providers:
            if status.name == source.name:
                status.fixtures = len(outcome.prices)
                status.used = True
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
            report.errors.append(f"{status.name}: {status.error}")


def _default_leagues() -> list[str]:
    """The leagues that work with no credentials at all.

    OpenLigaDB needs nothing, so a zero-config install still produces a board
    from German football rather than an error page. Everything else is opt-in.
    """
    return [key for key, spec in LEAGUES.items() if spec.oldb]


__all__ = [
    "FeedReport", "ProviderStatus", "ProviderSet",
    "build_providers", "run_feed", "fetch_calendar", "fetch_prices",
    "to_scored", "to_fixtures",
]
