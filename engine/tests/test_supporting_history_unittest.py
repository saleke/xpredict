import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from lisa.history_service import HistoryBackfillService
from lisa.storage import SqliteStorage
from lisa.odds_history import OddsHistoryRepository


class SupportingHistoryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = str(Path(folder.name) / 'history.db')
        self.storage = SqliteStorage(self.path)
        self.service = HistoryBackfillService.__new__(HistoryBackfillService)
        self.service.storage = self.storage
        self.provider = Mock()
        self.provider.leagues.return_value = ['soccer_epl']
        self.provider.get_range.return_value = SimpleNamespace(fixtures=[])
        self.providers = Mock()
        self.providers.named.return_value = self.provider
        self.history = Mock()
        self.history.ingest.return_value = 0
        self.now = datetime(2026, 10, 3, tzinfo=timezone.utc)

    def run_batch(self):
        return self.service._supporting_history(self.providers, self.history, 'soccer_epl', self.now)

    def test_odds_history_failures_are_bounded_and_retry_survives_restart(self):
        source = SimpleNamespace(name='oddspapi', historical=Mock(side_effect=RuntimeError('private-key')))
        providers = SimpleNamespace(prices=[source])
        history = OddsHistoryRepository(self.storage)
        for event in ('one', 'two', 'three'):
            history.ingest([{'source': 'oddspapi', 'event_id': event, 'bookmaker': 'testbook',
                'odds': 2.0, 'observed_at': (self.now - timedelta(days=2)).isoformat(),
                'kickoff': (self.now - timedelta(days=1)).isoformat()}])
        status = self.service._offer_history(providers, self.now)
        self.assertEqual((status['attempts'], status['failures']), (2, 2))
        self.assertNotIn('private-key', str(self.storage.get_telemetry('history:oddspapi:retry:one')))
        self.service.storage = SqliteStorage(self.path)
        source.historical.reset_mock()
        source.historical.side_effect = None
        source.historical.return_value = {'observations': 1}
        self.assertEqual(self.service._offer_history(providers, self.now)['events_imported'], 1)
        self.assertEqual(source.historical.call_count, 1)
        source.historical.reset_mock()
        self.assertEqual(self.service._offer_history(providers, self.now)['attempts'], 0)

    def test_restart_continues_next_month_and_handles_year_boundary(self):
        key = 'history:checkpoint:allsports:soccer_epl'
        self.storage.set_telemetry(key, {'next_month': '2025-12-01'})
        self.assertEqual(self.run_batch()['state'], 'ok')
        self.provider.get_range.assert_called_once_with('soccer_epl', '2025-12-01', '2025-12-31', ttl=86400)
        self.service.storage = SqliteStorage(self.path)
        self.run_batch()
        self.assertEqual(self.storage.get_telemetry(key)['next_month'], '2026-02-01')

    def test_failure_retains_cursor_and_sanitizes_error(self):
        key = 'history:checkpoint:allsports:soccer_epl'
        self.storage.set_telemetry(key, {'next_month': '2025-01-01'})
        self.provider.get_range.side_effect = ValueError('credential=secret')
        self.assertEqual(self.run_batch()['state'], 'failed')
        checkpoint = self.storage.get_telemetry(key)
        self.assertEqual(checkpoint['next_month'], '2025-01-01')
        self.assertEqual(checkpoint['error'], 'ValueError')
        self.assertEqual(self.run_batch()['state'], 'waiting')
        self.assertEqual(self.provider.get_range.call_count, 1)

    def test_current_month_stops_at_today_and_refreshes_daily(self):
        self.storage.set_telemetry('history:checkpoint:allsports:soccer_epl', {'next_month': '2026-10-01'})
        self.run_batch()
        self.provider.get_range.assert_called_once_with('soccer_epl', '2026-10-01', '2026-10-03', ttl=86400)
        self.assertEqual(self.run_batch()['state'], 'waiting')


if __name__ == '__main__':
    unittest.main()
