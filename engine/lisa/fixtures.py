"""Bundled, realistic The Odds API payloads.

These mirror the upstream contract exactly (see parsing.py) so the pipeline,
settlement and tests exercise the same code paths as live data. Crafted to
cover every gate outcome:

  NBA
    nba-a  heavy favourite, consensus ~0.825, bet365 lagging  -> PASS + EV
    nba-b  coin flip                                          -> below_threshold
    nba-c  books disagree wildly                              -> high_dispersion
    nba-d  only 2 books                                       -> insufficient_books
    nba-e  confident (~0.90) but no book prices above fair    -> PASS, no execution
    nba-g  postponed (commenced in the past)                  -> PASS then VOID at settle

  Soccer
    lig-a  Real Madrid 1.19-1.30 favourite                    -> PASS + EV
    lig-b  mid-table 1X2                                      -> below_threshold
    bdl-a  Bayern favourite                                   -> PASS
    bdl-b  mid-table 1X2                                      -> below_threshold
The bundled payloads describe a single coherent matchday (2026-09-21) so the
48h product window always contains the whole set. The engine's own tests use a
fixed clock of 2026-09-20T15:00Z, i.e. the evening before that matchday, which
is when a 48h board is actually useful.
"""
from __future__ import annotations

from typing import Any, Optional

_LAST_UPDATE = "2026-09-20T14:00:00Z"

FIXTURE_SPORTS: tuple[str, ...] = (
    "basketball_nba",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga",
)


def _book(key: str, title: str, prices: dict[str, float],
          last_update: str = _LAST_UPDATE,
          market: str = "h2h",
          point: Optional[float] = None,
          points: Optional[dict[str, float]] = None) -> dict:
    outcomes: list[dict[str, Any]] = []
    for name, price in prices.items():
        oc: dict[str, Any] = {"name": name, "price": price}
        if points is not None and name in points:
            oc["point"] = points[name]
        elif point is not None:
            oc["point"] = point
        outcomes.append(oc)
    return {
        "key": key,
        "title": title,
        "last_update": last_update,
        "markets": [{
            "key": market,
            "outcomes": outcomes,
        }],
    }


def _game(match_id: str, sport: str, home: str, away: str,
          commence: str, books: list[dict], completed: bool = False) -> dict:
    return {
        "id": match_id,
        "sport_key": sport,
        "commence_time": commence,
        "home_team": home,
        "away_team": away,
        "completed": completed,
        "bookmakers": books,
    }


NBA_ODDS: list[dict] = [
    _game("nba-a", "basketball_nba", "Celtics", "Knicks",
          "2026-09-21T00:00:00Z", [
              _book("pinnacle", "Pinnacle", {"Celtics": 1.16, "Knicks": 5.20}),
              _book("bet365", "Bet365", {"Celtics": 1.22, "Knicks": 4.60}),
              _book("draftkings", "DraftKings", {"Celtics": 1.18, "Knicks": 4.90}),
              _book("fanduel", "FanDuel", {"Celtics": 1.16, "Knicks": 5.10}),
              _book("betmgm", "BetMGM", {"Celtics": 1.17, "Knicks": 5.00}),
          ]),
    _game("nba-b", "basketball_nba", "Lakers", "Warriors",
          "2026-09-21T02:00:00Z", [
              _book("pinnacle", "Pinnacle", {"Lakers": 1.91, "Warriors": 1.95}),
              _book("bet365", "Bet365", {"Lakers": 1.94, "Warriors": 1.92}),
              _book("draftkings", "DraftKings", {"Lakers": 1.90, "Warriors": 1.96}),
              _book("fanduel", "FanDuel", {"Lakers": 1.93, "Warriors": 1.93}),
              _book("betmgm", "BetMGM", {"Lakers": 1.92, "Warriors": 1.94}),
          ]),
    _game("nba-c", "basketball_nba", "Heat", "Nets",
          "2026-09-21T00:30:00Z", [
              _book("pinnacle", "Pinnacle", {"Heat": 1.72, "Nets": 2.10}),
              _book("bet365", "Bet365", {"Heat": 1.25, "Nets": 4.00}),
              _book("draftkings", "DraftKings", {"Heat": 1.68, "Nets": 2.20}),
              _book("fanduel", "FanDuel", {"Heat": 1.70, "Nets": 2.15}),
              _book("betmgm", "BetMGM", {"Heat": 1.62, "Nets": 2.30}),
          ]),
    _game("nba-d", "basketball_nba", "Bucks", "Pacers",
          "2026-09-21T01:00:00Z", [
              _book("pinnacle", "Pinnacle", {"Bucks": 1.55, "Pacers": 2.45}),
              _book("bet365", "Bet365", {"Bucks": 1.58, "Pacers": 2.40}),
          ]),
    _game("nba-e", "basketball_nba", "Thunder", "Wizards",
          "2026-09-21T03:00:00Z", [
              _book("pinnacle", "Pinnacle", {"Thunder": 1.10, "Wizards": 7.50}),
              _book("bet365", "Bet365", {"Thunder": 1.09, "Wizards": 6.50}),
              _book("draftkings", "DraftKings", {"Thunder": 1.08, "Wizards": 6.80}),
              _book("fanduel", "FanDuel", {"Thunder": 1.095, "Wizards": 7.00}),
              _book("betmgm", "BetMGM", {"Thunder": 1.09, "Wizards": 6.90}),
          ]),
    _game("nba-g", "basketball_nba", "Spurs", "Rockets",
          "2026-09-01T00:00:00Z", [
              _book("pinnacle", "Pinnacle", {"Spurs": 1.18, "Rockets": 4.80}),
              _book("bet365", "Bet365", {"Spurs": 1.19, "Rockets": 4.55}),
              _book("draftkings", "DraftKings", {"Spurs": 1.20, "Rockets": 4.40}),
              _book("fanduel", "FanDuel", {"Spurs": 1.17, "Rockets": 4.90}),
              _book("betmgm", "BetMGM", {"Spurs": 1.18, "Rockets": 4.70}),
          ]),
]

