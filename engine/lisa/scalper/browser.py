"""Optional independent browser worker. Normal public pages supply the feeds.

Browser startup is lazy, retries/circuits survive restart, and each capture is
bounded. No private browser profile or bookmaker account is required or read.
"""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import random
import shutil
from urllib.parse import urlsplit
import uuid
from time import perf_counter

from ..odds_history import OddsHistoryRepository
from ..providers.base import ParseError
from .browser_sources import BROWSER_SOURCES, PUBLIC_HEADERS, response
from .contracts import bridge_contract, timestamp
from .repository import ScalperRepository, encoded
from .service import MAX_BYTES


class BrowserFailure(Exception):
    def __init__(self, error, *, retry_after=0):
        super().__init__(error)
        self.error, self.retry_after = error, retry_after


def retry_after(headers, now):
    value = headers.get('retry-after', '')
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = (parsedate_to_datetime(value) - now).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return 0
    import math
    return max(0., delay) if math.isfinite(delay) else 0


class BrowserCollector:
    """One Chromium process, bounded ephemeral contexts, no captured secrets."""
    def __init__(self, *, executable=None, headed=False, proxy=None, clock=None):
        self.executable, self.headed, self.proxy = executable, headed, proxy
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.playwright = self.browser = None
        self._start_lock = asyncio.Lock()

    async def start(self):
        async with self._start_lock:
            await self._start()

    async def _start(self):
        if self.browser is not None and self.browser.is_connected():
            return
        if self.playwright is None:
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                raise BrowserFailure('browser_dependency_missing') from None
            self.playwright = await async_playwright().start()
        kwargs = dict(headless=not self.headed)
        executable = self.executable or shutil.which('google-chrome') or shutil.which('chromium')
        if executable:
            kwargs['executable_path'] = executable
        if self.proxy:
            parts = urlsplit(self.proxy)
            if (parts.scheme not in ('http', 'https', 'socks5') or not parts.hostname
                    or parts.username or parts.password or parts.path not in ('', '/') or parts.query or parts.fragment):
                raise BrowserFailure('invalid_browser_proxy')
            kwargs['proxy'] = {'server': self.proxy}
        try:
            self.browser = await self.playwright.chromium.launch(**kwargs)
        except Exception:
            raise BrowserFailure('browser_launch_failed') from None

    async def close(self):
        try:
            if self.browser is not None:
                await self.browser.close()
        finally:
            self.browser = None
            if self.playwright is not None:
                await self.playwright.stop()
                self.playwright = None

    async def capture(self, source, *, seconds=30):
        await self.start()
        context = await self.browser.new_context(service_workers='block')
        page = await context.new_page()
        pending, responses, errors = set(), {}, []
        ready = asyncio.Event()

        async def receive(reply, role, received):
            try:
                headers = await reply.all_headers()
                if reply.status in (401, 403, 429):
                    errors.append(BrowserFailure('rate_limited' if reply.status == 429 else 'access_denied',
                                                 retry_after=retry_after(headers, received)))
                    ready.set()
                    return
                if reply.status != 200 or 'json' not in headers.get('content-type', '').lower():
                    return
                if int(headers.get('content-length', '0')) > MAX_BYTES:
                    raise ParseError('scalper: oversized browser response')
                body = await reply.body()
                if len(body) > MAX_BYTES:
                    raise ParseError('scalper: oversized browser response')
                raw = dict(received_at=received.isoformat(), payload=json.loads(body),
                           headers={key: headers[key] for key in PUBLIC_HEADERS if key in headers})
                if role not in responses or responses[role]['received_at'] <= raw['received_at']:
                    responses[role] = raw
                if all(role in responses for role in source.roles):
                    ready.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                errors.append(BrowserFailure('schema_rejected'))
                ready.set()

        def receive_handler(reply):
            role = source.role(reply.url)
            if role is None:
                return
            if len(pending) >= 8:
                errors.append(BrowserFailure('capture_overflow'))
                ready.set()
                return
            task = asyncio.create_task(receive(reply, role, self.clock()))
            pending.add(task)
            task.add_done_callback(pending.discard)

        async def observe():
            page.on('response', receive_handler)
            document = await page.goto(source.url, wait_until='domcontentloaded', timeout=int(seconds * 1000))
            if document is not None and document.status in (401, 403, 429):
                headers = await document.all_headers()
                raise BrowserFailure('rate_limited' if document.status == 429 else 'access_denied',
                                     retry_after=retry_after(headers, self.clock()))
            await ready.wait()
            if errors:
                raise errors[0]
            return dict(capture_version=1, responses={role: responses[role] for role in source.roles})

        try:
            return await asyncio.wait_for(observe(), timeout=seconds)
        except asyncio.TimeoutError:
            raise BrowserFailure('feed_not_observed') from None
        except BrowserFailure:
            raise
        except Exception:
            raise BrowserFailure('transport_failed') from None
        finally:
            page.remove_listener('response', receive_handler)
            for task in tuple(pending):
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await context.close()


