"""Transport layer for The Odds API, plus a fixture stand-in.

Design:
  * the provider is DISABLED by default and must be opted into explicitly
    (``Settings.odds_api_enabled`` / ``LISA_ENABLE_ODDS_API=1``). While
    disabled, ``OddsApiClient._get`` raises before a socket is opened, so no
    credit can be spent no matter which command, thread or scheduler reaches
    it. The key is never placed in a URL, never logged, never transmitted;
  * retry with exponential backoff on 5xx / transport errors and 429s;
  * 4xx (other than 408/429) fail fast — retrying a permission error is
    wasted effort and wasted credits;
  * every retryable failure eventually raises ``ApiError`` so the pipeline
    can degrade per-sport instead of dying.

The live data path no longer needs this module: see ``lisa/providers/`` and
``lisa/feed.py`` for the free-tier stack. This is retained so the legacy
refinery keeps working for anyone who explicitly re-enables it.
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE_URL = "https://api.the-odds-api.com"
DEFAULT_MAX_RETRIES = 4
DEFAULT_BACKOFF_BASE = 2.0

DISABLED_HINT = (
    "The Odds API is disabled (LISA_ENABLE_ODDS_API is not set), so no "
    "credit was spent. Live data comes from the free stack instead: run "
    "`python -m lisa feed` (or GET /api/opportunity-board). Re-enable the "
    "retired provider with LISA_ENABLE_ODDS_API=1 only if you intend to "
    "spend credits."
)


class ApiError(RuntimeError):
    pass


class RateLimited(ApiError):
    pass


class OddsApiDisabled(ApiError):
    """Raised instead of spending a credit. Never retried, never retried hard."""


def _sleep_backoff(attempt: int, base: float) -> None:
    if attempt <= 0:
        return
    delay = min(base * (2 ** (attempt - 1)), 60.0) + random.uniform(0.0, 0.3)
    time.sleep(delay)


class OddsApiClient:
    def __init__(self, api_key: str = "", *, base_url: str = BASE_URL,
                 timeout: float = 15.0,
                 max_retries: int = DEFAULT_MAX_RETRIES,
                 backoff_base: float = DEFAULT_BACKOFF_BASE,
                 enabled: bool = True):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.enabled = enabled
        self.last_remaining: int | None = None
        self.last_used: int | None = None

    # -- transport -----------------------------------------------------------

    def _get(self, path: str, params: dict[str, str]) -> Any:
        # Kill switch. Placed ahead of URL assembly so the API key is never
        # formatted into a request, and ahead of the retry loop so a disabled
        # client cannot burn a single credit or a single retry.
        if not self.enabled:
            raise OddsApiDisabled(DISABLED_HINT)
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    self._track_remaining(resp.headers)
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                self._track_remaining(exc.headers)
                if exc.code == 429:
                    if attempt == self.max_retries:
                        raise RateLimited(
                            f"rate limited after {self.max_retries} retries") from exc
                    _sleep_backoff(attempt, self.backoff_base)
                    continue
                if exc.code >= 500 or exc.code == 408:
                    last_err = exc
                    _sleep_backoff(attempt, self.backoff_base)
                    continue
                raise ApiError(f"HTTP {exc.code}: {exc.reason}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_err = exc
                _sleep_backoff(attempt, self.backoff_base)
        raise ApiError(
            f"request failed after {self.max_retries + 1} attempts: {last_err}"
        ) from last_err

    def _track_remaining(self, headers) -> None:
        try:
            raw = headers.get("x-requests-remaining")
            if raw is not None and raw != "":
                self.last_remaining = int(raw)
            used = headers.get("x-requests-used")
            if used is not None and used != "":
                self.last_used = int(used)
        except (AttributeError, TypeError, ValueError):
            pass

    # -- endpoints -----------------------------------------------------------

    def list_sports(self) -> list[dict]:
        return self._get("/v4/sports/", {"apiKey": self.api_key})

    def get_odds(self, sport_key: str, *, regions: str = "eu,us",
                 markets: str = "h2h", odds_format: str = "decimal",
                 date_format: str = "iso") -> list[dict]:
        return self._get(
            f"/v4/sports/{urllib.parse.quote(sport_key)}/odds/",
            {
                "apiKey": self.api_key,
                "regions": regions,
                "markets": markets,
                "oddsFormat": odds_format,
                "dateFormat": date_format,
            },
        )

    def fetch_league_odds(self, sport_key: str, region: str = "eu,us") -> list[dict]:
        """Ingest one league's Head-to-Head moneyline market.

        Canonical spec-shaped entry point: h2h + decimal odds, with retry and
        per-league error isolation handled by the transport below.
        """
        return self.get_odds(sport_key, regions=region, markets="h2h")

    def get_scores(self, sport_key: str, *, days_from: int = 1) -> list[dict]:
        return self._get(
            f"/v4/sports/{urllib.parse.quote(sport_key)}/scores/",
            {"apiKey": self.api_key, "daysFrom": str(days_from)},
        )

    def get_events(self, sport_key: str, *,
                   commence_time_from: str = "",
                   commence_time_to: str = "") -> list[dict]:
        """Free /events endpoint: pre-match fixture list with kickoff times.

        Documented and measured to NOT count against the usage quota. This is
        the discovery primitive the planner relies on to spend /odds credits
        only on leagues that actually have fixtures in the product window.
        """
        params: dict[str, str] = {"apiKey": self.api_key, "dateFormat": "iso"}
        if commence_time_from:
            params["commenceTimeFrom"] = commence_time_from
        if commence_time_to:
            params["commenceTimeTo"] = commence_time_to
        return self._get(
            f"/v4/sports/{urllib.parse.quote(sport_key)}/events/", params)


class FixtureClient:
    """Serves static payloads — a drop-in stand-in for OddsApiClient.

    Used for the demo, CI and local development before an API key exists.
    """

    def __init__(self, odds_payloads: dict[str, list] | None = None,
                 scores_payloads: dict[str, list] | None = None,
                 events_payloads: dict[str, list] | None = None):
        self.odds = odds_payloads or {}
        self.scores = scores_payloads or {}
        self.events = events_payloads or {}

    def list_sports(self) -> list[dict]:
        keys = list(self.odds) or list(self.events)
        return [{"key": k, "active": True, "title": k} for k in keys]

    def get_odds(self, sport_key: str, **kwargs) -> list[dict]:
        return list(self.odds.get(sport_key, []))

    def fetch_league_odds(self, sport_key: str, region: str = "eu,us") -> list[dict]:
        return self.get_odds(sport_key)

    def get_scores(self, sport_key: str, **kwargs) -> list[dict]:
        return list(self.scores.get(sport_key, []))

    def get_events(self, sport_key: str, **kwargs) -> list[dict]:
        """Serve the free /events discovery endpoint.

        Synthesised from the bundled odds payloads (which already carry id,
        kickoff and both teams) unless an explicit events map is supplied, so
        the stand-in exercises the real discovery path the planner depends on
        instead of silently reporting an empty calendar.
        """
        if sport_key in self.events:
            return list(self.events[sport_key])
        out: list[dict] = []
        for ev in self.odds.get(sport_key, []):
            if not isinstance(ev, dict):
                continue
            out.append({
                "id": str(ev.get("id", "")),
                "sport_key": sport_key,
                "sport_title": str(ev.get("sport_title", sport_key)),
                "commence_time": str(ev.get("commence_time", "")),
                "home_team": str(ev.get("home_team", "")),
                "away_team": str(ev.get("away_team", "")),
                "completed": bool(ev.get("completed", False)),
            })
        return out