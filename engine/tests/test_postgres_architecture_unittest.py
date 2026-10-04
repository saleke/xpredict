"""Architecture checks without network, plus real PostgreSQL release checks.

Set LISA_TEST_POSTGRES_URL to an isolated database whose name ends in _test.
Integration tests truncate that database's application tables between tests.
"""
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from lisa import config, feed
from lisa.auth import AuthManager
from lisa.daily_service import DailyService
from lisa.postgres_storage import bind_postgres, require_isolated_database
from lisa.settlement_service import SettlementService
from lisa.storage import PostgresStorage, RelationalStorage, SqliteStorage
from lisa.telegram_bot import VerificationRegistry, BankrollFSMManager
from test_daily_service_unittest import NOW, report, final_result, LEAGUE


class ArchitectureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SqliteStorage(str(Path(directory.name) / 'test.db'))
        self.settings = config.Settings(board_leagues=(LEAGUE,))

    def test_parameter_binding_preserves_literals_comments_and_percent(self):
        query = "SELECT '?' AS literal, ? AS bound, 'a%' AS pattern -- ?\n/* ? */"
        self.assertEqual(bind_postgres(query),
            "SELECT '?' AS literal, %s AS bound, 'a%%' AS pattern -- ?\n/* ? */")

    def test_postgres_uses_shared_repositories_not_sqlite_connections(self):
        self.assertTrue(issubclass(PostgresStorage, RelationalStorage))
        self.assertFalse(issubclass(PostgresStorage, SqliteStorage))
        with self.assertRaises(ValueError):
            PostgresStorage('data/lisa.db')

    def test_settlement_does_not_depend_on_model_or_prices(self):
        producer = DailyService(self.store, self.settings, runner=Mock(return_value=report()))
        producer.tick(now=NOW)
        settler = SettlementService(self.store, self.settings, providers=feed.ProviderSet())
        with patch('lisa.settlement_service.feed.fetch_calendar', return_value={LEAGUE: [final_result()]}) as calendar, \
             patch('lisa.feed.run_feed', side_effect=AssertionError('must not fit a model')):
            self.assertEqual(settler.tick(now=NOW + timedelta(days=2))['settled'], 3)
            calendar.assert_called_once_with([], [LEAGUE], [], purpose='settlement')

    def test_readiness_detects_failed_independent_settlement(self):
        producer = DailyService(self.store, self.settings, runner=Mock(return_value=report()))
        producer.tick(now=NOW)
        self.store.set_telemetry('daily:worker_mode', 'separate')
        self.store.set_telemetry('daily:settlement_status', {'last_attempt': NOW.isoformat(), 'error': 'result feed down'})
        self.assertFalse(producer.status(now=NOW)['ready'])
        self.store.set_telemetry('daily:settlement_status', {'last_attempt': NOW.isoformat(), 'error': None})
        self.assertTrue(producer.status(now=NOW)['ready'])

    def test_generation_and_settlement_have_independent_leases(self):
        producer = DailyService(self.store, self.settings)
        settler = SettlementService(self.store, self.settings)
        self.assertTrue(producer._claim(NOW))
        self.assertTrue(settler._claim(NOW))
        self.assertFalse(DailyService(self.store, self.settings)._claim(NOW))

    def test_refresh_request_is_visible_to_separate_worker_instance(self):
        web = DailyService(self.store, self.settings)
        worker = DailyService(self.store, self.settings)
        web.request_refresh()
        self.assertIsNotNone(worker.storage.get_telemetry('daily:refresh_request'))

    def test_telegram_verification_and_bankroll_use_shared_database(self):
        registry = VerificationRegistry(storage=self.store)
        registry.verify('test-user', '100')
        self.assertIsNone(registry.db_path)
        self.assertTrue(VerificationRegistry(storage=self.store).is_verified('test-user'))
        fsm = BankrollFSMManager(storage=self.store)
        fsm.save_profile('100', 'test', 200, 'balanced', 0.25, 'Test book')
        self.assertEqual(BankrollFSMManager(storage=self.store).get_profile('100')['bankroll_amount'], 200)


