"""Persisted calendar, publication shaping and date-boundary regressions."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lisa import config, feed
from lisa.calendar_snapshot import CALENDAR_KEY, calendar_snapshot, saved_daily_board
from lisa.daily_service import DailyService
from lisa.observability import FIXTURES_KEY
from lisa.dashboard import build_dashboard
from lisa.storage import SqliteStorage
from test_daily_service_unittest import Handler, report, NOW, LEAGUE


def fixture(at, **overrides):
    return dict({'match_id': 'test-'+at.isoformat(), 'sport_key': LEAGUE,
        'home_team': 'Test Home', 'away_team': 'Test Away', 'kickoff': at.isoformat(),
        'epoch': at.timestamp(), 'completed': False, 'status': 'SCHEDULED',
        'provider': 'test', 'home_score': None, 'away_score': None}, **overrides)


class CalendarSnapshotTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.storage = SqliteStorage(str(Path(folder.name)/'test.db'))
        self.addCleanup(self.storage.close)
        self.settings = config.Settings(product_timezone='Africa/Lagos', paper_mode=True,
                                       enable_sharpapi=False, enable_sportsdb=False)

    def test_lagos_midnight_week_is_inclusive_and_completed_is_not_upcoming(self):
        now = datetime(2026,10,3,22,30,tzinfo=timezone.utc)
        rows = [fixture(now+timedelta(minutes=20)), fixture(now+timedelta(minutes=40)),
                fixture(now+timedelta(days=3)), fixture(now+timedelta(days=6)),
                fixture(now-timedelta(hours=1),completed=True,status='FINISHED',home_score=2,away_score=1)]
        self.storage.set_telemetry(CALENDAR_KEY, calendar_snapshot(rows,now))
        result = saved_daily_board(self.storage,self.settings,now=now)
        self.assertEqual([len(result['board'][k]) for k in ('today','tomorrow','this_week','all_upcoming')], [2,1,5,4])
        self.assertEqual(result['timezone'],'Africa/Lagos')

    def test_settlement_updates_scores_without_a_generation_cycle(self):
        at = NOW+timedelta(hours=1)
        self.storage.set_telemetry(CALENDAR_KEY, calendar_snapshot([fixture(at)],NOW))
        result = fixture(at,provider='second-source',match_id='alternate-id',completed=True,
                         status='FINISHED',home_score=2,away_score=1)
        self.storage.set_telemetry('settlement:'+FIXTURES_KEY,calendar_snapshot([result],NOW+timedelta(hours=3)))
        entries = saved_daily_board(self.storage,self.settings,now=NOW)['board']['this_week']
        self.assertEqual(len(entries),1)
        self.assertEqual((entries[0]['score'],entries[0]['source']),([2,1],'second-source'))

    def test_invalid_or_outside_window_rows_are_withheld_and_truncation_is_explicit(self):
        rows = [fixture(NOW+timedelta(days=8)), {'kickoff':'malformed'},
                fixture(NOW+timedelta(hours=1),home_score=True,away_score=0)]
        snapshot = calendar_snapshot(rows,NOW)
        self.assertEqual(len(snapshot['rows']),1)
        self.assertIsNone(snapshot['rows'][0]['score'])
        self.storage.set_telemetry(CALENDAR_KEY,dict(snapshot,truncated=True))
        self.assertTrue(saved_daily_board(self.storage,self.settings,now=NOW)['provenance']['truncated'])

    def test_existing_short_observations_work_without_a_new_snapshot(self):
        self.storage.set_telemetry(FIXTURES_KEY,calendar_snapshot([fixture(NOW+timedelta(hours=1))],NOW))
        self.assertEqual(len(saved_daily_board(self.storage,self.settings,now=NOW)['board']['this_week']),1)

    def test_calendar_endpoint_never_calls_providers_or_invents_source_coverage(self):
        handler = Handler(SimpleNamespace(storage=self.storage,settings=self.settings))
        with patch('lisa.daily_board.DailyBoardBuilder.build_from_leagues') as fetch:
            handler._handle_daily_board()
        fetch.assert_not_called()
        self.assertEqual(handler.response['state'],'starting')
        self.assertEqual(handler.response['provenance']['sources'],[])
        self.assertEqual(handler.response['provenance']['provider_requests'],0)

    def test_generation_saves_calendar_even_when_model_cannot_publish(self):
        empty = feed.FeedReport(began=NOW,fixture_updates=[fixture(NOW+timedelta(hours=1))],
                                errors=['No completed training results'])
        service = DailyService(self.storage,self.settings,runner=Mock(return_value=empty),providers=feed.ProviderSet())
        service.tick(now=NOW)
        self.assertIsNone(service.read())
        self.assertEqual(len(saved_daily_board(self.storage,self.settings,now=NOW)['board']['this_week']),1)

    def test_dashboard_marks_public_model_rows_and_exposes_publication_progress(self):
        service = DailyService(self.storage,self.settings,runner=Mock(return_value=report()),providers=feed.ProviderSet())
        service.tick(now=NOW)
        result = build_dashboard(self.storage,self.settings)
        self.assertTrue(all(r['model_forecast'] for r in result['active_picks']))
        self.assertTrue(result['pipeline']['paper_mode'])
        self.assertEqual(result['pipeline']['upcoming_selections'],1)
        self.assertEqual(result['pipeline']['selected_matches'],1)
        self.assertIsNotNone(result['pipeline']['published_at'])

    def test_past_kickoff_predictions_remain_visible_while_awaiting_final_results(self):
        service = DailyService(self.storage,self.settings,runner=Mock(return_value=report()),providers=feed.ProviderSet())
        service.tick(now=NOW)
        with patch('lisa.odds.utcnow',return_value=NOW+timedelta(hours=3)):
            result = build_dashboard(self.storage,self.settings)
        self.assertEqual(len(result['active_picks']),0)
        self.assertEqual(len(result['awaiting_results']),3)
        self.assertEqual(len(result['settled_ledger']),0)

    def test_total_provider_outage_keeps_last_calendar_and_original_observation_time(self):
        original = calendar_snapshot([fixture(NOW+timedelta(hours=1))],NOW)
        self.storage.set_telemetry(CALENDAR_KEY,original)
        failure = feed.FeedReport(began=NOW,errors=['All providers unavailable'])
        service = DailyService(self.storage,self.settings,runner=Mock(return_value=failure),providers=feed.ProviderSet())
        service.tick(now=NOW+timedelta(minutes=1))
        self.assertEqual(self.storage.get_telemetry(CALENDAR_KEY),original)


if __name__ == '__main__':
    unittest.main()
