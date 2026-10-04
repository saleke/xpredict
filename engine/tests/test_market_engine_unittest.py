"""Independent payout examples, distribution identities and durable data checks."""
import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock
from lisa.contracts import MarketContract, PayoutDistribution, unit_profit, same_game_probability
from lisa.corners import CornerTotalModel, count_pmf
from lisa.match_history import HistoryRepository
from lisa.league_model import LeagueGoalModel
from lisa.dixon_coles import ScoredMatch
from lisa.storage import SqliteStorage
from lisa.providers.api_football import ApiFootballProvider
from lisa.providers.calendar import normalise_fixture
from lisa import config, feed

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def row(index=1, league='soccer_epl', **changes):
    item = normalise_fixture(provider='test', sport_key=league, match_id=str(index),
        kickoff_epoch=(NOW - timedelta(days=index)).timestamp(), home='A', away='B',
        home_score=2, away_score=1, status='FINISHED')
    item.update(changes)
    return item


class ContractTests(unittest.TestCase):
    def test_quarter_total_payout_examples(self):
        examples = [('Over', 2.25, 2, 'HALF_LOSS'), ('Over', 2.75, 3, 'HALF_WIN'),
                    ('Under', 2.25, 2, 'HALF_WIN'), ('Under', 2.75, 3, 'HALF_LOSS'),
                    ('Over', 2., 2, 'VOID'), ('Under', 2.5, 3, 'LOSS')]
        for side, line, goals, expected in examples:
            self.assertEqual(MarketContract('totals', side, line).grade(goals, 0), expected)
        self.assertEqual(unit_profit('HALF_WIN', 2.6), .8)
        self.assertEqual(unit_profit('HALF_LOSS', 2.6), -.5)

    def test_negative_handicap_split_and_draw_no_bet(self):
        self.assertEqual(MarketContract('asian_handicap', 'Home', -1.25).grade(2, 1), 'HALF_LOSS')
        self.assertEqual(MarketContract('asian_handicap', 'Home', -.75).grade(2, 1), 'HALF_WIN')
        self.assertEqual(MarketContract('asian_handicap', 'Away', .25).grade(1, 1), 'HALF_WIN')
        self.assertEqual(MarketContract('draw_no_bet', 'Away').grade(1, 1), 'VOID')

    def test_push_probability_changes_fair_odds_and_ev(self):
        dist = PayoutDistribution({'WIN': .4, 'VOID': .3, 'LOSS': .3})
        self.assertAlmostEqual(dist.fair_odds, 1.75)
        self.assertAlmostEqual(dist.ev(2.), .1)
        self.assertAlmostEqual(dist.kelly(2., fraction=1, cap=.9), 1 / 7)
        binary = PayoutDistribution({'WIN': .6, 'LOSS': .4})
        self.assertAlmostEqual(binary.kelly(2., fraction=1, cap=.9), .2)
        self.assertEqual(dist.kelly(1.5), 0)

    def test_joint_probability_is_not_product_of_same_game_marginals(self):
        # Four equally likely scorelines: (0,0),(0,1),(1,0),(1,1).
        matrix = [[.25, .25], [.25, .25]]
        contracts = [MarketContract('btts', 'Yes'), MarketContract('totals', 'Over', 1.5)]
        self.assertAlmostEqual(same_game_probability(matrix, contracts), .25)
        self.assertNotEqual(.25, .25 * .25)
        with self.assertRaises(ValueError):
            same_game_probability(matrix, [MarketContract('draw_no_bet', 'Home')])
        with self.assertRaises(ValueError):
            same_game_probability(matrix, [MarketContract('corners', 'Over', 8.5)])

    def test_missing_corner_data_and_wrong_period_never_settle(self):
        contract = MarketContract('corners', 'Over', 8.5)
        with self.assertRaises(ValueError):
            contract.grade(2, 1)
        self.assertEqual(contract.grade(2, 1, home_corners=6, away_corners=3), 'WIN')
        with self.assertRaises(ValueError):
            MarketContract('totals', 'Over', 2.5, 'first_half').grade(2, 1)
        with self.assertRaises(ValueError):
            MarketContract('totals', 'Over', 2.3).grade(2, 1)


class HistoryAndModelTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / 'test.db')
        self.store = SqliteStorage(self.path)
        self.history = HistoryRepository(self.store)

    def test_restart_idempotence_corrections_and_corner_preservation(self):
        self.assertEqual(self.history.ingest([row(home_corners=7, away_corners=4)], observed_at=NOW), 1)
        self.history.ingest([row(home_score=3)], observed_at=NOW)
        reopened = HistoryRepository(SqliteStorage(self.path)).results(['soccer_epl'], as_of=NOW)
        self.assertEqual(len(reopened), 1)
        self.assertEqual(reopened[0]['home_score'], 3)
        self.assertEqual(reopened[0]['home_corners'], 7)
        self.history.ingest([row(ended_after_extra_time=True)], observed_at=NOW)
        self.assertNotIn('home_corners', self.history.results(['soccer_epl'], as_of=NOW)[0])

    def test_invalid_and_future_results_do_not_train(self):
        self.history.ingest([row(1), row(2, home_score=True), row(3, completed=False),
                             row(4, kickoff=(NOW + timedelta(days=1)).isoformat())], observed_at=NOW)
        self.assertEqual(len(self.history.results(['soccer_epl'], as_of=NOW)), 1)
        self.assertEqual(self.history.results(['soccer_epl'], as_of=NOW - timedelta(days=2)), [])

    def test_identical_names_in_other_leagues_do_not_share_ratings(self):
        matches = [ScoredMatch(league, NOW - timedelta(days=i + 1), 'A', 'B', score, 0)
                   for league, score in [('high', 4), ('low', 1)] for i in range(24)]
        model = LeagueGoalModel()
        model.fit(matches, as_of=NOW)
        self.assertGreater(model.for_league('high').predict('A', 'B')['expected_goals']['home'],
                           model.for_league('low').predict('A', 'B')['expected_goals']['home'])
        self.assertFalse(model.for_league('missing').knows('A'))
        first = model.for_league('high').report
        model.fit(matches, as_of=NOW)
        self.assertIs(first, model.for_league('high').report)

    def test_count_distributions_have_correct_moments_and_mass(self):
        for alpha in (0., .1, .5):
            pmf = count_pmf(10., alpha)
            mean = sum(k * p for k, p in enumerate(pmf))
            variance = sum((k - mean) ** 2 * p for k, p in enumerate(pmf))
            self.assertAlmostEqual(sum(pmf), 1., places=9)
            self.assertAlmostEqual(mean, 10., places=6)
            self.assertAlmostEqual(variance, 10 + alpha * 100, places=4)

    def test_corners_need_real_counts_and_sufficient_local_samples(self):
        rows = [row(i, home_corners=3 + i % 7, away_corners=2 + i % 6) for i in range(1, 81)]
        model = CornerTotalModel().fit(rows, as_of=NOW)
        prediction = model.predict('A', 'B')
        self.assertIsNotNone(prediction)
        self.assertIsNone(model.predict('unknown', 'B'))
        self.assertIsNone(CornerTotalModel().fit([row(i) for i in range(1, 81)], as_of=NOW).predict('A', 'B'))
        self.assertGreater(prediction['over']['8.5'], prediction['over']['9.5'])
        # Goals cannot change a separately trained corner distribution.
        changed = [dict(r, home_score=10, away_score=10) for r in rows]
        self.assertEqual(prediction, CornerTotalModel().fit(changed, as_of=NOW).predict('A', 'B'))

    def test_full_feed_uses_persisted_prior_seasons_and_league_models(self):
        rows = [row(i) for i in range(1, 30)]
        self.history.ingest(rows, observed_at=NOW)
        upcoming = row(100, completed=False, status='SCHEDULED', home_score=None, away_score=None,
                       kickoff=(NOW + timedelta(hours=5)).isoformat(), epoch=(NOW + timedelta(hours=5)).timestamp())
        calendar = Mock(name='test-provider')
        calendar.name = 'football_data'
        calendar.leagues.return_value = ('soccer_epl',)
        calendar.get_fixtures.return_value = __import__('lisa.providers.base', fromlist=['FixturesResult']).FixturesResult('football_data', 'soccer_epl', (upcoming,))
        report = feed.run_feed(config.Settings(board_leagues=('soccer_epl',), board_volume_target=1),
            providers=feed.ProviderSet(calendar=[calendar]), now=NOW, prices={}, history=self.history)
        self.assertIsNotNone(report.board)
        self.assertEqual(report.board.coverage.fixtures_modelled, 1)
        self.assertEqual(report.model['matches_used'], 29)
        self.assertIn('soccer_epl', report.model['leagues'])

    def test_api_budget_survives_restart_and_cache_hit_uses_no_quota(self):
        transport = Mock()
        transport.get_json.return_value = {'response': [], 'errors': []}
        api = ApiFootballProvider('test-key', transport=transport, daily_limit=5)
        api.storage = self.store
        api._get('/countries', {}, ttl=600)
        api._get('/countries', {}, ttl=600)
        self.assertEqual(transport.get_json.call_count, 1)
        restarted = ApiFootballProvider('test-key', transport=transport, daily_limit=5)
        restarted.storage = self.store
        for i in range(3):
            restarted._get('/countries', {'id': i})
        with self.assertRaises(Exception) as caught:
            restarted._get('/countries', {'id': 10})
        from lisa.providers.diagnostics import safe_error_summary
        self.assertIn('daily_budget_exhausted', safe_error_summary(caught.exception))
        self.assertNotIn('authentication_rejected', safe_error_summary(caught.exception))
        restarted._get('/countries', {'id': 10}, purpose='settlement')
        self.assertEqual(transport.get_json.call_count, 5)

    def test_history_budget_refusal_is_distinct_and_spends_no_network_request(self):
        transport = Mock()
        api = ApiFootballProvider('test-key', transport=transport, daily_limit=100)
        api.storage = self.store
        day = datetime.now(timezone.utc).date().isoformat()
        with self.store._tx() as conn:
            conn.execute('INSERT INTO provider_daily_usage VALUES (?, ?, ?)',
                         ('api_football:history', day, 8))
        with self.assertRaises(Exception) as caught:
            api._get('/fixtures', {'league':39}, purpose='history')
        from lisa.providers.diagnostics import safe_error_summary
        self.assertIn('history_budget_exhausted', safe_error_summary(caught.exception))
        transport.get_json.assert_not_called()


