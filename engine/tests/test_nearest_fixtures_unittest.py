"""Nearest verified fixtures, chronological picks and autonomous cold starts."""
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lisa import config, feed
from lisa.board import Opportunity, OpportunityBoard, MAX_WINNING_LADDER, MAX_MICRO_BETS
from lisa.calendar_snapshot import saved_daily_board
from lisa.daily_service import DailyService
from lisa.runtime import RuntimeConfig
from lisa.storage import SqliteStorage
from lisa.providers.base import FixturesResult
from lisa.providers.calendar import normalise_fixture

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)
LEAGUE = 'soccer_epl'


def fixture(hours, match_id=None, **changes):
    result = normalise_fixture(provider='football_data', sport_key=LEAGUE,
        match_id=match_id or str(hours), kickoff_epoch=(NOW+timedelta(hours=hours)).timestamp(),
        home='Test Home', away='Test Away', status='SCHEDULED')
    result.update(changes)
    return result


def history():
    return [fixture(-24*i, f'history-{i}', completed=True, status='FINISHED',
                    home_score=1+i%3, away_score=i%2) for i in range(1, 31)]


def source(rows):
    return SimpleNamespace(name='football_data', leagues=lambda: (LEAGUE,),
        get_fixtures=Mock(return_value=FixturesResult('football_data', LEAGUE, tuple(rows))))