class BrowserService:
    def __init__(self, storage, *, bookmakers=('sportybet', 'pinnacle'), leagues, collector=None,
                 capture_seconds=30, quote_max_age=300, clock=None):
        from ..providers.calendar import LEAGUES
        if (not bookmakers or len(bookmakers) != len(set(bookmakers))
                or any(book not in BROWSER_SOURCES for book in bookmakers)):
            raise ValueError('Choose distinct verified browser bookmakers')
        if not leagues or any(key not in LEAGUES for key in leagues):
            raise ValueError('Choose canonical league keys')
        if not 5 <= capture_seconds <= 45 or not 15 <= quote_max_age <= 3600:
            raise ValueError('Invalid browser budget or freshness allowance')
        self.storage, self.repository = storage, ScalperRepository(storage)
        self.sources = tuple(BROWSER_SOURCES[book]() for book in bookmakers)
        self.leagues, self.capture_seconds, self.quote_max_age = tuple(leagues), capture_seconds, quote_max_age
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.collector = collector or BrowserCollector(clock=self.clock)
        self.owner = uuid.uuid4().hex

    def accept(self, source, capture, *, now):
        if len(encoded(capture).encode()) > MAX_BYTES:
            raise ParseError('scalper: oversized combined capture')
        resource = 'browser:landing'
        batch = source.parse(capture, now=now)
        fixture_ids = {r['source_event_id'] for r in batch.fixtures if r['sport_key'] in self.leagues}
        batch = replace(batch, fixtures=tuple(r for r in batch.fixtures if r['source_event_id'] in fixture_ids),
                        quotes=tuple(q for q in batch.quotes if q['event_id'] in fixture_ids),
                        replace_books=tuple(b for b in batch.replace_books if b[0] in fixture_ids))
        previous = self.repository.resource(source.name, resource)
        old = None
        if previous and previous['payload']:
            try:
                old = source.parse(json.loads(previous['payload']), now=now)
            except (ParseError, ValueError, TypeError, KeyError, AttributeError):
                # An upgraded parser must not get stuck replaying obsolete raw
                # data. Unreplaced prior offers still expire under their clocks.
                pass
        if old:
            current_time = max(timestamp(r['received_at']) for r in capture['responses'].values())
            previous_capture = json.loads(previous['payload'])
            previous_time = max(timestamp(r['received_at']) for r in previous_capture['responses'].values())
            price_time = response(capture, source.roles[-1], now)[2]
            previous_price_time = response(previous_capture, source.roles[-1], now)[2]
            if (current_time < previous_time or (price_time and previous_price_time and price_time < previous_price_time)):
                return dict(source=source.name, state='out_of_order', fixtures=0, quotes=0, fresh_supported_quotes=0,
                            history_added=0, warnings=['An older publisher snapshot cannot replace newer offers.'])
            # Absence from the same refreshed page revokes previous page offers.
            # It does not mean the source's entire league has been enumerated.
            missing = [dict(r, observed_at=now.isoformat(), status='UNKNOWN', discovery_only=True,
                            calendar_confirmed_at=None) for r in old.fixtures
                       if r['sport_key'] in self.leagues and r['source_event_id'] not in fixture_ids]
            batch = replace(batch, fixtures=batch.fixtures + tuple(missing), replace_books=batch.replace_books +
                            tuple((r['source_event_id'], source.book) for r in missing))
        merge_started = perf_counter()
        self.repository.accept(batch, resource=resource, payload=capture, now=now, ttl=0)
        merge_ms = round((perf_counter() - merge_started) * 1000, 2)
        fresh = [q for q in batch.quotes if q['active'] and bridge_contract(q) and q.get('confirmed_at')
                 and -60 <= (now - timestamp(q['confirmed_at'])).total_seconds() <= self.quote_max_age]
        old_quotes = {(q['event_id'], q['market'], q['selection'], q['line'], q['period']): q for q in old.quotes} if old else {}
        record = []
        for q in fresh:
            earlier = old_quotes.get((q['event_id'], q['market'], q['selection'], q['line'], q['period']))
            # Record changes/reopenings and an hourly confirmation checkpoint.
            # Every unchanged poll must not duplicate the entire odds archive.
            if (earlier and earlier['active'] and earlier.get('confirmed_at')
                    and -60 <= (timestamp(earlier['observed_at']) - timestamp(earlier['confirmed_at'])).total_seconds() <= self.quote_max_age
                    and earlier['odds'] == q['odds'] and earlier['updated_at'] == q['updated_at']
                    and int(timestamp(earlier['observed_at']).timestamp() // 3600) == int(timestamp(q['observed_at']).timestamp() // 3600)):
                continue
            record.append(dict(source=source.name, event_id=source.name + ':' + q['event_id'],
                bookmaker=q['book_key'], observed_at=q['observed_at'], odds=q['odds'], market=q['market'],
                selection=q['selection'], line=q['line'], period=q['period'], confirmed_at=q['confirmed_at'],
                freshness_basis=q['freshness_basis'], provider_reported_update_at=q['updated_at'],
                publisher_market_key=q.get('publisher_market_key'), publisher_version=q.get('publisher_version')))
        history = OddsHistoryRepository(self.storage).ingest(record)
        # The immutable archive has its own bounded retention. Current snapshots
        # retain suspensions; their missing odds cannot become price observations.
        with self.storage._tx() as conn:
            conn.execute('DELETE FROM odds_observations WHERE source=? AND observed_at<?',
                         (source.name, datetime.fromtimestamp(now.timestamp() - 30 * 86400, timezone.utc).isoformat()))
        return dict(source=source.name, state='updated', fixtures=len(fixture_ids), quotes=len(batch.quotes),
                    fresh_supported_quotes=len(fresh), history_added=history, merge_ms=merge_ms,
                    warnings=list(batch.warnings))

    async def _collect(self, source):
        now = self.clock()
        if not set(source.leagues).intersection(self.leagues):
            return dict(source=source.name, state='outside_coverage')
        if self.repository.source_state(source.name)['next_attempt'] > now.timestamp():
            return dict(source=source.name, state='source_cooldown')
        lease = 'scalper:browser:' + source.book
        if not self.repository.claim(self.owner, now, self.capture_seconds + 30, name=lease):
            return dict(source=source.name, state='already_running')
        try:
            capture = await asyncio.wait_for(self.collector.capture(source, seconds=self.capture_seconds),
                                             timeout=self.capture_seconds + 10)
            return self.accept(source, capture, now=self.clock())
        except Exception as exc:
            error = getattr(exc, 'error', 'schema_rejected' if isinstance(exc, (ParseError, ValueError, TypeError, KeyError, AttributeError)) else 'worker_error')
            setup = error in ('browser_dependency_missing', 'browser_launch_failed', 'invalid_browser_proxy', 'worker_error')
            if not setup:
                failures = self.repository.source_state(source.name)['failures']
                delay = 21600 if error == 'access_denied' else 3600 if error == 'schema_rejected' else min(3600, 30 * 2**min(failures, 7)) * random.uniform(1, 1.2)
                delay = max(delay, getattr(exc, 'retry_after', 0))
                self.repository.failure(source.name, 'browser:landing', self.clock(), delay, error, host=True)
            return dict(source=source.name, state='failed', error=error)
        finally:
            self.repository.release(self.owner, name=lease)

    async def tick(self):
        # Two independently leased captures share Chromium startup. A slow or
        # blocked page cannot hold the other bookmaker's commit until timeout.
        outcomes = await asyncio.gather(*(self._collect(source) for source in self.sources))
        state = 'degraded' if any(r['state'] in ('failed', 'source_cooldown', 'out_of_order') for r in outcomes) else 'ok'
        status = dict(state=state, last_attempt=self.clock().isoformat(), outcomes=outcomes)
        self.storage.set_telemetry('scalper:browser:status', status)
        self.repository.prune(self.clock())
        return status