class AdapterTests(unittest.TestCase):
    def test_market_mapping_rejects_wrong_period_unknown_and_bad_lines(self):
        parse = ApiFootballProvider.parse_bet
        self.assertEqual(parse('Double Chance', 'Home/Draw'), ('double_chance', '1X', None))
        self.assertEqual(parse('Asian Total', 'Over 2.25'), ('totals', 'Over', 2.25))
        self.assertEqual(parse('Corners Over Under', 'Under 10.5'), ('corners', 'Under', 10.5))
        self.assertIsNone(parse('Goals Over/Under First Half', 'Over 2.5'))
        self.assertIsNone(parse('Asian Total', 'Over 2.3'))
        self.assertIsNone(parse('To Qualify', 'Home'))

    def test_regulation_score_is_used_after_extra_time(self):
        payload = {'fixture': {'id': 1, 'timestamp': NOW.timestamp(), 'status': {'short': 'AET'}},
            'teams': {'home': {'id': 2, 'name': 'A'}, 'away': {'id': 3, 'name': 'B'}},
            'league': {'season': 2026}, 'goals': {'home': 3, 'away': 2},
            'score': {'fulltime': {'home': 1, 'away': 1}}}
        result = ApiFootballProvider.to_fixture(payload, 'soccer_epl')
        self.assertEqual((result['home_score'], result['away_score']), (1, 1))
        api = ApiFootballProvider('test', transport=Mock())
        self.assertEqual(api.with_corners(result), result)
        api.transport.get_json.assert_not_called()


