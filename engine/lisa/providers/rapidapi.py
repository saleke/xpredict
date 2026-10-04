"""Private GET transport for a verified RapidAPI endpoint contract.

No default host/routes: marketplace slugs are not endpoint contracts. All calls,
including account and history, reserve monthly capacity before network I/O.
"""
import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from .base import ProviderNotAvailableError
from .oddspapi import NoRedirect

# Verified from the subscriber's actual RapidAPI request example. This API
# route differs from direct OddsPapi v4; its JSON contract is still pending.
ODDSPAPI_RAPIDAPI_HOST = 'odds-api1.p.rapidapi.com'
ODDSPAPI_RAPIDAPI_ROUTES = {'main-odds': '/fixtures/odds/main'}
ODDSPAPI_RAPIDAPI_PARAMS = {'since': '0', 'bookmakers': 'pinnacle,stake,draftkings'}


def http_diagnostic(status, body):
    """Classify bounded provider text into fixed codes; never return the text."""
    try:
        payload = json.loads(body)
        values = [payload.get(k) for k in ('message', 'error', 'detail')] if isinstance(payload, dict) else []
        text = ' '.join(v for v in values if isinstance(v, str)).lower()
    except (ValueError, TypeError, UnicodeError):
        text = ''
    if 'not subscribed' in text or 'subscribe to this api' in text:
        return 'subscription_not_active'
    if any(v in text for v in ('missing apikey', 'missing api key', 'apikey is required', 'api key is required')):
        return 'additional_provider_authentication_required'
    if any(v in text for v in ('invalid api key', 'invalid apikey', 'invalid token')):
        return 'credential_rejected'
    return {401: 'authentication_rejected', 403: 'access_forbidden',
        404: 'endpoint_not_found', 429: 'rate_or_quota_limited'}.get(status, 'http_error')


