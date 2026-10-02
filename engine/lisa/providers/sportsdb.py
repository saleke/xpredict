"""TheSportsDB -- free key ``3``, but severely truncated. Supplementary only.

Verified behaviour of the public free key (probed live, 10 leagues exposed):

    GET /api/v1/json/3/all_leagues.php          -> 10 leagues (EPL, Bundesliga,
                                                    Serie A, La Liga, Ligue 1,
                                                    Eredivisie, Greece, Belgium,
                                                    Scotland, Championship)
    GET /api/v1/json/3/eventsseason.php?id=4328&s=2025-2026
                                                 -> **5 events**, not 380
    GET /api/v1/json/3/eventsnextleague.php?id=4328
                                                 -> **1 event**

This is the trap worth naming: the free key is a *legacy test key* and every
collection endpoint is clipped to a handful of rows. An adapter that ignored
that would appear to work -- it returns HTTP 200, a well-formed ``events``
array, and plausible-looking fixtures -- while silently contributing almost
nothing to a season. A model trained on five results is worse than no model,
because it looks calibrated.

So this adapter is explicit about being degraded:

* it declares ``degraded = True`` so the registry can rank it below the real
  sources and the health view can say why;
* it measures what it actually received and reports the truncation, instead of
  presenting a clipped list as a complete season;
* it is still worth keeping: it covers Greek, Belgian and Scottish football
  that no other free source reaches, and it is a genuine second opinion on the
  five leagues that overlap.

``strTimestamp`` (unix seconds) is the only unambiguous kickoff field;
``dateEvent`` + ``strTime`` is a local-time pairing with no offset and is used
only as a fallback.
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

logger = logging.getLogger("lisa.providers.sportsdb")

BASE_URL = "https://www.thesportsdb.com/api/v1/json"
NAME = "sportsdb"

#: The documented public free key. No account needed.
FREE_KEY = "3"

RATE_PER_SEC = 30.0 / 60.0     # documented 30 requests/minute
BURST = 5
SEASON_TTL_SEC = 6 * 3600

#: Observed ceiling of the free key on a collection endpoint. Anything at or
#: below this is a clip, not a season, and is reported as such.
FREE_KEY_CLIP = 12


class SportsDbProvider:
    """Supplementary fixtures for leagues no other free source covers."""

    name = NAME
    tier = SourceTier.OFFICIAL
    #: Set so the registry can rank it below the genuine backbones.
    degraded = True

    def __init__(self, key: str = FREE_KEY, *,
                 transport: Optional[HttpTransport] = None,
                 timeout: float = 15.0) -> None:
        self._key = (key or FREE_KEY).strip() or FREE_KEY
        self._transport = transport or HttpTransport(timeout=timeout)
        self._transport.set_rate_limit("www.thesportsdb.com", RATE_PER_SEC, BURST)
        self.last_truncated = False

    # -- contract -----------------------------------------------------------

    def is_available(self) -> bool:
        return True

    def quota_status(self) -> ProviderQuota:
        limit = 30
        return ProviderQuota(remaining=limit, limit=limit, reset_at=0.0,
                             tier=SourceTier.OFFICIAL)

    def leagues(self) -> list[str]:
        return [k for k, spec in LEAGUES.items() if spec.tsdb]

    def all_leagues(self) -> list[dict[str, Any]]:
        return self._get("all_leagues.php", ttl=24 * 3600)

    # -- endpoints ----------------------------------------------------------

    def get_fixtures(self, sport_key: str) -> FixturesResult:
        spec = LEAGUES.get(sport_key)
        if spec is None or not spec.tsdb:
            return FixturesResult(NAME, sport_key, ())

        self.last_truncated = False
        # One request, not three. ``eventsnextleague`` is the only one of the
        # three the free key does not clip to a handful of rows, and the only
        # one that can contribute upcoming fixtures -- which is all the board
        # needs. Measured across 10 leagues the extra two calls cost ~50s of the
        # cycle's 90s fetch budget (the documented 30 req/min pacing makes each
        # one ~1s of enforced wait) and returned 10 rows in total: one per
        # league, every one already present from eventsnextleague.
        #
        # Model training data does not come from here either: football-data.org
        # and OpenLigaDB both return whole seasons. This source's real jobs are
        # cross-checking a fixture list and covering the three leagues no other
        # free source carries.
        rows: list[dict[str, Any]] = []
        rows.extend(self._get("eventsnextleague.php",
                              params={"id": spec.tsdb}, ttl=900.0))

        if 0 < len(rows) <= FREE_KEY_CLIP:
            self.last_truncated = True
            logger.warning(
                "TheSportsDB free key returned only %d rows for %s -- this is "
                "the documented clip, not a season. Treat as supplementary.",
                len(rows), spec.title)

        out: list[dict[str, Any]] = []
        for row in rows:
            fx = self._to_fixture(row, sport_key)
            if fx is not None:
                out.append(fx)
        return FixturesResult(NAME, sport_key, tuple(out))

    # -- internals ----------------------------------------------------------

    def _to_fixture(self, row: dict[str, Any], sport_key: str) -> Optional[dict[str, Any]]:
        try:
            home = str(row.get("strHomeTeam") or "").strip()
            away = str(row.get("strAwayTeam") or "").strip()
            if not home or not away:
                return None

            epoch = _epoch_of(row)
            if epoch is None:
                return None

            hs = _to_int(row.get("intHomeScore"))
            aws = _to_int(row.get("intAwayScore"))
            raw_status = str(row.get("strStatus") or "").upper()
            # strStatus is a short code ("FT", "NS", "Match Finished"); the
            # human field is more reliable when present, so prefer it.
            if str(row.get("strPostponed", "")).lower() in ("1", "true", "yes"):
                status = "POSTPONED"
            elif raw_status in ("FT", "AET", "PEN", "MATCH FINISHED"):
                status = "FINISHED"
            elif raw_status in ("NS", "SCHEDULED", "TIMED"):
                status = "SCHEDULED"
            else:
                status = "SCHEDULED"
            if status == "FINISHED" and (hs is None or aws is None):
                status = "TIMED"

            return normalise_fixture(
                provider=NAME, sport_key=sport_key,
                match_id=str(row.get("idEvent", "")),
                kickoff_epoch=epoch, home=home, away=away,
                home_score=hs, away_score=aws, status=status,
                season=str(row.get("strSeason", "")),
            )
        except Exception as exc:
            logger.debug("skipping malformed TheSportsDB row: %r", exc)
            return None

    def _get(self, path: str, *, params: Optional[dict[str, Any]] = None,
             ttl: float = 0.0) -> list[dict[str, Any]]:
        url = f"{BASE_URL}/{self._key}/{path}"
        payload = self._transport.get_json(url, params=params, ttl=ttl, provider=NAME)
        if isinstance(payload, dict):
            for key in ("events", "event", "results", "leagues", "league"):
                rows = payload.get(key)
                if isinstance(rows, list):
                    return [r for r in rows if isinstance(r, dict)]
            return []
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        return []


def _epoch_of(row: dict[str, Any]) -> Optional[float]:
    """Kickoff as unix seconds. Prefers the explicit timestamp."""
    ts = row.get("strTimestamp")
    if ts:
        try:
            value = float(ts)
            # Guard against a sentinel zero, which would place the match in 1970
            # and make it look decades overdue.
            if value > 1_000_000_000:
                return value
        except (TypeError, ValueError):
            pass
    date_s = str(row.get("dateEvent") or "").strip()
    if not date_s:
        return None
    time_s = str(row.get("strTime") or "00:00:00").strip()
    from datetime import datetime, timezone
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(f"{date_s} {time_s}", fmt).replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _current_season() -> str:
    """TheSportsDB season labels, derived from the European season boundary."""
    from datetime import date
    today = date.today()
    start = today.year if today.month >= 7 else today.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def _to_int(value: Any) -> Optional[int]:
    try:
        if value in (None, "", "null"):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = ["SportsDbProvider", "NAME", "BASE_URL", "FREE_KEY"]
