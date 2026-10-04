"""OddsPapi v4 supporting prematch offers, with subscription-scoped quotas.

Catalog contracts: https://oddspapi.io/us/docs
Unknown/player/period markets are archived but never attached to a goal model.
"""
import hashlib
import json
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .base import SourceTier, ProviderNotAvailableError
from .diagnostics import http_error, transport_error, safe_error_summary
from .sharpapi import SharpSnapshot, SharpQuote, MatchOutcome
from .calendar import normalise_team
from ..board import MarketPrice
from ..odds_history import OddsHistoryRepository

NAME = 'oddspapi'
TOURNAMENTS = {
 'soccer_epl': ('england', 'premier-league'),
 'soccer_england_championship': ('england', 'championship'),
 'soccer_spain_la_liga': ('spain', 'laliga'),
 'soccer_germany_bundesliga': ('germany', 'bundesliga'),
 'soccer_italy_serie_a': ('italy', 'serie-a'),
 'soccer_france_ligue_one': ('france', 'ligue-1'),
 'soccer_netherlands_eredivisie': ('netherlands', 'eredivisie'),
 'soccer_portugal_primeira_liga': ('portugal', 'primeira-liga'),
 'soccer_uefa_champions_league': ('international-clubs', 'uefa-champions-league'),
}


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PrivateTransport:
    """Query authentication is required; URLs and server bodies never enter logs."""
    def __init__(self):
        self.opener = build_opener(NoRedirect())
        self.next_request = 0.
        self.lock = threading.Lock()

    def request(self, path, params, key):
        from ..job_budget import check_budget, bounded_timeout, budget_sleep
        check_budget()
        if path not in ('account', 'markets', 'tournaments', 'participants',
                        'odds-by-tournaments', 'historical-odds'):
            raise ValueError('Unsupported OddsPapi endpoint')
        with self.lock:
            wait = self.next_request - time.monotonic()
            if wait > 0:
                budget_sleep(wait)
            self.next_request = time.monotonic() + (5.1 if path == 'historical-odds' else 1.1)
            try:
                request = Request('https://api.oddspapi.io/v4/' + path + '?' +
                    urlencode(dict(params, apiKey=key)), headers={'Accept': 'application/json'})
                with self.opener.open(request, timeout=bounded_timeout(10)) as response:
                    raw = response.read(10_000_001)
                if len(raw) > 10_000_000:
                    raise ValueError('Response too large')
                return json.loads(raw)
            except HTTPError as exc:
                raise http_error(NAME, exc.code) from None
            except Exception as exc:
                raise transport_error(NAME, exc) from None


class OddsPapiSnapshot(SharpSnapshot):
    def to_dict(self):
        return dict(super().to_dict(), source=NAME)


