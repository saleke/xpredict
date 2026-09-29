"""Tests for the Odds API branch of MatchRouter.

The board is a public, unauthenticated endpoint and every fetch costs a
credit, so these cover the two things that can actually hurt: spending the
quota, and breaking the other tests' assumptions about source selection.
"""
from __future__ import annotations

import pytest

from lisa.match_router import MatchRouter
from lisa.odds import H2H, Book, Match, utcnow


def _match(match_id="m1", days_ahead=3, home="Arsenal", away="Chelsea"):
    from datetime import timedelta
    return Match(
        id=match_id,
        sport_key="soccer_epl",
        commence_time=utcnow() + timedelta(days=days_ahead),
        home_team=home,
        away_team=away,
        completed=False,
        market=H2H,
        bookmakers=(Book(key="b1", title="B1", last_update=utcnow(),
                         outcomes={home: 1.9, away: 2.0, "Draw": 3.4}),),
    )


class _FakeOddsClient:
    """Stands in for OddsApiClient; never touches the network."""

    def __init__(self, matches=None, remaining=None, raises=None):
        self.payload_calls = 0
        self.last_remaining = remaining
        self._matches = matches if matches is not None else [_match()]
        self._raises = raises

    def get_odds(self, sport_key, *, regions="eu,us", markets="h2h"):
        self.payload_calls += 1
        if self._raises is not None:
            raise self._raises
        # Mirror the real parse contract: raw dicts keyed by market.
        return [{
            "id": m.id,
            "sport_key": m.sport_key,
            "commence_time": m.commence_time.isoformat().replace("+00:00", "Z"),
            "home_team": m.home_team,
            "away_team": m.away_team,
            "bookmakers": [{
                "key": b.key, "title": b.title, "last_update": b.last_update.isoformat(),
                "markets": [{"key": H2H, "outcomes": [
                    {"name": n, "price": p} for n, p in b.outcomes.items()]}],
            } for b in m.bookmakers],
        } for m in self._matches]


def _router(client, **kw):
    r = MatchRouter(**kw)
    r._odds_client = client
    r._odds_client_resolved = True
    return r


# -- the happy path -------------------------------------------------------
def test_tier_a_league_fetches_from_odds_api():
    client = _FakeOddsClient()
    router = _router(client)
    matches = router.get_matches("soccer_epl", days_ahead=14)
    assert len(matches) == 1
    assert client.payload_calls == 1
    assert matches[0].home_team == "Arsenal"
    assert matches[0].bookmakers  # prices survived the parse


def test_second_call_is_served_from_cache():
    client = _FakeOddsClient()
    router = _router(client)
    router.get_matches("soccer_epl", days_ahead=14)
    router.get_matches("soccer_epl", days_ahead=14)
    router.get_matches("soccer_epl", days_ahead=14)
    assert client.payload_calls == 1, "public endpoint must not spend per refresh"


def test_cache_ttl_is_longer_for_odds_than_classification():
    client = _FakeOddsClient()
    router = _router(client, cache_ttl=300, odds_cache_ttl=900)
    router.get_matches("soccer_epl")
    expires, _ = router._cache["matches:soccer_epl:14:auto"]
    assert expires > utcnow().timestamp() + 600


# -- the credit floor ----------------------------------------------------
def test_credit_floor_prevents_spending_the_last_credits():
    client = _FakeOddsClient(remaining=9)
    router = _router(client, credit_floor=20)
    assert router.get_matches("soccer_epl") == []
    assert client.payload_calls == 0, "must not spend below the floor"


def test_credit_floor_allows_spend_above_it():
    client = _FakeOddsClient(remaining=50)
    router = _router(client, credit_floor=20)
    assert len(router.get_matches("soccer_epl")) == 1
    assert client.payload_calls == 1


def test_unknown_remaining_does_not_block():
    """No observed credit count yet must not be treated as zero."""
    client = _FakeOddsClient(remaining=None)
    router = _router(client, credit_floor=20)
    assert len(router.get_matches("soccer_epl")) == 1


def test_credits_remaining_property():
    router = _router(_FakeOddsClient(remaining=7), credit_floor=0)
    router.get_matches("soccer_epl")
    assert router.credits_remaining == 7


# -- the window ----------------------------------------------------------
def test_fixtures_beyond_the_horizon_are_dropped():
    from datetime import timedelta
    client = _FakeOddsClient(matches=[
        _match("soon", days_ahead=2),
        _match("later", days_ahead=40),
    ])
    router = _router(client)
    matches = router.get_matches("soccer_epl", days_ahead=14)
    assert [m.id for m in matches] == ["soon"]


def test_fixtures_in_the_past_are_dropped():
    from datetime import timedelta
    client = _FakeOddsClient(matches=[
        _match("old", days_ahead=-1),
        _match("now", days_ahead=1),
    ])
    router = _router(client)
    assert [m.id for m in router.get_matches("soccer_epl")] == ["now"]


# -- failure isolation ---------------------------------------------------
def test_api_failure_degrades_to_empty_not_exception():
    client = _FakeOddsClient(raises=RuntimeError("401 Unauthorized"))
    router = _router(client)
    assert router.get_matches("soccer_epl") == []


def test_exhausted_quota_error_is_swallowed():
    class _Exhausted(RuntimeError):
        pass
    client = _FakeOddsClient(raises=_Exhausted("Usage quota has been reached"))
    router = _router(client, credit_floor=0)
    assert router.get_matches("soccer_epl") == []


# -- source selection ----------------------------------------------------
def test_explicit_flashscore_source_does_not_call_odds_api():
    client = _FakeOddsClient()
    router = _router(client)
    router.get_matches("soccer_epl", source="flashscore")
    assert client.payload_calls == 0


def test_missing_key_degrades_without_raising(monkeypatch):
    monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)
    router = MatchRouter()  # real lazy client resolution, no key present
    assert router._get_odds_client() is None
    assert router.get_matches("soccer_epl") == []
