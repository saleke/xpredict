"""Transport layer for The Odds API, plus a fixture stand-in.

Design:
  * retry with exponential backoff on 5xx / transport errors and 429s;
  * 4xx (other than 408/429) fail fast — retrying a permission error is
    wasted effort and wasted credits;
  * every retryable failure eventually raises ``ApiError`` so the pipeline
    can degrade per-sport instead of dying.
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


class ApiError(RuntimeError):
    pass


class RateLimited(ApiError):
    pass


def _sleep_backoff(attempt: int, base: float) -> None:
    if attempt <= 0:
        return
    delay = min(base * (2 ** (attempt - 1)), 60.0) + random.uniform(0.0, 0.3)
    time.sleep(delay)


class OddsApiClient:
    def __init__(self, api_key: str = "", *, base_url: str = BASE_URL,
                 timeout: float = 15.0,
                 max_retries: int = DEFAULT_MAX_RETRIES,
                 backoff_base: float = DEFAULT_BACKOFF_BASE):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.last_remaining: int | None = None

    # -- transport -----------------------------------------------------------

    def _get(self, path: str, params: dict[str, str]) -> Any:
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
            if raw:
                self.last_remaining = int(raw)
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


class FixtureClient:
    """Serves static payloads — a drop-in stand-in for OddsApiClient.

    Used for the demo, CI and local development before an API key exists.
    """

    def __init__(self, odds_payloads: dict[str, list] | None = None,
                 scores_payloads: dict[str, list] | None = None):
        self.odds = odds_payloads or {}
        self.scores = scores_payloads or {}

    def list_sports(self) -> list[dict]:
        return [{"key": k, "active": True, "title": k} for k in self.odds]

    def get_odds(self, sport_key: str, **kwargs) -> list[dict]:
        return list(self.odds.get(sport_key, []))

    def fetch_league_odds(self, sport_key: str, region: str = "eu,us") -> list[dict]:
        return self.get_odds(sport_key)

    def get_scores(self, sport_key: str, **kwargs) -> list[dict]:
        return list(self.scores.get(sport_key, []))