"""Offline boundary checks; these never certify live data or Vercel routing."""
import io
import json
import tempfile
import unittest
import importlib.util
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from lisa import config, feed
from lisa.admin_api import AdminAPI
from lisa.auth import AuthManager
from lisa.control import Control
from lisa.daily_service import DailyService
from lisa.dixon_coles import DixonColesModel, ScoredMatch
from lisa.job_budget import execution_budget, check_budget, JobDeadlineExceeded
from lisa.league_model import LeagueGoalModel
from lisa.observability import fixture_observations, safe_timings
from lisa.provider_credentials import EncryptedCredentialStore
from lisa.runtime import RuntimeConfig, editable_fields
from lisa.server import configure_production_context
from lisa.serverless import VercelHandler, cron_authorization, routed_path, run_job, bootstrap_owner
from lisa.storage import SqliteStorage
from test_daily_service_unittest import NOW, LEAGUE, report, final_result


class MemorySocket:
    def __init__(self, request):
        self.input = io.BytesIO(request)
        self.output = bytearray()

    def makefile(self, *args):
        return self.input

    def sendall(self, data):
        self.output.extend(data)


def request(path, *, method='GET', headers=None):
    raw = method+' '+path+' HTTP/1.0\r\nHost: example.test\r\n'
    raw += ''.join(key+': '+value+'\r\n' for key,value in (headers or {}).items())
    connection = MemorySocket((raw+'\r\n').encode())
    VercelHandler(connection,('127.0.0.1',1000),SimpleNamespace())
    header,body = bytes(connection.output).split(b'\r\n\r\n',1)
    return int(header.split()[1]),json.loads(body)


class ServerlessTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.storage = SqliteStorage(str(self.path/'test.db'))
        self.settings = config.Settings(paper_mode=True,board_leagues=(LEAGUE,),
            provider_credentials_path=str(self.path/'credentials.json'))
        self.runtime = RuntimeConfig(self.settings,storage=self.storage)
        self.auth = AuthManager(storage=self.storage)
        self.control = Control(self.storage,auth=self.auth,runtime=self.runtime)
        self.context = configure_production_context(SimpleNamespace(),storage=self.storage,
            settings=self.settings,auth=self.auth,control=self.control)

    def test_authentication_precedes_database_and_provider_initialization(self):
        env = {'VERCEL_ENV':'production','CRON_SECRET':'test-secret-'+('abc12345'*5)}
        with patch.dict('os.environ',env),patch('lisa.serverless.get_context') as context:
            status,_ = request('/api/index?__lisa_path=/api/cron/generation')
        self.assertEqual(status,401)
        context.assert_not_called()

    def test_preview_jobs_and_missing_secret_fail_closed(self):
        headers = {'Authorization':'Bearer '+('a'*32)}
        self.assertEqual(cron_authorization(headers,environment={'VERCEL_ENV':'preview','CRON_SECRET':'a'*32}),403)
        self.assertEqual(cron_authorization(headers,environment={'VERCEL_ENV':'production'}),503)
        self.assertEqual(cron_authorization(headers,environment={'VERCEL_ENV':'production','CRON_SECRET':'a'*32}),200)

    def test_rewrite_preserves_query_and_rejects_ambiguous_paths(self):
        self.assertEqual(routed_path('/api/index?__lisa_path=/api/ledger&limit=7'),'/api/ledger?limit=7')
        self.assertEqual(routed_path('/api/status'),'/api/status')
        for path in ('/api/index?__lisa_path=/.env','/api/index?__lisa_path=/api/../.env',
                     '/api/index?__lisa_path=/api/status&__lisa_path=/api/cron/history'):
            with self.assertRaises(ValueError):
                routed_path(path)

    def test_public_read_and_admin_auth_never_execute_jobs(self):
        self.context.jobs = {'generation':Mock()}
        with patch('lisa.serverless.get_context',return_value=self.context),patch('lisa.serverless.run_job') as job:
            status,payload = request('/api/index?__lisa_path=/api/status')
            self.assertEqual(status,200)
            self.assertTrue(payload['paper_mode'])
            status,_ = request('/api/index?__lisa_path=/api/admin/operations')
            self.assertEqual(status,401)
        job.assert_not_called()

    def test_cron_request_runs_exactly_one_job_and_returns_safe_result(self):
        secret = 'safe-test-secret-'+('1a2b3c4d'*4)
        with patch.dict('os.environ',{'VERCEL_ENV':'production','CRON_SECRET':secret}), \
             patch('lisa.serverless.get_context',return_value=self.context), \
             patch('lisa.serverless.run_job',return_value={'job':'settlement','state':'ok','paper_mode':True}) as run:
            status,payload = request('/api/index?__lisa_path=/api/cron/settlement',method='POST',
                headers={'Authorization':'Bearer '+secret})
        self.assertEqual(status,200)
        self.assertEqual(payload['job'],'settlement')
        run.assert_called_once_with(self.context,'settlement')

    def test_private_exception_text_never_enters_http_response(self):
        with patch('lisa.serverless.get_context',side_effect=ValueError('private-key-or-database-url')):
            status,payload = request('/api/status')
        self.assertEqual(status,503)
        self.assertNotIn('private-key',json.dumps(payload))

    def test_dispatch_cadence_is_shared_across_instances_and_failures_release_leases(self):
        job = Mock()
        job.tick.return_value = {'state':'ok','error':None,'predictions_added':2,'secret':'withheld'}
        self.context.jobs = {'generation':job}
        first = run_job(self.context,'generation')
        self.assertEqual(first['predictions_added'],2)
        self.assertNotIn('secret',first)
        other = SimpleNamespace(control=self.control,storage=self.storage,jobs={'generation':Mock()})
        self.assertEqual(run_job(other,'generation')['state'],'not_due')
        other.jobs['generation'].tick.assert_not_called()
        self.storage.set_telemetry('serverless:last_dispatch:generation',{})
        job.tick.side_effect = RuntimeError('private-exception')
        with self.assertRaises(RuntimeError):
            run_job(self.context,'generation')
        lease = DailyService(self.storage,self.settings)
        lease.lease_name = 'dispatch:generation'
        self.assertTrue(lease._claim(NOW+timedelta(days=100)))

    def test_failed_dispatch_retries_after_five_minutes_instead_of_an_hour(self):
        job = Mock()
        job.tick.side_effect = [dict(state='failed',error=True),dict(state='ok',error=None)]
        self.context.jobs = {'generation':job}
        with patch('lisa.serverless.datetime') as clock:
            clock.now.return_value = NOW
            self.assertTrue(run_job(self.context,'generation')['failed'])
            clock.now.return_value = NOW+timedelta(minutes=4)
            self.assertEqual(run_job(self.context,'generation')['state'],'not_due')
            clock.now.return_value = NOW+timedelta(minutes=6)
            self.assertFalse(run_job(self.context,'generation')['failed'])
            clock.now.return_value = NOW+timedelta(minutes=12)
            self.assertEqual(run_job(self.context,'generation')['state'],'not_due')
        self.assertEqual(job.tick.call_count,2)

    def test_interrupted_dispatch_does_not_suppress_recovery_for_an_hour(self):
        job = Mock()
        job.tick.return_value = dict(state='ok',error=None)
        self.context.jobs = {'generation':job}
        self.storage.set_telemetry('serverless:last_dispatch:generation',
            {'timestamp':(NOW-timedelta(minutes=6)).timestamp(),'state':'running'})
        with patch('lisa.serverless.datetime') as clock:
            clock.now.return_value = NOW
            self.assertEqual(run_job(self.context,'generation')['state'],'ok')
        job.tick.assert_called_once()

    def test_encrypted_credentials_survive_restart_disable_and_inherit(self):
        key = Fernet.generate_key().decode()
        store = EncryptedCredentialStore(self.storage,key)
        store.update('oddspapi','artificial-provider-secret')
        with self.storage._tx() as conn:
            row = conn.execute('SELECT * FROM provider_credentials').fetchone()
        self.assertNotIn('artificial-provider-secret',str(dict(row)))
        self.assertEqual(EncryptedCredentialStore(self.storage,key).read()['oddspapi_key'],'artificial-provider-secret')
        store.update('oddspapi','')
        self.assertEqual(store.read(),{'oddspapi_key':''})
        store.update('oddspapi','',inherit=True)
        self.assertEqual(store.read(),{})
        self.assertEqual(len(self.storage.admin_get_telemetry()),0)

    def test_supporting_history_failure_is_reported_by_dispatch(self):
        job = Mock()
        job.tick.return_value = {'state':'ok','error':None,'supporting':{'state':'failed'}}
        self.context.jobs = {'history':job}
        self.assertTrue(run_job(self.context,'history')['failed'])

    def test_wrong_encryption_key_and_corrupt_rows_fail_closed(self):
        store = EncryptedCredentialStore(self.storage,Fernet.generate_key().decode())
        store.update('the_odds_api','artificial-secret')
        with self.assertRaises(ValueError):
            EncryptedCredentialStore(self.storage,Fernet.generate_key().decode()).read()
        with self.storage._tx() as conn:
            conn.execute('UPDATE provider_credentials SET ciphertext=?',('broken',))
        with self.assertRaises(ValueError):
            store.read()

    def test_runtime_database_keys_are_cached_but_rotation_invalidates_them(self):
        key = Fernet.generate_key().decode()
        settings = replace(self.settings,provider_credentials_backend='postgres',credential_encryption_key=key,
                           oddspapi_key='environment-key')
        runtime = RuntimeConfig(settings,storage=self.storage)
        self.assertEqual(runtime.settings().oddspapi_key,'environment-key')
        EncryptedCredentialStore(self.storage,key).update('oddspapi','new-artificial-key')
        runtime.invalidate_credentials()
        self.assertEqual(runtime.settings().oddspapi_key,'new-artificial-key')
        self.assertNotIn('credential_encryption_key',editable_fields())
        self.assertNotIn(key,json.dumps(runtime.describe()))

    def test_independent_admin_instances_share_csrf_secret(self):
        first = AdminAPI(self.control)
        second = AdminAPI(self.control)
        self.assertEqual(first.csrf_token('test-session'),second.csrf_token('test-session'))

    def test_owner_is_created_before_public_registration_and_cannot_be_claimed(self):
        env = {'LISA_ADMIN_EMAILS':'owner@example.test','LISA_OWNER_PASSWORD':'test-owner-password-123456'}
        bootstrap_owner(self.auth,env)
        bootstrap_owner(self.auth,env)
        with self.assertRaises(ValueError):
            self.auth.register_user('owner@example.test','attacker-password')
        self.assertEqual(self.auth.authenticate_user('owner@example.test',env['LISA_OWNER_PASSWORD'])['tier'],'admin')

    def test_existing_unproven_owner_requires_password_proof(self):
        self.auth.register_user('owner@example.test','some-other-password')
        with self.assertRaises(ValueError):
            bootstrap_owner(self.auth,{'LISA_ADMIN_EMAILS':'owner@example.test','LISA_OWNER_PASSWORD':'new-bootstrap-password'})

    def test_score_observations_preserve_inplay_without_grading_it(self):
        row = dict(final_result(),kickoff=NOW.isoformat(),completed=False,status='IN_PLAY',home_score=1,away_score=0)
        observed = fixture_observations([row],NOW)
        self.assertEqual(observed['rows'][0]['score'],[1,0])
        self.assertFalse(observed['rows'][0]['completed'])
        self.assertEqual(DailyService(self.storage,self.settings)._settle([row],NOW),0)
        row['home_score'] = True
        self.assertIsNone(fixture_observations([row],NOW)['rows'][0]['score'])

    def test_job_budget_expires_and_is_restored_after_context(self):
        with self.assertRaises(JobDeadlineExceeded):
            with execution_budget(deadline=0):
                pass
        check_budget()

    def test_timing_values_are_bounded_and_finite(self):
        self.assertEqual(safe_timings({'total':12.34567,'calendar':float('nan'),
                                      'prices':-1,'secret':'key'}),{'total':12.346})

    def test_model_cache_corruption_triggers_a_refit(self):
        rows = [ScoredMatch(LEAGUE,NOW-timedelta(days=10+i),'A','B',i%3,i%2) for i in range(20)]
        model = LeagueGoalModel()
        model.cache_storage = self.storage
        model.fit(rows,as_of=NOW)
        value = self.storage.get_telemetry('model:league_fit:'+LEAGUE)
        value['attack']['A'] = 'invalid'
        self.storage.set_telemetry('model:league_fit:'+LEAGUE,value)
        other = LeagueGoalModel()
        other.cache_storage = self.storage
        other.fit(rows,as_of=NOW)
        self.assertIsInstance(other.for_league(LEAGUE).strength('A').attack,float)

    def test_public_build_excludes_private_files_and_local_credentials_are_preserved(self):
        root = Path(__file__).resolve().parents[2]
        def script(name):
            spec = importlib.util.spec_from_file_location(name,root/'scripts'/name)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        builder = script('build_vercel.py')
        local = script('local_paper.py')
        source = self.path/'web'
        source.mkdir()
        for name in ('index.html','admin.html'):
            (source/name).write_text('<html></html>')
        (source/'data').mkdir()
        (source/'data'/'secret.js').write_text('private')
        (source/'.env').write_text('secret')
        target = self.path/'public'
        builder.build(source,target)
        self.assertEqual({p.name for p in target.iterdir()},{'index.html','admin.html'})
        owner_file = self.path/'owner.env'
        first = local.local_owner(owner_file)
        self.assertEqual(local.local_owner(owner_file),first)
        self.assertEqual(owner_file.stat().st_mode & 0o777,0o600)
        owner_file.chmod(0o644)
        with self.assertRaises(ValueError):
            local.local_owner(owner_file)

    def test_persisted_fit_reuses_parameters_and_changed_data_refits(self):
        rows = [ScoredMatch(LEAGUE,NOW-timedelta(days=10+i),'A','B',i%3,i%2) for i in range(20)]
        first = LeagueGoalModel()
        first.cache_storage = self.storage
        first.fit(rows,as_of=NOW)
        expected = first.for_league(LEAGUE).predict('A','B')
        other = LeagueGoalModel()
        other.cache_storage = self.storage
        with patch.object(DixonColesModel,'fit',side_effect=AssertionError('cold restart must reuse fit')):
            other.fit(rows,as_of=NOW)
        self.assertEqual(other.for_league(LEAGUE).predict('A','B'),expected)
        rows.append(ScoredMatch(LEAGUE,NOW-timedelta(days=50),'A','B',0,0))
        with patch.object(DixonColesModel,'fit',autospec=True,wraps=DixonColesModel.fit) as fit:
            # Real refit behavior is separately exercised; ensure invalidation.
            fit.side_effect = JobDeadlineExceeded('test interruption')
            with self.assertRaises(JobDeadlineExceeded):
                other.fit(rows,as_of=NOW)
        fit.assert_called_once()


if __name__ == '__main__':
    unittest.main()
