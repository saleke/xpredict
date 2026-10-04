"""API-Sports v3: regulation-time fixtures, statistics and prematch offers.

Endpoint contracts: https://www.api-football.com/documentation-v3
Only exact supported bet names are accepted. Half/extra-time, qualification,
player, live and unknown markets are never mapped to a full-match model.
"""
import hashlib
import json
import math
import re
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from .base import HttpTransport, FixturesResult, SourceTier, ProviderNotAvailableError, ParseError
from .calendar import normalise_fixture, normalise_team
from .sharpapi import SharpQuote, SharpSnapshot, MatchOutcome
from ..board import MarketPrice
from .diagnostics import response_error

NAME = 'api_football'
BASE_URL = 'https://v3.football.api-sports.io'
# Resolve official IDs and current seasons from the catalog, not season-year guesses.
COMPETITIONS = {
 'soccer_epl': ('England', 'Premier League'),
 'soccer_england_championship': ('England', 'Championship'),
 'soccer_germany_bundesliga': ('Germany', 'Bundesliga'),
 'soccer_italy_serie_a': ('Italy', 'Serie A'),
 'soccer_spain_la_liga': ('Spain', 'La Liga'),
 'soccer_france_ligue_one': ('France', 'Ligue 1'),
 'soccer_netherlands_eredivisie': ('Netherlands', 'Eredivisie'),
 'soccer_portugal_primeira_liga': ('Portugal', 'Primeira Liga'),
 'soccer_uefa_champions_league': ('World', 'UEFA Champions League'),
}

BUDGET_DDL = '''CREATE TABLE IF NOT EXISTS provider_daily_usage (
 provider TEXT NOT NULL, day TEXT NOT NULL, requests INTEGER NOT NULL,
 PRIMARY KEY(provider, day));'''


class ApiSnapshot(SharpSnapshot):
    def to_dict(self):
        return dict(super().to_dict(), source=NAME)


