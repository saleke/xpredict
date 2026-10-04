import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from lisa import feed
from lisa.config import Settings
from lisa.coverage import api_football_plan, apply_coverage
from lisa.providers.api_football import ApiFootballProvider, COMPETITIONS
from lisa.storage import SqliteStorage


class CoverageTests(unittest.TestCase):
    def test_free_budget_includes_cold_catalog_and_result_reserve(self):
        plan = api_football_plan(100, COMPETITIONS)
        self.assertEqual(plan['settlement_reserve'], 20)
        rounds = plan['full_refreshes_per_day']
        self.assertGreater(rounds, 0)
        self.assertLessEqual(2 * rounds * len(COMPETITIONS) +
            plan['catalog_and_enrichment_allowance'], plan['ordinary_ceiling'])
        self.assertEqual(plan['refresh_interval_sec'], 28800)

    def test_small_budget_reports_incomplete_coverage(self):
        self.assertEqual(api_football_plan(5, COMPETITIONS)['state'],
                         'insufficient_for_full_daily_coverage')
        self.assertEqual(api_football_plan(100, [])['leagues'], [])
        with self.assertRaises(ValueError):
            api_football_plan(True, [])

    def test_worker_configuration_is_persisted_without_network_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = SqliteStorage(str(Path(folder) / 'test.db'))
            transport = Mock()
            api = ApiFootballProvider('secret', transport=transport)
            providers = feed.ProviderSet(calendar=[api])
            settings = Settings(board_leagues=tuple(COMPETITIONS))
            plan = apply_coverage(providers, settings, storage, datetime.now(timezone.utc))
            self.assertEqual(api.calendar_ttl, 28800)
            self.assertEqual(api.odds_ttl, 28800)
            self.assertEqual(api.odds_max_pages, 1)
            self.assertEqual(storage.get_telemetry('coverage:plan'), plan)
            transport.get_json.assert_not_called()
            self.assertNotIn('secret', str(plan))

    def test_result_requests_do_not_reuse_slow_calendar_cache(self):
        transport = Mock()
        transport.get_json.return_value = {'response': [], 'errors': {}}
        api = ApiFootballProvider('secret', transport=transport)
        api._get('/fixtures', {'league': 1}, ttl=28800)
        api._get('/fixtures', {'league': 1}, ttl=43200, purpose='settlement')
        self.assertEqual(transport.get_json.call_count, 2)
        api._get('/fixtures', {'league': 1}, ttl=43200, purpose='settlement')
        self.assertEqual(transport.get_json.call_count, 2)

    def test_backfill_cannot_consume_forecast_and_settlement_allowances(self):
        with tempfile.TemporaryDirectory() as folder:
            transport = Mock()
            transport.get_json.return_value = {'response': [], 'errors': {}}
            api = ApiFootballProvider('secret', transport=transport)
            api.storage = SqliteStorage(str(Path(folder) / 'test.db'))
            for fixture in range(8):
                api._get('/fixtures/statistics', {'fixture': fixture}, purpose='history')
            with self.assertRaises(Exception):
                api._get('/fixtures/statistics', {'fixture': 9}, purpose='history')
            api._get('/fixtures', {'league': 1})
            api._get('/fixtures', {'league': 1}, purpose='settlement')
            self.assertEqual(transport.get_json.call_count, 10)
            # Rebuilding the adapter must not restore the backfill allowance.
            restarted = ApiFootballProvider('replacement', transport=transport)
            restarted.storage = api.storage
            with self.assertRaises(Exception):
                restarted._get('/fixtures/statistics', {'fixture': 10}, purpose='history')
    def test_odds_rotation_and_page_cap_report_partial_coverage(self):
        api = ApiFootballProvider('secret')
        api.coverage_rotation = 1
        api.odds_max_pages = 1
        api.competition = Mock(side_effect=lambda sport: (sport, 2026))
        api._get = Mock(return_value={'response': [], 'paging': {'total': 3}})
        report = api.fetch(sport_keys=['a', 'b'], window_hours=24,
                           now=datetime.now(timezone.utc), max_pages=6)
        self.assertEqual([c.args[0] for c in api.competition.call_args_list], ['b', 'a'])
        self.assertEqual(api._get.call_count, 2)
        self.assertTrue(report.truncated)


if __name__ == '__main__':
    unittest.main()
