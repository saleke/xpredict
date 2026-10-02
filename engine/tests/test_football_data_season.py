"""football-data.org season resolution and fixture parsing.

Two bugs lived here, and both presented as the same symptom: an empty board.

1. ``current_season`` called ``_get`` with no ``envelope_key``, so it received
   a single-element *list* and called ``.get()`` on it. That raised an
   ``AttributeError`` inside the provider, which the feed swallowed as a
   provider error. Every football-data.org league therefore returned zero
   fixtures, and an invalid token looked exactly like a quiet league.

2. Even once (1) was fixed, the season *id* was passed to the matches endpoint.
   football-data.org names a season as an internal sequence in
   ``/competitions/{code}`` (2522 for 2026/27) but only accepts a calendar year
   in ``/competitions/{code}/matches`` (2026), answering 404 for the sequence.
   A 404 from the fetch phase is reported as "no fixtures", so a correct token
   still produced an empty board.

Together these meant the token could never be exercised end-to-end. The tests
below pin the translation and the unwrapping without touching the network.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lisa.providers.football_data import (  # noqa: E402
    NAME,
    FootballDataProvider,
    SeasonRef,
    _season_year,
    _to_int,
)


class _StubTransport:
    """Returns canned payloads and records every URL it was asked for."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_json(self, url: str, *, params: Any = None, headers: Any = None,
                 ttl: float = 0.0, provider: str = "") -> Any:
        self.calls.append((url, dict(params or {})))
        for fragment, payload in self.responses.items():
            if fragment in url:
                return payload
        return None

    def set_rate_limit(self, *_a: Any, **_k: Any) -> None:
        pass


# ---------------------------------------------------------------------------
# _season_year: bridging the two namespaces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("start_date,expected", [
    ("2026-08-28", "2026"),      # the season that triggered this
    ("2025-08-22", "2025"),
    ("2024-08-23", "2024"),
    ("2026-08-28T00:00:00Z", "2026"),
])
def test_season_year_is_taken_from_start_date(start_date: str, expected: str) -> None:
    assert _season_year(start_date) == expected


@pytest.mark.parametrize("bad", [None, "", "unknown", "18/19", "not-a-date"])
def test_season_year_refuses_to_guess(bad: Any) -> None:
    """A missing startDate must yield None, never a fabricated year.

    Guessing here would send an arbitrary season to the matches endpoint, and
    the resulting rows would be attributed to the wrong season without any
    error -- the exact silent-wrong-data failure this project forbids.
    """
    assert _season_year(bad) is None


def test_season_year_rejects_implausible_years() -> None:
    assert _season_year("0026-08-28") is None
    assert _season_year("9999-08-28") is None


# ---------------------------------------------------------------------------
# current_season: the unwrap that used to raise
# ---------------------------------------------------------------------------


def test_current_season_returns_id_and_year_not_a_list() -> None:
    transport = _StubTransport({
        "/competitions/BL1": {"currentSeason": {"id": 2522, "startDate": "2026-08-28"}},
    })
    provider = FootballDataProvider("t" * 32, transport=transport)

    ref = provider.current_season("BL1")

    assert ref is not None
    assert ref.sequence_id == 2522
    assert ref.year == "2026"


def test_current_season_returns_none_when_the_payload_is_a_list() -> None:
    """The original bug, pinned: a list payload must not raise."""
    transport = _StubTransport({"/competitions/BL1": [{"unexpected": "shape"}]})
    provider = FootballDataProvider("t" * 32, transport=transport)

    # Before the fix this raised AttributeError: 'list' object has no
    # attribute 'get'. A provider must degrade to "no season", not explode.
    assert provider.current_season("BL1") is None


def test_current_season_returns_none_when_there_is_no_current_season() -> None:
    transport = _StubTransport({"/competitions/BL1": {"code": "BL1", "name": "Bundesliga"}})
    provider = FootballDataProvider("t" * 32, transport=transport)
    assert provider.current_season("BL1") is None


def test_current_season_is_cached_so_the_lookup_costs_one_request() -> None:
    transport = _StubTransport({
        "/competitions/BL1": {"currentSeason": {"id": 2522, "startDate": "2026-08-28"}},
    })
    provider = FootballDataProvider("t" * 32, transport=transport)

    provider.current_season("BL1")
    provider.current_season("BL1")
    provider.current_season("BL1")

    assert len(transport.calls) == 1, "the season lookup must be cached"


# ---------------------------------------------------------------------------
# get_fixtures: the end-to-end shape, without the network
# ---------------------------------------------------------------------------


def _match_row(**over: Any) -> dict[str, Any]:
    row = {
        "utcDate": "2026-10-02T18:30:00Z",
        "status": "FINISHED",
        "homeTeam": {"name": "Bayern Munich"},
        "awayTeam": {"name": "Borussia Dortmund"},
        "score": {"fullTime": {"home": 2, "away": 1}},
        "season": {"startDate": "2026-08-28"},
    }
    row.update(over)
    return row


def test_get_fixtures_never_sends_the_sequence_id_as_a_season() -> None:
    """2522 is a 404 on the matches endpoint; the year is what it accepts."""
    transport = _StubTransport({
        "/competitions/BL1/matches": {"matches": [_match_row()]},
        "/competitions/BL1": {"currentSeason": {"id": 2522, "startDate": "2026-08-28"}},
    })
    provider = FootballDataProvider("t" * 32, transport=transport)

    provider.get_fixtures("soccer_germany_bundesliga")

    for url, params in transport.calls:
        if "/matches" not in url:
            continue
        sent = params.get("season")
        assert sent != "2522", "the sequence id was sent as a season; that 404s"


