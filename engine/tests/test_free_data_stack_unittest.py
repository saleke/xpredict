import gzip
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lisa import config, feed
from lisa.board import Opportunity, OpportunityBoard
from lisa.daily_service import DailyService
from lisa.data_quality import reconcile_results
from lisa.dixon_coles import ScoredMatch
from lisa.history_service import HistoryBackfillService
from lisa.league_model import LeagueGoalModel
from lisa.match_history import HistoryRepository
from lisa.model_policy import MODEL_VERSION, FORECAST_SOURCES
from lisa.observability import fixture_observations
from lisa.providers.base import FixturesResult, HttpTransport, ParseError, ProviderNotAvailableError, _decompress
from lisa.providers.calendar import dedupe_fixtures, normalise_fixture
from lisa.providers.openfootball import OpenFootballProvider
from lisa.storage import SqliteStorage

NOW = datetime(2026, 10, 4, 15, tzinfo=timezone.utc)
LEAGUE = 'soccer_epl'


def document(year=2026):
    # Representative schema fixtures, not production match observations.
    return {'name': f'English Premier League {year}/{str(year+1)[-2:]}', 'matches': [
        {'round': 'Matchday 1', 'date': f'{year}-09-01', 'time': '15:00',
         'team1': 'Arsenal FC', 'team2': 'Chelsea FC', 'score': {'ft': [2, 1], 'ht': [1, 0]}},
        {'round': 'Matchday 2', 'date': f'{year}-09-08',
         'team1': 'Chelsea FC', 'team2': 'Arsenal FC', 'score': [0, 0]},
        {'round': 'Matchday 3', 'date': f'{year}-11-01', 'time': '13:00',
         'team1': 'Arsenal FC', 'team2': 'Chelsea FC'}]}


class FileTransport(HttpTransport):
    def __init__(self, payloads=None):
        super().__init__(max_retries=0)
        self.payloads = payloads or {}
        self.calls = []

    def _request_with_retries(self, url, headers, *, provider):
        self.calls.append((url, dict(headers)))
        if headers.get('If-None-Match'):
            return 304, {}, b''
        year = int(url.split('/')[-2].split('-')[0])
        payload = self.payloads.get(year, document(year))
        if isinstance(payload, Exception):
            raise payload
        return 200, {'etag': 'file-version'}, json.dumps(payload).encode()


def official(when=NOW-timedelta(days=3), **overrides):
    row = normalise_fixture(provider='football_data', sport_key=LEAGUE, match_id='official',
        kickoff_epoch=when.timestamp(), home='Arsenal', away='Chelsea',
        status='FINISHED', home_score=2, away_score=1, season='2026')
    row.update(overrides)
    return row


class FreeFileTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = SqliteStorage(str(Path(self.folder.name)/'files.db'))
        self.addCleanup(self.store.close)
        self.clock = NOW
        self.transport = FileTransport()
        self.provider = OpenFootballProvider(transport=self.transport, clock=lambda: self.clock)
        self.provider.storage = self.store

    def test_supported_score_shapes_and_halftime_preserve_unknown_kickoffs(self):
        rows = self.provider.parse(document(), LEAGUE, 2026)
        self.assertEqual([(r['home_score'], r['away_score']) for r in rows[:2]], [(2, 1), (0, 0)])
        self.assertEqual(rows[0]['home_first_half_score'], 1)
        self.assertNotIn('home_first_half_score', rows[1])
        self.assertTrue(all(r['kickoff_time_known'] is False and r['settlement_eligible'] is False for r in rows))
        self.assertEqual(feed.to_fixtures(rows), [])
        self.assertEqual(fixture_observations(rows, NOW)['rows'], [])

    def test_bad_scores_future_scores_and_wrong_season_are_rejected(self):
        for change in ({'score': [True, 0]}, {'score': {'ft': [1, 0], 'ht': [2, 0]}},
                       {'date': '2026-11-01'}, {'date': '2024-09-01'}, {'team2': 'Arsenal'}):
            with self.subTest(change=change):
                payload = document()
                payload['matches'][0].update(change)
                with self.assertRaises(ParseError):
                    self.provider.parse(payload, LEAGUE, 2026)
        with self.assertRaises(ParseError):
            self.provider.parse(document(2025), LEAGUE, 2026)

    def test_ids_survive_kickoff_revision_and_no_url_can_escape_allowlist(self):
        payload = document()
        before = self.provider.parse(payload, LEAGUE, 2026)
        payload['matches'][2]['date'] = '2026-11-08'
        after = self.provider.parse(payload, LEAGUE, 2026)
        self.assertEqual(before[2]['match_id'], after[2]['match_id'])
        for league in ('../../outside', 'https://example.invalid', 'soccer_uefa_champions_league'):
            with self.assertRaises(ProviderNotAvailableError):
                self.provider.get_season(league, 2026)
        self.assertEqual(self.transport.calls, [])

    def test_two_file_bootstrap_survives_restart_without_more_network(self):
        first = self.provider.get_fixtures(LEAGUE)
        self.assertEqual((len(first.fixtures), len(self.transport.calls)), (6, 2))
        other = OpenFootballProvider(transport=FileTransport(), clock=lambda: self.clock)
        other.storage = self.store
        self.assertEqual(other.get_fixtures(LEAGUE).fixtures, first.fixtures)
        self.assertEqual(other._transport.calls, [])

    def test_expired_durable_file_revalidates_and_304_refreshes_ttl(self):
        with patch('lisa.providers.base.utcnow_ts', side_effect=lambda: self.clock.timestamp()):
            self.provider.get_season(LEAGUE, 2026)
            self.clock += timedelta(days=2)
            other = OpenFootballProvider(transport=FileTransport(), clock=lambda: self.clock)
            other.storage = self.store
            other.get_season(LEAGUE, 2026)
            self.assertEqual(other._transport.calls[0][1]['If-None-Match'], 'file-version')
            other.get_season(LEAGUE, 2026)
            self.assertEqual(len(other._transport.calls), 1)

    def test_failed_download_has_persistent_cooldown_and_does_not_erase_other_season(self):
        self.transport.payloads[2026] = ParseError('corrupt file')
        result = self.provider.get_fixtures(LEAGUE)
        self.assertEqual(len(result.fixtures), 3)
        self.assertTrue(result.warnings)
        restarted = OpenFootballProvider(transport=FileTransport(), clock=lambda: self.clock)
        restarted.storage = self.store
        self.assertTrue(restarted.get_fixtures(LEAGUE).warnings)
        self.assertEqual(restarted._transport.calls, [])

    def test_corrupt_persistent_cache_is_repaired(self):
        key = 'openfootball:file:2026-27/en.1.json'
        self.store.set_telemetry(key, {'parser_version': 1, 'checked_at': NOW.timestamp(), 'payload': {}})
        self.assertEqual(len(self.provider.get_season(LEAGUE, 2026).fixtures), 3)
        self.assertEqual(len(self.transport.calls), 1)

    def test_unknown_kickoff_results_have_conservative_availability(self):
        history = HistoryRepository(self.store)
        rows = self.provider.parse(document(), LEAGUE, 2026)
        history.ingest(rows, observed_at=NOW)
        early = datetime(2026, 9, 2, 3, tzinfo=timezone.utc)
        self.assertEqual(history.results([LEAGUE], as_of=early), [])
        self.assertEqual(feed.to_scored(rows, LEAGUE, as_of=early), [])
        self.assertEqual(len(history.results([LEAGUE], as_of=early+timedelta(hours=10))), 1)

    def test_date_only_overlap_counts_once_and_conflicting_score_is_withheld(self):
        archive = self.provider.parse(document(), LEAGUE, 2026)[0]
        timed = official(datetime(2026, 9, 1, 14, tzinfo=timezone.utc))
        rows = dedupe_fixtures([archive, timed])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['provider'], 'football_data')
        accepted, conflicts = reconcile_results([archive, dict(timed, home_score=3)])
        self.assertEqual(accepted, [])
        self.assertEqual(len(conflicts), 1)

    def test_unknown_date_does_not_guess_between_two_same_day_rematches(self):
        archive = self.provider.parse(document(), LEAGUE, 2026)[0]
        first = official(datetime(2026, 9, 1, 10, tzinfo=timezone.utc))
        second = official(datetime(2026, 9, 1, 20, tzinfo=timezone.utc), match_id='second')
        result = dedupe_fixtures([archive, first, second])
        self.assertEqual(len(result), 2)
        self.assertFalse(any(r['provider'] == 'openfootball' for r in result))

    def test_provisional_score_cannot_settle_existing_predictions(self):
        service = DailyService(self.store, config.Settings(), providers=feed.ProviderSet())
        result = official(settlement_eligible=False)
        with self.store._tx() as conn:
            conn.execute('INSERT INTO picks (dedupe_key,match_id,sport_key,market,outcome_name,'
                'home_team,away_team,commence_time,p_true,fair_odds,n_books,state,created_at,source) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', ('test-pick', result['match_id'], LEAGUE,
                'h2h', 'Home', 'Arsenal', 'Chelsea', result['kickoff'], .7, 1/.7, 0,
                'CONFIRMED', result['kickoff'], 'league-dixon-coles-v2'))
        self.assertEqual(service._settle([result], NOW), 0)
        self.assertEqual(len(self.store.list_pending_picks()), 1)
        self.assertEqual(service._settle([dict(result, home_score=0, away_score=4), official()], NOW), 1)
        self.assertEqual(self.store.list_settled_picks()[0]['result'], 'WIN')

    def test_bulk_goals_survive_failed_statistics_and_official_history_is_not_duplicated(self):
        api = SimpleNamespace(name='api_football', leagues=lambda: (LEAGUE,),
            competition=Mock(side_effect=ProviderNotAvailableError('unavailable')),
            get_season=Mock())
        fdo = SimpleNamespace(name='football_data', leagues=lambda: (LEAGUE,), current_season=Mock())
        service = HistoryBackfillService(self.store, config.Settings(board_leagues=(LEAGUE,)),
            providers=feed.ProviderSet(calendar=[self.provider, fdo, api]))
        state = service.tick(now=NOW)
        self.assertEqual(state['state'], 'degraded')
        self.assertEqual(state['goals']['provider'], 'openfootball')
        self.assertEqual(len(HistoryRepository(self.store).results([LEAGUE], as_of=NOW)), 8)
        fdo.current_season.assert_not_called()
        api.get_season.assert_not_called()

    def test_completed_statistics_checkpoint_survives_a_later_failed_request(self):
        first = official(match_id='api:first', provider='api_football')
        second = official(match_id='api:second', provider='api_football', home_team='Liverpool', away_team='Everton')
        api = SimpleNamespace(name='api_football', competition=Mock(return_value=(1, 2026)),
            get_season=Mock(return_value=FixturesResult('api_football', LEAGUE, (first, second))),
            with_corners=Mock(side_effect=[dict(first, home_corners=7, away_corners=4),
                ProviderNotAvailableError('quota unavailable')]))
        service = HistoryBackfillService(self.store, config.Settings(), providers=feed.ProviderSet())
        service.season_batch_limit = 1
        history = HistoryRepository(self.store)
        state = service._collect_seasons(api, history, LEAGUE, NOW, statistics=True)
        self.assertEqual(state['state'], 'degraded')
        checkpoint = self.store.get_telemetry('history:checkpoint:api_football:soccer_epl:2026')
        self.assertEqual(checkpoint['statistics_attempted'], ['api:first'])
        api.with_corners.reset_mock()
        api.with_corners.side_effect = None
        api.with_corners.return_value = dict(second, home_corners=2, away_corners=3)
        service._collect_seasons(api, history, LEAGUE, NOW+timedelta(days=2), statistics=True)
        api.with_corners.assert_called_once_with(second, purpose='history')

    def test_bulk_history_and_verified_fixture_generate_publish_and_settle_without_visitors(self):
        upcoming = official(NOW+timedelta(hours=2), completed=False, status='SCHEDULED',
            home_score=None, away_score=None)
        calendar = SimpleNamespace(name='football_data', leagues=lambda: (LEAGUE,),
            get_fixtures=Mock(return_value=FixturesResult('football_data', LEAGUE, (upcoming,))))
        providers = feed.ProviderSet(calendar=[self.provider, calendar])
        settings = config.Settings(board_leagues=(LEAGUE,))
        service = DailyService(self.store, settings, providers=providers)
        generated = service.tick(now=NOW)
        self.assertGreater(generated['predictions_added'], 0)
        snapshot = service.read()
        self.assertEqual(snapshot['forecast']['count'], 1)
        self.assertTrue(snapshot['board']['micro_bets'])
        self.assertTrue(all(r['match_id'] == upcoming['match_id'] and r['stake_fraction'] == 0
            for name in ('winning', 'micro_bets') for r in snapshot['board'][name]))
        settled = service._settle([dict(upcoming, completed=True, status='FINISHED', home_score=2, away_score=1)],
            NOW+timedelta(days=1))
        self.assertEqual(settled, generated['predictions_added'])
        self.assertEqual(len(self.store.list_pending_picks()), 0)

    def test_file_future_rows_alone_cannot_publish_unverified_fixtures(self):
        payload = document()
        payload['matches'][2]['date'] = '2026-10-05'
        self.transport.payloads[2026] = payload
        settings = config.Settings(board_leagues=(LEAGUE,))
        report = feed.run_feed(settings, providers=feed.ProviderSet(calendar=[self.provider]), now=NOW,
            history=HistoryRepository(self.store))
        self.assertIsNotNone(report.board)
        self.assertEqual(report.forecast['count'], 0)
        self.assertEqual(report.board.coverage.fixtures_modelled, 0)
        self.assertEqual(report.board.micro_bets, ())