class NearestFixtureTests(unittest.TestCase):
    def run_feed(self, rows, *, target=3, window=24):
        calendar = source(history()+rows)
        price = SimpleNamespace(name='counted', is_available=lambda: True,
            fetch=Mock(side_effect=RuntimeError('test outage')))
        report = feed.run_feed(config.Settings(board_leagues=(LEAGUE,),
            board_window_hours=float(window), board_volume_target=target), now=NOW,
            providers=feed.ProviderSet(calendar=[calendar], prices=[price]))
        return report, calendar, price

    def test_default_reaches_matches_beyond_seven_days(self):
        report, calendar, price = self.run_feed([fixture(24*14), fixture(24*15)])
        self.assertEqual(report.forecast['count'], 2)
        self.assertEqual(report.window_hours, 24*15)
        self.assertEqual(report.board.window_hours, report.window_hours)
        self.assertEqual(report.fixtures_in_window, 2)
        self.assertEqual(sum(report.leagues.values()), 2)
        self.assertTrue(report.window_selection['expanded'])
        self.assertEqual(report.window_selection['configured_hours'], 24)
        calendar.get_fixtures.assert_called_once()
        price.fetch.assert_called_once()
        self.assertEqual(price.fetch.call_args.kwargs['window_hours'], report.window_hours)

    def test_reaches_only_nearest_volume_not_entire_season(self):
        report, _, _ = self.run_feed([fixture(h) for h in (500, 105, 72, 96, 200)], target=3)
        self.assertEqual(report.window_hours, 105)
        self.assertEqual([r['match_id'] for r in report.forecast['matches']],
                         ['football_data:72', 'football_data:96', 'football_data:105'])

    def test_same_kickoff_group_is_not_split_at_volume_target(self):
        rows = [fixture(96, f'tie-{i}', home_team=f'Tie Home {i}') for i in range(5)]
        rows += [fixture(-1000-i, f'tie-history-{i}', home_team=f'Tie Home {i}',
            completed=True, status='FINISHED', home_score=2, away_score=1) for i in range(5)]
        report, _, _ = self.run_feed(rows, target=2)
        self.assertEqual(report.forecast['count'], 5)
        self.assertEqual(report.window_hours, 96)

    def test_sufficient_nearby_matches_do_not_expand(self):
        report, _, _ = self.run_feed([fixture(h) for h in (2, 6, 20, 96)], target=3)
        self.assertEqual(report.window_hours, 24)
        self.assertFalse(report.window_selection['expanded'])
        self.assertEqual(report.forecast['count'], 3)

    def test_subhour_kickoff_is_included_and_clock_is_recomputed(self):
        report, calendar, _ = self.run_feed([fixture(96.25)], target=1)
        self.assertEqual(report.window_hours, 97)
        self.assertEqual(report.forecast['count'], 1)
        later = feed.run_feed(config.Settings(board_leagues=(LEAGUE,), board_volume_target=1),
            now=NOW+timedelta(hours=95), providers=feed.ProviderSet(calendar=[calendar]))
        self.assertEqual(later.window_hours, 24)
        self.assertEqual(later.forecast['count'], 1)
        final = feed.run_feed(config.Settings(board_leagues=(LEAGUE,)),
            now=NOW+timedelta(hours=97), providers=feed.ProviderSet(calendar=[calendar]))
        self.assertEqual(final.forecast['count'], 0)

    def test_unknown_teams_do_not_block_the_next_forecastable_fixture(self):
        report, _, _ = self.run_feed([fixture(2, home_team='Unknown'), fixture(100)], target=1)
        self.assertEqual(report.window_hours, 100)
        self.assertEqual(report.forecast['count'], 1)
        self.assertEqual(report.window_selection['available_verified'], 2)
        self.assertEqual(report.window_selection['available_modelled'], 1)

    def test_noneligible_rows_never_expand_or_spend_price_quota(self):
        rows = [fixture(-2), fixture(0)]
        rows += [fixture(100, status=status) for status in ('POSTPONED', 'CANCELLED', 'LIVE')]
        rows += [fixture(200, discovery_only=True), fixture(300, kickoff_time_known=False),
                 fixture(400, completed=True, status='FINISHED', home_score=2, away_score=1)]
        report, _, price = self.run_feed(rows)
        self.assertEqual(report.window_hours, 24)
        self.assertEqual(report.forecast['count'], 0)
        self.assertEqual(report.window_selection['available_verified'], 0)
        price.fetch.assert_not_called()

    def test_zero_volume_target_still_finds_the_next_match(self):
        report, _, _ = self.run_feed([fixture(100), fixture(200)], target=0)
        self.assertEqual(report.window_hours, 100)
        self.assertEqual(report.forecast['count'], 1)

    def test_no_training_results_cannot_fabricate_forecasts(self):
        report = feed.run_feed(config.Settings(board_leagues=(LEAGUE,)), now=NOW,
            providers=feed.ProviderSet(calendar=[source([fixture(100)])]))
        self.assertIsNone(report.board)
        self.assertTrue(report.errors)

    def test_untrained_distant_fixture_still_appears_in_the_default_calendar(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteStorage(str(Path(directory)/'untrained.db'))
            try:
                settings = config.Settings(board_leagues=(LEAGUE,))
                service = DailyService(store, settings, settle_in_cycle=False,
                    providers=feed.ProviderSet(calendar=[source([fixture(24*14)])]))
                service.tick(now=NOW)
                self.assertIsNone(service.read())
                board = saved_daily_board(store, settings, now=NOW)['board']
                self.assertEqual(len(board['all_upcoming']), 1)
                self.assertEqual(board['this_week'], [])
            finally:
                store.close()

    def test_cold_start_and_restart_need_no_override_or_visitor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory)/'cold.db')
            for attempt in range(2):
                store = SqliteStorage(path)
                try:
                    runtime = RuntimeConfig(config.Settings(board_leagues=(LEAGUE,)), storage=store)
                    self.assertNotIn('board_window_hours', runtime.overrides())
                    calendar = source(history()+[fixture(24*14)])
                    service = DailyService(store, runtime.settings,
                        providers=feed.ProviderSet(calendar=[calendar]), settle_in_cycle=False)
                    done = threading.Event()
                    original_tick = service.tick
                    def tick():
                        try:
                            return original_tick()
                        finally:
                            done.set()
                    with patch.object(service, 'tick', side_effect=tick):
                        service.start()
                        try:
                            self.assertTrue(done.wait(5))
                        finally:
                            service.stop()
                    publication = service.read()
                    self.assertIsNotNone(publication)
                    self.assertEqual(publication['forecast']['count'], 1)
                    self.assertTrue(publication['window_selection']['expanded'])
                    upcoming = saved_daily_board(store, runtime.settings())['board']
                    self.assertEqual(len(upcoming['all_upcoming']), 1)
                    self.assertEqual(upcoming['this_week'], [])
                    count = store.count_picks()['total']
                    self.assertGreater(count, 0)
                    if attempt:
                        self.assertEqual(count, previous_count)
                    previous_count = count
                    calendar.get_fixtures.assert_called_once()
                finally:
                    store.close()


