import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from lisa.providers.the_odds_api import TheOddsApiProvider, Snapshot
from lisa.providers.base import ProviderNotAvailableError
from lisa.board import Fixture
from lisa.config import Settings
from lisa import feed

NOW = datetime.now(timezone.utc)
SPORT = 'soccer_epl'


def event(sport=SPORT):
    return {'id': 'event', 'sport_key': sport, 'home_team': 'Home FC', 'away_team': 'Away FC',
        'commence_time': (NOW+timedelta(hours=2)).isoformat(), 'bookmakers': [{
        'key': 'book', 'title': 'Book', 'last_update': (NOW-timedelta(minutes=1)).isoformat(),
        'markets': [{'key': 'h2h', 'outcomes': [{'name': 'Home FC', 'price': 2.1},
            {'name': 'Away FC', 'price': 3.2}, {'name': 'Draw', 'price': 3.1}]},
            {'key': 'totals', 'outcomes': [{'name': 'Over', 'point': 2.5, 'price': 1.95},
                {'name': 'Under', 'point': 2.5, 'price': 1.95}]}]}]}


class TheOddsApiTests(unittest.TestCase):
    def setUp(self):
        from lisa.storage import SqliteStorage
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.storage = SqliteStorage(str(Path(self.folder.name)/'test.db'))
        self.transport = Mock()
        self.transport.request.side_effect = self.request
        self.provider = self.make()

    def make(self, **kwargs):
        provider = TheOddsApiProvider('private-key', transport=self.transport, **kwargs)
        provider.storage = self.storage
        return provider

    def request(self, path, params, key):
        headers = {'x-requests-used': '0' if path == '/v4/sports' else '2', 'x-requests-remaining': '500' if path == '/v4/sports' else '498'}
        if path == '/v4/sports':
            return [{'key': SPORT, 'active': True, 'has_outrights': False}], headers
        return [event()], headers

    def fetch(self, provider=None):
        return (provider or self.provider).fetch(sport_keys=[SPORT], window_hours=24, now=NOW)

    def test_core_contracts_and_cache_preserve_provider_time(self):
        report = self.fetch()
        self.assertEqual(len(report.quotes), 5)
        self.assertEqual({q.market for q in report.quotes}, {'h2h','totals'})
        self.assertEqual(report.quotes[0].updated_at, NOW-timedelta(minutes=1))
        self.fetch()
        self.assertEqual(self.transport.request.call_count, 2)
        self.assertNotIn('private-key', str(self.storage.admin_get_telemetry()))

    def test_concurrent_credit_reservations_account_for_two_markets(self):
        quota = {'used':0, 'limit':10}
        def reserve(_):
            try:
                self.make(monthly_limit=10, reserve=2)._reserve(quota, NOW)
                return True
            except ProviderNotAvailableError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(reserve,range(10))),4)

    def test_daily_cap_rolls_back_monthly_increment(self):
        provider = self.make(daily_limit=2)
        provider._reserve({'used':0,'limit':500}, NOW)
        with self.assertRaises(ProviderNotAvailableError):
            provider._reserve({'used':0,'limit':500}, NOW)
        with self.storage._tx() as conn:
            row=conn.execute('SELECT requests FROM provider_daily_usage WHERE provider=? AND day=?',
                ('the_odds_api',provider.scope)).fetchone()
        self.assertEqual(row['requests'],2)

    def test_server_consumption_floor_survives_stale_catalog(self):
        self.provider._status({'x-requests-used':'450','x-requests-remaining':'50'})
        with self.assertRaises(ProviderNotAvailableError):
            self.provider._reserve({'used':0,'limit':500},NOW)

    def test_replacement_key_checks_server_usage_before_paid_requests(self):
        self.transport.request.return_value = None
        self.transport.request.side_effect=lambda path,params,key: (
            [{'key':SPORT,'active':True,'has_outrights':False}],
            {'x-requests-used':'450','x-requests-remaining':'50'})
        provider=TheOddsApiProvider('replacement-key',transport=self.transport)
        provider.storage=self.storage
        result=self.fetch(provider)
        self.assertEqual(result.stopped_because,'credit budget')
        self.assertEqual(self.transport.request.call_count,1)

    def test_two_way_home_away_market_is_not_inferred_as_three_way(self):
        row=event()
        row['bookmakers'][0]['markets'][0]['outcomes'].pop()
        quotes=self.provider.parse([row],SPORT,NOW,24,Snapshot())
        self.assertEqual({q.market for q in quotes},{'totals'})

    def test_ambiguous_fixtures_are_rejected(self):
        quotes=self.fetch().quotes
        fixture=Fixture('fixture', SPORT, NOW+timedelta(hours=2), 'Home FC', 'Away FC')
        matched=self.provider.match(quotes,[fixture])
        self.assertEqual(matched.matched_fixtures,1)
        self.assertEqual(matched.prices['fixture'][0].source,'the_odds_api')
        self.assertEqual(self.provider.match(quotes,[fixture,replace(fixture,match_id='other')]).matched_fixtures,0)

    def test_invalid_or_future_prices_are_not_accepted(self):
        row=event()
        row['bookmakers'][0]['last_update']=(NOW+timedelta(minutes=1)).isoformat()
        self.assertFalse(self.provider.parse([row],SPORT,NOW,24,Snapshot()))

    def test_new_integration_is_separate_from_disabled_legacy_client(self):
        settings=Settings(the_odds_enabled=True,odds_api_key='private-key')
        providers=feed.build_providers(settings)
        self.assertIn('the_odds_api',[p.name for p in providers.prices])
        self.assertFalse(settings.odds_api_enabled)


if __name__ == '__main__':
    unittest.main()
