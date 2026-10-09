"""Bookmaker capture regressions based on the verified public response shapes."""
import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lisa.board import Fixture
from lisa.providers.base import ParseError
from lisa.providers.scalper import ScalperProvider
from lisa.scalper.browser import BrowserCollector, BrowserFailure, BrowserService
from lisa.scalper.browser_sources import PinnacleBrowserSource, SportyBetBrowserSource, publisher_confirmation
from lisa.scalper.contracts import bridge_contract, normalized
from lisa.scalper.repository import ScalperRepository
from lisa.scalper.sources import JsonSource
from lisa.storage import SqliteStorage

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
LEAGUE = 'soccer_epl'


def capture(payload, *, role='events', now=NOW, age=0):
    return {'capture_version': 1, 'responses': {role: {'payload': payload,
        'received_at': now.isoformat(), 'headers': {'date': format_datetime(now, usegmt=True), 'age': str(age)}}}}


def sporty(*, now=NOW, changed=None, price='1.95', active=1, kickoff=None):
    event = {'eventId': 'sr:match:123', 'estimateStartTime': int((kickoff or NOW + timedelta(hours=2)).timestamp() * 1000),
        'homeTeamName': 'Arsenal', 'awayTeamName': 'Chelsea', 'status': 0, 'matchStatus': 'Not start',
        'sport': {'id': 'sr:sport:1', 'category': {'name': 'England', 'tournament': {'id': 'sr:tournament:17'}}},
        'markets': [{'id': '18', 'specifier': 'total=2.5', 'status': 0, 'banned': False,
            'lastOddsChangeTime': int((changed or NOW - timedelta(days=1)).timestamp() * 1000),
            'outcomes': [{'id': '12', 'odds': price, 'isActive': active}, {'id': '13', 'odds': '1.85', 'isActive': 1}]}]}
    return capture({'bizCode': 10000, 'data': {'totalNum': 1200, 'tournaments': [
        {'id': 'sr:tournament:17', 'categoryName': 'England', 'name': 'Premier League', 'events': [event]}]}}, now=now)


def pinnacle(*, now=NOW, age=0):
    event = {'id': 123, 'parentId': None, 'type': 'matchup', 'units': 'Regular',
        'startTime': (NOW + timedelta(hours=2)).isoformat(), 'status': 'pending', 'isLive': False, 'hasMarkets': True,
        'league': {'id': 1980, 'name': 'England - Premier League', 'sport': {'id': 29}},
        'participants': [{'alignment': 'home', 'name': 'Arsenal'}, {'alignment': 'away', 'name': 'Chelsea'}],
        'periods': [{'period': 0, 'status': 'open'}, {'period': 1, 'status': 'open'}]}
    markets = [{'matchupId': 123, 'key': 's;0;s;-0.25', 'period': 0, 'type': 'spread', 'version': 10,
        'status': 'open', 'cutoffAt': event['startTime'], 'prices': [
            {'designation': 'home', 'points': -.25, 'price': -110}, {'designation': 'away', 'points': .25, 'price': 100}]}]
    result = capture([event], now=now)
    result['responses'].update(capture(markets, role='markets', now=now, age=age)['responses'])
    return result


class BrowserSourceTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = str(Path(folder.name) / 'browser.db')
        self.store = SqliteStorage(self.path)
        self.addCleanup(self.store.close)
        self.service = BrowserService(self.store, leagues=(LEAGUE,))
        self.provider = ScalperProvider(leagues=(LEAGUE,), clock=lambda: NOW)
        self.provider.storage = self.store

    def save(self, raw=None, *, source=None, now=NOW):
        return self.service.accept(source or SportyBetBrowserSource(), raw or sporty(now=now), now=now)

    def test_http_cache_age_does_not_become_zero_when_download_finishes(self):
        self.assertEqual(publisher_confirmation({'date': format_datetime(NOW, usegmt=True), 'age': '683'}, NOW), NOW - timedelta(seconds=683))
        self.assertEqual(publisher_confirmation({'date': format_datetime(NOW - timedelta(seconds=100), usegmt=True), 'age': '20'}, NOW), NOW - timedelta(seconds=100))
        for headers in ({}, {'date': 'yesterday'}, {'date': format_datetime(NOW, usegmt=True), 'age': '-1'},
                        {'date': format_datetime(NOW, usegmt=True), 'age': 'nan'},
                        {'date': format_datetime(NOW + timedelta(minutes=2), usegmt=True)}):
            self.assertIsNone(publisher_confirmation(headers, NOW))

    def test_unchanged_offer_is_current_without_falsifying_its_last_change(self):
        result = self.save()
        self.assertEqual(result['fresh_supported_quotes'], 2)
        quote = self.provider.fetch(sport_keys=(LEAGUE,)).quotes[0]
        self.assertEqual(quote.updated_at, NOW - timedelta(days=1))
        self.assertEqual(quote.confirmed_at, NOW)
        self.assertEqual(quote.freshness_basis, 'publisher_snapshot')
        fixture = Fixture('calendar:123', LEAGUE, NOW + timedelta(hours=2), 'Arsenal', 'Chelsea')
        matched = self.provider.match((quote,), (fixture,))
        self.assertEqual(matched.matched_events, 1)
        price = matched.prices[fixture.match_id][0]
        self.assertEqual(price.confirmed_at, NOW)
        self.assertEqual(price.updated_at, NOW - timedelta(days=1))

    def test_cached_prices_are_research_even_when_local_capture_is_new(self):
        self.save(pinnacle(age=683), source=PinnacleBrowserSource())
        snapshot = self.provider.fetch(sport_keys=(LEAGUE,))
        self.assertEqual(snapshot.quotes, ())
        self.assertEqual(snapshot.dropped['stale_bookmaker_timestamp'], 2)

    def test_missing_confirmation_cannot_fall_back_to_recent_price_change(self):
        raw = sporty(changed=NOW)
        raw['responses']['events']['headers'] = {}
        self.save(raw)
        self.assertEqual(self.provider.fetch(sport_keys=(LEAGUE,)).quotes, ())
        self.assertEqual(self.provider.get_fixtures(LEAGUE).fixtures, ())

    def test_stale_calendar_cannot_publish_a_freshly_downloaded_fixture(self):
        raw = sporty()
        raw['responses']['events']['headers']['age'] = '1000'
        self.save(raw)
        self.assertEqual(self.provider.get_fixtures(LEAGUE).fixtures, ())

    def test_pinnacle_american_odds_and_selected_team_handicap_direction(self):
        batch = PinnacleBrowserSource().parse(pinnacle(), now=NOW)
        home, away = batch.quotes
        self.assertAlmostEqual(home['odds'], 1 + 100 / 110)
        self.assertEqual((home['selection'], home['line'], away['selection'], away['line']), ('Home', -.25, 'Away', .25))
        from lisa.contracts import MarketContract
        self.assertEqual(MarketContract('asian_handicap', 'Home', -.25).grade(0, 0), 'HALF_LOSS')
        self.assertEqual(MarketContract('asian_handicap', 'Away', .25).grade(0, 0), 'HALF_WIN')

    def test_first_half_is_stored_without_becoming_a_full_match_price(self):
        raw = pinnacle()
        raw['responses']['markets']['payload'][0]['period'] = 1
        batch = PinnacleBrowserSource().parse(raw, now=NOW)
        self.assertEqual(batch.quotes[0]['period'], 'first_half')
        self.assertFalse(bridge_contract(batch.quotes[0]))
        self.save(raw, source=PinnacleBrowserSource())
        self.assertEqual(self.provider.fetch(sport_keys=(LEAGUE,)).quotes, ())

    def test_virtual_and_parent_specials_cannot_become_goal_fixtures(self):
        raw = pinnacle()
        raw['responses']['events']['payload'][0]['type'] = 'special'
        batch = PinnacleBrowserSource().parse(raw, now=NOW)
        self.assertEqual((batch.fixtures, batch.quotes), ((), ()))
        raw = pinnacle()
        raw['responses']['events']['payload'][0]['units'] = 'Corners'
        with self.assertRaises(ParseError):
            PinnacleBrowserSource().parse(raw, now=NOW)

    def test_mixed_competitions_or_incomplete_lines_are_rejected(self):
        for change in ('competition', 'line', 'outcome', 'odds'):
            raw = pinnacle()
            if change == 'competition':
                raw['responses']['events']['payload'][0]['league']['sport']['id'] = 4
            else:
                prices = raw['responses']['markets']['payload'][0]['prices']
                if change == 'line':
                    prices[1]['points'] = .75
                elif change == 'outcome':
                    prices.pop()
                else:
                    prices[0]['price'] = 0
            with self.assertRaises(ParseError):
                PinnacleBrowserSource().parse(raw, now=NOW)

    def test_snapshot_parts_and_future_source_clocks_must_be_valid(self):
        raw = pinnacle()
        raw['responses']['events']['received_at'] = (NOW - timedelta(seconds=31)).isoformat()
        with self.assertRaises(ParseError):
            PinnacleBrowserSource().parse(raw, now=NOW)
        with self.assertRaises(ParseError):
            SportyBetBrowserSource().parse(sporty(changed=NOW + timedelta(minutes=2)), now=NOW)

    def test_suspension_and_disappearance_revoke_existing_prices(self):
        self.save()
        later = NOW + timedelta(seconds=10)
        self.save(sporty(now=later, active=0), now=later)
        quotes = self.provider.fetch(sport_keys=(LEAGUE,), now=later).quotes
        self.assertEqual([q.selection for q in quotes], ['Under'])
        raw = sporty(now=later + timedelta(seconds=10))
        raw['responses']['events']['payload']['data']['tournaments'] = []
        self.save(raw, now=later + timedelta(seconds=10))
        self.assertEqual(self.provider.fetch(sport_keys=(LEAGUE,), now=later + timedelta(seconds=10)).quotes, ())
        self.assertTrue(self.service.repository.fixtures((LEAGUE,), now=later, include_stale=True)[0]['discovery_only'])

    def test_older_capture_or_publisher_confirmation_cannot_revive_prices(self):
        self.save()
        later = NOW + timedelta(seconds=20)
        self.save(sporty(now=later, active=0), now=later)
        self.assertEqual(self.save(sporty(), now=later)['state'], 'out_of_order')
        stale = sporty(now=later + timedelta(seconds=10))
        stale['responses']['events']['headers']['age'] = '20'
        self.assertEqual(self.save(stale, now=later + timedelta(seconds=10))['state'], 'out_of_order')
        self.assertEqual([q.selection for q in self.provider.fetch(sport_keys=(LEAGUE,), now=later).quotes], ['Under'])

    def test_schema_error_leaves_the_last_valid_snapshot_intact(self):
        self.save()
        previous = self.service.repository.resource('sportybet_browser', 'browser:landing')['content_hash']
        raw = sporty(now=NOW + timedelta(seconds=20))
        raw['responses']['events']['payload']['data']['tournaments'][0]['events'][0]['markets'][0]['outcomes'].pop()
        with self.assertRaises(ParseError):
            self.save(raw, now=NOW + timedelta(seconds=20))
        self.assertEqual(self.service.repository.resource('sportybet_browser', 'browser:landing')['content_hash'], previous)

    def test_partial_page_count_is_not_fabricated_complete_coverage(self):
        result = self.save()
        self.assertEqual(result['fixtures'], 1)
        self.assertIn('partial', result['warnings'][0])

    def test_odds_history_records_changes_and_skips_unchanged_polls(self):
        # Immutable history rejects future observations against the real clock.
        observed = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
        # Keep the same upcoming event throughout the real-clock archive test.
        # A calendar-fixed kickoff expires and makes it test closed offers.
        def sample(now, price='1.95'):
            return sporty(now=now, changed=observed - timedelta(days=1), price=price,
                          kickoff=observed + timedelta(hours=2))
        self.assertEqual(self.save(sample(observed), now=observed)['history_added'], 2)
        self.assertEqual(self.save(sample(observed + timedelta(seconds=10)), now=observed + timedelta(seconds=10))['history_added'], 0)
        self.assertEqual(self.save(sample(observed + timedelta(seconds=20), price='2.00'), now=observed + timedelta(seconds=20))['history_added'], 1)
        self.assertEqual(self.save(sample(observed + timedelta(hours=1), price='2.00'), now=observed + timedelta(hours=1))['history_added'], 2)
        with self.store._tx() as conn:
            rows = conn.execute('SELECT payload FROM odds_observations').fetchall()
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(json.loads(row['payload'])['provider_reported_update_at'] != json.loads(row['payload'])['confirmed_at'] for row in rows))

    def test_generic_feeds_cannot_assert_browser_freshness(self):
        for name in ('pinnacle_browser', 'sportybet_browser'):
            with self.assertRaises(ValueError):
                JsonSource(name, 'https://example.test/feed')
        raw = {'schema_version': 1, 'collected_at': NOW.isoformat(), 'fixtures': [
            {'event_id': 'e1', 'sport_key': LEAGUE, 'kickoff': (NOW + timedelta(hours=2)).isoformat(),
             'home_team': 'Arsenal', 'away_team': 'Chelsea', 'status': 'SCHEDULED'}], 'quotes': [
            {'event_id': 'e1', 'book_key': 'bet365', 'market': 'h2h', 'selection': 'Home', 'odds': 1.95,
             'period': 'regulation', 'settlement_contract': 'regulation', 'active': True,
             'updated_at': None, 'confirmed_at': NOW.isoformat(), 'freshness_basis': 'publisher_snapshot'}]}
        batch = normalized(raw, 'owned_feed', now=NOW)
        self.assertNotIn('confirmed_at', batch.quotes[0])

    def test_browser_leases_do_not_block_statistics_worker(self):
        repo = self.service.repository
        self.assertTrue(repo.claim('main', NOW, 60))
        self.assertTrue(repo.claim('browser', NOW, 60, name='scalper:browser:sportybet'))
        self.assertFalse(repo.claim('other', NOW, 60, name='scalper:browser:sportybet'))
        repo.release('other', name='scalper:browser:sportybet')
        self.assertFalse(repo.claim('other', NOW, 60, name='scalper:browser:sportybet'))

    def test_disconnect_only_serves_recent_confirmations_for_thirty_seconds(self):
        self.save()
        self.service.repository.failure('sportybet_browser', 'browser:landing', NOW, 3600, 'transport_failed', host=True)
        self.assertEqual(len(self.provider.fetch(sport_keys=(LEAGUE,), now=NOW + timedelta(seconds=30)).quotes), 2)
        self.assertEqual(self.provider.fetch(sport_keys=(LEAGUE,), now=NOW + timedelta(seconds=31)).quotes, ())

    def test_normal_route_matching_ignores_unrelated_endpoints(self):
        source = PinnacleBrowserSource()
        self.assertEqual(source.role('https://guest.api.arcadia.pinnacle.com/0.1/leagues/1980/markets/straight'), 'markets')
        for url in ('https://evil.example/0.1/leagues/1980/markets/straight',
                    'https://guest.api.arcadia.pinnacle.com/0.1/leagues/1981/markets/straight',
                    'https://www.pinnacle.com/account/balance'):
            self.assertIsNone(source.role(url))

    def test_closed_market_with_no_prices_revokes_without_a_schema_circuit(self):
        for source, build, role, price_field in ((SportyBetBrowserSource(), sporty, 'events', 'outcomes'),
                                                (PinnacleBrowserSource(), pinnacle, 'markets', 'prices')):
            self.save(build(), source=source)
            later = NOW + timedelta(seconds=10)
            raw = build(now=later)
            market = (raw['responses'][role]['payload']['data']['tournaments'][0]['events'][0]['markets'][0]
                      if role == 'events' else raw['responses'][role]['payload'][0])
            market.update(status=1 if role == 'events' else 'closed')
            market.pop(price_field)
            result = self.save(raw, source=source, now=later)
            self.assertEqual(result['state'], 'updated')
            self.assertEqual(result['fresh_supported_quotes'], 0)
            self.assertEqual(self.service.repository.source_state(source.name)['next_attempt'], 0)



