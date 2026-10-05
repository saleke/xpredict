"""Supply integrity and restart regressions using representative source shapes."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lisa import config, feed
from lisa.board import Fixture, Opportunity, OpportunityBoard
from lisa.data_quality import reconcile_results
from lisa.match_history import HistoryRepository
from lisa.providers.base import FixturesResult, ParseError, RateLimitedError
from lisa.providers.scalper import ScalperProvider
from lisa.scalper.contracts import normalized
from lisa.scalper.repository import ScalperRepository
from lisa.scalper.service import ScalperService, ScalperTransport
from lisa.scalper.sources import EspnSource, JsonSource, SportyBetSource
from lisa.storage import SqliteStorage

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
LEAGUE = 'soccer_epl'


def interchange(*, now=NOW, eid='e1', source='local_book', quotes=True):
    event = dict(event_id=eid, sport_key=LEAGUE, kickoff=(NOW + timedelta(hours=2)).isoformat(),
                 home_team='Arsenal', away_team='Chelsea', status='SCHEDULED')
    quote = dict(event_id=eid, book_key='bet365', book_title='Bet365', market='totals',
                 selection='Over', line=2.5, period='regulation', settlement_contract='regulation',
                 active=True, odds=1.95, updated_at=now.isoformat())
    return dict(schema_version=1, collected_at=now.isoformat(), fixtures=[event],
                quotes=[quote] if quotes else [], replace_books=[dict(event_id=eid, book_key='bet365')])


def scoreboard(*, final=False, extra=False):
    people = []
    for side, name, score, team_id in [('home', 'Arsenal', '2', '1'), ('away', 'Chelsea', '1', '2')]:
        people.append(dict(homeAway=side, team={'id': team_id, 'displayName': name}, score=score,
            statistics=[{'name': 'wonCorners', 'displayValue': '0' if side == 'home' else '4'}]))
    comp = dict(id='123', date=(NOW - timedelta(days=1) if final else NOW + timedelta(hours=2)).isoformat(),
                timeValid=True, competitors=people, status={'type': dict(
                    name='STATUS_FINAL_AET' if extra else 'STATUS_FULL_TIME' if final else 'STATUS_SCHEDULED',
                    state='post' if final else 'pre', completed=final), 'period': 4 if extra else 2 if final else 0})
    return {'leagues': [{'slug': 'eng.1'}], 'events': [{'id': '123', 'competitions': [comp]}]}


class FakeTransport(ScalperTransport):
    def __init__(self, response=None):
        super().__init__(max_retries=0)
        self.response = response or interchange()
        self.calls = []
        self.status = 200
        self.headers = {'etag': 'version1'}

    def _request_with_retries(self, url, headers, *, provider):
        self.calls.append((url, dict(headers)))
        if isinstance(self.response, Exception):
            raise self.response
        return self.status, self.headers, json.dumps(self.response).encode()


class ScalperTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = str(Path(folder.name) / 'scalper.db')
        self.store = SqliteStorage(self.path)
        self.repo = ScalperRepository(self.store)
        self.transport = FakeTransport()
        self.service = ScalperService(self.store, leagues=(LEAGUE,), espn=False,
                                     transport=self.transport, clock=lambda: NOW)
        self.provider = ScalperProvider(leagues=(LEAGUE,), clock=lambda: NOW)
        self.provider.storage = self.store

    def save(self, payload=None, source='local_book', now=NOW):
        payload = payload or interchange(now=now)
        batch = normalized(payload, source, now=now)
        self.repo.accept(batch, resource='feed', payload=payload, now=now, ttl=60)
        return batch

    def request(self, *, now=NOW, parser=None):
        return self.service.request('local_book', 'feed', 'https://example.test/feed',
            parser or (lambda p, now: normalized(p, 'local_book', now=now)), ttl=60, now=now)

    def test_real_zero_corners_are_retained_missing_statistics_stay_missing(self):
        rows = EspnSource().parse_scoreboard(scoreboard(final=True), LEAGUE, now=NOW).fixtures
        self.assertEqual((rows[0]['home_corners'], rows[0]['away_corners']), (0, 4))
        self.assertNotIn('home_yellow_cards', rows[0])
        self.assertTrue(rows[0]['completed'])

    def test_summary_preserves_half_scores_and_maps_boxscore_by_team_id(self):
        raw = scoreboard(final=True)['events'][0]
        for c in raw['competitions'][0]['competitors']:
            c['linescores'] = [{'displayValue': '1'}, {'displayValue': '1' if c['homeAway'] == 'home' else '0'}]
        payload = {'header': raw, 'boxscore': {'teams': [
            {'team': {'id': '2'}, 'statistics': [{'name': 'yellowCards', 'displayValue': '0'}]},
            {'team': {'id': '1'}, 'statistics': [{'name': 'yellowCards', 'displayValue': '3'}]}]}}
        row = EspnSource().parse_summary(payload, LEAGUE, '123', now=NOW).fixtures[0]
        self.assertEqual((row['home_first_half_score'], row['away_first_half_score']), (1, 1))
        self.assertEqual((row['home_yellow_cards'], row['away_yellow_cards']), (3, 0))

    def test_extra_time_without_periods_cannot_train_or_settle(self):
        row = EspnSource().parse_scoreboard(scoreboard(final=True, extra=True), LEAGUE, now=NOW).fixtures[0]
        self.assertFalse(row['completed'])
        self.assertFalse(row['settlement_eligible'])
        self.assertNotIn('home_corners', row)

    def test_explicit_regulation_periods_exclude_extra_time_scores_and_corners(self):
        raw = scoreboard(final=True, extra=True)
        for c in raw['events'][0]['competitions'][0]['competitors']:
            c['score'] = '5'
            c['linescores'] = [{'displayValue': '1'}, {'displayValue': '0'}, {'displayValue': '2'}, {'displayValue': '2'}]
        row = EspnSource().parse_scoreboard(raw, LEAGUE, now=NOW).fixtures[0]
        self.assertTrue(row['completed'])
        self.assertEqual((row['home_score'], row['away_score']), (1, 1))
        self.assertNotIn('home_corners', row)

    def test_empty_source_is_distinct_from_schema_failure(self):
        source = EspnSource()
        raw = scoreboard()
        raw['events'] = []
        self.assertEqual(source.parse_scoreboard(raw, LEAGUE, now=NOW).fixtures, ())
        for changed in ({}, {'events': [], 'leagues': [{'slug': 'ger.1'}]}):
            with self.assertRaises(ParseError):
                source.parse_scoreboard(changed, LEAGUE, now=NOW)

    def test_unknown_time_and_status_are_discovery_only(self):
        raw = scoreboard()
        raw['events'][0]['competitions'][0]['timeValid'] = False
        row = EspnSource().parse_scoreboard(raw, LEAGUE, now=NOW).fixtures[0]
        self.assertEqual(feed.to_fixtures([row]), [])

    def test_restart_uses_durable_cache_without_network(self):
        self.assertEqual(self.request()['state'], 'updated')
        other = ScalperService(SqliteStorage(self.path), leagues=(LEAGUE,), espn=False,
                               transport=FakeTransport())
        result = other.request('local_book', 'feed', 'https://example.test/feed',
            lambda p, now: normalized(p, 'local_book', now=now), ttl=60, now=NOW + timedelta(seconds=5))
        self.assertEqual(result['state'], 'cached')
        self.assertEqual(other.transport.calls, [])

    def test_expired_resource_uses_conditional_304_but_not_fresh_quote_time(self):
        self.request()
        self.transport.status = 304
        later = NOW + timedelta(minutes=6)
        self.request(now=later)
        self.assertEqual(self.transport.calls[-1][1]['If-None-Match'], 'version1')
        self.assertEqual(self.provider.fetch(sport_keys=[LEAGUE], now=later).quotes, ())
        self.assertEqual(self.repo.resource('local_book', 'feed')['fetched_at'], later.timestamp())

    def test_schema_failure_preserves_old_snapshot_and_opens_resource_cooldown(self):
        self.request()
        old = self.repo.resource('local_book', 'feed')['content_hash']
        self.transport.response = {}
        result = self.request(now=NOW + timedelta(seconds=61))
        self.assertEqual(result['error'], 'schema_rejected')
        self.assertEqual(self.repo.resource('local_book', 'feed')['content_hash'], old)
        self.assertEqual(self.request(now=NOW + timedelta(seconds=62))['state'], 'resource_cooldown')

    def test_blocked_host_does_not_retry_after_restart(self):
        self.transport.status = 403
        self.assertEqual(self.request()['error'], 'access_denied')
        other = ScalperService(SqliteStorage(self.path), leagues=(LEAGUE,), espn=False, transport=FakeTransport())
        result = other.request('local_book', 'other', 'https://example.test/other',
            lambda p, now: normalized(p, 'local_book', now=now), ttl=60, now=NOW + timedelta(hours=1))
        self.assertEqual(result['state'], 'source_cooldown')
        self.assertEqual(other.transport.calls, [])

    def test_long_retry_after_is_honoured_and_other_source_remains_usable(self):
        self.transport.status = 429
        self.transport.headers = {'retry-after': '7200'}
        self.assertGreaterEqual(self.request()['retry_in_sec'], 7200)
        self.transport.status = 200
        result = self.service.request('other_book', 'feed', 'https://example.test/feed',
            lambda p, now: normalized(p, 'other_book', now=now), ttl=60, now=NOW)
        self.assertEqual(result['state'], 'updated')

    def test_empty_quote_replacement_revokes_old_price_and_survives_old_replay(self):
        original = interchange()
        self.save(original)
        self.assertEqual(len(self.provider.fetch(sport_keys=[LEAGUE]).quotes), 1)
        self.save(interchange(now=NOW + timedelta(seconds=1), quotes=False), now=NOW + timedelta(seconds=1))
        self.save(original, now=NOW + timedelta(seconds=2))
        self.assertEqual(self.provider.fetch(sport_keys=[LEAGUE]).quotes, ())

    def test_suspended_quote_revokes_active_price(self):
        self.save()
        payload = interchange(now=NOW + timedelta(seconds=1))
        payload['quotes'][0].update(active=False, odds=None)
        self.save(payload, now=NOW + timedelta(seconds=1))
        self.assertEqual(self.provider.fetch(sport_keys=[LEAGUE]).dropped, {'suspended': 1})

    def test_first_half_and_unknown_contracts_do_not_cross_price_bridge(self):
        payload = interchange()
        payload['quotes'][0]['period'] = 'first_half'
        self.save(payload)
        self.assertEqual(self.provider.fetch(sport_keys=[LEAGUE]).dropped, {'unsupported_period_or_contract': 1})

    def test_missing_price_timestamp_never_substitutes_collection_time(self):
        payload = interchange()
        payload['quotes'][0]['updated_at'] = None
        self.save(payload)
        self.assertEqual(self.provider.fetch(sport_keys=[LEAGUE]).dropped, {'missing_bookmaker_timestamp': 1})

    def test_stale_schedule_rejected_but_final_history_retained(self):
        self.save()
        payload = interchange(eid='finished', quotes=False)
        payload['fixtures'][0].update(kickoff=(NOW - timedelta(days=1)).isoformat(), status='FINISHED', home_score=0, away_score=0, score_period='regulation')
        self.save(payload)
        later = NOW + timedelta(hours=1)
        rows = self.repo.fixtures([LEAGUE], now=later, max_age=900)
        self.assertEqual([r['source_event_id'] for r in rows], ['finished'])

    def test_delayed_live_update_does_not_erase_final(self):
        payload = interchange(quotes=False)
        payload['fixtures'][0].update(kickoff=(NOW - timedelta(days=1)).isoformat(), status='FINISHED', home_score=1, away_score=0, score_period='regulation')
        self.save(payload)
        payload['collected_at'] = (NOW + timedelta(seconds=1)).isoformat()
        payload['fixtures'][0]['status'] = 'LIVE'
        self.save(payload, now=NOW + timedelta(seconds=1))
        self.assertTrue(self.repo.fixtures([LEAGUE], now=NOW)[0]['completed'])

    def test_reschedule_keeps_source_event_id_and_moves_kickoff(self):
        self.save()
        payload = interchange(now=NOW + timedelta(seconds=1))
        payload['fixtures'][0]['kickoff'] = (NOW + timedelta(days=1)).isoformat()
        self.save(payload, now=NOW + timedelta(seconds=1))
        rows = self.repo.fixtures([LEAGUE], now=NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['match_id'], 'local_book:e1')
        self.assertEqual(rows[0]['epoch'], (NOW + timedelta(days=1)).timestamp())

    def test_two_collectors_and_all_books_join_without_false_ambiguity(self):
        self.save()
        payload = interchange()
        payload['quotes'][0]['book_key'] = 'bet9ja'
        payload['replace_books'][0]['book_key'] = 'bet9ja'
        self.save(payload, source='another_book')
        snapshot = self.provider.fetch(sport_keys=[LEAGUE])
        fixture = Fixture('calendar:1', LEAGUE, NOW + timedelta(hours=2), 'Arsenal FC', 'Chelsea')
        result = self.provider.match(snapshot.quotes, [fixture])
        self.assertEqual(len(result.prices[fixture.match_id]), 2)
        self.assertEqual(result.ambiguous_events, 0)

    def test_wrong_league_reversed_sides_and_rematches_are_not_guessed(self):
        self.save()
        quotes = self.provider.fetch(sport_keys=[LEAGUE]).quotes
        fixture = Fixture('calendar:1', LEAGUE, NOW + timedelta(hours=2), 'Arsenal', 'Chelsea')
        for wrong in (replace(fixture, sport_key='soccer_uefa_champions_league'),
                      replace(fixture, home='Chelsea', away='Arsenal'),
                      replace(fixture, home='Arsenal Women')):
            self.assertEqual(self.provider.match(quotes, [wrong]).prices, {})
        result = self.provider.match(quotes, [fixture, replace(fixture, match_id='rematch')])
        self.assertEqual(result.prices, {})
        self.assertEqual(result.ambiguous_events, 1)

    def test_conflicting_final_sources_withheld(self):
        payload = interchange(quotes=False)
        payload['fixtures'][0].update(kickoff=(NOW - timedelta(days=1)).isoformat(), status='FINISHED', home_score=1, away_score=0, score_period='regulation')
        self.save(payload)
        payload['fixtures'][0]['home_score'] = 2
        self.save(payload, source='another_book')
        self.assertEqual(self.provider.get_fixtures(LEAGUE).fixtures, ())

    def test_duplicate_collection_lease_excludes_another_worker(self):
        self.assertTrue(self.repo.claim('worker1', NOW, 60))
        self.assertFalse(self.repo.claim('worker2', NOW, 60))
        self.assertTrue(self.repo.claim('worker2', NOW + timedelta(seconds=61), 60))
        self.repo.release('worker1')
        self.assertFalse(self.repo.claim('worker3', NOW + timedelta(seconds=61), 60))

    def test_only_mode_constructs_no_remote_provider(self):
        settings = config.Settings(scalper_mode='only', board_leagues=(LEAGUE,), football_data_token='artificial-key')
        providers = feed.build_providers(settings)
        self.assertEqual([p.name for p in providers.calendar], ['scalper'])
        self.assertEqual([p.name for p in providers.prices], ['scalper'])

    def test_history_only_scalper_does_not_block_calendar_fallback(self):
        payload = interchange(quotes=False)
        payload['fixtures'][0].update(kickoff=(NOW - timedelta(days=1)).isoformat(), status='FINISHED', home_score=1, away_score=0, score_period='regulation')
        self.save(payload)
        class Fallback:
            name = 'fallback'
            def leagues(self): return (LEAGUE,)
            def get_fixtures(self, league): return FixturesResult('fallback', league, ({'match_id': 'upcoming'},))
        rows = feed.fetch_calendar([self.provider, Fallback()], [LEAGUE], [])
        self.assertEqual(len(rows[LEAGUE]), 2)

    def test_malformed_values_future_time_and_duplicate_quotes_rejected(self):
        for change in ({'odds': True}, {'odds': 'nan'}, {'odds': float('inf')},
                       {'updated_at': (NOW + timedelta(hours=1)).isoformat()}, {'active': 'true'}):
            payload = interchange()
            payload['quotes'][0].update(change)
            with self.subTest(change=change), self.assertRaises(ParseError):
                normalized(payload, 'local_book', now=NOW)
        payload = interchange()
        payload['quotes'].append(dict(payload['quotes'][0], odds=2.3))
        with self.assertRaises(ParseError): normalized(payload, 'local_book', now=NOW)

    def test_request_limit_counts_failures_and_leaves_cursor_unchanged(self):
        service = ScalperService(self.store, leagues=(LEAGUE,), request_limit=1, summary_limit=0,
                                transport=self.transport, history_days=30)
        self.transport.response = scoreboard()
        service.requests = 1
        self.assertEqual(service._history(NOW)[0]['state'], 'request_budget')
        self.assertIsNone(self.store.get_telemetry('scalper:history_cursor:' + LEAGUE))

    def test_old_final_capture_cannot_rewrite_the_training_archive(self):
        first = interchange(quotes=False)
        first['fixtures'][0].update(kickoff=(NOW - timedelta(days=1)).isoformat(), status='FINISHED',
            home_score=1, away_score=0, score_period='regulation')
        self.service.ingest(first, 'owned_capture', now=NOW)
        later = deepcopy(first)
        later['collected_at'] = (NOW + timedelta(seconds=1)).isoformat()
        later['fixtures'][0]['home_score'] = 2
        self.service.ingest(later, 'owned_capture', now=NOW + timedelta(seconds=1))
        self.service.ingest(first, 'owned_capture', now=NOW + timedelta(seconds=2))
        rows = HistoryRepository(self.store).results([LEAGUE], as_of=NOW + timedelta(hours=1))
        self.assertEqual(rows[0]['home_score'], 2)

    def test_cached_scalper_avoids_remote_calendar_calls_when_fresh(self):
        self.save()
        class Remote:
            name = 'football_data'
            def leagues(self): return (LEAGUE,)
            def get_fixtures(self, league): raise AssertionError('Fresh Scalper coverage should avoid this request')
        result = feed.fetch_calendar([Remote(), self.provider], [LEAGUE], [])
        self.assertEqual(result[LEAGUE][0]['match_id'], 'local_book:e1')

    def test_collector_runs_other_feed_after_a_block_and_stays_within_request_cap(self):
        from urllib.parse import urlsplit
        class MixedTransport(FakeTransport):
            def _request_with_retries(self, url, headers, *, provider):
                if provider == 'espn':
                    self.calls.append((url, headers))
                    return 403, {}, b'blocked'
                return super()._request_with_retries(url, headers, provider=provider)
        transport = MixedTransport()
        service = ScalperService(self.store, leagues=(LEAGUE,), transport=transport,
            feeds=[JsonSource('local_book', 'https://example.test/feed')], request_limit=3,
            summary_limit=0, history_days=0, openfootball=False)
        status = service.tick(now=NOW)
        self.assertEqual(status['state'], 'degraded')
        self.assertEqual(status['requests'], 2)
        self.assertTrue(any(row['source'] == 'local_book' and row['state'] == 'updated' for row in status['outcomes']))

    def test_source_parse_failure_does_not_stop_another_configured_feed(self):
        class MixedTransport(FakeTransport):
            def _request_with_retries(self, url, headers, *, provider):
                return 200, {}, json.dumps({} if provider == 'broken_feed' else interchange()).encode()
        service = ScalperService(self.store, leagues=(LEAGUE,), espn=False,
            feeds=[JsonSource('broken_feed', 'https://example.test/broken'), JsonSource('local_book', 'https://example.test/good')],
            transport=MixedTransport(), request_limit=2, summary_limit=0, history_days=0, openfootball=False)
        result = service.tick(now=NOW)
        self.assertEqual(result['requests'], 2)
        self.assertEqual(result['state'], 'degraded')
        self.assertEqual(len(self.provider.fetch(sport_keys=[LEAGUE]).quotes), 1)

    def test_sportybet_unverified_prices_remain_research_and_wrong_geography_is_ignored(self):
        event = {'eventId': 'sr:match:123', 'homeTeamName': 'Arsenal', 'awayTeamName': 'Chelsea',
            'estimateStartTime': int((NOW + timedelta(hours=2)).timestamp() * 1000), 'markets': [
            {'id': '18', 'specifier': 'total=2.5', 'status': 0, 'outcomes': [{'id': '12', 'isActive': 1, 'odds': '1.95'}]},
            {'id': '60', 'specifier': 'total=2.5', 'status': 0, 'outcomes': [{'id': '12', 'isActive': 1, 'odds': '1.50'}]}]}
        payload = {'bizCode': 10000, 'data': {'tournaments': [
            {'name': 'Premier League', 'categoryName': 'England', 'events': [event]},
            {'name': 'Premier League', 'categoryName': 'Kenya', 'events': [dict(event, eventId='sr:match:999')]}]}}
        batch = SportyBetSource().parse(payload, now=NOW)
        self.assertEqual(len(batch.fixtures), 1)
        self.assertEqual(len(batch.quotes), 1)
        self.assertIsNone(batch.quotes[0]['updated_at'])
        self.assertTrue(batch.fixtures[0]['discovery_only'])

    def test_sole_source_pipeline_reads_persisted_history_and_current_quotes(self):
        from lisa.daily_service import DailyService
        now = datetime.now(timezone.utc)
        payload = interchange(now=now)
        payload['fixtures'][0]['kickoff'] = (now + timedelta(hours=2)).isoformat()
        for index in range(12):
            payload['fixtures'].append(dict(event_id='historic' + str(index), sport_key=LEAGUE,
                kickoff=(now - timedelta(days=index + 2)).isoformat(), home_team='Arsenal', away_team='Chelsea',
                status='FINISHED', score_period='regulation', home_score=index % 3, away_score=index % 2))
        self.service.ingest(payload, 'owned_capture', now=now)
        settings = config.Settings(scalper_mode='only', board_leagues=(LEAGUE,), paper_mode=True)
        service = DailyService(self.store, settings)
        status = service.tick(now=now)
        self.assertGreater(status['predictions_added'], 0)
        publication = service.read()
        self.assertEqual(publication['forecast']['count'], 1)
        self.assertEqual(publication['board']['coverage']['fixtures_priced'], 1)
        self.assertTrue(all(row['stake_fraction'] == 0 for category in ('earning', 'winning', 'micro_bets')
                            for row in publication['board'][category]))

    def test_proxy_transport_honours_configured_egress_without_redirects(self):
        import io
        from types import SimpleNamespace
        from urllib.parse import urlsplit
        class Response(io.BytesIO):
            code = 200
            headers = {'ETag': 'proxy-version'}
        response = Response(b'{}')
        with patch('urllib.request.getproxies', return_value={'https': 'http://egress.test:8080'}), \
                patch('urllib.request.proxy_bypass', return_value=False), \
                patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = response
            actual = ScalperTransport()._single(urlsplit('https://example.test/feed'), {},
                SimpleNamespace(lock=__import__('threading').Lock()), 'example.test')
        self.assertEqual(actual, (200, {'etag': 'proxy-version'}, b'{}'))

    def test_reused_source_id_for_another_match_is_quarantined(self):
        self.save()
        payload = interchange(now=NOW + timedelta(seconds=1))
        payload['fixtures'][0]['home_team'] = 'Liverpool'
        with self.assertRaises(ParseError):
            self.save(payload, now=NOW + timedelta(seconds=1))
        self.assertEqual(self.repo.fixtures([LEAGUE], now=NOW)[0]['home_team'], 'Arsenal')

    def test_bulk_bootstrap_survives_restart_without_network_and_cannot_publish(self):
        from test_free_data_stack_unittest import document
        self.transport.response = document(2026)
        self.assertEqual(self.service._bulk_history(NOW)[0]['fixtures'], 3)
        rows = self.repo.fixtures([LEAGUE], now=NOW)
        self.assertTrue(all(r['discovery_only'] and not r['settlement_eligible'] for r in rows))
        self.assertEqual(feed.to_fixtures(rows), [])
        # Both requested season snapshots get their own durable cache.
        self.transport.response = document(2025)
        self.assertEqual(self.service._bulk_history(NOW)[0]['fixtures'], 3)
        restarted = ScalperService(SqliteStorage(self.path), leagues=(LEAGUE,), espn=False,
                                  transport=FakeTransport())
        self.assertEqual(restarted._bulk_history(NOW), [])
        self.assertEqual(restarted.transport.calls, [])
        self.assertEqual(len(HistoryRepository(self.store).results([LEAGUE], as_of=NOW)), 4)


    def test_storage_failure_does_not_open_the_publishers_circuit(self):
        with patch.object(self.service.repository, 'accept', side_effect=RuntimeError('database offline')):
            with self.assertRaises(RuntimeError):
                self.request()
        self.assertEqual(self.repo.source_state('local_book')['next_attempt'], 0)

    def test_lease_release_failure_still_releases_the_process_lock(self):
        with patch.object(self.service.repository, 'release', side_effect=RuntimeError('database offline')):
            with self.assertRaises(RuntimeError):
                self.service.tick(now=NOW)
        self.assertFalse(self.service._lock.locked())

    def test_authorized_feed_urls_are_explicit_and_do_not_follow_payload_links(self):
        for url in ('file:///etc/passwd', 'http://example.test', 'https://user:pass@example.test', 'https://example.test/#secret'):
            with self.assertRaises(ValueError): JsonSource('custom', url)

    def test_below_floor_prices_do_not_fill_ladders_or_accumulators(self):
        board = OpportunityBoard(None)
        row = Opportunity('e', LEAGUE, NOW, 'Arsenal', 'Chelsea', 'h2h', 'Home', .8,
                          1.25, best_odds=1.17, priced=True, ev=.1)
        self.assertEqual(board._earning_ladder([row]), ())
        self.assertEqual(board._winning_ladder([row]), ())
        self.assertEqual(board._micro_bets([replace(row, market='btts')], set()), ())
        self.assertEqual(board._earning_ladder([replace(row, best_odds=1.18)])[0].best_odds, 1.18)


@unittest.skipUnless(os.getenv('LISA_TEST_POSTGRES_URL'), 'isolated PostgreSQL test URL is not configured')
class ScalperPostgresTests(unittest.TestCase):
    def setUp(self):
        import uuid
        from lisa.postgres_storage import require_isolated_database
        from lisa.storage import PostgresStorage
        dsn = os.environ['LISA_TEST_POSTGRES_URL']
        require_isolated_database(dsn, '_test')
        self.store = PostgresStorage(dsn)
        self.addCleanup(self.store.close)
        self.repo = ScalperRepository(self.store)
        self.source = 'scalper_ci_' + uuid.uuid4().hex
        self.addCleanup(self.clean)

    def clean(self):
        with self.store._tx() as conn:
            for table in ('scalper_resources', 'scalper_events', 'scalper_quotes', 'scalper_quote_sets', 'scalper_sources'):
                conn.execute('DELETE FROM ' + table + ' WHERE source=?', (self.source,))

    def test_atomic_merge_cache_and_quote_revocation(self):
        first = interchange()
        self.repo.accept(normalized(first, self.source, now=NOW), resource='feed', payload=first, now=NOW, ttl=60)
        self.assertTrue(self.repo.resource(self.source, 'feed')['content_hash'])
        empty = interchange(now=NOW + timedelta(seconds=1), quotes=False)
        self.repo.accept(normalized(empty, self.source, now=NOW + timedelta(seconds=1)), resource='feed',
            payload=empty, now=NOW + timedelta(seconds=1), ttl=60)
        with self.store._tx() as conn:
            count = conn.execute('SELECT COUNT(*) AS n FROM scalper_quotes WHERE source=?', (self.source,)).fetchone()['n']
        self.assertEqual(count, 0)

    def test_concurrent_first_insert_preserves_the_newer_observation(self):
        from concurrent.futures import ThreadPoolExecutor
        def save(index):
            now = NOW + timedelta(seconds=index)
            payload = interchange(now=now)
            payload['fixtures'][0]['kickoff'] = (NOW + timedelta(hours=index + 2)).isoformat()
            return self.repo.accept(normalized(payload, self.source, now=now), resource='feed',
                                    payload=payload, now=now, ttl=60)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(save, (3, 0, 2, 1)))
        with self.store._tx() as conn:
            row = conn.execute('SELECT kickoff FROM scalper_events WHERE source=?', (self.source,)).fetchone()
        self.assertEqual(row['kickoff'], (NOW + timedelta(hours=5)).timestamp())

if __name__ == '__main__':
    unittest.main()