LA_LIGA_ODDS: list[dict] = [
    _game("lig-a", "soccer_spain_la_liga", "Real Madrid", "Elche",
          "2026-09-21T17:00:00Z", [
              _book("pinnacle", "Pinnacle",
                    {"Real Madrid": 1.20, "Draw": 6.50, "Elche": 11.00}),
              _book("bet365", "Bet365",
                    {"Real Madrid": 1.30, "Draw": 6.00, "Elche": 10.00}),
              _book("unibet", "Unibet",
                    {"Real Madrid": 1.21, "Draw": 6.40, "Elche": 11.50}),
              _book("williamhill", "William Hill",
                    {"Real Madrid": 1.19, "Draw": 6.80, "Elche": 12.00}),
              _book("betfair", "Betfair",
                    {"Real Madrid": 1.20, "Draw": 6.60, "Elche": 11.00}),
          ]),
    _game("lig-b", "soccer_spain_la_liga", "Valencia", "Getafe",
          "2026-09-21T19:00:00Z", [
              _book("pinnacle", "Pinnacle",
                    {"Valencia": 2.30, "Draw": 3.30, "Getafe": 3.00}),
              _book("bet365", "Bet365",
                    {"Valencia": 2.25, "Draw": 3.40, "Getafe": 3.10}),
              _book("draftkings", "DraftKings",
                    {"Valencia": 2.35, "Draw": 3.25, "Getafe": 2.95}),
              _book("fanduel", "FanDuel",
                    {"Valencia": 2.28, "Draw": 3.35, "Getafe": 3.05}),
              _book("betmgm", "BetMGM",
                    {"Valencia": 2.32, "Draw": 3.28, "Getafe": 3.02}),
          ]),
]

BUNDESLIGA_ODDS: list[dict] = [
    _game("bdl-a", "soccer_germany_bundesliga", "Bayern Munchen", "Bochum",
          "2026-09-21T15:30:00Z", [
              _book("pinnacle", "Pinnacle",
                    {"Bayern Munchen": 1.22, "Draw": 6.20, "Bochum": 10.50}),
              _book("bet365", "Bet365",
                    {"Bayern Munchen": 1.31, "Draw": 5.80, "Bochum": 9.50}),
              _book("unibet", "Unibet",
                    {"Bayern Munchen": 1.23, "Draw": 6.10, "Bochum": 11.00}),
              _book("williamhill", "William Hill",
                    {"Bayern Munchen": 1.21, "Draw": 6.50, "Bochum": 11.50}),
              _book("betfair", "Betfair",
                    {"Bayern Munchen": 1.22, "Draw": 6.30, "Bochum": 10.80}),
          ]),
    _game("bdl-b", "soccer_germany_bundesliga", "Wolfsburg", "Augsburg",
          "2026-09-21T13:30:00Z", [
              _book("pinnacle", "Pinnacle",
                    {"Wolfsburg": 2.20, "Draw": 3.40, "Augsburg": 3.10}),
              _book("bet365", "Bet365",
                    {"Wolfsburg": 2.15, "Draw": 3.50, "Augsburg": 3.20}),
              _book("draftkings", "DraftKings",
                    {"Wolfsburg": 2.25, "Draw": 3.30, "Augsburg": 3.05}),
              _book("fanduel", "FanDuel",
                    {"Wolfsburg": 2.18, "Draw": 3.45, "Augsburg": 3.15}),
              _book("betmgm", "BetMGM",
                    {"Wolfsburg": 2.22, "Draw": 3.38, "Augsburg": 3.12}),
          ]),
]


def _score(match_id: str, sport: str, commence: str, home: str, away: str,
           home_score: int, away_score: int) -> dict:
    return {
        "id": match_id,
        "sport_key": sport,
        "commence_time": commence,
        "home_team": home,
        "away_team": away,
        "completed": True,
        "score_status": "final",
        "scores": [
            {"name": home, "score": str(home_score)},
            {"name": away, "score": str(away_score)},
        ],
    }