def pick(index, *, hours=2, probability=.6, market='h2h', **changes):
    values = dict(match_id=str(index), sport_key=LEAGUE,
        kickoff=NOW+timedelta(hours=hours), home='Home '+str(index), away='Away',
        market=market, selection='Home' if market == 'h2h' else 'Yes',
        p_model=probability, fair_odds=1/probability)
    values.update(changes)
    if values.get('priced'):
        values.setdefault('best_odds', 1.3)
    return Opportunity(**values)


class ChronologicalPickTests(unittest.TestCase):
    def setUp(self):
        # These checks isolate ordering from the configurable odds floor.
        self.board = OpportunityBoard(Mock(), min_fair_odds=1.05)

    def test_winning_probability_is_secondary_to_kickoff_and_strongest_side_is_kept(self):
        candidates = [pick('later', hours=100, probability=.9),
            pick('early-low', probability=.5), pick('early-high', probability=.8),
            pick('early-low', probability=.7, market='double_chance', selection='1X')]
        self.assertEqual([(p.match_id, p.p_model) for p in self.board._winning_ladder(candidates)],
            [('early-high', .8), ('early-low', .7), ('later', .9)])

    def test_winning_display_limit_cannot_drop_the_nearest_match(self):
        candidates = [pick('nearest', probability=.5)] + [
            pick(i, hours=100+i, probability=.9) for i in range(MAX_WINNING_LADDER+5)]
        result = self.board._winning_ladder(candidates)
        self.assertEqual(len(result), MAX_WINNING_LADDER)
        self.assertEqual(result[0].match_id, 'nearest')

    def test_micro_display_limit_cannot_drop_the_nearest_match_or_prefer_market_type(self):
        candidates = [pick('nearest', probability=.6, market='btts')] + [
            pick(i, hours=100+i, probability=.9, market='double_chance')
            for i in range(MAX_MICRO_BETS+5)]
        result = self.board._micro_bets(candidates, excluded=set())
        self.assertEqual(len(result), MAX_MICRO_BETS)
        self.assertEqual(result[0].match_id, 'nearest')
        ties = self.board._micro_bets([pick('low', probability=.6, market='totals'),
            pick('high', probability=.8, market='btts')], excluded=set())
        self.assertEqual(ties[0].match_id, 'high')

    def test_earning_requires_real_ev_and_orders_urgency_then_probability(self):
        candidates = [pick('later', hours=100, probability=.9, priced=True, ev=.5),
            pick('early-low', probability=.5, priced=True, ev=.4),
            pick('early-high', probability=.8, priced=True, ev=.1),
            pick('unpriced', hours=1), pick('no-edge', hours=1, priced=True, ev=-.1)]
        result = self.board._earning_ladder(candidates)
        self.assertEqual([p.match_id for p in result], ['early-high', 'early-low', 'later'])

    def test_accumulators_prioritize_earliest_leg_without_duplicate_fixtures(self):
        candidates = [pick('later-a', hours=100, probability=.9),
            pick('later-b', hours=120, probability=.9), pick('nearest', probability=.6)]
        result = self.board._accumulators([], {}, candidates=candidates)
        self.assertTrue(result)
        self.assertTrue(any(p.match_id == 'nearest' for p in result[0].legs))
        self.assertTrue(all(len({p.match_id for p in a.legs}) == len(a.legs) for a in result))
        self.assertTrue(all(p.stake_fraction == 0 for a in result for p in a.legs))


if __name__ == '__main__':
    unittest.main()
