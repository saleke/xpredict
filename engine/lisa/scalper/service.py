"""Bounded collection with durable circuits, checkpoints and per-cycle lease."""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import random
import threading
import uuid
from time import perf_counter

from ..job_budget import JobDeadlineExceeded, execution_budget, check_budget
from ..match_history import HistoryRepository
from ..providers.base import HttpTransport, ParseError, _decompress
from .repository import ScalperRepository
from .sources import ESPN_LEAGUES, EspnSource, OpenFootballSource, SportyBetSource

MAX_BYTES = 3_000_000


class ScalperTransport(HttpTransport):
    def _single(self, parts, headers, state, host, *, method='GET', body=None):
        # Use a configured network proxy normally (corporate/local egress).
        # The base connection pool intentionally uses direct sockets, which
        # otherwise fails on hosts whose only route is HTTPS_PROXY.
        import urllib.error
        import urllib.request
        from ..job_budget import bounded_timeout
        proxies = urllib.request.getproxies()
        if not proxies or urllib.request.proxy_bypass(host):
            return super()._single(parts, headers, state, host, method=method, body=body)
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies), NoRedirect())
        request = urllib.request.Request(parts.geturl(), data=body, headers=headers, method=method)
        with state.lock:
            try:
                response = opener.open(request, timeout=bounded_timeout(self.timeout))
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                content = response.read(self.max_response_bytes + 1)
                if len(content) > self.max_response_bytes:
                    raise ParseError('scalper: oversized response')
                self.request_count += 1
                return response.code, {k.lower(): v for k, v in response.headers.items()}, content

    @staticmethod
    def _error_for(status, headers, body, provider):
        error = HttpTransport._error_for(status, headers, body, provider)
        # Preserve Retry-After rather than the generic transport's 300s cap.
        value = headers.get('retry-after')
        if value:
            try:
                delay = float(value)
            except ValueError:
                try:
                    delay = parsedate_to_datetime(value).timestamp() - datetime.now(timezone.utc).timestamp()
                except (TypeError, ValueError, OverflowError):
                    delay = 0
            import math
            if math.isfinite(delay) and delay > 0:
                error.retry_after = delay
        return error


