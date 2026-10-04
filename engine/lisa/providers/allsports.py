"""Supporting AllSportsAPI v2 source; primary providers remain configured.

Only documented regulation scores and named contracts are used. Unverified
corner field names and odds timestamps do not become fabricated observations.
"""
import hashlib
import json
import math
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from .base import HttpTransport, FixturesResult, SourceTier, ParseError, ProviderNotAvailableError
from .calendar import normalise_fixture, normalise_team
from .api_football import COMPETITIONS, ApiFootballProvider
from .sharpapi import SharpQuote, SharpSnapshot, MatchOutcome
from ..board import MarketPrice
from .diagnostics import response_error

NAME = 'allsports'
URL = 'https://apiv2.allsportsapi.com/football/'


class AllSportsSnapshot(SharpSnapshot):
    def to_dict(self):
        return dict(super().to_dict(), source=NAME, freshness='bookmaker update time unavailable')


class AllSportsProvider:
    name, tier = NAME, SourceTier.OFFICIAL

    def __init__(self, key='', *, transport=None, hourly_limit=260, odds_enabled=False,
                 corner_stat_type='', bookmakers=()):
        if type(hourly_limit) is not int or hourly_limit < 1:
            raise ValueError('AllSportsAPI hourly limit must be a positive integer')
        self.key = key.strip()
        self.transport = transport or HttpTransport(timeout=10, max_retries=0)
        self.transport.set_rate_limit('apiv2.allsportsapi.com', hourly_limit / 3600., 1)
        self.hourly_limit, self.odds_enabled = hourly_limit, odds_enabled
        self.corner_stat_type, self.bookmakers = corner_stat_type, set(bookmakers)
        self.storage = None
        self._cache = {}
        self._period, self._requests = '', 0
        self._lock = threading.RLock()
        self.purpose = 'ordinary'

    def is_available(self):
        return bool(self.key)

    def leagues(self):
        return list(COMPETITIONS)

    def _get(self, method, parameters=None, *, ttl=900, purpose=None):
        if not self.key:
            raise ProviderNotAvailableError('ALLSPORTS_API_KEY is not configured', provider=NAME)
        purpose = purpose or self.purpose
        parameters = parameters or {}
        identity = hashlib.sha256(json.dumps([method, parameters, hashlib.sha256(self.key.encode()).hexdigest()], sort_keys=True).encode()).hexdigest()
        cache_key = f'{NAME}:{identity}'
        with self._lock:
            cached = self.storage.get_live(cache_key) if self.storage else self._cache.get(identity)
            if self.storage and cached is not None:
                return cached
            if not self.storage and cached and cached[0] > time.time():
                return cached[1]
            hour = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H')
            ceiling = self.hourly_limit if purpose == 'settlement' else max(1, int(self.hourly_limit * .8))
            if self.storage:
                with self.storage._tx() as conn:
                    conn.execute('INSERT INTO provider_daily_usage VALUES (?, ?, 0) ON CONFLICT(provider, day) DO NOTHING', (NAME, hour))
                    if not conn.execute('UPDATE provider_daily_usage SET requests=requests+1 WHERE provider=? AND day=? AND requests<?',
                                        (NAME, hour, ceiling)).rowcount:
                        raise ProviderNotAvailableError('AllSportsAPI hourly request budget exhausted', provider=NAME)
            else:
                if self._period != hour:
                    self._period, self._requests = hour, 0
                if self._requests >= ceiling:
                    raise ProviderNotAvailableError('AllSportsAPI hourly request budget exhausted', provider=NAME)
                self._requests += 1
            payload = self.transport.post_json(URL, form=dict(met=method, APIkey=self.key, **parameters), provider=NAME)
            if not isinstance(payload, dict) or payload.get('success') != 1:
                raise response_error('AllSportsAPI rejected this method or returned an invalid envelope', provider=NAME,
                    errors=payload.get('error') if isinstance(payload, dict) else None,
                    fallback='provider_error' if isinstance(payload, dict) else 'malformed_envelope')
            result = payload.get('result')
            if not isinstance(result, (dict, list)):
                raise response_error('AllSportsAPI result has an unsupported shape', provider=NAME,
                                     fallback='unsupported_result_shape')
            if self.storage:
                self.storage.upsert_live(cache_key, result, ttl)
            else:
                self._cache[identity] = (time.time() + ttl, result)
            return result

    def league_id(self, sport_key):
        if sport_key not in COMPETITIONS:
            return None
        country, name = COMPETITIONS[sport_key]
        rows = self._get('Leagues', ttl=86400)
        if not isinstance(rows, list):
            raise ParseError('AllSportsAPI league catalog is not a list', provider=NAME)
        matches = [r for r in rows if (r.get('country_name'), r.get('league_name')) == (country, name)]
        if len(matches) != 1:
            raise ProviderNotAvailableError(f'AllSportsAPI catalog does not uniquely cover {sport_key}', provider=NAME)
        return matches[0]['league_key']

    def get_range(self, sport_key, start, end, *, ttl=900):
        league_id = self.league_id(sport_key)
        if league_id is None:
            return FixturesResult(NAME, sport_key, ())
        rows = self._get('Fixtures', {'leagueId': league_id, 'from': start, 'to': end, 'timezone': 'UTC'}, ttl=ttl)
        if not isinstance(rows, list):
            raise ParseError('AllSportsAPI fixtures result is not a list', provider=NAME)
        fixtures = [self.to_fixture(r, sport_key, corner_stat_type=self.corner_stat_type) for r in rows]
        return FixturesResult(NAME, sport_key, tuple(f for f in fixtures if f is not None))

    def get_fixtures(self, sport_key):
        today = datetime.now(timezone.utc).date()
        # Batch includes recent finals for settlement and four days of upcoming
        # matches; historical ingestion is a separate bounded job.
        return self.get_range(sport_key, (today - timedelta(days=7)).isoformat(),
                              (today + timedelta(days=4)).isoformat())

    @staticmethod
    def to_fixture(row, sport_key, *, corner_stat_type=''):
        try:
            kickoff = datetime.fromisoformat(row['event_date'] + 'T' + row['event_time']).replace(tzinfo=timezone.utc)
            raw_status = str(row.get('event_status', '')).strip().lower()
            scores = re.fullmatch(r'\s*(\d+)\s*-\s*(\d+)\s*', str(row.get('event_ft_result', '')))
            finished = raw_status in ('finished', 'after et', 'after penalties')
            status = 'FINISHED' if finished and scores else 'CANCELED' if raw_status in ('cancelled', 'canceled') else 'POSTPONED' if raw_status == 'postponed' else 'IN_PLAY' if row.get('event_live') == '1' else 'SCHEDULED'
            home, away = tuple(map(int, scores.groups())) if scores else (None, None)
            result = normalise_fixture(provider=NAME, sport_key=sport_key, match_id=str(row['event_key']),
                kickoff_epoch=kickoff.timestamp(), home=row['event_home_team'], away=row['event_away_team'],
                home_score=home, away_score=away, status=status, season=row.get('league_season'))
            result.update(home_team_id=row.get('home_team_key'), away_team_id=row.get('away_team_key'),
                provider_fixture_id=str(row['event_key']), source_statistics=row.get('statistics', []),
                source_lineups=row.get('lineups', {}), ended_after_extra_time=raw_status in ('after et', 'after penalties'))
            # The exact field must be confirmed with a trial sample. No free-text
            # commentary counting, missing-to-zero conversion or extra-time counts.
            if corner_stat_type and result['completed'] and not result['ended_after_extra_time']:
                candidates = [s for s in result['source_statistics'] if s.get('type') == corner_stat_type]
                if len(candidates) == 1:
                    for side in ('home', 'away'):
                        value = str(candidates[0].get(side, ''))
                        if re.fullmatch(r'\d+', value) and int(value) <= 60:
                            result[side + '_corners'] = int(value)
            return result
        except (TypeError, ValueError, KeyError):
            return None

    def fetch(self, *, sport_keys, window_hours, now, max_pages=6, deadline=None):
        snapshot = AllSportsSnapshot()
        if not self.odds_enabled:
            snapshot.stopped_because = 'odds entitlement not configured'
            return snapshot
        quotes = []
        for sport in sport_keys:
            if deadline and datetime.now(timezone.utc) >= deadline:
                snapshot.truncated, snapshot.stopped_because = True, 'time budget'
                break
            league_id = self.league_id(sport)
            result = self._get('FullOdds', {'leagueId': league_id, 'from': now.date().isoformat(),
                'to': (now + timedelta(hours=window_hours)).date().isoformat()}, ttl=900)
            if not isinstance(result, dict):
                raise ParseError('AllSportsAPI FullOdds is not an event mapping', provider=NAME)
            calendar = {r['provider_fixture_id']: r for r in self.get_fixtures(sport).fixtures}
            snapshot.pages += 1
            for event_id, markets in result.items():
                fixture = calendar.get(str(event_id))
                if not fixture or not now.timestamp() <= fixture['epoch'] <= now.timestamp() + window_hours * 3600:
                    continue
                if not isinstance(markets, dict):
                    continue
                snapshot.rows += 1
                for market_name, selections in markets.items():
                    if not isinstance(selections, dict):
                        continue
                    for label, offers in selections.items():
                        parsed = ApiFootballProvider.parse_bet(market_name, label)
                        if not parsed:
                            snapshot.count('unsupported_contract'); continue
                        if not isinstance(offers, dict):
                            continue
                        for book, raw in offers.items():
                            if self.bookmakers and book not in self.bookmakers:
                                continue
                            try:
                                odds = float(raw)
                                if not math.isfinite(odds) or odds <= 1:
                                    raise ValueError()
                            except (TypeError, ValueError):
                                snapshot.count('invalid_odds'); continue
                            quotes.append(SharpQuote(fixture['match_id'], parsed[0], parsed[1], odds,
                                book, book, NAME, parsed[2], datetime.fromtimestamp(fixture['epoch'], timezone.utc),
                                fixture['home_team'], fixture['away_team'], None, sport))
            # Capture the raw, dated offers for research. Retrieval time is NOT
            # silently substituted for unavailable bookmaker update timestamps.
            if self.storage:
                self.storage.set_telemetry(f'allsports:last_offers:{sport}', {
                    'observed_at': datetime.now(timezone.utc).isoformat(), 'offers': result,
                    'bookmaker_updated_at': None, 'purpose': 'supporting_data'})
        snapshot.quotes = tuple(quotes)
        return snapshot

    def match(self, quotes, fixtures, *, max_kickoff_gap_h=6):
        grouped = {}
        for q in quotes:
            grouped.setdefault(q.event_id, []).append(q)
        matches_by_event = {}
        for event_id, offers in grouped.items():
            q = offers[0]
            candidates = [f for f in fixtures if f.sport_key == q.sport_key and normalise_team(f.home) == normalise_team(q.home)
                and normalise_team(f.away) == normalise_team(q.away)
                and q.kickoff is not None and abs((f.kickoff - q.kickoff).total_seconds()) <= max_kickoff_gap_h * 3600]
            matches_by_event[event_id] = candidates
        contested = {f.match_id for candidates in matches_by_event.values() for f in candidates
                     if sum(f in other for other in matches_by_event.values()) > 1}
        accepted, ambiguous, unmatched = {}, 0, 0
        for event_id, candidates in matches_by_event.items():
            if len(candidates) != 1 or candidates[0].match_id in contested:
                ambiguous += bool(candidates)
                unmatched += not candidates
                continue
            fixture = candidates[0]
            accepted[fixture.match_id] = tuple(MarketPrice(fixture.match_id, q.selection, q.odds,
                q.book_key, q.book_title, NAME, q.market, q.line, q.updated_at) for q in grouped[event_id])
        return MatchOutcome(accepted, len(accepted), len(accepted), unmatched, ambiguous, len(contested))