class RapidApiTransport:
    def __init__(self, *, host, routes, storage, subscription_scope,
                 monthly_limit=250, reserve=25, opener=None, clock=time.time):
        if not isinstance(host, str) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.p\.rapidapi\.com', host):
            raise ValueError('A verified RapidAPI host is required')
        allowed = {'account', 'markets', 'tournaments', 'participants', 'main-odds',
                   'odds-by-tournaments', 'historical-odds'}
        if not isinstance(routes, dict) or not routes or set(routes) - allowed:
            raise ValueError('Unsupported RapidAPI route contract')
        for path in routes.values():
            if not isinstance(path, str) or not re.fullmatch(r'/[a-zA-Z0-9_/-]+', path) or '..' in path or '//' in path:
                raise ValueError('Routes must be literal absolute endpoint paths')
        if (type(monthly_limit) is not int or not 1 <= monthly_limit <= 250
                or type(reserve) is not int or not 0 <= reserve < monthly_limit):
            raise ValueError('RapidAPI free-plan budget must remain within 250 monthly requests')
        if not isinstance(subscription_scope, str) or not subscription_scope.strip():
            raise ValueError('Stable subscription identity is required')
        self.host, self.routes, self.storage = host, dict(routes), storage
        self.scope = hashlib.sha256((host + '|' + subscription_scope).encode()).hexdigest()
        self.limit, self.reserve, self.clock = monthly_limit, reserve, clock
        self.opener = opener or build_opener(NoRedirect())
        self.lock = threading.Lock()
        self.next_request = 0.

    @property
    def status_key(self):
        return 'provider:oddspapi_rapidapi:status:' + self.scope

    @property
    def ledger_name(self):
        return 'oddspapi_rapidapi:' + self.scope

    def _reserve(self):
        now = self.clock()
        with self.storage._tx() as conn:
            # A stable, non-calendar scope deliberately cannot reset on key
            # replacement or an assumed calendar-month boundary. Activation
            # needs a confirmed billing-period identity; renewals are fail-closed.
            conn.execute('INSERT INTO provider_daily_usage VALUES (?, ?, 0) '
                'ON CONFLICT(provider,day) DO NOTHING', (self.ledger_name, 'confirmed-period'))
            conn.execute('UPDATE provider_daily_usage SET requests=requests WHERE provider=? AND day=?',
                         (self.ledger_name, 'confirmed-period'))
            raw = conn.execute('SELECT val_json FROM system_telemetry WHERE key=?', (self.status_key,)).fetchone()
            state = json.loads(raw['val_json']) if raw else {}
            limit = min(self.limit, state.get('reported_limit', self.limit))
            blocked = state.get('blocked_until', 0) > now
            # Quota exhaustion never clears merely because a reset estimate
            # passed; confirm the next billing period before resuming traffic.
            blocked = blocked or state.get('quota_exhausted', False)
            if blocked or not conn.execute('UPDATE provider_daily_usage SET requests=requests+1 '
                'WHERE provider=? AND day=? AND requests<?',
                (self.ledger_name, 'confirmed-period', max(0, limit - self.reserve))).rowcount:
                raise ProviderNotAvailableError('RapidAPI request allowance unavailable', provider='oddspapi_rapidapi')

    def _reconcile(self, headers, status, diagnostic=None):
        headers = {k.lower(): v for k, v in headers.items()}
        def integer(name, maximum):
            try:
                value = int(headers[name])
                return value if 0 <= value <= maximum else None
            except (KeyError, TypeError, ValueError):
                return None
        limit = integer('x-ratelimit-requests-limit', 10_000_000)
        remaining = integer('x-ratelimit-requests-remaining', 10_000_000)
        reset = integer('x-ratelimit-requests-reset', 32 * 86400)
        now = self.clock()
        with self.storage._tx() as conn:
            conn.execute('UPDATE provider_daily_usage SET requests=requests WHERE provider=? AND day=?',
                         (self.ledger_name, 'confirmed-period'))
            raw = conn.execute('SELECT val_json FROM system_telemetry WHERE key=?', (self.status_key,)).fetchone()
            state = json.loads(raw['val_json']) if raw else {}
            if limit is not None and remaining is not None and remaining <= limit:
                # Only raise usage floors. Concurrent reservations must never
                # be lost when a slower response reports an earlier count.
                used = limit - remaining
                conn.execute('UPDATE provider_daily_usage SET requests=? WHERE provider=? AND day=? AND requests<?',
                    (used, self.ledger_name, 'confirmed-period', used))
                state['reported_limit'] = min(state.get('reported_limit', self.limit), limit, self.limit)
                state['reported_remaining'] = remaining
                if remaining == 0:
                    state['quota_exhausted'] = True
                if reset is not None:
                    state['reported_reset_at'] = now + reset
            if status == 429:
                retry = integer('retry-after', 86400)
                state['blocked_until'] = max(state.get('blocked_until', 0), now + max(60, retry or 3600))
            if status in (401, 403):
                state['blocked_until'] = max(state.get('blocked_until', 0), now + 3600)
            used = conn.execute('SELECT requests FROM provider_daily_usage WHERE provider=? AND day=?',
                                (self.ledger_name, 'confirmed-period')).fetchone()['requests']
            state.update(requests_reserved=used, reserve=self.reserve,
                checked_at=datetime.fromtimestamp(now, timezone.utc).isoformat(),
                last_http_status=status, state='blocked' if status >= 400 else 'observed',
                billing_period_renewal='requires_confirmation')
            if diagnostic:
                state['diagnostic_code'] = diagnostic
            conn.execute('INSERT INTO system_telemetry(key,val_json,updated_at) VALUES (?, ?, ?) '
                'ON CONFLICT(key) DO UPDATE SET val_json=excluded.val_json,updated_at=excluded.updated_at',
                (self.status_key, json.dumps(state), datetime.fromtimestamp(now, timezone.utc).isoformat()))

    def request(self, path, params, key, *, provider_api_key=None):
        if path not in self.routes:
            raise ValueError('RapidAPI endpoint has not been verified')
        if not isinstance(key, str) or not key or any(ord(c) < 32 or ord(c) > 126 for c in key):
            raise ValueError('A valid RapidAPI credential is required')
        if not isinstance(params, dict) or any(str(k).lower() in ('apikey', 'api_key', 'x-rapidapi-key') for k in params):
            raise ValueError('RapidAPI credentials belong only in headers')
        # Explicit diagnostic option: some gateways also require upstream auth.
        # Never infer interchangeability between the two credential types.
        query = dict(params)
        if provider_api_key is not None:
            if (not isinstance(provider_api_key, str) or not provider_api_key or
                    any(ord(c) < 32 or ord(c) > 126 for c in provider_api_key)):
                raise ValueError('An upstream credential is required')
            query['apiKey'] = provider_api_key
        with self.lock:
            wait = self.next_request - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.next_request = time.monotonic() + 3.7
            self._reserve()
            try:
                request = Request('https://' + self.host + self.routes[path] +
                    ('?' + urlencode(query) if query else ''), headers={
                    'Accept': 'application/json', 'X-RapidAPI-Key': key, 'X-RapidAPI-Host': self.host})
                with self.opener.open(request, timeout=10) as response:
                    self._reconcile(response.headers, response.status)
                    if any(k.lower() == 'x-rapidapi-mock-response' and str(v).lower() == 'true'
                           for k, v in response.headers.items()):
                        raise ValueError('Mock response cannot validate a provider')
                    raw = response.read(10_000_001)
                if len(raw) > 10_000_000:
                    raise ValueError('Response too large')
                return json.loads(raw)
            except HTTPError as exc:
                try:
                    body = exc.read(16384)
                except Exception:
                    body = b''
                diagnostic = http_diagnostic(exc.code, body)
                self._reconcile(exc.headers or {}, exc.code, diagnostic)
                error = ProviderNotAvailableError(f'RapidAPI HTTP {exc.code}; response omitted',
                                                 provider='oddspapi_rapidapi')
                error.http_status = exc.code
                error.diagnostic_code = diagnostic
                raise error from None
            except Exception:
                raise ProviderNotAvailableError('RapidAPI transport or payload failure; details omitted',
                                               provider='oddspapi_rapidapi') from None
