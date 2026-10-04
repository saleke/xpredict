"""The Odds API v4 supporting prematch prices; bounded credits, no retries."""
import hashlib
import json
import math
import re
import threading
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from .base import SourceTier, ProviderNotAvailableError
from .oddspapi import NoRedirect, timestamp
from .calendar import normalise_team
from .sharpapi import SharpQuote, SharpSnapshot, MatchOutcome
from ..board import MarketPrice
from .diagnostics import http_error, transport_error, safe_error_summary

NAME = 'the_odds_api'


class PrivateTransport:
    def __init__(self):
        self.opener = build_opener(NoRedirect())

    def request(self, path, params, key):
        from ..job_budget import check_budget, bounded_timeout
        check_budget()
        if path != '/v4/sports' and not re.fullmatch(r'/v4/sports/soccer_[a-z0-9_]+/odds', path):
            raise ValueError('Unsupported The Odds API endpoint')
        try:
            req = Request('https://api.the-odds-api.com' + path + '?' +
                urlencode(dict(params, apiKey=key)), headers={'Accept': 'application/json'})
            with self.opener.open(req, timeout=bounded_timeout(10)) as response:
                raw = response.read(10_000_001)
                headers = dict(response.headers.items())
            if len(raw) > 10_000_000:
                raise ValueError('Response too large')
            return json.loads(raw), headers
        except HTTPError as exc:
            raise http_error(NAME, exc.code) from None
        except Exception as exc:
            raise transport_error(NAME, exc) from None


class Snapshot(SharpSnapshot):
    def to_dict(self):
        return dict(super().to_dict(), source=NAME)