class OddsPapiProvider:
    name, tier = NAME, SourceTier.OFFICIAL

    def __init__(self, key='', *, transport=None, monthly_limit=250, reserve=25,
                 poll_interval_sec=21600, bookmakers=()):
        if type(monthly_limit) is not int or monthly_limit < 1 or type(reserve) is not int or not 0 <= reserve < monthly_limit:
            raise ValueError('OddsPapi reserve must be smaller than its positive request limit')
        if type(poll_interval_sec) is not int or poll_interval_sec < 900:
            raise ValueError('OddsPapi poll interval must be at least 900 seconds')
        if len(bookmakers) > 3 or any(not isinstance(b, str) or not b for b in bookmakers):
            raise ValueError('OddsPapi supports at most three configured bookmakers')
        self.key = key.strip()
        self.transport = transport or PrivateTransport()
        self.monthly_limit, self.reserve = monthly_limit, reserve
        self.poll_interval_sec, self.bookmakers = poll_interval_sec, tuple(bookmakers)
        self.storage = None
        self.lock = threading.RLock()
        self.cache = {}
        self.local_usage = {}
        self.received_at = {}
        self.deadline = None

    def is_available(self):
        return bool(self.key)

    def leagues(self):
        return list(TOURNAMENTS)

    def _cached(self, path, params, ttl):
        identity = 'oddspapi:cache:' + hashlib.sha256(json.dumps(
            [hashlib.sha256(self.key.encode()).hexdigest(), path, params], sort_keys=True).encode()).hexdigest()
        if self.storage:
            value = self.storage.get_live(identity)
        else:
            expiry, value = self.cache.get(identity, (0, None))
            if expiry <= time.time():
                value = None
        return identity, value

    def _store(self, identity, value, ttl):
        if self.storage:
            self.storage.upsert_live(identity, value, ttl)
        else:
            self.cache[identity] = (time.time() + ttl, value)

    def account(self):
        identity, cached = self._cached('account', {}, 3600)
        if cached is not None:
            return cached
        payload = self.transport.request('account', {}, self.key)
        if not isinstance(payload, dict):
            raise ProviderNotAvailableError('OddsPapi account envelope is invalid', provider=NAME)
        current = payload.get('current_subscription_id')
        subscriptions = [s for s in payload.get('subscriptions', []) if s.get('subscription_id') == current
                         and s.get('is_active') is True]
        if len(subscriptions) != 1:
            raise ProviderNotAvailableError('OddsPapi has no unique active subscription', provider=NAME)
        sub = subscriptions[0]
        if 10 not in sub.get('sport_ids', []):
            raise ProviderNotAvailableError('OddsPapi subscription does not include football', provider=NAME)
        end = timestamp(sub.get('valid_until'))
        if end and end <= datetime.now(timezone.utc):
            raise ProviderNotAvailableError('OddsPapi subscription expired', provider=NAME)
        if any(type(sub.get(k)) is not int or sub[k] < 0 for k in ('request_limit', 'request_count')):
            raise ProviderNotAvailableError('OddsPapi account quota is invalid', provider=NAME)
        # Never persist the raw account payload: it contains the API key.
        if not current or not timestamp(sub.get('valid_from')):
            raise ProviderNotAvailableError('OddsPapi subscription period identity is missing', provider=NAME)
        account = {'scope': hashlib.sha256(f'{current}|{sub.get("valid_from")}'.encode()).hexdigest(),
            'limit': min(sub['request_limit'], self.monthly_limit), 'used': sub['request_count'],
            'books': sorted(sub.get('bookmakers', {})), 'checked_at': datetime.now(timezone.utc).isoformat()}
        self._store(identity, account, 3600)
        return account

    def _reserve(self, account, *, unmetered=False):
        ceiling = account['limit'] - (0 if unmetered else self.reserve)
        if self.storage:
            with self.storage._tx() as conn:
                conn.execute('INSERT INTO provider_daily_usage VALUES (?, ?, ?) ON CONFLICT(provider,day) DO NOTHING',
                             (NAME, account['scope'], account['used']))
                conn.execute('UPDATE provider_daily_usage SET requests=? WHERE provider=? AND day=? AND requests<?',
                             (account['used'], NAME, account['scope'], account['used']))
                if unmetered:
                    used = conn.execute('SELECT requests FROM provider_daily_usage WHERE provider=? AND day=?',
                                        (NAME, account['scope'])).fetchone()['requests']
                    allowed = used < ceiling
                else:
                    allowed = conn.execute('UPDATE provider_daily_usage SET requests=requests+1 '
                        'WHERE provider=? AND day=? AND requests<?', (NAME, account['scope'], ceiling)).rowcount
                used = conn.execute('SELECT requests FROM provider_daily_usage WHERE provider=? AND day=?',
                                    (NAME, account['scope'])).fetchone()['requests']
            self.storage.set_telemetry('provider:oddspapi:status', {'requests_reserved': used,
                'limit': account['limit'], 'reserve': self.reserve, 'account_checked_at': account['checked_at'],
                'state': 'available' if allowed else 'quota_exhausted',
                'detail': 'Subscription-scoped reservations; replacing a key does not reset usage.'})
        else:
            used = max(self.local_usage.get(account['scope'], 0), account['used'])
            allowed = used < ceiling
            self.local_usage[account['scope']] = used + bool(allowed and not unmetered)
        if not allowed:
            raise ProviderNotAvailableError('OddsPapi subscription request budget exhausted', provider=NAME)

    def _get(self, path, params, *, ttl=86400):
        with self.lock:
            if not self.key:
                raise ProviderNotAvailableError('ODDSPAPI_KEY is missing', provider=NAME)
            identity, value = self._cached(path, params, ttl)
            if value is not None:
                self.received_at[path] = value['received_at']
                return value['payload']
            if self.deadline and datetime.now(timezone.utc) >= self.deadline:
                raise ProviderNotAvailableError('OddsPapi fetch deadline reached', provider=NAME)
            account = self.account()
            self._reserve(account, unmetered=path == 'historical-odds')
            value = self.transport.request(path, params, self.key)
            self.received_at[path] = datetime.now(timezone.utc).isoformat()
            self._store(identity, {'payload': value, 'received_at': self.received_at[path]}, ttl)
            return value

    @staticmethod
    def contract(meta, outcome):
        if meta.get('sportId') != 10 or meta.get('period') != 'fulltime' or meta.get('playerProp') is not False:
            return None
        name = meta.get('marketName')
        label = outcome.get('outcomeName')
        if name == 'Full Time Result':
            side = {'1': 'Home', 'X': 'Draw', '2': 'Away'}.get(label)
            return ('h2h', side, None) if side else None
        if name == 'Both Teams To Score' and label in ('Yes', 'No'):
            return ('btts', label, None)
        if name == 'Over Under Full Time' and label in ('Over', 'Under'):
            line = meta.get('handicap')
            if type(line) in (int, float) and math.isfinite(line) and 0 <= line <= 10 and abs(line*4-round(line*4)) < 1e-8:
                return ('totals', label, float(line))
        return None

    def fetch(self, *, sport_keys, window_hours, now, max_pages=1, deadline=None):
        snapshot = OddsPapiSnapshot()
        self.deadline = deadline
        try:
            catalog = self._get('tournaments', {'sportId': 10, 'language': 'en'}, ttl=604800)
            selected = {}
            for league in sport_keys:
                target = TOURNAMENTS.get(league)
                matches = [r for r in catalog if (r.get('categorySlug'), r.get('tournamentSlug')) == target] if target else []
                if len(matches) == 1:
                    selected[str(matches[0]['tournamentId'])] = league
            if not selected:
                snapshot.stopped_because = 'no_verified_competitions'
                return snapshot
            markets = {str(m['marketId']): m for m in self._get('markets', {'language': 'en'}, ttl=604800)}
            participants = self._get('participants', {'sportId': 10, 'language': 'en'}, ttl=604800)
            account = self.account()
            books = list(self.bookmakers or account['books'][:3])
            if 'betfair-ex' in books and len(books) > 1:
                if self.bookmakers:
                    raise ValueError('Betfair Exchange requires a separate single-bookmaker request')
                books = [b for b in books if b != 'betfair-ex']
            if not books or any(b not in account['books'] for b in books):
                raise ProviderNotAvailableError('OddsPapi bookmaker entitlement unavailable', provider=NAME)
            if deadline and datetime.now(timezone.utc) >= deadline:
                snapshot.stopped_because = 'deadline'
                return snapshot
            rows = self._get('odds-by-tournaments', {'tournamentIds': ','.join(sorted(selected)),
                'bookmakers': ','.join(books), 'language': 'en', 'verbosity': 3}, ttl=self.poll_interval_sec)
            if isinstance(rows, dict) and 'fixtureId' in rows:
                rows = [rows]
            if not isinstance(rows, list):
                raise ValueError('Unexpected OddsPapi event envelope')
            quotes, observations = [], []
            observed = self.received_at['odds-by-tournaments']
            for row in rows:
                snapshot.rows += 1
                league = selected.get(str(row.get('tournamentId')))
                kickoff = timestamp(row.get('startTime'))
                if row.get('sportId') != 10 or row.get('statusId') != 0 or not league or not kickoff or not now <= kickoff <= now + timedelta(hours=window_hours):
                    snapshot.count('outside_prematch_scope')
                    continue
                home, away = participants.get(str(row.get('participant1Id'))), participants.get(str(row.get('participant2Id')))
                for book, offers in row.get('bookmakerOdds', {}).items():
                    if book not in books or offers.get('bookmakerIsActive') is not True or offers.get('suspended') is True:
                        snapshot.count('inactive_bookmaker')
                        continue
                    for market_id, market in offers.get('markets', {}).items():
                        if market.get('marketActive') is False:
                            continue
                        meta = markets.get(str(market_id), {})
                        outcomes = {str(o['outcomeId']): o for o in meta.get('outcomes', [])}
                        for outcome_id, outcome in market.get('outcomes', {}).items():
                            for player_id, offer in outcome.get('players', {}).items():
                                price = offer.get('price')
                                if offer.get('active') is not True or type(price) not in (float, int) or not math.isfinite(price) or price <= 1:
                                    continue
                                contract = self.contract(meta, outcomes.get(str(outcome_id), {})) if str(player_id) == '0' else None
                                changed = timestamp(offer.get('changedAt'))
                                bookmaker_changed = timestamp(offer.get('bookmakerChangedAt'))
                                observations.append({'source': NAME, 'event_id': str(row['fixtureId']), 'bookmaker': book,
                                    'market_id': str(market_id), 'outcome_id': str(outcome_id), 'player_id': str(player_id),
                                    'odds': price, 'observed_at': observed, 'period': meta.get('period'),
                                    'market_name': meta.get('marketName'), 'outcome_name': outcomes.get(str(outcome_id), {}).get('outcomeName'),
                                    'line': meta.get('handicap'), 'sport_key': league, 'kickoff': kickoff.isoformat(),
                                    'home': home, 'away': away, 'provider_changed_at': changed.isoformat() if changed else None,
                                    'bookmaker_changed_at': bookmaker_changed.isoformat() if bookmaker_changed else None,
                                    'model_supported': bool(contract)})
                                if contract and home and away:
                                    market_name, side, line = contract
                                    quotes.append(SharpQuote(str(row['fixtureId']), market_name, side, price, book,
                                        book, NAME, line, kickoff, home, away, bookmaker_changed, league))
                                else:
                                    snapshot.count('unsupported_contract_or_identity')
            if self.storage:
                OddsHistoryRepository(self.storage).ingest(observations)
            snapshot.quotes, snapshot.pages = tuple(quotes), 1
            snapshot.stopped_because = 'batched_competitions'
        except Exception as exc:
            snapshot.error = 'OddsPapi request failed: ' + safe_error_summary(exc)
        finally:
            self.deadline = None
        return snapshot

    def match(self, quotes, fixtures, *, max_kickoff_gap_h=6):
        grouped = {}
        for q in quotes:
            grouped.setdefault(q.event_id, []).append(q)
        candidates, counts = {}, {}
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

    def historical(self, event_id, bookmakers):
        if not isinstance(event_id, str) or not event_id or not 1 <= len(bookmakers) <= 3:
            raise ValueError('A fixture ID and one to three bookmakers are required')
        if 'betfair-ex' in bookmakers and len(bookmakers) > 1:
            raise ValueError('Betfair Exchange requires a separate single-bookmaker request')
        payload = self._get('historical-odds', {'fixtureId': event_id, 'bookmakers': ','.join(bookmakers)}, ttl=86400)
        if not isinstance(payload, dict) or payload.get('fixtureId') != event_id:
            raise ValueError('Unexpected OddsPapi historical envelope')
        observations = []
        for book, data in payload.get('bookmakers', {}).items():
            if book not in bookmakers:
                continue
            for market_id, market in data.get('markets', {}).items():
                for outcome_id, outcome in market.get('outcomes', {}).items():
                    for player_id, entries in outcome.get('players', {}).items():
                        for entry in entries:
                            recorded = timestamp(entry.get('createdAt'))
                            price = entry.get('price')
                            if (not recorded or recorded > datetime.now(timezone.utc)
                                    or type(price) not in (int, float)
                                    or not math.isfinite(price) or price <= 1):
                                continue
                            observations.append({'source': NAME, 'event_id': event_id, 'bookmaker': book,
                                'market_id': str(market_id), 'outcome_id': str(outcome_id), 'player_id': str(player_id),
                                'odds': entry.get('price'), 'active': entry.get('active'),
                                'observed_at': self.received_at['historical-odds'], 'historical': True,
                                'provider_recorded_at': recorded.isoformat(),
                                'bookmaker_changed_at': None})
        if self.storage:
            OddsHistoryRepository(self.storage).ingest(observations)
        return {'event_id': event_id, 'observations': len(observations),
                'detail': 'Provider history timestamps retained; bookmaker update time is unknown.'}
