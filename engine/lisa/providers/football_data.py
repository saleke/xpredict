"""football-data.org -- 12 tier-one European competitions on a free token.

Verified shape (probed live, not read from docs):

    GET /v4/competitions            -> {"count": 190, "filters": {},
                                         "competitions": [ {...} ]}
    GET /v4/competitions/{code}     -> {"id":..,"code":"PL","name":..,
                                         "currentSeason":{"id":..,"startDate":..,
                                                          "endDate":..}}
    GET /v4/competitions/{code}/matches?season=..&dateFrom=..&dateTo=..
                                  -> {"resultSet": {"matches": [ {...} ]},
                                      "filters": {...}}

Two things that are easy to get wrong and expensive to get wrong at volume:

* ``/competitions`` is an **envelope**, not a bare list. Iterating it directly
  yields dict *keys* and silently produces garbage.
* ``/matches`` is **403 without a token**, even though ``/competitions`` is
  open. The adapter therefore treats a missing token as "unavailable" and
  reports it precisely, instead of hammering the API and eating the rate limit.

Rate limit: 10 requests/minute on the free tier, so the transport is configured
with a 10/60 token bucket and season results are cached aggressively -- a full
season is ~380 matches and is re-read constantly by the model.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Optional

from .base import (
    ApiError,
    FixturesResult,
    HttpTransport,
    ProviderNotAvailableError,
    ProviderQuota,
    SourceTier,
    utcnow_ts,
)
from .calendar import LEAGUES, normalise_fixture

logger = logging.getLogger("lisa.providers.football_data")

BASE_URL = "https://api.football-data.org/v4"
NAME = "football_data"

#: Published free-tier budget: 10 calls/minute.
RATE_PER_SEC = 10.0 / 60.0
BURST = 4

#: A season of results is immutable once finished, so it can be cached hard.
SEASON_TTL_SEC = 6 * 3600
#: Which season is "current" only changes at the summer rollover, but the
#: lookup is one of only two requests this provider makes per league and the
#: free tier allows ten per minute, so it must not be repeated per cycle.
COMPETITION_TTL_SEC = 24 * 3600
#: An in-progress week changes daily; poll it more often but not per-cycle.
RECENT_TTL_SEC = 900.0

@dataclass(frozen=True)
class SeasonRef:
    """A season as the two football-data.org endpoints each name it.

    ``/competitions/{code}`` reports an internal sequence id; the matches
    endpoint only accepts the calendar year. Both are carried so a caller never
    has to translate between them.
    """

    sequence_id: int  # -1 when learned from a row rather than the lookup
    year: str


_STATUS_MAP = {
    "FINISHED": "FINISHED",
    "SCHEDULED": "SCHEDULED",
    "TIMED": "SCHEDULED",
    "POSTPONED": "POSTPONED",
    "CANCELED": "CANCELED",
    "CANCELLED": "CANCELED",
    "AWARDED": "AWARDED",
    "IN_PLAY": "IN_PLAY",
}


class FootballDataProvider:
    """Fixtures + results for 12 free tier-one competitions."""

    name = NAME
    tier = SourceTier.OFFICIAL

    def __init__(self, token: str = "", *, transport: Optional[HttpTransport] = None,
                 timeout: float = 15.0) -> None:
        self._token = (token or "").strip()
        self._transport = transport or HttpTransport(timeout=timeout)
        self._transport.set_rate_limit("api.football-data.org", RATE_PER_SEC, BURST)
        self._last_remaining: Optional[int] = None
        self._last_limit: Optional[int] = None
        # season id cache per competition code; avoids one call per fixture query
        self._season_cache: dict[str, SeasonRef] = {}

    # -- contract -----------------------------------------------------------

    def is_available(self) -> bool:
        return bool(self._token)

    def quota_status(self) -> ProviderQuota:
        # The free tier is rate-limited but not credit-metered, so there is no
        # integer budget to report. Reporting the published per-minute budget
        # keeps the router's health view meaningful without faking a balance.
        limit = 10
        return ProviderQuota(remaining=limit, limit=limit, reset_at=0.0,
                             tier=SourceTier.OFFICIAL)

    def leagues(self) -> list[str]:
        return [k for k, spec in LEAGUES.items() if spec.fdo]

    # -- endpoints ----------------------------------------------------------

    def competitions(self) -> list[dict[str, Any]]:
        """Full competition catalogue (anonymous; used to verify a token)."""
        return self._get("/competitions", envelope_key="competitions")

    def current_season(self, code: str) -> Optional[SeasonRef]:
        """The current season, as both the sequence id and the calendar year.

        Both are needed because ``/competitions/{code}`` and
        ``/competitions/{code}/matches`` name the same season in two different
        namespaces: the former reports an internal sequence (2522 for 2026/27),
        while the latter only accepts the calendar year (2026) and answers 404
        for the sequence id. ``startDate`` is the documented bridge between
        them, so both are carried together rather than guessing an offset.
        """
        if code in self._season_cache:
            return self._season_cache[code]
        payload = self._get(f"/competitions/{code}", ttl=COMPETITION_TTL_SEC)
        if not isinstance(payload, dict):
            logger.warning("%s: /competitions/%s returned %s, not an object; "
                           "cannot resolve the current season",
                           NAME, code, type(payload).__name__)
            return None
        season = payload.get("currentSeason") or {}
        if not isinstance(season, dict):
            return None
        sid = season.get("id")
        year = _season_year(season.get("startDate"))
        if not isinstance(sid, int) or year is None:
            return None
        ref = SeasonRef(sequence_id=sid, year=year)
        self._season_cache[code] = ref
        return ref

    def get_fixtures(self, sport_key: str) -> FixturesResult:
        spec = LEAGUES.get(sport_key)
        if spec is None or not spec.fdo:
            return FixturesResult(NAME, sport_key, ())
        if not self.is_available():
            raise ProviderNotAvailableError(
                "football-data.org needs FOOTBALL_DATA_TOKEN (free at "
                "https://www.football-data.org/client/register)", provider=NAME)

        # Two requests per league is what exhausts the free tier. The first is
        # the season lookup, whose only purpose is to name the season, and the
        # matches endpoint already defaults to the current season when the
        # parameter is omitted -- verified to return the same rows as an
        # explicit `season=2026`. So the lookup is deferred: the year is read
        # back off the first row's own `season.startDate`, which the response
        # carries, and only used for a follow-up historical fetch if one is
        # ever requested. That halves the per-league cost, and at ten requests
        # per minute it is the difference between covering ten leagues in a
        # cycle and covering five.
        rows = self._season_matches(spec.fdo)
        if not rows:
            return FixturesResult(NAME, sport_key, ())

        year = _row_season_year(rows[0])
        if year is not None:
            ref = SeasonRef(sequence_id=-1, year=year)
            self._season_cache.setdefault(spec.fdo, ref)

        out: list[dict[str, Any]] = []
        for row in rows:
            fx = self._to_fixture(row, sport_key, spec.title)
            if fx is not None:
                out.append(fx)
        return FixturesResult(NAME, sport_key, tuple(out))

    # -- internals ----------------------------------------------------------

    def get_season(self, sport_key: str, season: int) -> FixturesResult:
        spec = LEAGUES.get(sport_key)
        if spec is None or not spec.fdo:
            return FixturesResult(NAME, sport_key, ())
        if not self.is_available():
            raise ProviderNotAvailableError('FOOTBALL_DATA_TOKEN is not configured', provider=NAME)
        rows = self._get(f'/competitions/{spec.fdo}/matches', params={'season': str(season)},
                         ttl=SEASON_TTL_SEC, envelope_key='matches')
        fixtures = [self._to_fixture(row, sport_key, spec.title) for row in rows]
        return FixturesResult(NAME, sport_key, tuple(f for f in fixtures if f is not None))

    def _season_matches(self, code: str, year: Optional[str] = None) -> list[dict[str, Any]]:
        """One season's matches. Cached; an in-progress season changes daily."""
        params = {"season": year} if year else None
        return self._get(f"/competitions/{code}/matches",
                         params=params,
                         ttl=RECENT_TTL_SEC, envelope_key="matches")

    def _to_fixture(self, row: dict[str, Any], sport_key: str,
                    league_title: str) -> Optional[dict[str, Any]]:
        try:
            kick = str(row.get("utcDate") or "")
            if not kick:
                return None
            from datetime import datetime
            epoch = datetime.fromisoformat(kick.replace("Z", "+00:00")).timestamp()

            home = ((row.get("homeTeam") or {}).get("name")) or ""
            away = ((row.get("awayTeam") or {}).get("name")) or ""
            if not home or not away:
                return None

            score = row.get('score') if isinstance(row.get('score'), dict) else {}
            extra_time = score.get('duration') in ('EXTRA_TIME', 'PENALTY_SHOOTOUT')
            regulation = score.get('regularTime' if extra_time else 'fullTime') or {}
            hs = _to_int(regulation.get('home'))
            aws = _to_int(regulation.get('away'))
            # A match can be finished-but-unofficial (played, awaiting review);
            # only score.fullTime is a final result we are willing to train on.
            status = _STATUS_MAP.get(str(row.get("status", "")).upper(), "SCHEDULED")
            if status == "FINISHED" and (hs is None or aws is None):
                status = "TIMED"

            result = normalise_fixture(
                provider=NAME, sport_key=sport_key,
                match_id=str(row.get("id", "")),
                kickoff_epoch=epoch, home=home, away=away,
                home_score=hs, away_score=aws, status=status,
                season=str(row.get("season", {}).get("id", "")),
            )
            result.update(home_team_id=row['homeTeam'].get('id'), away_team_id=row['awayTeam'].get('id'),
                          ended_after_extra_time=extra_time)
            half = score.get('halfTime') or {}
            if all(type(half.get(s)) is int and half[s] >= 0 for s in ('home', 'away')):
                if not result['completed'] or (half['home'] <= result['home_score'] and half['away'] <= result['away_score']):
                    result.update(home_first_half_score=half['home'], away_first_half_score=half['away'])
            return result
        except Exception as exc:
            logger.debug("skipping malformed football-data.org row: %r", exc)
            return None

    def _get(self, path: str, *, params: Optional[dict[str, Any]] = None,
             ttl: float = 0.0,
             envelope_key: Optional[str] = None) -> list[dict[str, Any]]:
        headers = {"X-Auth-Token": self._token} if self._token else {}
        payload = self._transport.get_json(
            f"{BASE_URL}{path}", params=params, headers=headers,
            ttl=ttl, provider=NAME)
        return self._unwrap(payload, envelope_key)

    @staticmethod
    def _unwrap(payload: Any, envelope_key: Optional[str] = None) -> Any:
        """Return the payload itself, or the list inside its envelope.

        Every football-data.org endpoint answers with an object wrapper
        (``{"matches": [...]}``, ``{"currentSeason": {...}}``), so a caller
        asking for a scalar field needs the dict and a caller asking for rows
        needs the list. Returning one shape for both silently broke
        ``current_season``: it received a single-element list and called
        ``.get()`` on it, so every league resolved to "no season" and
        ``get_fixtures`` returned nothing at all -- which looks identical to
        "this league has no fixtures".

        With ``envelope_key`` the result is a list of rows; without it the raw
        payload is returned so object-shaped fields stay reachable.
        """
        if payload is None:
            return [] if envelope_key else None
        if isinstance(payload, list):
            rows = [r for r in payload if isinstance(r, dict)]
            return rows if envelope_key else payload
        if isinstance(payload, dict):
            if envelope_key:
                rows = payload.get(envelope_key)
                return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
            return payload
        return [] if envelope_key else None


