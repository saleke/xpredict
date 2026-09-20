"""Parsing: strict sanitisation of odds payloads, h2h default, line harvest."""
from __future__ import annotations

from datetime import datetime, timezone

from lisa.odds import Book, Match
from lisa.parsing import parse_odds_payload

GAME = {
    "id": "m1",
    "sport_key": "soccer_spain_la_liga",
    "home_team": "Real Madrid",
    "away_team": "Barcelona",
    "commence_time": "2026-09-21T20:00:00Z",
    "bookmakers": [
        {
            "key": "pinnacle",
            "title": "Pinnacle",
            "last_update": "2026-09-20T14:00:00Z",
            "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Real Madrid", "price": 1.8},
                    {"name": "Barcelona", "price": 2.1}]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": 2.5, "price": 1.9},
                    {"name": "Under", "point": 2.5, "price": 1.95}]},
            ],
        },
        {
            "key": "bet365",
            "title": "bet365",
            "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "Real Madrid", "price": 1.78},
                    {"name": "Barcelona", "price": 2.15}]},
            ],
        },
    ],
}


def test_h2h_parse_filters_metadata_and_prices():
    matches = parse_odds_payload([GAME])
    assert len(matches) == 1
    match: Match = matches[0]
    assert match.id == "m1"
    assert match.sport_key == "soccer_spain_la_liga"
    assert match.home_team == "Real Madrid"
    assert match.away_team == "Barcelona"
    assert match.market == "h2h"
    assert match.commence_time == datetime(2026, 9, 21, 20, 0, 0, tzinfo=timezone.utc)
    assert len(match.bookmakers) == 2
    assert match.bookmakers[0].outcomes == {"Real Madrid": 1.8, "Barcelona": 2.1}
    assert match.bookmakers[0].line is None


def test_totals_parse_harvests_line():
    matches = parse_odds_payload([GAME], market_keys=("totals",))
    assert len(matches) == 1
    match: Match = matches[0]
    assert match.market == "totals"
    assert match.bookmakers[0].outcomes == {"Over": 1.9, "Under": 1.95}
    assert match.bookmakers[0].line == 2.5


def test_de_mux_emits_one_match_per_market():
    matches = parse_odds_payload([GAME], market_keys=("h2h", "totals"))
    assert len(matches) == 2
    by_market = {m.market: m for m in matches}
    assert set(by_market) == {"h2h", "totals"}
    assert len(by_market["h2h"].bookmakers) == 2
    assert len(by_market["totals"].bookmakers) == 1
    assert by_market["totals"].bookmakers[0].key == "pinnacle"
    assert by_market["h2h"].id == by_market["totals"].id == "m1"


def test_unquoted_market_emits_no_match():
    assert parse_odds_payload([GAME], market_keys=("spreads",)) == []


def test_malformed_game_is_skipped():
    payload = [{"home_team": "no id"}, GAME]
    matches = parse_odds_payload(payload)
    assert len(matches) == 1
    assert matches[0].id == "m1"


def test_non_numeric_price_is_dropped():
    game = {
        "id": "m2",
        "sport_key": "soccer_epl",
        "home_team": "A",
        "away_team": "B",
        "commence_time": "2026-09-22T20:00:00Z",
        "bookmakers": [{
            "key": "pinnacle",
            "title": "Pinnacle",
            "markets": [{"key": "h2h", "outcomes": [
                {"name": "A", "price": "fuzzy"},
                {"name": "B", "price": 2.0}]}],
        }],
    }
    matches = parse_odds_payload([game])
    book: Book = matches[0].bookmakers[0]
    assert book.outcomes == {"B": 2.0}


def test_missing_commence_time_is_skipped():
    game = {k: v for k, v in GAME.items() if k != "commence_time"}
    assert parse_odds_payload([game]) == []


def test_spreads_parse_harvests_home_line():
    game = {
        "id": "m-spr",
        "sport_key": "basketball_nba",
        "home_team": "Celtics",
        "away_team": "Knicks",
        "commence_time": "2026-09-22T20:00:00Z",
        "bookmakers": [{
            "key": "pinnacle",
            "title": "Pinnacle",
            "markets": [{"key": "spreads", "outcomes": [
                {"name": "Celtics", "price": 1.91, "point": -4.5},
                {"name": "Knicks", "price": 1.91, "point": 4.5}]}],
        }],
    }
    matches = parse_odds_payload([game], market_keys=("spreads",))
    assert len(matches) == 1
    m = matches[0]
    assert m.market == "spreads"
    assert m.bookmakers[0].line == -4.5
    assert m.bookmakers[0].outcomes == {"Celtics": 1.91, "Knicks": 1.91}