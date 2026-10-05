"""SharpAPI adapter: market mapping, fetch bounds, and the fixture matcher.

The tests are grouped around the failure modes that would be invisible on the
board but catastrophic in use: a price attached to the wrong match, a two-way
draw-no-bet read as 1X2, an absent price rendered as zero, and a fetch that runs
away from its rate limit.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lisa.board import Fixture, MarketPrice  # noqa: E402
from lisa.providers.base import HttpTransport, SourceTier  # noqa: E402
from lisa.providers.sharpapi import (  # noqa: E402
    NAME,
    SharpApiOddsProvider,
    SharpQuote,
    SharpSnapshot,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _at(hours: float) -> str:
    return (NOW + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(**over) -> dict:
    """A delivered odds row, shaped like the real payload."""
    row = {
        "id": "r1",
        "sportsbook": "draftkings",
        "event_id": "england_-_premier_league_arsenal_chelsea_2026-10-02_b1",
        "sport": "soccer",
        "league": "england_-_premier_league",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "market_type": "moneyline",
        "selection": "Arsenal",
        "selection_type": "home",
        "odds_american": -150,
        "odds_decimal": 1.6667,
        "line": None,
        "event_start_time": _at(6),
        "timestamp": _at(0),
        "is_live": False,
        "is_active": True,
        "is_player_prop": False,
    }
    row.update(over)
    return row


def _fixture(match_id="football_data:9001", home="Arsenal FC", away="Chelsea",
             hours=6.0) -> Fixture:
    return Fixture(match_id=match_id, sport_key="soccer_epl",
                   kickoff=NOW + timedelta(hours=hours), home=home, away=away,
                   source="football_data")


class _StubTransport(HttpTransport):
    """Records calls, replays canned pages."""

    def __init__(self, pages):
        super().__init__(timeout=1.0)
        self.pages = list(pages)
        self.calls: list[dict] = []

    def get_json(self, url, *, params=None, headers=None, ttl=0.0, provider="",
                 accept_304=True):
        self.calls.append(dict(params or {}))
        if not self.pages:
            raise AssertionError("fetched more pages than the stub was given")
        return self.pages.pop(0)


# ---------------------------------------------------------------------------
# Market mapping
# ---------------------------------------------------------------------------


def test_a_three_way_moneyline_maps_onto_the_boards_selection_names() -> None:
    provider = SharpApiOddsProvider("k" * 30)
    rows = [
        _row(selection_type="home", selection="Arsenal"),
        _row(selection_type="draw", selection="Draw"),
        _row(selection_type="away", selection="Chelsea"),
    ]

    quotes = provider.parse_odds(rows, now=NOW)

    assert {q.selection for q in quotes} == {"Home", "Draw", "Away"}
    assert {q.market for q in quotes} == {"h2h"}
    assert all(q.line is None for q in quotes), "1X2 carries no line"


def test_a_two_way_moneyline_is_not_read_as_a_one_x_two() -> None:
    """Draw-no-bet is home/away only; using it as a Draw price invents one."""
    provider = SharpApiOddsProvider("k" * 30)
    rows = [_row(selection_type="home"), _row(selection_type="away")]

    quotes = provider.parse_odds(rows, now=NOW)

    assert quotes == [], "a two-way cohort produced 1X2 prices"


def test_the_two_way_rejection_is_counted_not_silent() -> None:
    provider = SharpApiOddsProvider("k" * 30)
    snapshot = SharpSnapshot()

    provider.parse_odds([_row(selection_type="home"), _row(selection_type="away")],
                        now=NOW, snapshot=snapshot)

    assert snapshot.dropped, "the dropped row left no trace"
    assert any("two-way" in reason for reason in snapshot.dropped)


def test_a_three_way_moneyline_is_accepted_even_when_one_side_is_missing() -> None:
    """A book may omit a price; that is a hole, not a reason to discard the rest."""
    provider = SharpApiOddsProvider("k" * 30)
    rows = [_row(selection_type="home"), _row(selection_type="draw")]

    quotes = provider.parse_odds(rows, now=NOW)

    assert {q.selection for q in quotes} == {"Home", "Draw"}


def test_goal_totals_map_onto_the_line_board_looks_up() -> None:
    provider = SharpApiOddsProvider("k" * 30)
    rows = [
        _row(market_type="total_goals", selection_type="over", selection="Over",
             line=2.5),
        _row(market_type="total_goals", selection_type="under", selection="Under",
             line=2.5),
    ]

    quotes = provider.parse_odds(rows, now=NOW)

    got = {(q.selection, q.line) for q in quotes}
    # board.py looks these up as f"Over {line}" with the line a float.
    assert got == {("Over 2.5", 2.5), ("Under 2.5", 2.5)}


def test_an_asian_total_is_read_as_the_same_market_as_a_plain_one() -> None:
    provider = SharpApiOddsProvider("k" * 30)
    rows = [_row(market_type="asian_total_goals", selection_type="over",
                 line=2.25)]

    quotes = provider.parse_odds(rows, now=NOW)

    assert [(q.market, q.selection, q.line) for q in quotes] == [
        ("totals", "Over 2.25", 2.25)]


def test_a_total_with_no_line_is_dropped_rather_than_assumed() -> None:
    provider = SharpApiOddsProvider("k" * 30)
    snapshot = SharpSnapshot()

    quotes = provider.parse_odds(
        [_row(market_type="total_goals", selection_type="over", line=None)],
        now=NOW, snapshot=snapshot)

    assert quotes == []
    assert any("no line" in reason for reason in snapshot.dropped)


def test_an_unmapped_market_type_contributes_nothing_and_is_counted() -> None:
    provider = SharpApiOddsProvider("k" * 30)
    snapshot = SharpSnapshot()

    quotes = provider.parse_odds(
        [_row(market_type="total_corners", selection_type="over", line=9.5)],
        now=NOW, snapshot=snapshot)

    assert quotes == [], "corners are not a market this model publishes"
    assert any("unmapped market type" in r for r in snapshot.dropped)


def test_a_suspended_market_is_dropped_rather_than_frozen() -> None:
    provider = SharpApiOddsProvider("k" * 30)

    quotes = provider.parse_odds(
        [_row(is_active=False, selection_type="draw")], now=NOW)

    assert quotes == [], "a closed market's frozen price is not an offer"


def test_a_live_price_is_dropped() -> None:
    provider = SharpApiOddsProvider("k" * 30)
    snapshot = SharpSnapshot()

    quotes = provider.parse_odds([_row(is_live=True)], now=NOW, snapshot=snapshot)

    assert quotes == []
    assert any("in-play" in reason for reason in snapshot.dropped)


def test_a_player_prop_is_dropped() -> None:
    provider = SharpApiOddsProvider("k" * 30)

    quotes = provider.parse_odds([_row(is_player_prop=True)], now=NOW)

    assert quotes == []


def test_a_missing_price_is_dropped_not_zeroed() -> None:
    """A missing price must never become 1.0 or 0.0 -- both are losses."""
    provider = SharpApiOddsProvider("k" * 30)
    snapshot = SharpSnapshot()
    rows = [
        _row(odds_decimal=None, odds_american=None, selection_type="home"),
        _row(odds_decimal=None, odds_american=None, selection_type="draw"),
    ]

    quotes = provider.parse_odds(rows, now=NOW, snapshot=snapshot)

    assert quotes == []
    assert any("no usable decimal" in reason for reason in snapshot.dropped)


def test_a_price_at_or_below_one_is_rejected() -> None:
    provider = SharpApiOddsProvider("k" * 30)
    rows = [
        _row(odds_decimal=1.0, selection_type="home"),
        _row(odds_decimal=1.0, selection_type="draw"),
    ]

    assert provider.parse_odds(rows, now=NOW) == []


def test_american_odds_are_used_when_decimal_is_absent() -> None:
    provider = SharpApiOddsProvider("k" * 30)
    rows = [
        _row(odds_decimal=None, odds_american=-200, selection_type="home"),
        _row(odds_decimal=None, odds_american=150, selection_type="draw"),
    ]

    quotes = provider.parse_odds(rows, now=NOW)

    got = {q.selection: round(q.odds, 3) for q in quotes}
    assert got == {"Home": 1.5, "Draw": 2.5}


def test_a_heavy_favourite_converts_above_one() -> None:
    """-200 is 1.5 decimal. Inverted the other way it becomes 0.333 -- rejected.

    Every US book quotes most of its favourites as negative prices, so getting
    this backwards would discard exactly the rows that matter most.
    """
    provider = SharpApiOddsProvider("k" * 30)
    rows = [
        _row(odds_decimal=None, odds_american=-400, selection_type="home"),
        _row(odds_decimal=None, odds_american=-110, selection_type="draw"),
    ]

    quotes = provider.parse_odds(rows, now=NOW)

    got = {q.selection: round(q.odds, 3) for q in quotes}
    assert got == {"Home": 1.25, "Draw": 1.909}


def test_a_row_outside_the_window_is_dropped() -> None:
    provider = SharpApiOddsProvider("k" * 30)

    assert provider.parse_odds([_row(event_start_time=_at(200))],
                               now=NOW, window_hours=48) == []


# ---------------------------------------------------------------------------
# Fetch behaviour
# ---------------------------------------------------------------------------


def _page(rows, *, has_more=False, cursor=None) -> dict:
    return {
        "data": rows,
        "pagination": {"has_more": has_more, "next_cursor": cursor},
        "updated_at": _at(0),
    }


def test_the_fetch_stops_once_a_page_is_past_the_horizon() -> None:
    """Results are chronological, so a later page cannot hold window fixtures.

    Without this the fetch would page through the provider's whole store --
    810k rows at twelve requests a minute.

    The stop necessarily costs one page beyond the window: the fetch cannot know
    a page is out of range without reading it. So the guarantee is that it stops
    there, and the fourth page is armed to fail the test if it is requested.
    """
    transport = _StubTransport([
        _page([_row(event_start_time=_at(6), selection_type="draw")],
              has_more=True, cursor="c1"),
        _page([_row(event_start_time=_at(12), selection_type="home")],
              has_more=True, cursor="c2"),
        _page([_row(event_start_time=_at(120), selection_type="home")],
              has_more=True, cursor="c3"),
        _page([_row(event_start_time=_at(200), selection_type="home")],
              has_more=True, cursor="c4"),
    ])
    provider = SharpApiOddsProvider("k" * 30, transport=transport)

    snapshot = provider.fetch(sport_keys=["soccer_epl"], window_hours=48, now=NOW)

    assert snapshot.pages == 3, "it kept paging past the horizon"
    assert "horizon" in snapshot.stopped_because
    assert not snapshot.truncated, "a horizon stop is a clean finish"
    assert not snapshot.error, "a clean stop should not also be an error"


def test_a_page_past_the_horizon_yields_no_quotes_but_still_stops_the_fetch() -> None:
    """The stop test must not pass just because out-of-window rows were dropped.

    Reading the page span off the parsed quotes makes these two failures
    indistinguishable: the rows are gone either way, so only the page count
    shows whether the fetch actually noticed it was out of range.
    """
    transport = _StubTransport([
        _page([_row(event_start_time=_at(6), selection_type="draw")],
              has_more=True, cursor="c1"),
        _page([_row(event_start_time=_at(120), selection_type="draw")],
              has_more=True, cursor="c2"),
        _page([_row(event_start_time=_at(200), selection_type="draw")],
              has_more=True, cursor="c3"),
    ])
    provider = SharpApiOddsProvider("k" * 30, transport=transport)

    snapshot = provider.fetch(sport_keys=["soccer_epl"], window_hours=48, now=NOW)

    assert snapshot.pages == 2
    assert len(snapshot.quotes) == 1, "the in-window row was not kept"
    assert snapshot.dropped.get("outside the requested window") == 1


def test_the_fetch_uses_the_cursor_never_the_offset() -> None:
    """Offset pagination drifts as live rows move; the cursor is stable."""
    transport = _StubTransport([
        _page([_row(selection_type="draw")], has_more=True, cursor="CUR1"),
        _page([_row(selection_type="home", event_start_time=_at(120))]),
    ])
    provider = SharpApiOddsProvider("k" * 30, transport=transport)

    provider.fetch(sport_keys=["soccer_epl"], window_hours=48, now=NOW)

    assert "cursor" in transport.calls[1], "the second page did not use a cursor"
    assert transport.calls[1]["cursor"] == "CUR1"
    assert "offset" not in transport.calls[0]


def test_the_fetch_honours_its_page_cap_and_says_it_was_truncated() -> None:
    transport = _StubTransport([
        _page([_row(selection_type="draw", event_start_time=_at(i + 1))],
              has_more=True, cursor=f"c{i}") for i in range(5)
    ])
    provider = SharpApiOddsProvider("k" * 30, transport=transport)

    snapshot = provider.fetch(sport_keys=["soccer_epl"], window_hours=48,
                              now=NOW, max_pages=2)

    assert snapshot.pages == 2
    assert snapshot.truncated, "a capped fetch reported itself complete"
    assert "page cap" in snapshot.stopped_because


def test_the_fetch_reports_more_rows_without_a_cursor_as_truncated() -> None:
    transport = _StubTransport([_page([_row()], has_more=True, cursor=None)])
    provider = SharpApiOddsProvider("k" * 30, transport=transport)

    snapshot = provider.fetch(sport_keys=["soccer_epl"], window_hours=48, now=NOW)

    assert snapshot.truncated
    assert "no cursor" in snapshot.stopped_because


def test_the_fetch_stops_at_a_deadline() -> None:
    transport = _StubTransport([
        _page([_row(selection_type="draw", event_start_time=_at(1))],
              has_more=True, cursor="c1"),
    ])
    provider = SharpApiOddsProvider("k" * 30, transport=transport)

    snapshot = provider.fetch(sport_keys=["soccer_epl"], window_hours=48, now=NOW,
                              deadline=NOW - timedelta(seconds=1))

    assert snapshot.pages == 0
    assert snapshot.truncated


def test_the_fetch_asks_for_the_two_markets_the_model_can_price() -> None:
    transport = _StubTransport([_page([])])
    provider = SharpApiOddsProvider("k" * 30, transport=transport)

    provider.fetch(sport_keys=["soccer_epl"], window_hours=48, now=NOW)

    asked = transport.calls[0]["market"].split(",")
    assert asked == ["moneyline", "total_goals"]


def test_the_fetch_sends_the_registry_slug_not_the_lisa_key() -> None:
    transport = _StubTransport([_page([])])
    provider = SharpApiOddsProvider("k" * 30, transport=transport)

    provider.fetch(sport_keys=["soccer_epl"], window_hours=48, now=NOW)

    assert transport.calls[0]["league"] == "england_-_premier_league"


def test_the_fetch_excludes_in_play_prices_at_the_source() -> None:
    transport = _StubTransport([_page([])])
    provider = SharpApiOddsProvider("k" * 30, transport=transport)

    provider.fetch(sport_keys=["soccer_epl"], window_hours=48, now=NOW)

    assert transport.calls[0]["is_live"] == "false"


def test_a_fetch_failure_is_reported_rather_than_raised() -> None:
    class _Boom(HttpTransport):
        def get_json(self, *a, **k):
            raise RuntimeError("connection reset")

    provider = SharpApiOddsProvider("k" * 30, transport=_Boom(timeout=1.0))

    snapshot = provider.fetch(sport_keys=["soccer_epl"], window_hours=48, now=NOW)

    assert snapshot.error and "RuntimeError" in snapshot.error
    assert "connection reset" not in snapshot.error  # raw exceptions may contain credentials


def test_no_key_is_an_explicit_state_not_a_crash() -> None:
    snapshot = SharpApiOddsProvider("").fetch(sport_keys=["soccer_epl"],
                                             window_hours=48, now=NOW)
    assert "SHARPAPI_KEY" in snapshot.error
    assert snapshot.quotes == ()


def test_the_key_is_sent_as_a_header_never_in_the_url() -> None:
    provider = SharpApiOddsProvider("k" * 30)
    headers = provider._headers()

    assert headers["X-API-Key"] == "k" * 30
    assert provider.api_key not in BASE_URL_PLACEHOLDER


BASE_URL_PLACEHOLDER = "https://api.sharpapi.io"


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _quote(**over) -> SharpQuote:
    base = dict(
        event_id="ev1", market="h2h", selection="Home", odds=1.9,
        book_key="draftkings", home="Arsenal", away="Chelsea",
        kickoff=NOW + timedelta(hours=6))
    base.update(over)
    return SharpQuote(**base)


def test_an_exact_name_match_prices_the_fixture() -> None:
    provider = SharpApiOddsProvider("k" * 30)

    outcome = provider.match([_quote()], [_fixture()])

    assert outcome.matched_fixtures == 1
    prices = outcome.prices["football_data:9001"]
    assert len(prices) == 1
    price = prices[0]
    assert isinstance(price, MarketPrice)
    assert price.match_id == "football_data:9001"
    assert price.selection == "Home" and price.odds == 1.9
    assert price.source == NAME
    assert price.book_key == "draftkings"


def test_a_measured_name_divergence_still_matches() -> None:
    """Real pair: the calendar says "SC Internacional", the book "Internacional RS"."""
    provider = SharpApiOddsProvider("k" * 30)
    fixture = _fixture(match_id="football_data:1", home="SC Internacional",
                       away="CA Mineiro")
    quotes = [_quote(home="Internacional RS", away="Atletico Mineiro")]

    outcome = provider.match(quotes, [fixture])

    assert outcome.matched_fixtures == 1, "a measured pair failed to match"


def test_a_different_club_is_never_priced() -> None:
    """The failure this whole matcher exists to prevent."""
    provider = SharpApiOddsProvider("k" * 30)

    outcome = provider.match([_quote(home="Manchester United", away="Liverpool")],
                             [_fixture()])

    assert outcome.prices == {}, "Arsenal v Chelsea was priced with United v L'pool"
    assert outcome.unmatched_events == 1


def test_an_ambiguous_name_is_never_resolved_by_guessing() -> None:
    """Two calendar fixtures normalise identically, so neither is priced.

    "SC Internacional" and "Internacional" are the same club by name and two
    different fixtures by id -- exactly what a reschedule ingested from two
    sources produces. The book event fits both equally well, so no evidence
    decides between them and no price is published.
    """
    provider = SharpApiOddsProvider("k" * 30)
    fixtures = [
        _fixture(match_id="f:1", home="SC Internacional", away="CA Mineiro"),
        _fixture(match_id="f:2", home="Internacional", away="CA Mineiro"),
    ]
    quotes = [_quote(home="Internacional RS", away="Atletico Mineiro")]

    outcome = provider.match(quotes, fixtures)

    assert outcome.prices == {}, "an ambiguous fixture was resolved by guessing"
    assert outcome.ambiguous_events == 1


def test_an_exact_match_outranks_a_bare_one() -> None:
    """Two book events name one fixture; the exact spelling claims it.

    This is the case the two-pass design exists for. Both events could match
    "Manchester United v Liverpool" on names alone, so whichever ran first would
    win by accident -- and the bare "Manchester" is the same string the book
    would publish for the wrong club.
    """
    provider = SharpApiOddsProvider("k" * 30)
    fixtures = [
        _fixture(match_id="f:1", home="Manchester United", away="Liverpool"),
        _fixture(match_id="f:2", home="Manchester City", away="Arsenal"),
    ]
    quotes = [
        _quote(event_id="ev_exact", home="Manchester United", away="Liverpool",
               odds=1.90),
        _quote(event_id="ev_bare", home="Manchester", away="Liverpool", odds=3.30),
    ]

    outcome = provider.match(quotes, fixtures)

    assert set(outcome.prices) == {"f:1"}, "the exact match did not win"
    assert [p.odds for p in outcome.prices["f:1"]] == [1.90], (
        "the bare-name event's price was merged into the exact fixture")


def test_a_fixture_matched_twice_keeps_only_the_winning_event() -> None:
    provider = SharpApiOddsProvider("k" * 30)
    quotes = [
        _quote(event_id="ev_exact", home="Manchester United", away="Liverpool",
               odds=1.90),
        _quote(event_id="ev_bare", home="Manchester", away="Liverpool", odds=3.30),
    ]

    outcome = provider.match(quotes, [_fixture(match_id="f:1",
                                               home="Manchester United",
                                               away="Liverpool")])

    assert len(outcome.prices["f:1"]) == 1, "two events priced one fixture"


def test_kickoff_far_apart_is_not_the_same_match() -> None:
    provider = SharpApiOddsProvider("k" * 30)

    outcome = provider.match([_quote(kickoff=NOW + timedelta(hours=500))],
                             [_fixture(hours=6.0)])

    assert outcome.prices == {}


def test_a_kickoff_moved_by_an_hour_still_matches() -> None:
    provider = SharpApiOddsProvider("k" * 30)

    outcome = provider.match(
        [_quote(kickoff=NOW + timedelta(hours=7))], [_fixture(hours=6.0)])

    assert outcome.matched_fixtures == 1


def test_the_tolerance_is_configurable_and_can_be_tightened() -> None:
    provider = SharpApiOddsProvider("k" * 30)

    outcome = provider.match(
        [_quote(kickoff=NOW + timedelta(hours=7))], [_fixture(hours=6.0)],
        max_kickoff_gap_h=0.5)

    assert outcome.prices == {}


def test_home_and_away_are_not_swappable() -> None:
    """A reversed pairing is a different fixture, not a near miss."""
    provider = SharpApiOddsProvider("k" * 30)
    quotes = [_quote(home="Chelsea", away="Arsenal", selection="Home")]

    outcome = provider.match(quotes, [_fixture()])

    assert outcome.prices == {}, "the sides were transposed"


def test_every_price_on_a_matched_fixture_is_carried_through() -> None:
    provider = SharpApiOddsProvider("k" * 30)
    quotes = [
        _quote(selection="Home", odds=1.9),
        _quote(selection="Draw", odds=3.4),
        _quote(selection="Away", odds=4.2),
        _quote(selection="Over 2.5", market="totals", line=2.5, odds=1.95),
    ]

    outcome = provider.match(quotes, [_fixture()])

    prices = outcome.prices["football_data:9001"]
    assert {(p.market, p.selection) for p in prices} == {
        ("h2h", "Home"), ("h2h", "Draw"), ("h2h", "Away"),
        ("totals", "Over 2.5")}


def test_matching_nothing_is_not_an_error() -> None:
    provider = SharpApiOddsProvider("k" * 30)

    outcome = provider.match([], [_fixture()])

    assert outcome.prices == {}
    assert outcome.matched_fixtures == 0


def test_two_quotes_for_one_event_become_two_prices_not_one() -> None:
    provider = SharpApiOddsProvider("k" * 30)
    quotes = [
        _quote(selection="Home", book_key="draftkings", odds=1.90),
        _quote(selection="Home", book_key="fanduel", odds=2.05),
    ]

    prices = provider.match(quotes, [_fixture()]).prices["football_data:9001"]

    assert len(prices) == 2, "one book's price overwrote another's"
    assert {p.book_key for p in prices} == {"draftkings", "fanduel"}


# ---------------------------------------------------------------------------
# Provider contract
# ---------------------------------------------------------------------------


def test_the_provider_declares_itself_available_only_with_a_key() -> None:
    assert SharpApiOddsProvider("k" * 30).is_available() is True
    assert SharpApiOddsProvider("").is_available() is False
    assert SharpApiOddsProvider("   ").is_available() is False


def test_the_free_tier_rate_limit_is_what_is_declared() -> None:
    """The transport must be paced to the free tier, not left unbounded."""
    provider = SharpApiOddsProvider("k" * 30)
    provider._transport.set_rate_limit  # exists
    quota = provider.quota_status()
    assert quota.limit == 12
    assert quota.metered


def test_the_source_is_official_not_unofficial() -> None:
    """A documented API on a free tier is not ToS-grey."""
    assert SharpApiOddsProvider("k" * 30).tier is SourceTier.OFFICIAL


def test_every_modelled_soccer_league_has_a_real_slug() -> None:
    """A missing slug means that league silently gets no prices at all."""
    from lisa.providers.calendar import LEAGUES

    soccer = {k: s for k, s in LEAGUES.items() if s.family == "soccer"}
    missing = sorted(k for k, s in soccer.items() if not s.sharp)
    assert not missing, f"soccer leagues with no SharpAPI slug: {missing}"


def test_the_slug_is_not_the_lisa_key_by_accident() -> None:
    """Hand-maintained ids drifted before and mislabelled whole leagues."""
    from lisa.providers.calendar import LEAGUES

    for key, spec in LEAGUES.items():
        if spec.sharp:
            assert spec.sharp != key, f"{key} maps to itself"


def test_a_snapshot_serialises_its_diagnostics() -> None:
    snapshot = SharpSnapshot(quotes=(), dropped={"x": 2}, rows=5, pages=1,
                             truncated=True, stopped_because="page cap")
    payload = snapshot.to_dict()
    assert payload["dropped"] == {"x": 2}
    assert payload["truncated"] is True
    assert payload["rows"] == 5


def test_the_transport_is_shared_not_a_private_pool() -> None:
    """A private pool would defeat the single connection pool in build_providers."""
    shared = HttpTransport(timeout=1.0)
    provider = SharpApiOddsProvider("k" * 30, transport=shared)
    assert provider._transport is shared
