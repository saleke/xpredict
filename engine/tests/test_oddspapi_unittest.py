import json
import tempfile
import unittest
import importlib.util
import io
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lisa.config import Settings
from lisa.runtime import RuntimeConfig, OverrideError
from lisa.provider_credentials import CredentialStore
from lisa.providers.oddspapi import OddsPapiProvider
from lisa.providers.sharpapi import SharpQuote
from lisa.board import Fixture
from lisa.storage import SqliteStorage
from lisa.odds_history import OddsHistoryRepository
from lisa.admin_api import AdminAPI, AdminError
from lisa import feed

NOW = datetime.now(timezone.utc)
LEAGUE = 'soccer_epl'


def account_payload(count=0):
    return {'api_key': 'should-never-be-stored', 'current_subscription_id': 'subscription',
        'subscriptions': [{'subscription_id': 'subscription', 'is_active': True,
            'valid_from': '2026-10-01T00:00:00Z', 'request_limit': 250, 'request_count': count,
            'sport_ids': [10], 'bookmakers': {'testbook': {}}}]}


class OddsPapiTests(unittest.TestCase):
    def test_direct_probe_never_reports_raw_account_credentials(self):
        spec = importlib.util.spec_from_file_location('direct_probe',
            Path(__file__).resolve().parents[2] / 'scripts' / 'probe_oddspapi.py')
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        settings = Settings(oddspapi_key='private-direct-key')
        provider = Mock()
        provider.account.return_value = {'limit':250,'used':0,'books':['testbook'],
            'api_key':'raw-account-secret','scope':'private-account-identity'}
        runtime = Mock()
        runtime.settings.return_value = settings
        output = io.StringIO()
        with patch.object(probe, 'load_settings', return_value=settings), \
             patch.object(probe, 'RuntimeConfig', return_value=runtime), \
             patch.object(probe.socket, 'getaddrinfo'), \
             patch.object(probe, 'OddsPapiProvider', return_value=provider), \
             patch('lisa.cli._make_storage', return_value=self.storage), \
             patch('sys.argv', ['probe_oddspapi.py', '--account-only']), redirect_stdout(output):
            self.assertEqual(probe.main(),0)
        report = json.loads(output.getvalue())
        self.assertEqual(report['state'],'account_sample_received')
        for secret in ('private-direct-key','raw-account-secret','private-account-identity'):
            self.assertNotIn(secret,output.getvalue())
        provider.fetch.assert_not_called()

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name)
        self.storage = SqliteStorage(str(self.path / 'test.db'))
        self.transport = Mock()
        self.provider = OddsPapiProvider('private-key', transport=self.transport)
        self.provider.storage = self.storage

    def test_raw_account_credential_never_enters_cache(self):
        self.transport.request.return_value = account_payload()
        self.provider.account()
        with self.storage._tx() as conn:
            values = conn.execute('SELECT data FROM live_cache').fetchall()
        self.assertNotIn('should-never-be-stored', json.dumps([dict(v) for v in values]))
        self.provider.account()
        self.assertEqual(self.transport.request.call_count, 1)

    def test_key_replacement_and_restart_do_not_reset_subscription_quota(self):
        self.transport.request.side_effect = lambda path, params, key: account_payload() if path == 'account' else []
        for key in ('old-key', 'replacement-key'):
            provider = OddsPapiProvider(key, transport=self.transport, monthly_limit=3, reserve=1)
            provider.storage = SqliteStorage(str(self.path / 'test.db'))
            provider._get('markets', {'key-independent-test-param': key})
        third = OddsPapiProvider('another-key', transport=self.transport, monthly_limit=3, reserve=1)
        third.storage = self.storage
        with self.assertRaises(Exception):
            third._get('participants', {'sportId': 10})
        self.assertEqual(sum(call.args[0] != 'account' for call in self.transport.request.call_args_list), 2)

    def test_atomic_budget_reservations_across_threads(self):
        quota = {'scope': 'same-account', 'used': 0, 'limit': 8, 'checked_at': NOW.isoformat()}
        def reserve(_):
            p = OddsPapiProvider('key', monthly_limit=8, reserve=2)
            p.storage = self.storage
            try:
                p._reserve(quota)
                return 1
            except Exception:
                return 0
        with ThreadPoolExecutor(max_workers=8) as pool:
            accepted = list(pool.map(reserve, range(12)))
        self.assertEqual(sum(accepted), 6)

    def test_remote_consumption_restricts_local_budget(self):
        account = {'scope': 'same-account', 'used': 225, 'limit': 250, 'checked_at': NOW.isoformat()}
        with self.assertRaises(Exception):
            self.provider._reserve(account)
        self.provider._reserve(account, unmetered=True)
        account['used'] = 250
        with self.assertRaises(Exception):
            self.provider._reserve(account, unmetered=True)

    def test_unknown_player_and_wrong_period_markets_are_not_modelled(self):
        base = {'sportId': 10, 'period': 'fulltime', 'playerProp': False,
                'marketName': 'Over Under Full Time', 'handicap': 2.25}
        self.assertEqual(self.provider.contract(base, {'outcomeName': 'Over'}), ('totals', 'Over', 2.25))
        for changes in ({'period': 'firsthalf'}, {'playerProp': True}, {'handicap': 2.3},
                        {'marketName': 'Over Under Corners'}, {'sportId': 11}):
            self.assertIsNone(self.provider.contract(dict(base, **changes), {'outcomeName': 'Over'}))

    def test_batched_fetch_archives_unsupported_prices_and_does_not_invent_freshness(self):
        kickoff = NOW + timedelta(hours=2)
        meta = {'marketId': 101, 'marketName': 'Full Time Result', 'period': 'fulltime',
            'sportId': 10, 'playerProp': False, 'outcomes': [{'outcomeId': 101, 'outcomeName': '1'}]}
        event = {'fixtureId': 'event', 'sportId': 10, 'tournamentId': 17, 'statusId': 0,
            'startTime': kickoff.isoformat(), 'participant1Id': 1, 'participant2Id': 2,
            'bookmakerOdds': {'testbook': {'bookmakerIsActive': True, 'suspended': False,
                'markets': {'101': {'marketActive': True, 'outcomes': {'101': {'players': {
                    '0': {'active': True, 'price': 2.2, 'changedAt': NOW.isoformat()},
                    '7': {'active': True, 'price': 4.0, 'changedAt': NOW.isoformat()}}}}}}}}}
        payloads = {'account': account_payload(), 'tournaments': [{'tournamentId': 17,
            'categorySlug': 'england', 'tournamentSlug': 'premier-league'}],
            'markets': [meta], 'participants': {'1': 'A', '2': 'B'}, 'odds-by-tournaments': [event]}
        self.transport.request.side_effect = lambda path, params, key: payloads[path]
        snapshot = self.provider.fetch(sport_keys=[LEAGUE], window_hours=24, now=NOW)
        self.assertEqual(snapshot.error, '')
        self.assertEqual(len(snapshot.quotes), 1)
        self.assertIsNone(snapshot.quotes[0].updated_at)
        history = OddsHistoryRepository(self.storage)
        self.assertEqual(len(history.observations('oddspapi', 'event')), 2)
        self.provider.fetch(sport_keys=[LEAGUE], window_hours=24, now=NOW)
        self.assertEqual(len(history.observations('oddspapi', 'event')), 2)
        self.assertEqual(self.transport.request.call_count, 5)

    def test_same_team_names_in_other_league_cannot_join(self):
        kickoff = NOW + timedelta(hours=1)
        quote = SharpQuote('event', 'h2h', 'Home', 2., 'book', kickoff=kickoff,
                           home='A', away='B', sport_key=LEAGUE)
        fixtures = [Fixture('other', 'soccer_spain_la_liga', kickoff, 'A', 'B')]
        self.assertEqual(self.provider.match([quote], fixtures).matched_fixtures, 0)

    def test_history_does_not_manufacture_bookmaker_update_time(self):
        self.transport.request.side_effect = lambda path, params, key: account_payload() if path == 'account' else {
            'fixtureId': 'event', 'bookmakers': {'testbook': {'markets': {'101': {'outcomes': {
                '101': {'players': {'0': [{'createdAt': (NOW-timedelta(days=1)).isoformat(),
                                         'price': 2.0, 'active': True}]}}}}}}}}
        summary = self.provider.historical('event', ['testbook'])
        self.assertEqual(summary['observations'], 1)
        rows = OddsHistoryRepository(self.storage).observations('oddspapi', 'event')
        self.assertIsNone(rows[0]['bookmaker_changed_at'])
        self.assertTrue(rows[0]['historical'])
        with self.storage._tx() as conn:
            self.assertEqual(conn.execute('SELECT count(*) AS n FROM provider_daily_usage').fetchone()['n'], 1)

    def test_price_source_keeps_existing_calendars(self):
        providers = feed.build_providers(Settings(oddspapi_key='new-key', api_football_key='old-key'))
        self.assertIn('oddspapi', [p.name for p in providers.prices])
        self.assertIn('api_football', [p.name for p in providers.calendar])


class CredentialTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'credentials.json'
        self.store = CredentialStore(self.path)
        self.settings = Settings(provider_credentials_path=str(self.path), oddspapi_key='base-private')

    def test_replacement_reaches_existing_worker_runtime_and_survives_restart(self):
        web, worker = RuntimeConfig(self.settings), RuntimeConfig(self.settings)
        self.store.update('oddspapi', 'replacement-private')
        self.assertEqual(worker.settings().oddspapi_key, 'replacement-private')
        self.assertEqual(RuntimeConfig(self.settings).settings().oddspapi_key, 'replacement-private')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        output = json.dumps({'fields': web.describe(), 'overrides': web.overrides()})
        self.assertNotIn('replacement-private', output)
        self.assertNotIn('base-private', output)

    def test_disable_and_environment_restore_are_distinct(self):
        runtime = RuntimeConfig(self.settings)
        self.store.update('oddspapi', '')
        self.assertEqual(runtime.settings().oddspapi_key, '')
        self.store.update('oddspapi', '', inherit=True)
        self.assertEqual(runtime.settings().oddspapi_key, 'base-private')

    def test_credentials_cannot_use_generic_settings(self):
        runtime = RuntimeConfig(self.settings)
        for name in ('oddspapi_key', 'api_football_key', 'allsports_api_key', 'football_data_token', 'sharpapi_key'):
            with self.assertRaises(OverrideError):
                runtime.apply({name: 'private'})
            self.assertNotIn(name, {r['name'] for r in runtime.describe()})

    def test_insecure_file_permissions_fail_closed(self):
        self.store.update('oddspapi', 'private')
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.store.read()

    def test_concurrent_updates_preserve_other_credentials(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda p: self.store.update(p, p+'-private'), ['oddspapi', 'allsports']))
        self.assertEqual(len(self.store.read()), 2)

    def test_owner_endpoint_never_echoes_or_audits_key(self):
        control = SimpleNamespace(runtime=RuntimeConfig(self.settings), storage=Mock())
        api = AdminAPI(control)
        result = api.put_provider(None, {}, {'credential': 'replacement-private'}, {'id': 'owner'},
                                  'owner', {'provider': 'oddspapi'})
        self.assertNotIn('replacement-private', json.dumps(result))
        audit_args = str(control.storage.log_admin_action.call_args)
        self.assertNotIn('replacement-private', audit_args)
        with self.assertRaises(AdminError):
            api.put_provider(None, {}, {'credential': 'no'}, {'id': 'admin'}, 'admin', {'provider': 'oddspapi'})

    def test_cross_field_quota_validation_is_atomic(self):
        runtime = RuntimeConfig(self.settings)
        with self.assertRaises(OverrideError):
            runtime.apply({'oddspapi_monthly_limit': 20, 'oddspapi_reserve': 25})
        self.assertEqual(runtime.settings().oddspapi_monthly_limit, 250)


if __name__ == '__main__':
    unittest.main()