class ApiFootballProvider:
    name, tier = NAME, SourceTier.OFFICIAL

    def __init__(self, key='', *, transport=None, daily_limit=100, bookmakers=()):
        self.key = key.strip()
        # No hidden retries: every attempted HTTP request consumes one quota reservation.
        self.transport = transport or HttpTransport(max_retries=0, timeout=10.)
        self.transport.set_rate_limit('v3.football.api-sports.io', .5, 1)
        self.daily_limit = daily_limit
        if type(daily_limit) is not int or daily_limit < 1:
            raise ValueError('API-Football daily limit must be a positive integer')
        self.purpose = 'ordinary'
        self.calendar_ttl = 900
        self.settlement_ttl = 900
        self.odds_ttl = 900
        self.odds_max_pages = None
        self.coverage_rotation = 0
        self.bookmakers = set(map(str, bookmakers))
        self.storage = None
        self._local_cache = {}
        self._local_day, self._local_requests = '', 0
        self._lock = __import__('threading').RLock()

    def is_available(self):
        return bool(self.key)

    def leagues(self):
        return list(COMPETITIONS)

    def _get(self, path, params, *, ttl=900, purpose='ordinary'):
        if not self.key:
            raise ProviderNotAvailableError('API_FOOTBALL_KEY is not configured', provider=NAME)
        cache_purpose = purpose if path == '/fixtures' else 'shared'
        identity = hashlib.sha256(json.dumps([path, params, cache_purpose, hashlib.sha256(self.key.encode()).hexdigest()],
                                             sort_keys=True).encode()).hexdigest()
        cache_key = 'api_football:' + identity
        with self._lock:
            if self.storage:
                cached = self.storage.get_live(cache_key)
                if cached is not None:
                    return cached
            elif identity in self._local_cache:
                expiry, payload = self._local_cache[identity]
                if expiry > time.time():
                    return payload
            day = datetime.now(timezone.utc).date().isoformat()
            # Reserve 20% of the configured daily budget for final results.
            ceiling = self.daily_limit if purpose == 'settlement' else max(1, int(self.daily_limit * .8))
            if self.storage:
                with self.storage._tx() as conn:
                    if purpose == 'history':
                        history_name = NAME + ':history'
                        history_ceiling = max(1, int(self.daily_limit * .8) // 10)
                        conn.execute('INSERT INTO provider_daily_usage VALUES (?, ?, 0) ON CONFLICT(provider, day) DO NOTHING',
                                     (history_name, day))
                        if not conn.execute('UPDATE provider_daily_usage SET requests=requests+1 '
                            'WHERE provider=? AND day=? AND requests<?',
                            (history_name, day, history_ceiling)).rowcount:
                            raise ProviderNotAvailableError('API-Football history allowance exhausted', provider=NAME)
                    conn.execute('INSERT INTO provider_daily_usage VALUES (?, ?, 0) ON CONFLICT(provider, day) DO NOTHING', (NAME, day))
                    if not conn.execute('UPDATE provider_daily_usage SET requests=requests+1 '
                        'WHERE provider=? AND day=? AND requests<?', (NAME, day, ceiling)).rowcount:
                        raise ProviderNotAvailableError('API-Football daily request budget exhausted', provider=NAME)
            else:
                if self._local_day != day:
                    self._local_day, self._local_requests = day, 0
                if self._local_requests >= ceiling:
                    raise ProviderNotAvailableError('API-Football daily request budget exhausted', provider=NAME)
                self._local_requests += 1
            payload = self.transport.get_json(BASE_URL + path, params=params,
                headers={'x-apisports-key': self.key}, ttl=0, provider=NAME)
            if not isinstance(payload, dict) or payload.get('errors') or not isinstance(payload.get('response'), list):
                raise response_error('API-Football returned an error or malformed envelope', provider=NAME,
                    errors=payload.get('errors') if isinstance(payload, dict) else None,
                    fallback='provider_error' if isinstance(payload, dict) and payload.get('errors') else 'malformed_envelope')
            if self.storage:
                self.storage.upsert_live(cache_key, payload, ttl)
            else:
                self._local_cache[identity] = (time.time() + ttl, payload)
            return payload

    def competition(self, sport_key, *, purpose='ordinary'):
        if sport_key not in COMPETITIONS:
            return None
        country, name = COMPETITIONS[sport_key]
        payload = self._get('/leagues', {'country': country, 'name': name}, ttl=86400, purpose=purpose)
        matches = [r for r in payload['response'] if r.get('league', {}).get('name') == name
                   and r.get('country', {}).get('name') == country]
        if len(matches) != 1:
            raise ParseError(f'Cannot uniquely resolve API-Football competition {sport_key}', provider=NAME)
        seasons = [s for s in matches[0].get('seasons', []) if s.get('current')]
        if len(seasons) != 1:
            raise ParseError('Current competition season is missing or ambiguous', provider=NAME)
        return matches[0]['league']['id'], seasons[0]['year']

    def get_season(self, sport_key, season=None, *, purpose='ordinary'):
        ref = self.competition(sport_key, purpose=purpose)
        if ref is None:
            return FixturesResult(NAME, sport_key, ())
        league_id, current = ref
        season = current if season is None else season
        payload = self._get('/fixtures', {'league': league_id, 'season': season},
                            ttl=(self.settlement_ttl if purpose == 'settlement' else
                                 86400 if purpose == 'history' else self.calendar_ttl) if season == current else 86400, purpose=purpose)
        rows = [self.to_fixture(r, sport_key) for r in payload['response']]
        return FixturesResult(NAME, sport_key, tuple(r for r in rows if r is not None))

    def get_fixtures(self, sport_key):
        return self.get_season(sport_key, purpose=self.purpose)

    @staticmethod
    def to_fixture(row, sport_key):
        try:
            fixture, teams = row['fixture'], row['teams']
            status = fixture['status']['short']
            # score.fulltime is regulation time; goals includes extra-time goals.
            score = row.get('score', {}).get('fulltime', {})
            home, away = score.get('home'), score.get('away')
            state = 'FINISHED' if status in ('FT', 'AET', 'PEN') else 'CANCELED' if status == 'CANC' else 'POSTPONED' if status == 'PST' else 'SCHEDULED' if status in ('NS', 'TBD') else 'IN_PLAY'
            if state == 'FINISHED' and any(type(v) is not int or v < 0 for v in (home, away)):
                return None
            result = normalise_fixture(provider=NAME, sport_key=sport_key,
                match_id=str(fixture['id']), kickoff_epoch=fixture['timestamp'],
                home=teams['home']['name'], away=teams['away']['name'],
                home_score=home, away_score=away, status=state, season=str(row['league']['season']))
            result.update(provider_fixture_id=fixture['id'], home_team_id=teams['home']['id'],
                          away_team_id=teams['away']['id'], ended_after_extra_time=status in ('AET', 'PEN'))
            half = row.get('score', {}).get('halftime') or {}
            if all(type(half.get(s)) is int and half[s] >= 0 for s in ('home', 'away')):
                if not result['completed'] or (half['home'] <= home and half['away'] <= away):
                    result.update(home_first_half_score=half['home'], away_first_half_score=half['away'])
            result['source_referee'] = fixture.get('referee')
            return result
        except (ValueError, TypeError, KeyError):
            return None

    def with_corners(self, result, *, purpose='ordinary'):
        if not result.get('completed') or result.get('ended_after_extra_time'):
            return result  # full-match aggregate corners may include extra time
        payload = self._get('/fixtures/statistics', {'fixture': result['provider_fixture_id']}, ttl=86400,
                            purpose=purpose)
        stats = {r.get('team', {}).get('id'): r.get('statistics', []) for r in payload['response']}
        copy = dict(result)
        copy['source_statistics'] = payload['response']
        copy['statistics_observed_at'] = datetime.now(timezone.utc).isoformat()
        for side in ('home', 'away'):
            values = [s.get('value') for s in stats.get(result[side + '_team_id'], []) if s.get('type') == 'Corner Kicks']
            # null means missing coverage, never zero corners.
            if len(values) == 1 and type(values[0]) is int and 0 <= values[0] <= 60:
                copy[side + '_corners'] = values[0]
        return copy

    def with_player_statistics(self, result, *, purpose='ordinary'):
        """Opt-in collection; does not spend free quota during normal scans."""
        if not result.get('completed'):
            return result
        payload = self._get('/fixtures/players', {'fixture': result['provider_fixture_id']},
                            ttl=86400, purpose=purpose)
        return dict(result, source_player_statistics=payload['response'],
                    player_statistics_observed_at=datetime.now(timezone.utc).isoformat())

    @staticmethod
    def parse_bet(name, value):
        simple = {'Match Winner': 'h2h', 'Double Chance': 'double_chance',
                  'Both Teams Score': 'btts', 'Both Teams To Score': 'btts',
                  'Draw No Bet': 'draw_no_bet', 'Exact Score': 'correct_score', 'Correct Score': 'correct_score'}
        if name in simple:
            market = simple[name]
            options = {'h2h': {'Home': 'Home', 'Draw': 'Draw', 'Away': 'Away'},
                       'double_chance': {'Home/Draw': '1X', 'Draw/Away': 'X2', 'Home/Away': '12'},
                       'btts': {'Yes': 'Yes', 'No': 'No'}, 'draw_no_bet': {'Home': 'Home', 'Away': 'Away'}}
            if market == 'correct_score':
                return (market, value.replace(':', '-'), None) if re.fullmatch(r'\d+[:-]\d+', value) else None
            return (market, options[market][value], None) if value in options[market] else None
        totals = {'Goals Over/Under': 'totals', 'Asian Total': 'totals',
                  'Total - Home': 'home_team_totals', 'Total - Away': 'away_team_totals',
                  'Corners Over Under': 'corners'}
        if name in totals:
            parsed = re.fullmatch(r'(Over|Under) (\d+(?:\.\d+)?)', value)
            if parsed:
                line = float(parsed[2])
                # Corners publish binary half-lines until corner split distributions exist.
                if line * 4 == round(line * 4) and (totals[name] != 'corners' or line % 1 == .5):
                    return totals[name], parsed[1], line
        if name == 'Asian Handicap':
            parsed = re.fullmatch(r'(Home|Away) ([+-]?\d+(?:\.\d+)?)', value)
            if parsed and float(parsed[2]) * 4 == round(float(parsed[2]) * 4):
                return 'asian_handicap', parsed[1], float(parsed[2])
        return None

    def fetch(self, *, sport_keys, window_hours, now, max_pages=6, deadline=None):
        snapshot = ApiSnapshot()
        quotes = []
        sport_keys = list(sport_keys)
        if sport_keys:
            offset = self.coverage_rotation % len(sport_keys)
            sport_keys = sport_keys[offset:] + sport_keys[:offset]
        if self.odds_max_pages is not None:
            max_pages = min(max_pages, self.odds_max_pages)
        for sport in sport_keys:
            ref = self.competition(sport)
            if not ref:
                continue
            league_id, season = ref
            for page in range(1, max_pages + 1):
                if deadline and datetime.now(timezone.utc) >= deadline:
                    snapshot.truncated, snapshot.stopped_because = True, 'time budget'
                    break
                payload = self._get('/odds', {'league': league_id, 'season': season, 'page': page}, ttl=self.odds_ttl)
                snapshot.pages += 1
                for row in payload['response']:
                    snapshot.rows += 1
                    fixture = row.get('fixture', {})
                    try:
                        kick = datetime.fromisoformat(fixture['date'].replace('Z', '+00:00'))
                        updated = datetime.fromisoformat(row['update'].replace('Z', '+00:00'))
                    except (ValueError, KeyError, TypeError):
                        snapshot.count('missing_time'); continue
                    if not now <= kick <= now + timedelta(hours=window_hours):
                        continue
                    # Odds rows contain fixture IDs, not team names; match() uses
                    # the separately fetched calendar as its authoritative identity.
                    for book in row.get('bookmakers', []):
                        if self.bookmakers and str(book.get('id')) not in self.bookmakers and book.get('name') not in self.bookmakers:
                            continue
                        for bet in book.get('bets', []):
                            for value in bet.get('values', []):
                                parsed = self.parse_bet(bet.get('name'), str(value.get('value', '')))
                                if not parsed:
                                    snapshot.count('unsupported_contract'); continue
                                try:
                                    odds = float(value['odd'])
                                    if not math.isfinite(odds) or odds <= 1:
                                        raise ValueError()
                                except (ValueError, TypeError, KeyError):
                                    snapshot.count('invalid_odds'); continue
                                quotes.append(SharpQuote(f'{NAME}:{fixture["id"]}', parsed[0], parsed[1], odds,
                                    str(book['id']), book.get('name', ''), NAME, parsed[2], kick, updated_at=updated))
                if page == max_pages and page < payload.get('paging', {}).get('total', 1):
                    snapshot.truncated, snapshot.stopped_because = True, 'coverage page budget'
                if page >= payload.get('paging', {}).get('total', 1):
                    break
                if page == max_pages:
                    snapshot.truncated, snapshot.stopped_because = True, 'page budget'
        snapshot.quotes = tuple(quotes)
        return snapshot

    def match(self, quotes, fixtures, *, max_kickoff_gap_h=6):
        calendars = {sport: self.get_fixtures(sport).fixtures for sport in {f.sport_key for f in fixtures}}
        events = defaultdict(list)
        for quote in quotes:
            events[quote.event_id].append(quote)
        accepted, ambiguous, unmatched = {}, 0, 0
        for event_id, offers in events.items():
            rows = [r for rows in calendars.values() for r in rows if r['match_id'] == event_id]
            if len(rows) != 1:
                unmatched += 1; continue
            row = rows[0]
            matches = [f for f in fixtures if f.sport_key == row['sport_key']
                and normalise_team(f.home) == normalise_team(row['home_team'])
                and normalise_team(f.away) == normalise_team(row['away_team'])
                and abs(f.kickoff.timestamp() - row['epoch']) <= max_kickoff_gap_h * 3600]
            if len(matches) != 1:
                ambiguous += len(matches) > 1
                unmatched += not matches
                continue
            fixture = matches[0]
            accepted.setdefault(fixture.match_id, []).extend(MarketPrice(fixture.match_id, q.selection,
                q.odds, q.book_key, q.book_title, NAME, q.market, q.line, q.updated_at) for q in offers)
        return MatchOutcome({k: tuple(v) for k, v in accepted.items()}, len(events) - ambiguous - unmatched,
                            len(accepted), unmatched, ambiguous)