@unittest.skipUnless(os.environ.get('LISA_TEST_POSTGRES_URL'), 'real PostgreSQL test URL is not configured')
class PostgresIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn = os.environ['LISA_TEST_POSTGRES_URL']
        require_isolated_database(dsn,'_test')
        cls.store = PostgresStorage(dsn)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    def setUp(self):
        with self.store._tx() as conn:
            conn.execute('TRUNCATE picks, users, sessions, live_cache, verified_sessions, '
                'system_telemetry, admin_audit_logs, notification_outbox, worker_leases, '
                'daily_publications, user_bankroll_profiles, pilot_cycles, odds_observations, '
                'match_observations, provider_daily_usage, provider_credentials RESTART IDENTITY CASCADE')
        self.settings = config.Settings(board_leagues=(LEAGUE,))
        self.service = DailyService(self.store, self.settings, runner=Mock(return_value=report()), providers=feed.ProviderSet())

    def test_generate_restart_settle_and_read_analytics(self):
        self.assertEqual(self.service.tick(now=NOW)['predictions_added'], 3)
        reader = DailyService(self.store, self.settings)
        self.assertIsNotNone(reader.read())
        self.service.runner.return_value = report([], [final_result()])
        self.assertEqual(self.service.tick(now=NOW + timedelta(days=2))['settled'], 3)
        self.assertEqual(len(self.store.list_settled_picks()), 3)
        self.assertEqual(self.store.admin_pick_stats()['wins'], 3)
        self.assertEqual(self.store.admin_database_info()['driver'], 'postgres')

    def test_serverless_schema_and_credentials_stay_separate_from_public(self):
        from cryptography.fernet import Fernet
        from lisa.provider_credentials import EncryptedCredentialStore
        cloud = PostgresStorage(pool=self.store.pool,serverless=True)
        with cloud._tx() as conn:
            self.assertEqual(conn.execute('SELECT current_schema() AS name').fetchone()['name'],'lisa_private')
            self.assertEqual({r['version'] for r in conn.execute('SELECT version FROM schema_migrations')},
                             {1,2,3,4,5,6})
            conn.execute('DELETE FROM provider_credentials')
            exposed = conn.execute("SELECT COUNT(*) AS n FROM pg_namespace,aclexplode(nspacl) "
                "WHERE nspname='lisa_private' AND grantee=0 AND privilege_type='USAGE'").fetchone()['n']
            self.assertEqual(exposed,0)
        key = Fernet.generate_key().decode()
        EncryptedCredentialStore(cloud,key).update('oddspapi','artificial-ci-secret')
        self.assertEqual(EncryptedCredentialStore(cloud,key).read()['oddspapi_key'],'artificial-ci-secret')
        with self.store._tx() as conn:
            self.assertEqual(conn.execute('SELECT current_schema() AS name').fetchone()['name'],'public')
            self.assertEqual(conn.execute('SELECT COUNT(*) AS n FROM provider_credentials').fetchone()['n'],0)

    def test_paper_publication_settlement_and_cycle_evidence(self):
        from dataclasses import replace
        from lisa.pilot import PaperGeneration, PaperSettlement, pilot_report
        settings=replace(self.settings,paper_mode=True)
        producer=PaperGeneration(self.store,settings,runner=Mock(return_value=report()),
            providers=feed.ProviderSet(),settle_in_cycle=False)
        self.assertEqual(producer.tick(now=NOW)['predictions_added'],3)
        settler=PaperSettlement(self.store,settings,providers=feed.ProviderSet())
        with patch('lisa.settlement_service.feed.fetch_calendar',return_value={LEAGUE:[final_result()]}):
            self.assertEqual(settler.tick(now=NOW+timedelta(days=2))['settled'],3)
        evidence=pilot_report(self.store,settings,now=NOW+timedelta(days=2))
        self.assertEqual(evidence['storage_driver'],'postgres')
        self.assertEqual(evidence['stake_violations'],0)
        self.assertEqual({r['job'] for r in evidence['jobs']},{'generation','settlement'})

    def test_price_history_and_atomic_provider_credit_reservations(self):
        from lisa.odds_history import OddsHistoryRepository
        from lisa.providers.the_odds_api import TheOddsApiProvider
        from lisa.providers.base import ProviderNotAvailableError
        history=OddsHistoryRepository(self.store)
        rows=[{'source':'test-provider','event_id':'test-event','bookmaker':'test-book',
            'observed_at':NOW.isoformat(),'odds':2.0,'selection':str(i)} for i in range(150)]
        self.assertEqual(history.ingest(rows),150)
        self.assertEqual(history.ingest(rows),0)
        self.assertEqual(len(history.observations('test-provider','test-event')),150)
        def reserve(_):
            provider=TheOddsApiProvider('artificial-test-key',monthly_limit=10,reserve=2)
            provider.storage=self.store
            try:
                provider._reserve({'used':0,'limit':10},NOW)
                return True
            except ProviderNotAvailableError:
                return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(reserve,range(8))),4)

    def test_concurrent_workers_only_one_claims_lease(self):
        workers = [DailyService(self.store, self.settings) for _ in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda worker: worker._claim(NOW), workers))
        self.assertEqual(sum(claims), 1)

    def test_authentication_sessions_and_duplicate_rollback(self):
        auth = AuthManager(storage=self.store)
        user = auth.register_user('test@example.com', 'Strong-passphrase-42', 'Test')
        self.assertEqual(auth.authenticate_user('test@example.com', 'Strong-passphrase-42')['id'], user['id'])
        with self.assertRaises(ValueError):
            auth.register_user('test@example.com', 'Strong-passphrase-42')
        self.assertIsNotNone(auth.get_user_by_id(user['id']))
        session = auth.create_session(user['id'])
        self.assertEqual(auth.validate_session(session['session_id'])['user']['id'], user['id'])

    def test_failed_publication_rolls_back_then_connection_is_reusable(self):
        payload = report()
        payload.model['invalid'] = float('nan')
        self.assertTrue(self.service._claim(NOW))
        with self.assertRaises(ValueError):
            self.service._publish(payload, NOW)
        self.assertEqual(self.store.count_picks()['total'], 0)
        self.assertIsNone(self.store.get_telemetry('daily:board'))

    def test_outbox_ids_and_telemetry(self):
        self.assertTrue(self.store.enqueue_notification('test-pick', 'artificial test alert'))
        self.assertFalse(self.store.enqueue_notification('test-pick', 'duplicate'))
        self.assertEqual(len(self.store.list_pending_notifications()), 1)
        self.store.mark_notification_sent('test-pick')
        self.assertEqual(self.store.list_pending_notifications(), [])
        self.assertGreater(self.store.log_admin_action('test', 'TEST'), 0)
        self.store.set_telemetry('test:key', {'ok': True})
        self.assertEqual(self.store.get_telemetry('test:key'), {'ok': True})

    def test_concurrent_transaction_rollback_does_not_undo_another_request(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        def failing_request():
            try:
                with self.store._tx() as conn:
                    conn.execute("INSERT INTO system_telemetry VALUES (?, ?, ?)", ('rollback:key', '{}', NOW.isoformat()))
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError('test synchronization failed')
                    raise ValueError('deliberate rollback')
            except ValueError:
                pass
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = executor.submit(failing_request)
            try:
                self.assertTrue(entered.wait(5))
                self.store.set_telemetry('committed:key', {'ok': True})
            finally:
                release.set()
            pending.result(timeout=5)
        self.assertIsNone(self.store.get_telemetry('rollback:key'))
        self.assertEqual(self.store.get_telemetry('committed:key'), {'ok': True})
