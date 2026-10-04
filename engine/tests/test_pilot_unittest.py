import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lisa import config, feed
from lisa.daily_service import DailyService
from lisa.odds_history import OddsHistoryRepository
from lisa.pilot import PaperGeneration, PaperSettlement, PaperHistory, pilot_report
from lisa.pilot_setup import prepare_pilot
from lisa.runtime import RuntimeConfig, OverrideError, OVERRIDES_KEY
from lisa.storage import SqliteStorage
from test_daily_service_unittest import NOW, LEAGUE, report, final_result


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)
        self.storage = SqliteStorage(str(self.path/'pilot.db'))
        self.settings = config.Settings(paper_mode=True,board_leagues=(LEAGUE,),
                                       provider_credentials_path=str(self.path/'private.json'))

    def test_paper_guard_cannot_be_disabled_by_generic_admin_edits(self):
        runtime=RuntimeConfig(self.settings,storage=self.storage)
        self.assertEqual(runtime.settings().board_max_stake,0)
        runtime.apply({'board_max_stake':0.05,'model_validation_path':'approved-artifact.json'})
        self.assertEqual(runtime.settings().board_max_stake,0)
        self.assertEqual(runtime.settings().model_validation_path,'')
        with self.assertRaises(OverrideError):
            runtime.apply({'paper_mode':False})

    def test_runtime_reload_avoids_scanning_provider_caches_and_validates_overrides(self):
        self.storage.set_telemetry(OVERRIDES_KEY,{'board_volume_target':18,'telegram_token':'tampered-secret'})
        with patch.object(self.storage,'admin_get_telemetry',side_effect=AssertionError('full telemetry scan')), \
             self.assertLogs('lisa.runtime',level='WARNING'):
            runtime=RuntimeConfig(self.settings,storage=self.storage)
        self.assertEqual(runtime.settings().board_volume_target,18)
        self.assertNotEqual(runtime.settings().telegram_token,'tampered-secret')
        self.assertNotIn('telegram_token',self.storage.get_telemetry(OVERRIDES_KEY))

    def test_recorded_generation_restart_and_independent_settlement(self):
        producer=PaperGeneration(self.storage,self.settings,runner=Mock(return_value=report()),
                                 providers=feed.ProviderSet(),settle_in_cycle=False)
        self.assertEqual(producer.tick(now=NOW)['predictions_added'],3)
        self.assertEqual(producer.tick(now=NOW)['predictions_added'],0)
        saved=DailyService(self.storage,self.settings).read()
        self.assertTrue(saved['paper_mode'])
        self.assertTrue(saved['board']['unproven'])
        self.assertTrue(all(row['stake_fraction']==0 for key in ('winning','earning','micro_bets')
                            for row in saved['board'][key]))
        settler=PaperSettlement(self.storage,self.settings,providers=feed.ProviderSet())
        with patch('lisa.settlement_service.feed.fetch_calendar',return_value={LEAGUE:[final_result()]}), \
             patch('lisa.feed.run_feed',side_effect=AssertionError('settlement must not fit a model')):
            self.assertEqual(settler.tick(now=NOW+timedelta(days=2))['settled'],3)
        self.assertTrue(all(not row['is_recommendation'] and row['recommended_stake_pct']==0
                            for row in self.storage.list_settled_picks()))
        evidence=pilot_report(self.storage,self.settings,now=NOW+timedelta(days=2))
        self.assertEqual(evidence['stake_violations'],0)
        self.assertEqual(sum(r['picks'] for r in evidence['settlement_counts']),3)
        self.assertFalse(evidence['profitability_approved'])
        self.assertIn('PostgreSQL execution is not verified by this report',evidence['blockers'])

    def test_failed_cycle_diagnostics_do_not_persist_provider_secrets(self):
        producer=PaperGeneration(self.storage,self.settings,
            runner=Mock(return_value=report(error='secret-provider-key')),
            providers=feed.ProviderSet())
        producer.tick(now=NOW)
        with self.storage._tx() as conn:
            rows=conn.execute('SELECT payload,has_error FROM pilot_cycles').fetchall()
        self.assertEqual(rows[0]['has_error'],1)
        self.assertNotIn('secret-provider-key',rows[0]['payload'])
        self.assertNotIn('secret-provider-key',json.dumps(pilot_report(self.storage,self.settings)))

    def test_failed_supporting_history_is_visible_when_primary_history_succeeds(self):
        provider=Mock(name='primary history')
        provider.name='football_data'
        provider.leagues.return_value=[LEAGUE]
        provider.current_season.return_value=SimpleNamespace(year=2026)
        historical = final_result()
        historical.update(provider='football_data', kickoff=(NOW-timedelta(days=3)).isoformat())
        provider.get_season.return_value=SimpleNamespace(fixtures=[historical])
        job=PaperHistory(self.storage,self.settings,providers=feed.ProviderSet(calendar=[provider]))
        with patch.object(job,'_offer_history',return_value={'state':'degraded','failures':2}), \
             patch.object(job,'_supporting_history',return_value={'state':'failed','error':'secret-key'}):
            self.assertEqual(job.tick()['state'],'ok')
        with self.storage._tx() as conn:
            cycle=conn.execute('SELECT payload,has_error FROM pilot_cycles').fetchone()
        self.assertEqual(cycle['has_error'],1)
        self.assertEqual(json.loads(cycle['payload'])['offer_history_state'],'degraded')
        self.assertNotIn('secret-key',cycle['payload'])
        self.assertIn('One or more recorded worker cycles failed',pilot_report(self.storage,self.settings)['blockers'])

    def test_empty_database_is_not_reported_as_operationally_verified(self):
        evidence=pilot_report(self.storage,self.settings)
        self.assertEqual(evidence['operational_state'],'blocked')
        self.assertEqual(evidence['odds_observations'],[])
        self.assertEqual(len(evidence['daily_publications']),14)
        self.assertFalse(evidence['missing_completed_days'])

    def test_missing_completed_days_are_detected_after_pilot_start(self):
        old=(NOW-timedelta(days=3)).isoformat()
        with self.storage._tx() as conn:
            conn.execute('INSERT INTO pilot_cycles VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                         ('test-cycle','generation',old,old,1,'ok',0,'{}'))
        evidence=pilot_report(self.storage,self.settings,now=NOW,days=4)
        self.assertEqual(len(evidence['missing_completed_days']),3)

    def test_interrupted_cycle_is_retained_and_fresh_active_cycles_are_distinguished(self):
        for cycle_id,age in (('interrupted',timedelta(hours=1)),('active',timedelta(seconds=10))):
            started=(NOW-age).isoformat()
            with self.storage._tx() as conn:
                conn.execute('INSERT INTO pilot_cycles VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                             (cycle_id,'generation',started,None,None,'running',0,'{}'))
        evidence=pilot_report(self.storage,self.settings,now=NOW)
        self.assertEqual(evidence['running_cycles'],2)
        self.assertEqual(len(evidence['unfinished_old_cycles']),1)
        self.assertFalse(evidence['jobs'])
        self.assertIn('Recorded job starts have no completion evidence after ten minutes',evidence['blockers'])

    def test_setup_keeps_existing_passwords_private_and_unchanged(self):
        target=self.path/'.env.pilot'
        prepare_pilot(target)
        first=target.read_bytes()
        self.assertEqual(target.stat().st_mode & 0o777,0o600)
        prepare_pilot(target)
        self.assertEqual(target.read_bytes(),first)
        target.chmod(0o644)
        with self.assertRaises(ValueError):
            prepare_pilot(target)

    def test_setup_rejects_corrupt_existing_config_without_overwriting(self):
        target=self.path/'.env.pilot'
        prepare_pilot(target)
        target.write_text('LISA_PILOT_ADMIN_PASSWORD=partial\n')
        with self.assertRaises(ValueError):
            prepare_pilot(target)
        self.assertEqual(target.read_text(),'LISA_PILOT_ADMIN_PASSWORD=partial\n')

    def test_isolation_guard_rejects_query_overrides_before_connecting(self):
        from lisa.postgres_storage import require_isolated_database
        self.assertEqual(require_isolated_database('postgresql://localhost/safe_pilot?sslmode=require','_pilot'),
                         'safe_pilot')
        for url,suffix in (
            ('postgresql://localhost/production','_pilot'),
            ('postgresql://localhost/safe_pilot?dbname=production','_pilot'),
            ('postgresql://localhost/safe_test?service=production','_test'),
            ('postgresql://localhost/safe_test?options=-csearch_path=production','_test')):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                require_isolated_database(url,suffix)

    def test_worker_launch_reads_settings_without_missing_import_and_caches_reload(self):
        from lisa.cli import _cmd_worker
        def job(storage, settings, **kwargs):
            self.assertTrue(settings().paper_mode)
            self.assertTrue(settings().paper_mode)
            return Mock(_thread=Mock(is_alive=Mock(return_value=False)))
        with patch('lisa.cli.cfg.load_settings',return_value=self.settings), \
             patch('lisa.cli._make_storage',return_value=self.storage), \
             patch('lisa.runtime.RuntimeConfig') as runtime_class, \
             patch('lisa.pilot.PaperGeneration',side_effect=job), \
             patch('lisa.pilot.PaperSettlement',side_effect=job), \
             patch('lisa.pilot.PaperHistory',side_effect=job), \
             patch('threading.Event') as event, patch('signal.signal'):
            runtime_class.return_value.settings.return_value=self.settings
            event.return_value.wait.return_value=True
            self.assertEqual(_cmd_worker(SimpleNamespace()),0)
            self.assertEqual(runtime_class.return_value.load.call_count,1)

    def test_shutdown_signals_all_jobs_before_joining_and_preserves_active_pool(self):
        from lisa.cli import _stop_worker_jobs
        jobs=[Mock(_thread=Mock()),Mock(_thread=Mock())]
        for job in jobs:
            job._thread.join.side_effect=lambda **kwargs: self.assertTrue(
                all(j.request_stop.called for j in jobs))
            job._thread.is_alive.return_value=False
        storage=Mock()
        self.assertTrue(_stop_worker_jobs(jobs,storage,timeout_sec=0))
        storage.close.assert_called_once()
        storage.reset_mock()
        jobs[0]._thread.is_alive.return_value=True
        self.assertFalse(_stop_worker_jobs(jobs,storage,timeout_sec=0))
        storage.close.assert_not_called()


if __name__ == '__main__':
    unittest.main()