def test_get_fixtures_costs_one_request_per_league() -> None:
    """Two requests per league exhausts the 10 req/min free tier.

    The season lookup exists only to name the season, and the matches endpoint
    defaults to the current season when the parameter is omitted, so the year
    is read back off the response instead of spending a request to ask for it.
    """
    transport = _StubTransport({
        "/competitions/BL1/matches": {"matches": [_match_row()]},
        "/competitions/BL1": {"currentSeason": {"id": 2522, "startDate": "2026-08-28"}},
    })
    provider = FootballDataProvider("t" * 32, transport=transport)

    provider.get_fixtures("soccer_germany_bundesliga")

    assert len(transport.calls) == 1, (
        f"expected one request per league, made {len(transport.calls)}: "
        f"{[u for u, _ in transport.calls]}"
    )


def test_the_season_year_is_learned_from_the_response_rows() -> None:
    transport = _StubTransport({"/competitions/BL1/matches": {"matches": [_match_row()]}})
    provider = FootballDataProvider("t" * 32, transport=transport)

    provider.get_fixtures("soccer_germany_bundesliga")

    ref = provider._season_cache.get("BL1")
    assert ref is not None, "the year should be cached for later use"
    assert ref.year == "2026"
    assert ref.sequence_id == -1, "the id was never fetched, so it is unknown"


def test_a_row_without_season_data_caches_nothing() -> None:
    """A row missing season.startDate must not have a year invented for it."""
    row = _match_row()
    row.pop("season")
    transport = _StubTransport({"/competitions/BL1/matches": {"matches": [row]}})
    provider = FootballDataProvider("t" * 32, transport=transport)

    result = provider.get_fixtures("soccer_germany_bundesliga")

    assert len(result.fixtures) == 1, "the row itself is still usable"
    assert "BL1" not in provider._season_cache, "no year was fabricated"


def test_an_explicit_season_year_is_forwarded_when_asked_for() -> None:
    transport = _StubTransport({"/competitions/BL1/matches": {"matches": [_match_row()]}})
    provider = FootballDataProvider("t" * 32, transport=transport)

    provider._season_matches("BL1", "2024")

    url, params = transport.calls[-1]
    assert params.get("season") == "2024", "an explicit historical year must be sent"


def test_get_fixtures_parses_a_finished_match() -> None:
    transport = _StubTransport({
        "/competitions/BL1/matches": {"matches": [_match_row()]},
        "/competitions/BL1": {"currentSeason": {"id": 2522, "startDate": "2026-08-28"}},
    })
    provider = FootballDataProvider("t" * 32, transport=transport)

    result = provider.get_fixtures("soccer_germany_bundesliga")

    assert len(result.fixtures) == 1
    fixture = result.fixtures[0]
    assert fixture["home_team"] == "Bayern Munich"
    assert fixture["away_team"] == "Borussia Dortmund"
    assert fixture["home_score"] == 2
    assert fixture["away_score"] == 1
    assert fixture["completed"] is True
    assert result.finished()


def test_a_finished_status_without_a_scoreline_is_not_a_result() -> None:
    """A played-but-unreviewed match must not become training data."""
    transport = _StubTransport({
        "/competitions/BL1/matches": {
            "matches": [_match_row(score={"fullTime": {"home": None, "away": None}})],
        },
        "/competitions/BL1": {"currentSeason": {"id": 2522, "startDate": "2026-08-28"}},
    })
    provider = FootballDataProvider("t" * 32, transport=transport)

    result = provider.get_fixtures("soccer_germany_bundesliga")

    assert not result.finished(), "a match with no goals was treated as a result"
    assert result.fixtures[0]["completed"] is False


def test_get_fixtures_returns_empty_when_the_season_cannot_be_resolved() -> None:
    """Unresolvable season => no fixtures, stated, not invented."""
    transport = _StubTransport({"/competitions/PL": None})
    provider = FootballDataProvider("t" * 32, transport=transport)

    result = provider.get_fixtures("soccer_epl")

    assert result.fixtures == ()


def test_an_unavailable_token_raises_a_named_error() -> None:
    provider = FootballDataProvider("", transport=_StubTransport({}))
    assert provider.is_available() is False
    with pytest.raises(Exception) as excinfo:
        provider.get_fixtures("soccer_epl")
    assert "FOOTBALL_DATA_TOKEN" in str(excinfo.value)


def test_matches_envelope_absent_yields_no_rows() -> None:
    transport = _StubTransport({
        "/competitions/BL1/matches": {"message": "nothing here"},
        "/competitions/BL1": {"currentSeason": {"id": 2522, "startDate": "2026-08-28"}},
    })
    provider = FootballDataProvider("t" * 32, transport=transport)
    assert provider.get_fixtures("soccer_germany_bundesliga").fixtures == ()


def test_a_league_the_source_does_not_carry_is_empty_not_an_error() -> None:
    provider = FootballDataProvider("t" * 32, transport=_StubTransport({}))
    # basketball_nba has no football-data.org competition code.
    assert provider.get_fixtures("basketball_nba").fixtures == ()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_season_ref_is_frozen() -> None:
    ref = SeasonRef(sequence_id=2522, year="2026")
    with pytest.raises(Exception):
        ref.year = "2027"  # type: ignore[misc]


def test_to_int_is_defensive() -> None:
    assert _to_int("2") == 2
    assert _to_int(2) == 2
    assert _to_int(None) is None
    assert _to_int("n/a") is None
    assert _to_int({}) is None


def test_provider_name_is_stable() -> None:
    assert NAME == "football_data"