class FakeCollector:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    async def capture(self, source, *, seconds):
        self.calls.append(source.book)
        value = self.replies[source.book]
        if isinstance(value, Exception):
            raise value
        return value


class BrowserWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = SqliteStorage(str(Path(folder.name) / 'worker.db'))
        self.addCleanup(self.store.close)

    def worker(self, collector):
        return BrowserService(self.store, leagues=(LEAGUE,), collector=collector, clock=lambda: NOW)

    async def test_one_book_failure_does_not_stop_an_independent_book(self):
        worker = self.worker(FakeCollector({'sportybet': BrowserFailure('access_denied'), 'pinnacle': pinnacle()}))
        result = await worker.tick()
        self.assertEqual(result['state'], 'degraded')
        self.assertEqual(result['outcomes'][1]['fresh_supported_quotes'], 2)
        self.assertEqual(worker.repository.source_state('sportybet_browser')['next_attempt'], NOW.timestamp() + 21600)

    async def test_fast_book_commits_while_another_capture_is_waiting(self):
        release, committed = asyncio.Event(), asyncio.Event()
        class Collector:
            async def capture(self, source, *, seconds):
                if source.book == 'sportybet':
                    await release.wait()
                    raise BrowserFailure('feed_not_observed')
                return pinnacle()
        worker = self.worker(Collector())
        original = worker.accept
        def accept(source, payload, *, now):
            result = original(source, payload, now=now)
            committed.set()
            return result
        with patch.object(worker, 'accept', side_effect=accept):
            task = asyncio.create_task(worker.tick())
            try:
                await asyncio.wait_for(committed.wait(), timeout=2)
                self.assertFalse(task.done())
                self.assertIsNotNone(worker.repository.resource('pinnacle_browser', 'browser:landing'))
            finally:
                release.set()
                await task

    async def test_restart_honours_cooldown_and_long_retry_after(self):
        first = self.worker(FakeCollector({'sportybet': BrowserFailure('rate_limited', retry_after=7200), 'pinnacle': pinnacle()}))
        await first.tick()
        collector = FakeCollector({'pinnacle': pinnacle()})
        result = await self.worker(collector).tick()
        self.assertEqual(result['outcomes'][0]['state'], 'source_cooldown')
        self.assertEqual(collector.calls, ['pinnacle'])
        self.assertEqual(first.repository.source_state('sportybet_browser')['next_attempt'], NOW.timestamp() + 7200)

    async def test_database_failure_is_not_attributed_to_publisher(self):
        worker = self.worker(FakeCollector({'sportybet': sporty(), 'pinnacle': pinnacle()}))
        with patch.object(worker.repository, 'accept', side_effect=RuntimeError('database unavailable')):
            result = await worker.tick()
        self.assertTrue(all(r.get('error') == 'worker_error' for r in result['outcomes']))
        self.assertEqual(worker.repository.source_state('sportybet_browser')['next_attempt'], 0)
        self.assertTrue(worker.repository.claim('next', NOW, 60, name='scalper:browser:sportybet'))

    async def test_browser_setup_failure_does_not_open_source_circuits(self):
        result = await self.worker(FakeCollector({'sportybet': BrowserFailure('browser_dependency_missing'),
                                                'pinnacle': BrowserFailure('browser_launch_failed')})).tick()
        self.assertEqual(result['state'], 'degraded')
        self.assertEqual(ScalperRepository(self.store).source_state('pinnacle_browser')['next_attempt'], 0)

    async def test_passive_capture_keeps_only_public_payload_and_freshness_headers(self):
        raw = sporty()['responses']['events']['payload']
        class Reply:
            url = 'https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents?publicFilter=1'
            status = 200
            async def all_headers(self):
                return {'date': format_datetime(NOW, usegmt=True), 'content-type': 'application/json',
                        'set-cookie': 'PRIVATE_SESSION', 'authorization': 'PRIVATE_TOKEN'}
            async def body(self):
                return json.dumps(raw).encode()
        class Page:
            def on(self, name, handler):
                self.handler = handler
            def remove_listener(self, name, handler):
                self.handler = None
            async def goto(self, url, **kwargs):
                self.handler(Reply())
                return Reply()
        class Context:
            closed = False
            async def new_page(self):
                self.page = Page()
                return self.page
            async def close(self):
                self.closed = True
        context = Context()
        class Browser:
            async def new_context(self, **kwargs):
                return context
        class Collector(BrowserCollector):
            async def start(self):
                self.browser = Browser()
        result = await Collector(clock=lambda: NOW).capture(SportyBetBrowserSource(), seconds=5)
        self.assertEqual(result['responses']['events']['payload'], raw)
        self.assertNotIn('PRIVATE_', json.dumps(result))
        self.assertNotIn('publicFilter', json.dumps(result))
        self.assertTrue(context.closed)
        self.assertIsNone(context.page.handler)


if __name__ == '__main__':
    unittest.main()