class SupportingSourceTests(unittest.TestCase):
    def setUp(self):
        from lisa.providers.allsports import AllSportsProvider
        self.provider_class = AllSportsProvider
        self.fixture = {'event_key': '123', 'event_date': '2026-08-01', 'event_time': '15:00',
            'event_home_team': 'A', 'event_away_team': 'B', 'event_ft_result': '1 - 1',
            'event_final_result': '4 - 3', 'event_status': 'Finished', 'event_live': '0',
            'statistics': [{'type': 'Verified Corners', 'home': '6', 'away': '4'}]}

    def test_supporting_source_does_not_remove_other_adapters(self):
        settings = config.Settings(football_data_token='test', sharpapi_key='test', api_football_key='test',
                                   allsports_api_key='test', allsports_odds_enabled=True)
        providers = feed.build_providers(settings)
        self.assertTrue({'football_data', 'api_football', 'allsports'} <= {p.name for p in providers.calendar})
        self.assertTrue({'sharpapi', 'api_football', 'allsports'} <= {p.name for p in providers.prices})

    def test_supporting_results_use_regulation_scores_and_verified_statistic_name(self):
        result = self.provider_class.to_fixture(self.fixture, 'soccer_epl')
        self.assertEqual((result['home_score'], result['away_score']), (1, 1))
        self.assertNotIn('home_corners', result)
        checked = self.provider_class.to_fixture(self.fixture, 'soccer_epl', corner_stat_type='Verified Corners')
        self.assertEqual((checked['home_corners'], checked['away_corners']), (6, 4))
        missing = self.provider_class.to_fixture(dict(self.fixture, event_ft_result=''), 'soccer_epl')
        self.assertFalse(missing['completed'])
        extra = self.provider_class.to_fixture(dict(self.fixture, event_status='After ET'),
                                              'soccer_epl', corner_stat_type='Verified Corners')
        self.assertNotIn('home_corners', extra)

    def test_conflicting_results_are_withheld_and_missing_corners_do_not_become_zero(self):
        from lisa.data_quality import reconcile_results
        first = row()
        second = dict(first, provider='supporting', match_id='supporting:1', home_score=3)
        accepted, conflicts = reconcile_results([first, second])
        self.assertEqual(accepted, [])
        self.assertEqual(conflicts[0]['kind'], 'final_score')
        good, no_conflicts = reconcile_results([first, dict(first, provider='supporting', home_corners=None)])
        self.assertEqual(len(good), 2)
        self.assertEqual(no_conflicts, [])
        accepted, conflicts = reconcile_results([dict(first, home_corners=5, away_corners=3),
                                                 dict(first, provider='supporting', home_corners=6, away_corners=3)])
        self.assertTrue(all('home_corners' not in r for r in accepted))
        self.assertEqual(conflicts[0]['kind'], 'corners')

    def test_cross_league_quotes_cannot_join_and_unknown_freshness_is_preserved(self):
        from lisa.providers.sharpapi import SharpQuote
        from lisa.board import Fixture
        provider = self.provider_class('test', transport=Mock())
        fixtures = [Fixture('a', 'soccer_epl', NOW, 'A', 'B'),
                    Fixture('b', 'soccer_spain_la_liga', NOW, 'A', 'B')]
        quotes = [SharpQuote('support:1', 'h2h', 'Home', 2., 'test', home='A', away='B',
                             kickoff=NOW, source='allsports', sport_key='soccer_epl')]
        outcome = provider.match(quotes, fixtures)
        self.assertEqual(set(outcome.prices), {'a'})
        self.assertIsNone(outcome.prices['a'][0].updated_at)

    def test_post_credentials_are_not_added_to_url_and_cache_uses_no_new_request(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = SqliteStorage(str(Path(directory.name) / 'test.db'))
        transport = Mock()
        transport.post_json.return_value = {'success': 1, 'result': []}
        provider = self.provider_class('private-test-key', transport=transport)
        provider.storage = store
        provider._get('Leagues')
        provider._get('Leagues')
        self.assertEqual(transport.post_json.call_count, 1)
        args, kwargs = transport.post_json.call_args
        self.assertNotIn('private-test-key', args[0])
        self.assertEqual(kwargs['form']['APIkey'], 'private-test-key')


class EvidenceAndNumericalTests(unittest.TestCase):
    def test_unvalidated_market_has_zero_stake_even_with_high_estimated_ev(self):
        from lisa.board import OpportunityBoard, Fixture, MarketPrice
        from lisa.dixon_coles import DixonColesModel
        model = DixonColesModel()
        model.fit([ScoredMatch('soccer_epl', NOW - timedelta(days=i + 1), 'A', 'B', 2, 1)
                   for i in range(30)], as_of=NOW)
        fixture = Fixture('test', 'soccer_epl', NOW + timedelta(hours=3), 'A', 'B')
        quotes = [MarketPrice('test', 'Home', 10., 'testbook', market='h2h', updated_at=NOW)]
        board = OpportunityBoard(model).build([fixture], {'test': quotes}, now=NOW)
        self.assertTrue(board.earning)
        self.assertTrue(all(o.stake_fraction == 0 for o in (*board.winning, *board.earning, *board.micro_bets)))

    def test_missing_stale_future_or_nonfinite_quotes_cannot_create_an_edge(self):
        from lisa.board import OpportunityBoard, Fixture, MarketPrice
        from lisa.dixon_coles import DixonColesModel
        model = DixonColesModel()
        model.fit([ScoredMatch('soccer_epl', NOW - timedelta(days=i + 1), 'A', 'B', 2, 1)
                   for i in range(30)], as_of=NOW)
        fixture = Fixture('test', 'soccer_epl', NOW + timedelta(hours=3), 'A', 'B')
        prices = [MarketPrice('test', 'Home', 10., 'testbook', updated_at=stamp)
                  for stamp in (None, NOW - timedelta(hours=1), NOW + timedelta(hours=1))]
        prices.append(MarketPrice('test', 'Home', float('inf'), 'testbook', updated_at=NOW))
        board = OpportunityBoard(model).build([fixture], {'test': prices}, now=NOW)
        self.assertEqual(board.earning, ())
        self.assertEqual(board.coverage.fixtures_priced, 0)

    def test_goal_tail_retains_mass_at_high_scoring_boundary(self):
        from lisa.dixon_coles import DixonColesModel
        model = DixonColesModel(base_mu=8, home_adv=0, rho=0)
        matrix = model.score_matrix('A', 'B')
        self.assertGreater(len(matrix), 12)
        self.assertAlmostEqual(sum(sum(row) for row in matrix), 1.)
        self.assertAlmostEqual(sum(h * p for h, row in enumerate(matrix) for p in row), 8., places=7)
        self.assertAlmostEqual(sum(a * p for row in matrix for a, p in enumerate(row)), 8., places=7)

    def test_football_data_penalties_do_not_grade_regulation_markets_as_a_win(self):
        from lisa.providers.football_data import FootballDataProvider
        payload = {'id': 1, 'utcDate': NOW.isoformat(), 'status': 'FINISHED',
                   'homeTeam': {'id': 1, 'name': 'A'}, 'awayTeam': {'id': 2, 'name': 'B'}, 'season': {},
                   'score': {'duration': 'PENALTY_SHOOTOUT', 'fullTime': {'home': 7, 'away': 6},
                             'regularTime': {'home': 1, 'away': 1}}}
        fixture = FootballDataProvider('test', transport=Mock())._to_fixture(payload, 'soccer_epl', 'Test')
        self.assertEqual((fixture['home_score'], fixture['away_score']), (1, 1))
        del payload['score']['regularTime']
        fixture = FootballDataProvider('test', transport=Mock())._to_fixture(payload, 'soccer_epl', 'Test')
        self.assertFalse(fixture['completed'])