class RoutingAndModelTests(unittest.TestCase):
    def provider(self, name, leagues=(LEAGUE,), result=None, error=None):
        return SimpleNamespace(name=name, leagues=lambda: leagues,
            get_fixtures=Mock(side_effect=error, return_value=FixturesResult(name, LEAGUE,
                tuple([official()] if result is None else result))))

    def test_healthy_primary_skips_duplicate_metered_sources(self):
        primary = self.provider('football_data')
        support = self.provider('allsports')
        api = self.provider('api_football')
        result = feed.fetch_calendar([primary, support, api], [LEAGUE], [])
        self.assertEqual(len(result[LEAGUE]), 1)
        primary.get_fixtures.assert_called_once()
        support.get_fixtures.assert_not_called()
        api.get_fixtures.assert_not_called()

    def test_failed_or_empty_primary_uses_only_needed_fallback(self):
        other = 'soccer_france_ligue_one'
        primary = self.provider('football_data', leagues=(LEAGUE, other))
        primary.get_fixtures.side_effect = lambda league: FixturesResult('football_data', league,
            () if league == LEAGUE else (official(sport_key=other),))
        fallback = self.provider('allsports', leagues=(LEAGUE, other))
        result = feed.fetch_calendar([primary, fallback], [LEAGUE, other], [])
        self.assertEqual([len(result[k]) for k in (LEAGUE, other)], [1, 1])
        fallback.get_fixtures.assert_called_once_with(LEAGUE)

    def test_bulk_history_never_satisfies_verified_calendar_slot_or_runs_for_settlement(self):
        bulk = self.provider('openfootball', result=[official(discovery_only=True)])
        primary = self.provider('football_data')
        self.assertEqual(len(feed.fetch_calendar([bulk, primary], [LEAGUE], [])[LEAGUE]), 2)
        self.assertEqual(len(feed.fetch_calendar([bulk, primary], [LEAGUE], [], purpose='settlement')[LEAGUE]), 1)
        self.assertEqual(bulk.get_fixtures.call_count, 1)

    def test_cancelled_or_unconfirmed_fixtures_never_become_selections(self):
        rows = [official(NOW+timedelta(hours=1), completed=False, status=status,
                         home_score=None, away_score=None) for status in ('POSTPONED', 'CANCELED', 'IN_PLAY')]
        self.assertEqual(feed.to_fixtures(rows), [])

    def test_production_model_uses_shared_team_identity_and_reuses_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SqliteStorage(str(Path(folder)/'model.db'))
            self.addCleanup(store.close)
            matches = [ScoredMatch(LEAGUE, NOW-timedelta(days=i+1),
                'Arsenal FC' if i % 2 else 'Arsenal', 'Chelsea FC' if i % 2 else 'Chelsea', 2, 1)
                for i in range(30)]
            model = LeagueGoalModel()
            model.cache_storage = store
            model.fit(matches, as_of=NOW)
            league_model = model.for_league(LEAGUE)
            self.assertEqual(league_model.strength('Arsenal').games, 30)
            original = league_model.predict('Arsenal', 'Chelsea')
            alternate = league_model.predict('Arsenal FC', 'Chelsea FC')
            self.assertEqual({k: v for k, v in original.items() if k != 'top_outcome'},
                             {k: v for k, v in alternate.items() if k != 'top_outcome'})
            restart = LeagueGoalModel()
            restart.cache_storage = store
            with patch('lisa.dixon_coles.DixonColesModel.fit', side_effect=AssertionError('unchanged data refit')):
                restart.fit(matches, as_of=NOW)
            self.assertTrue(restart.for_league(LEAGUE).knows('Arsenal FC'))
            self.assertIn('league-dixon-coles-v2', FORECAST_SOURCES)
            self.assertNotEqual(MODEL_VERSION, 'league-dixon-coles-v2')

    def test_accumulator_threshold_applies_after_correlation_adjustment(self):
        board = OpportunityBoard(SimpleNamespace(), min_accumulator_prob=.6)
        def leg(name):
            return Opportunity(match_id=name, sport_key=LEAGUE, kickoff=NOW,
                home=name, away='Away', market='h2h', selection='Home', p_model=.8, fair_odds=1.25)
        self.assertIsNone(board._score_accumulator((leg('one'), leg('two'))))

    def test_default_stack_omits_clipped_and_unconfigured_price_sources(self):
        providers = feed.build_providers(config.Settings())
        self.assertIsNotNone(providers.named('openfootball'))
        self.assertIsNone(providers.named('sportsdb'))
        self.assertEqual(providers.prices, [])


class TransportBoundsTests(unittest.TestCase):
    def test_compressed_bomb_and_plain_oversize_are_rejected(self):
        for body, encoding in ((gzip.compress(b'x'*10000), 'gzip'), (b'x'*10000, 'identity')):
            with self.assertRaises(ParseError):
                _decompress(body, encoding, max_bytes=100)

    def test_bad_json_never_enters_response_cache(self):
        transport = HttpTransport()
        with patch.object(transport, '_request_with_retries', return_value=(200, {}, b'broken')):
            with self.assertRaises(ParseError):
                transport.get_json('https://example.invalid/data', ttl=86400)
        self.assertIsNone(transport.cache.get('https://example.invalid/data'))


if __name__ == '__main__':
    unittest.main()