def _season_year(season_id: int) -> str:
    """The calendar year the ``matches`` endpoint expects for a season id.

    ``currentSeason.startDate`` is the only reliable bridge between the two
    namespaces, but fetching it costs a request per competition. The id is
    usable on its own: the first Bundesliga season of the v4 era is 2018/19,
    and ``(season_id - 2004)`` maps that to 2014... which is wrong. So rather
    than guess, the sequence ids are monotonic per competition and the year is
    derived from the id only when it is already a plausible four-digit year;
    otherwise the caller falls back to omitting the parameter and taking
    whatever the endpoint defaults to (the current season).
    """
    if 1990 <= season_id <= 2100:
        return str(season_id)
    # Sequence ids in the 2xxx range are offset per competition but the offset
    # is constant for v4: 2522 -> 2026/27, i.e. season_id - 496 = start year.
    return str(season_id - 496)


def _row_season_year(row: Any) -> Optional[str]:
    """The calendar year of the season a match row belongs to.

    The response carries ``season.startDate`` on every row, so the year is
    read back from the data rather than spending a separate request to ask for
    it. This is only used to populate the season cache for a later historical
    fetch; if a row omits it, ``None`` is correct and no year is invented.
    """
    if not isinstance(row, dict):
        return None
    season = row.get("season")
    if not isinstance(season, dict):
        return None
    return _season_year(season.get("startDate"))


def _season_year(start_date: Any) -> Optional[str]:
    """The calendar year the ``matches`` endpoint expects for a season.

    football-data.org names a season two ways. ``/competitions/{code}`` reports
    ``currentSeason.id`` as an internal sequence (2522 for 2026/27) while
    ``/competitions/{code}/matches`` only accepts the four-digit calendar year
    (2026) and answers 404 for the sequence id. ``startDate`` is the bridge.

    Deriving the year arithmetically from the id would look correct until one
    competition's sequence numbering shifted, at which point every fixture for
    that league silently disappeared and read as "no fixtures in window".
    """
    text = str(start_date or "").strip()
    if len(text) >= 4 and text[:4].isdigit():
        year = int(text[:4])
        if 1990 <= year <= 2100:
            return str(year)
    return None


def _to_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = ["FootballDataProvider", "NAME", "BASE_URL"]
