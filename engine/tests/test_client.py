"""Client: fetch_league_odds URL contract, defaults, graceful error states."""
from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from lisa.client import ApiError, FixtureClient, OddsApiClient, RateLimited


class _Recorder:
    def __init__(self) -> None:
        self.path: str | None = None
        self.params: dict[str, str] = {}

    def __call__(self, path: str, params: dict[str, str]) -> list:
        self.path = path
        self.params = params
        return []


def _client() -> tuple[OddsApiClient, _Recorder]:
    rec = _Recorder()
    client = OddsApiClient("test-key", max_retries=0)
    client._get = rec
    return client, rec


def test_fetch_league_odds_h2h_url_contract():
    client, rec = _client()
    client.fetch_league_odds("basketball_nba")
    assert rec.path == "/v4/sports/basketball_nba/odds/"
    assert rec.params == {
        "apiKey": "test-key",
        "regions": "eu,us",
        "markets": "h2h",
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }


def test_fetch_league_odds_default_region_is_eu_us():
    client, rec = _client()
    client.fetch_league_odds("soccer_epl")
    assert rec.params["regions"] == "eu,us"


def test_fetch_league_odds_region_override():
    client, rec = _client()
    client.fetch_league_odds("soccer_epl", region="eu")
    assert rec.params["regions"] == "eu"


def test_get_odds_default_regions_is_eu_us():
    client, rec = _client()
    client.get_odds("soccer_epl")
    assert rec.params["regions"] == "eu,us"


def _boom(code: int):
    def _urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "boom", {}, None)
    return _urlopen


def test_http_422_raises_api_error_without_retry(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _boom(422))
    client = OddsApiClient("test-key", max_retries=4)
    with pytest.raises(ApiError) as exc:
        client.fetch_league_odds("soccer_epl")
    assert "422" in str(exc.value)


def test_http_500_retries_then_raises_api_error(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _boom(500))
    client = OddsApiClient("test-key", max_retries=0)
    with pytest.raises(ApiError) as exc:
        client.fetch_league_odds("soccer_epl")
    assert "failed" in str(exc.value)


def test_http_429_raises_rate_limited(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _boom(429))
    client = OddsApiClient("test-key", max_retries=0)
    with pytest.raises(RateLimited):
        client.fetch_league_odds("soccer_epl")


def test_fixture_client_fetch_league_odds_parity():
    client = FixtureClient({"soccer_epl": [{"id": "x"}]})
    assert client.fetch_league_odds("soccer_epl") == [{"id": "x"}]
    assert client.fetch_league_odds("basketball_nba") == []