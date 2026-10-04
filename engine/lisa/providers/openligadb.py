"""OpenLigaDB -- the only free source with no key and no published limit.

Verified shape (probed live):

    GET https://openligadb.de/api/getmatchdata/bl1
    -> 301 to a trailing-slash URL; MUST be followed or the body is HTML.
    -> [{ "matchID": 83190, "matchDateTimeUTC": "2026-09-20T15:30:00Z",
          "team1": {"teamId": 9, "teamName": "FC Schalke 04"},
          "team2": {...}, "matchIsFinished": true,
          "matchResults": [{"resultTypeID": 1, "pointsTeam1": 0, "pointsTeam2": 0},
                           {"resultTypeID": 2, ...}],
          "goals": [...], "leagueShortcut": "bl1" }]

Two schema details that are silent data-corruption risks rather than crashes:

* ``matchDateTime`` is **local time with no offset**; only ``matchDateTimeUTC``
  is unambiguous. Using the local field shifts every kickoff by an hour across
  the summer/winter boundary, which quietly moves fixtures in and out of the
  48h product window at exactly the wrong time of year.
* ``resultTypeID`` 1 is half-time and 2 is full-time. Reading the first entry
  trains a goal model on 45-minute scorelines. Only ``resultTypeID == 2`` is a
  final result.

Coverage is German football (Bundesliga, 2. Bundesliga, DFB-Pokal). That is
narrow, but it is unmetered, so it is the ideal primary for the leagues it
covers and the ideal backfill when every other source is exhausted.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .base import (
    FixturesResult,
    HttpTransport,
    ProviderQuota,
    SourceTier,
)
from .calendar import LEAGUES, normalise_fixture

logger = logging.getLogger("lisa.providers.openligadb")

BASE_URL = "https://openligadb.de/api"
NAME = "openligadb"

#: No published limit, but it is a charity-hosted public service. A slow,
#: polite 1 req/s costs nothing and keeps us a good citizen.
RATE_PER_SEC = 1.0
BURST = 2

#: A finished German matchday never changes. 12h is long enough to make a full
#: 9-matchday sweep cost one request per matchday.
SEASON_TTL_SEC = 12 * 3600

FULL_TIME_RESULT_ID = 2


class OpenLigaDbProvider:
    """German football fixtures + results. No API key required."""

    name = NAME
    tier = SourceTier.OFFICIAL

    def __init__(self, *, transport: Optional[HttpTransport] = None,
                 timeout: float = 15.0, season_years: int = 2) -> None:
        self._transport = transport or HttpTransport(timeout=timeout)
        self._transport.set_rate_limit("openligadb.de", RATE_PER_SEC, BURST)
        #: How many calendar years of season data to pull. One Bundesliga season
        #: is 306 matches and 34 per club, which clears the model's sufficiency
        #: bar on its own; the extra year is depth for time decay, and costs one
        #: request on an unmetered host.
        self._season_years = max(1, season_years)

    # -- contract -----------------------------------------------------------

    def is_available(self) -> bool:
        return True

    def quota_status(self) -> ProviderQuota:
        # Unmetered: limit 0 means "never exhausted" by contract.
        return ProviderQuota(remaining=-1, limit=0, reset_at=0.0,
                             tier=SourceTier.OFFICIAL)

    def leagues(self) -> list[str]:
        return [k for k, spec in LEAGUES.items() if spec.oldb]

    # -- endpoints ----------------------------------------------------------

    def available_leagues(self) -> list[dict[str, Any]]:
        return self._get("/getavailableleagues", ttl=7 * 24 * 3600)

    def get_fixtures(self, sport_key: str) -> FixturesResult:
        spec = LEAGUES.get(sport_key)
        if spec is None or not spec.oldb:
            return FixturesResult(NAME, sport_key, ())
        out: list[dict[str, Any]] = []
        for year in self._recent_years():
            for row in self._season(spec.oldb, year):
                fx = self._to_fixture(row, sport_key)
                if fx is not None:
                    out.append(fx)
        # Chronological order matters downstream: the model sorts by it and a
        # season fetched as "current then previous year" arrives inverted.
        out.sort(key=lambda f: f["epoch"])
        return FixturesResult(NAME, sport_key, tuple(out))

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _recent_years() -> list[int]:
        """Calendar years worth asking for.

        The bare ``/getmatchdata/{league}`` endpoint returns only the *current*
        matchday -- which, right after a matchday, is a set of already-played
        games and produces an empty board. The season endpoint is the one that
        carries both history for the model and the fixture list for the board.

        Seasons straddle two calendar years and the API filters on each match's
        calendar year, so the current and previous year together cover the active
        season plus enough history to time-decay against.
        """
        from datetime import date
        today = date.today()
        return [today.year, today.year - 1]

    def _season(self, shortcut: str, year: int) -> list[dict[str, Any]]:
        from datetime import date
        ttl = 300 if year == date.today().year else SEASON_TTL_SEC
        return self._get(f"/getmatchdata/{shortcut}/{year}", ttl=ttl)

    def _to_fixture(self, row: dict[str, Any], sport_key: str) -> Optional[dict[str, Any]]:
        try:
            kick = str(row.get("matchDateTimeUTC") or "")
            if not kick:
                return None
            from datetime import datetime
            epoch = datetime.fromisoformat(kick.replace("Z", "+00:00")).timestamp()

            home = str((row.get("team1") or {}).get("teamName") or "")
            away = str((row.get("team2") or {}).get("teamName") or "")
            if not home or not away:
                return None

            hs = aws = None
            status = "SCHEDULED"
            if row.get("matchIsFinished"):
                for res in row.get("matchResults") or []:
                    if not isinstance(res, dict):
                        continue
                    if res.get("resultTypeID") == FULL_TIME_RESULT_ID:
                        hs = _to_int(res.get("pointsTeam1"))
                        aws = _to_int(res.get("pointsTeam2"))
                        break
                status = "FINISHED" if (hs is not None and aws is not None) else "TIMED"

            return normalise_fixture(
                provider=NAME, sport_key=sport_key,
                match_id=str(row.get("matchID", "")),
                kickoff_epoch=epoch, home=home, away=away,
                home_score=hs, away_score=aws, status=status,
                season=str(row.get("leagueSeason", "")),
            )
        except Exception as exc:
            logger.debug("skipping malformed OpenLigaDB row: %r", exc)
            return None

    def _get(self, path: str, *, ttl: float = 0.0) -> list[dict[str, Any]]:
        payload = self._transport.get_json(
            f"{BASE_URL}{path}/", ttl=ttl, provider=NAME)
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        if isinstance(payload, dict):
            return [payload]
        return []


def _to_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = ["OpenLigaDbProvider", "NAME", "BASE_URL"]