class TheOddsApiProvider:
    name, tier = NAME, SourceTier.OFFICIAL

    def __init__(self, key, *, transport=None, monthly_limit=500, reserve=50,
                 daily_limit=14, regions=('eu',), markets=('h2h', 'totals'), ttl=86400):
        if type(monthly_limit) is not int or monthly_limit < 1 or type(reserve) is not int or not 0 <= reserve < monthly_limit:
            raise ValueError('The Odds API reserve must be smaller than the credit limit')
        if type(daily_limit) is not int or daily_limit < 1 or type(ttl) is not int or ttl < 900:
            raise ValueError('The Odds API daily cap and cache interval are invalid')
        if not regions or len(set(regions)) != len(regions) or set(regions) - {'eu','uk','us','us2','au'}:
            raise ValueError('Unsupported or duplicate The Odds API regions')
        if not markets or len(set(markets)) != len(markets) or set(markets) - {'h2h','totals'}:
            raise ValueError('Only verified h2h and totals contracts are enabled')
        self.key, self.transport = key.strip(), transport or PrivateTransport()
        self.monthly_limit, self.reserve, self.daily_limit = monthly_limit, reserve, daily_limit
        self.regions, self.markets, self.ttl = tuple(regions), tuple(markets), ttl
        self.scope = hashlib.sha256(self.key.encode()).hexdigest()
        self.storage = None
        self.lock = threading.RLock()
        self._catalog_verified = False

    def is_available(self):
        return bool(self.key)

    def leagues(self):
        from .calendar import LEAGUES
        return list(LEAGUES)

    def _identity(self, kind):
        return NAME + ':cache:' + hashlib.sha256(json.dumps(
            [self.scope, kind, self.regions, self.markets], sort_keys=True).encode()).hexdigest()

    def _status(self, headers):
        headers = {k.lower(): v for k,v in headers.items()}
        try:
            used, remaining = int(headers['x-requests-used']), int(headers['x-requests-remaining'])
            if min(used, remaining) < 0:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise ProviderNotAvailableError('The Odds API quota headers missing or invalid', provider=NAME)
        status = {'used': used, 'remaining': remaining,
            'limit': min(self.monthly_limit, used + remaining),
            'checked_at': datetime.now(timezone.utc).isoformat()}
        with self.storage._tx() as conn:
            conn.execute('INSERT INTO provider_daily_usage VALUES (?, ?, ?) ON CONFLICT(provider,day) DO NOTHING',
                         (NAME, self.scope, used))
            conn.execute('UPDATE provider_daily_usage SET requests=? WHERE provider=? AND day=? AND requests<?',
                         (used, NAME, self.scope, used))
        self.storage.set_telemetry('provider:the_odds_api:status', status)
        return status

    def catalog(self):
        identity = self._identity('sports')
        cached = self.storage.get_live(identity)
        if cached is not None and self._catalog_verified:
            return cached
        rows, headers = self.transport.request('/v4/sports', {}, self.key)
        if not isinstance(rows, list):
            raise ProviderNotAvailableError('The Odds API sports catalog invalid', provider=NAME)
        status = self._status(headers)
        catalog = {'sports': [r['key'] for r in rows if isinstance(r, dict) and
            r.get('active') is True and r.get('has_outrights') is False and
            isinstance(r.get('key'), str) and r['key'].startswith('soccer_')], 'quota': status}
        self.storage.upsert_live(identity, catalog, 3600)
        self._catalog_verified = True
        return catalog

    def _reserve(self, quota, now):
        cost = len(self.regions) * len(self.markets)
        ceiling = min(self.monthly_limit, quota['limit']) - self.reserve
        day = now.astimezone(timezone.utc).date().isoformat()
        with self.storage._tx() as conn:
            conn.execute('INSERT INTO provider_daily_usage VALUES (?, ?, ?) ON CONFLICT(provider,day) DO NOTHING',
                         (NAME, self.scope, quota['used']))
            conn.execute('UPDATE provider_daily_usage SET requests=? WHERE provider=? AND day=? AND requests<?',
                         (quota['used'], NAME, self.scope, quota['used']))
            conn.execute('INSERT INTO provider_daily_usage VALUES (?, ?, 0) ON CONFLICT(provider,day) DO NOTHING', (NAME, day))
            monthly = conn.execute('UPDATE provider_daily_usage SET requests=requests+? '
                'WHERE provider=? AND day=? AND requests+?<=?', (cost, NAME, self.scope, cost, ceiling)).rowcount
            daily = conn.execute('UPDATE provider_daily_usage SET requests=requests+? '
                'WHERE provider=? AND day=? AND requests+?<=?', (cost, NAME, day, cost, self.daily_limit)).rowcount
            if not monthly or not daily:
                # Raising rolls back both increments, never losing a reservation
                # to a race between daily and subscription limits.
                raise ProviderNotAvailableError('The Odds API credit budget exhausted', provider=NAME)

    def fetch(self, *, sport_keys, window_hours, now, max_pages=6, deadline=None):
        snapshot = Snapshot()
        if self.storage is None:
            snapshot.error = 'Persistent storage is required for credit accounting'
            return snapshot
        if not self.key:
            snapshot.error = 'The Odds API credential is missing'
            return snapshot
        with self.lock:
            quotes = []
            try:
                catalog = self.catalog()
                supported = sorted(set(sport_keys) & set(catalog['sports']))
                # Least recently collected first, using a durable timestamp
                # rather than list order: partial budgets cannot starve a league.
                priorities = self.storage.get_telemetry('the_odds_api:league_refresh') or {}
                supported.sort(key=lambda s: (priorities.get(s, ''), s))
                calls = 0
                for sport in supported:
                    cached = self.storage.get_live(self._identity(sport))
                    if cached is None:
                        if calls >= max_pages or deadline and datetime.now(timezone.utc) >= deadline:
                            snapshot.truncated, snapshot.stopped_because = True, 'cycle request budget'
                            continue
                        try:
                            self._reserve(catalog['quota'], now)
                        except ProviderNotAvailableError:
                            snapshot.truncated, snapshot.stopped_because = True, 'credit budget'
                            continue
                        params = {'regions': ','.join(self.regions), 'markets': ','.join(self.markets),
                            'oddsFormat': 'decimal', 'dateFormat': 'iso'}
                        calls += 1
                        try:
                            rows, headers = self.transport.request('/v4/sports/' + sport + '/odds', params, self.key)
                            catalog['quota'] = self._status(headers)
                            if not isinstance(rows, list):
                                raise ValueError('Price envelope invalid')
                        except Exception as exc:
                            snapshot.error = 'The Odds API league request failed: ' + safe_error_summary(exc)
                            snapshot.count('league_request_failed')
                            continue
                        cached = {'rows': rows, 'received_at': datetime.now(timezone.utc).isoformat()}
                        self.storage.upsert_live(self._identity(sport), cached, self.ttl)
                        priorities[sport] = cached['received_at']
                        self.storage.set_telemetry('the_odds_api:league_refresh', priorities)
                    snapshot.pages += 1
                    snapshot.rows += len(cached['rows'])
                    quotes.extend(self.parse(cached['rows'], sport, now, window_hours, snapshot))
            except Exception as exc:
                snapshot.error = 'The Odds API fetch failed: ' + safe_error_summary(exc)
            snapshot.quotes = tuple(quotes)
        return snapshot

    @staticmethod
    def parse(rows, sport, now, window, snapshot):
        quotes = []
        for event in rows:
            if not isinstance(event, dict) or event.get('sport_key') != sport:
                snapshot.count('wrong_sport'); continue
            kickoff = timestamp(event.get('commence_time'))
            home, away, event_id = event.get('home_team'), event.get('away_team'), event.get('id')
            if not kickoff or not now <= kickoff <= now + timedelta(hours=window):
                continue
            if not all(isinstance(v, str) and v for v in (home, away, event_id)) or normalise_team(home) == normalise_team(away):
                snapshot.count('invalid_event'); continue
            for book in event.get('bookmakers', []):
                if not isinstance(book, dict) or not isinstance(book.get('key'), str) or not book['key']:
                    continue
                for market in book.get('markets', []):
                    if not isinstance(market, dict):
                        snapshot.count('invalid_market'); continue
                    kind = market.get('key')
                    updated = timestamp(market.get('last_update') or book.get('last_update'))
                    if updated and updated > now:
                        snapshot.count('future_update'); continue
                    outcomes = market.get('outcomes', [])
                    if not isinstance(outcomes, list) or any(not isinstance(r, dict) for r in outcomes):
                        snapshot.count('invalid_outcomes'); continue
                    labels = [r.get('name') for r in outcomes if isinstance(r, dict)]
                    if kind == 'h2h' and (len(labels) != 3 or set(labels) != {home, away, 'Draw'}):
                        snapshot.count('non_three_way_result'); continue
                    if kind not in ('h2h','totals'):
                        snapshot.count('unsupported_contract'); continue
                    for outcome in outcomes:
                        price = outcome.get('price')
                        if type(price) not in (int,float) or not math.isfinite(price) or price <= 1:
                            snapshot.count('invalid_price'); continue
                        line = outcome.get('point') if kind == 'totals' else None
                        if kind == 'totals' and (outcome.get('name') not in ('Over','Under') or
                            type(line) not in (int,float) or not math.isfinite(line) or not 0 <= line <= 10 or
                            abs(line * 4 - round(line * 4)) > 1e-8):
                            snapshot.count('invalid_line'); continue
                        selection = {'Draw':'Draw',home:'Home',away:'Away'}.get(outcome['name']) if kind == 'h2h' else outcome['name']
                        quotes.append(SharpQuote(NAME + ':' + event_id,
                            'h2h' if kind == 'h2h' else 'totals', selection, float(price), book['key'],
                            book.get('title',''), NAME, line, kickoff, home, away, updated, sport))
        return quotes

    def match(self, quotes, fixtures, *, max_kickoff_gap_h=6):
        grouped, candidates, counts = {}, {}, {}
        for q in quotes:
            grouped.setdefault(q.event_id, []).append(q)
        for event, offers in grouped.items():
            q = offers[0]
            found = [f for f in fixtures if f.sport_key == q.sport_key and q.kickoff and
                normalise_team(f.home) == normalise_team(q.home) and normalise_team(f.away) == normalise_team(q.away)
                and abs((f.kickoff-q.kickoff).total_seconds()) <= max_kickoff_gap_h*3600]
            candidates[event] = found
            for f in found:
                counts[f.match_id] = counts.get(f.match_id, 0) + 1
        accepted, unmatched, ambiguous = {}, 0, 0
        for event, found in candidates.items():
            if not found:
                unmatched += 1
            elif len(found) != 1 or counts[found[0].match_id] != 1:
                ambiguous += 1
            else:
                f = found[0]
                accepted[f.match_id] = tuple(MarketPrice(f.match_id, q.selection, q.odds, q.book_key,
                    q.book_title, NAME, q.market, q.line, q.updated_at) for q in grouped[event])
        return MatchOutcome(accepted, len(accepted), len(accepted), unmatched, ambiguous,
                            sum(n > 1 for n in counts.values()))