NBA_SCORES: list[dict] = [
    _score("nba-a", "basketball_nba", "2026-09-21T00:00:00Z",
           "Celtics", "Knicks", 110, 102),
    {
        "id": "nba-g",
        "sport_key": "basketball_nba",
        "commence_time": "2026-09-01T00:00:00Z",
        "home_team": "Spurs",
        "away_team": "Rockets",
        "completed": False,
        "score_status": "postponed",
        "scores": [],
    },
]

LA_LIGA_SCORES: list[dict] = [
    _score("lig-a", "soccer_spain_la_liga", "2026-09-21T17:00:00Z",
           "Real Madrid", "Elche", 2, 1),
]

NBA_TOTALS_ODDS: list[dict] = [
    _game("nba-tot-a", "basketball_nba", "Celtics", "Knicks",
          "2026-09-21T00:00:00Z", [
              # Line 220.5: 5 books (densest)
              _book("pinnacle", "Pinnacle", {"Over": 1.18, "Under": 5.00},
                    market="totals", point=220.5),
              _book("bet365", "Bet365", {"Over": 1.22, "Under": 4.50},
                    market="totals", point=220.5),
              _book("draftkings", "DraftKings", {"Over": 1.19, "Under": 4.80},
                    market="totals", point=220.5),
              _book("fanduel", "FanDuel", {"Over": 1.17, "Under": 5.10},
                    market="totals", point=220.5),
              _book("betmgm", "BetMGM", {"Over": 1.18, "Under": 4.90},
                    market="totals", point=220.5),
              # Line 221.5: 2 books (sparse)
              _book("unibet", "Unibet", {"Over": 1.30, "Under": 3.60},
                    market="totals", point=221.5),
              _book("williamhill", "William Hill", {"Over": 1.28, "Under": 3.70},
                    market="totals", point=221.5),
          ]),
]

LA_LIGA_TOTALS_ODDS: list[dict] = [
    _game("lig-tot-a", "soccer_spain_la_liga", "Real Madrid", "Elche",
          "2026-09-21T17:00:00Z", [
              # Line 2.5: 5 books (densest)
              _book("pinnacle", "Pinnacle", {"Over": 1.22, "Under": 4.50},
                    market="totals", point=2.5),
              _book("bet365", "Bet365", {"Over": 1.25, "Under": 4.20},
                    market="totals", point=2.5),
              _book("unibet", "Unibet", {"Over": 1.23, "Under": 4.40},
                    market="totals", point=2.5),
              _book("williamhill", "William Hill", {"Over": 1.21, "Under": 4.60},
                    market="totals", point=2.5),
              _book("betfair", "Betfair", {"Over": 1.22, "Under": 4.50},
                    market="totals", point=2.5),
              # Line 3.0: 3 books (sparse)
              _book("draftkings", "DraftKings", {"Over": 1.65, "Under": 2.30},
                    market="totals", point=3.0),
              _book("fanduel", "FanDuel", {"Over": 1.62, "Under": 2.35},
                    market="totals", point=3.0),
              _book("betmgm", "BetMGM", {"Over": 1.64, "Under": 2.32},
                    market="totals", point=3.0),
          ]),
]

NBA_SPREADS_ODDS: list[dict] = [
    _game("nba-spr-a", "basketball_nba", "Celtics", "Knicks",
          "2026-09-21T00:00:00Z", [
              # Line -4.5 on Celtics (+4.5 on Knicks): 5 books
              _book("pinnacle", "Pinnacle", {"Celtics": 1.18, "Knicks": 5.00},
                    market="spreads", points={"Celtics": -4.5, "Knicks": 4.5}),
              _book("bet365", "Bet365", {"Celtics": 1.22, "Knicks": 4.50},
                    market="spreads", points={"Celtics": -4.5, "Knicks": 4.5}),
              _book("draftkings", "DraftKings", {"Celtics": 1.19, "Knicks": 4.80},
                    market="spreads", points={"Celtics": -4.5, "Knicks": 4.5}),
              _book("fanduel", "FanDuel", {"Celtics": 1.17, "Knicks": 5.10},
                    market="spreads", points={"Celtics": -4.5, "Knicks": 4.5}),
              _book("betmgm", "BetMGM", {"Celtics": 1.18, "Knicks": 4.90},
                    market="spreads", points={"Celtics": -4.5, "Knicks": 4.5}),
          ]),
]

BUNDESLIGA_SCORES: list[dict] = []


ODDS_PAYLOADS: dict[str, list[dict]] = {
    "basketball_nba": NBA_ODDS,
    "soccer_spain_la_liga": LA_LIGA_ODDS,
    "soccer_germany_bundesliga": BUNDESLIGA_ODDS,
}

SCORES_PAYLOADS: dict[str, list[dict]] = {
    "basketball_nba": NBA_SCORES,
    "soccer_spain_la_liga": LA_LIGA_SCORES,
    "soccer_germany_bundesliga": BUNDESLIGA_SCORES,
}