class ScalperService:
    def __init__(self, storage, *, leagues, espn=True, openfootball=True, sportybet=False, feeds=(),
                 request_limit=24, summary_limit=4, history_days=90,
                 cycle_seconds=45, transport=None, clock=None):
        from ..providers.calendar import LEAGUES
        if not leagues or any(key not in LEAGUES for key in leagues):
            raise ValueError('Scalper needs known league keys')
        if not 1 <= request_limit <= 200 or not 0 <= summary_limit <= 100 or not 0 <= history_days <= 1464:
            raise ValueError('Invalid Scalper request, enrichment or history limit')
        if not 5 <= cycle_seconds <= 300:
            raise ValueError('Scalper cycle budget must be 5–300 seconds')
        self.repository = ScalperRepository(storage)
        self.storage = storage
        self.leagues = tuple(dict.fromkeys(leagues))
        self.espn = EspnSource() if espn else None
        self.openfootball = OpenFootballSource() if openfootball else None
        self.sportybet = SportyBetSource() if sportybet else None
        self.feeds = tuple(feeds)
        if len({f.name for f in self.feeds}) != len(self.feeds):
            raise ValueError('Custom source names must be unique')
        self.request_limit, self.summary_limit, self.history_days = request_limit, summary_limit, history_days
        self.cycle_seconds = cycle_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.transport = transport or ScalperTransport(timeout=10, max_retries=0,
            time_budget=12, max_redirects=0, max_response_bytes=MAX_BYTES, default_rate_per_sec=.5)
        self.owner = uuid.uuid4().hex
        self._lock = threading.Lock()
        self.requests = 0

    def request(self, source, resource, url, parser, *, ttl, now):
        """Last good state is committed only AFTER schema validation."""
        check_budget()
        cached = self.repository.resource(source, resource)
        if self.repository.source_state(source)['next_attempt'] > now.timestamp():
            return {'source': source, 'resource': resource, 'state': 'source_cooldown'}
        if cached and cached['next_attempt'] > now.timestamp():
            return {'source': source, 'resource': resource, 'state': 'resource_cooldown'}
        if cached and cached['payload'] and cached['parser_version'] == 1 and cached['expires_at'] > now.timestamp():
            return {'source': source, 'resource': resource, 'state': 'cached'}
        if self.requests >= self.request_limit:
            return {'source': source, 'resource': resource, 'state': 'request_budget'}
        headers = {'User-Agent': self.transport.user_agent, 'Accept': 'application/json',
                   'Accept-Encoding': 'gzip, deflate'}
        if cached and cached['payload']:
            if cached['etag']:
                headers['If-None-Match'] = cached['etag']
            if cached['last_modified']:
                headers['If-Modified-Since'] = cached['last_modified']
        self.requests += 1
        try:
            status, response_headers, body = self.transport._request_with_retries(url, headers, provider=source)
            if status == 304:
                if not cached or not cached['payload']:
                    raise ParseError('scalper: 304 without cached resource', provider=source)
                payload = json.loads(cached['payload'])
                etag = response_headers.get('etag', cached['etag'])
                modified = response_headers.get('last-modified', cached['last_modified'])
            elif status == 200:
                raw = _decompress(body, response_headers.get('content-encoding', ''), max_bytes=MAX_BYTES)
                if len(raw) > MAX_BYTES:
                    raise ParseError('scalper: oversized resource', provider=source)
                payload = json.loads(raw)
                etag, modified = response_headers.get('etag'), response_headers.get('last-modified')
            else:
                raise self.transport._error_for(status, response_headers, body, source)
            batch = parser(payload, now=now)
        except JobDeadlineExceeded:
            raise
        except Exception as exc:
            return self._request_failure(source, resource, cached, now, exc)
        try:
            merge_started = perf_counter()
            accepted = self.repository.accept(batch, resource=resource, payload=payload, now=now,
                ttl=ttl(batch) if callable(ttl) else ttl, etag=etag, last_modified=modified)
            merge_ms = round((perf_counter() - merge_started) * 1000, 2)
        except ParseError as exc:
            return self._request_failure(source, resource, cached, now, exc)
        # Database failures belong to the worker, not a publisher's circuit.
        HistoryRepository(self.storage).ingest(accepted, observed_at=now)
        return dict(source=source, resource=resource, state='updated', fixtures=len(batch.fixtures),
                    quotes=len(batch.quotes), merge_ms=merge_ms,
                    warnings=list(batch.warnings))

    def _request_failure(self, source, resource, cached, now, exc):
        status = getattr(exc, 'http_status', None)
        failures = max((cached or {}).get('failures', 0), self.repository.source_state(source)['failures'])
        error = ('access_denied' if status in (401, 403) else 'rate_limited' if status == 429 else
                 'schema_rejected' if isinstance(exc, (ParseError, ValueError, TypeError, KeyError, AttributeError)) else
                 'http_' + str(status) if status else 'transport_failed')
        delay = (21600 if error == 'access_denied' else 3600 if error == 'schema_rejected' else
                 min(3600, 30 * 2 ** min(failures, 7)) * random.uniform(1, 1.2))
        delay = max(delay, getattr(exc, 'retry_after', None) or 0)
        self.repository.failure(source, resource, now, delay, error,
            host=error in ('access_denied', 'rate_limited', 'transport_failed') or bool(status and status >= 500))
        return dict(source=source, resource=resource, state='failed', error=error, retry_in_sec=round(delay))

    def _calendar(self, now):
        tasks = []
        rows = self.repository.fixtures(self.leagues, now=now, include_stale=True)
        live = {(r['sport_key'], datetime.fromtimestamp(r['epoch'], timezone.utc).date())
                for r in rows if r['status'] in ('LIVE', 'HT')}
        for league in self.leagues:
            if league not in self.espn.leagues:
                continue
            today = now.date()
            # Soccer's inspected endpoint rejects date ranges (HTTP 400).
            # Each successful daily response contains ALL fixtures for that
            # league/day. Known season calendars prune irrelevant future days.
            today_resource = self.repository.resource(self.espn.name, f'calendar:{league}:{today}')
            calendar = None
            if today_resource and today_resource['payload']:
                metadata = json.loads(today_resource['payload'])
                values = next((v.get('calendar') for v in metadata.get('leagues', [])
                               if v.get('slug') == ESPN_LEAGUES[league]), None)
                if isinstance(values, list):
                    try:
                        from .contracts import timestamp
                        parsed = {timestamp(v).date() for v in values}
                        if today in parsed or any(today < day <= today + timedelta(days=7) for day in parsed):
                            calendar = parsed
                    except ParseError:
                        pass
            for offset in (0, -1, -2, 1, 2, 3, 4, 5, 6, 7):
                day = today + timedelta(days=offset)
                if offset > 0 and calendar is not None and day not in calendar:
                    continue
                resource = f'calendar:{league}:{day}'
                previous = self.repository.resource(self.espn.name, resource)
                active = (league, day) in live
                if previous and previous['expires_at'] > now.timestamp():
                    continue
                priority = 0 if active else 1 if offset == 0 else 2
                tasks.append((priority, (previous or {}).get('fetched_at', 0), abs(offset), league, day,
                              resource, 60 if active else 300 if offset <= 0 else 900))
        results = []
        for _, _, _, league, day, resource, ttl in sorted(tasks):
            check_budget()
            if self.requests >= self.request_limit:
                break
            results.append(self.request(self.espn.name, resource, self.espn.scoreboard_url(league, day),
                lambda p, now, league=league: self.espn.parse_scoreboard(p, league, now=now),
                ttl=lambda batch, ttl=ttl: ttl if batch.fixtures else 1800, now=now))
            if self.repository.source_state(self.espn.name)['next_attempt'] > now.timestamp():
                break
        return results

    def _summaries(self, now):
        if not self.summary_limit:
            return []
        rows = self.repository.fixtures(self.leagues, now=now, include_stale=True)
        eligible = [r for r in rows if r['provider'] == self.espn.name and
                    (r['status'] in ('LIVE', 'HT') or r.get('completed'))]
        eligible.sort(key=lambda r: (r['status'] not in ('LIVE', 'HT'),
            bool(r.get('statistics_observed_at')), -r['epoch']))
        results = []
        for row in eligible:
            if len(results) >= self.summary_limit or self.requests >= self.request_limit:
                break
            eid, league = row['source_event_id'], row['sport_key']
            key = f'summary:{league}:{eid}'
            previous = self.repository.resource(self.espn.name, key)
            # Completed summaries receive a later correction pass, then monthly
            # refresh; live summaries stay short-lived.
            live = row['status'] in ('LIVE', 'HT')
            ttl = 60 if live else 3600 if now.timestamp() - row['epoch'] < 86400 else 30 * 86400
            if previous and previous['expires_at'] > now.timestamp():
                continue
            result = self.request(self.espn.name, key, self.espn.summary_url(league, eid),
                lambda p, now, league=league, eid=eid: self.espn.parse_summary(p, league, eid, now=now), ttl=ttl, now=now)
            results.append(result)
        return results

    def _history(self, now):
        if not self.history_days:
            return []
        supported = [k for k in self.leagues if k in self.espn.leagues]
        if not supported:
            return []
        pointer = int(self.storage.get_telemetry('scalper:history_league') or 0) % len(supported)
        league = supported[pointer]
        self.storage.set_telemetry('scalper:history_league', pointer + 1)
        key = 'scalper:history_cursor:' + league
        cursor = self.storage.get_telemetry(key)
        day = datetime.fromisoformat(cursor).date() if cursor else now.date() - timedelta(days=3)
        if day < now.date() - timedelta(days=self.history_days):
            return []
        resource = f'calendar:{league}:{day}'
        result = self.request(self.espn.name, resource, self.espn.scoreboard_url(league, day),
            lambda p, now: self.espn.parse_scoreboard(p, league, now=now), ttl=30 * 86400, now=now)
        if result['state'] in ('updated', 'cached'):
            self.storage.set_telemetry(key, (day - timedelta(days=1)).isoformat())
        return [result]

    def _bulk_history(self, now):
        if not self.openfootball:
            return []
        current = now.year if now.month >= 7 else now.year - 1
        tasks = []
        for league in self.leagues:
            if league not in self.openfootball.leagues:
                continue
            for year in (current, current - 1):
                resource = f'season:{league}:{year}'
                previous = self.repository.resource(self.openfootball.name, resource)
                if previous and previous['expires_at'] > now.timestamp():
                    continue
                if previous and previous['next_attempt'] > now.timestamp():
                    continue
                tasks.append(((previous or {}).get('fetched_at', 0), -year, league, resource))
        if not tasks:
            return []
        # One bulk file per cycle: bounded cold start and fair rotation by age.
        _, negative_year, league, resource = min(tasks)
        year = -negative_year
        result = self.request(self.openfootball.name, resource, self.openfootball.url(league, year, now=now),
            lambda p, now: self.openfootball.parse(p, league, year, now=now),
            ttl=86400 if year == current else 30 * 86400, now=now)
        return [result]

    def tick(self, *, now=None):
        now = now or self.clock()
        if not self._lock.acquire(blocking=False):
            return {'state': 'busy'}
        claimed = False
        outcomes = []
        self.requests = 0
        try:
            claimed = self.repository.claim(self.owner, now, self.cycle_seconds + 30)
            if not claimed:
                return {'state': 'another_worker'}
            with execution_budget(seconds=self.cycle_seconds):
                # Reserve a share for summaries/history. Calendars still use
                # spare requests when other sources have no work.
                full_limit = self.request_limit
                try:
                    self.request_limit = max(1, full_limit - min(self.summary_limit, full_limit // 4) -
                        (1 if self.history_days and full_limit > 2 else 0) - len(self.feeds) - bool(self.sportybet)
                        - bool(self.openfootball))
                    if self.espn:
                        try:
                            with execution_budget(seconds=max(3, self.cycle_seconds * .6)):
                                outcomes.extend(self._calendar(now))
                        except JobDeadlineExceeded:
                            outcomes.append({'source': 'espn', 'state': 'calendar_budget'})
                finally:
                    self.request_limit = full_limit
                # A slow healthy source cannot monopolize the first slot on
                # every restart or cycle; rotate the explicit feed order.
                pointer = int(self.storage.get_telemetry('scalper:feed_cursor') or 0) % max(1, len(self.feeds))
                self.storage.set_telemetry('scalper:feed_cursor', pointer + 1)
                for source in self.feeds[pointer:] + self.feeds[:pointer]:
                    outcomes.append(self.request(source.name, 'feed', source.url, source.parse, ttl=60, now=now))
                outcomes.extend(self._bulk_history(now))
                if self.sportybet:
                    # One bounded page; truncation is visible rather than claimed
                    # as league completeness. No repeated pages after a block.
                    result = self.request(self.sportybet.name, 'upcoming:1', self.sportybet.url + '&pageNum=1',
                        self.sportybet.parse, ttl=120, now=now)
                    result['coverage'] = 'first page only; full coverage unverified'
                    outcomes.append(result)
                if self.espn:
                    outcomes.extend(self._summaries(now))
                    outcomes.extend(self._history(now))
                self.repository.prune(now)
            failures = [r for r in outcomes if r['state'] in ('failed', 'source_cooldown', 'resource_cooldown')]
            partial = any(r['state'] in ('calendar_budget', 'request_budget') for r in outcomes)
            status = dict(state='degraded' if failures else 'partial' if partial else 'ok', last_attempt=now.isoformat(),
                          requests=self.requests, outcomes=outcomes)
        except JobDeadlineExceeded:
            status = dict(state='partial', last_attempt=now.isoformat(), requests=self.requests,
                          outcomes=outcomes, error='cycle_budget_exhausted')
        except Exception as exc:
            status = dict(state='failed', last_attempt=now.isoformat(), requests=self.requests,
                          outcomes=outcomes, error=type(exc).__name__)
        finally:
            try:
                if claimed:
                    self.repository.release(self.owner)
            finally:
                self._lock.release()
        self.storage.set_telemetry('scalper:status', status)
        return status

    def ingest(self, payload, source, *, now=None):
        from .contracts import normalized
        now = now or self.clock()
        from .repository import encoded
        if len(encoded(payload).encode()) > MAX_BYTES:
            raise ParseError('scalper: oversized interchange')
        batch = normalized(payload, source, now=now)
        accepted = self.repository.accept(batch, resource='import', payload=payload, now=now, ttl=0)
        HistoryRepository(self.storage).ingest(accepted, observed_at=now)
        return {'source': source, 'fixtures': len(batch.fixtures), 'quotes': len(batch.quotes)}
