"""Cold-start publication must be bounded and distinguish data from HTTP health."""
import importlib.util
import json
import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from lisa.job_budget import JobDeadlineExceeded
from lisa.match_history import HistoryRepository
from lisa.providers.calendar import normalise_fixture
from lisa.storage import SqliteStorage

NOW = datetime(2026,10,10,5,tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('run_deployed_jobs',ROOT/'scripts/run_deployed_jobs.py')
deployed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployed)


class HistoryPopulationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = SqliteStorage(str(Path(folder.name)/'test.db'))
        self.addCleanup(self.store.close)
        self.history = HistoryRepository(self.store)

    def rows(self,count):
        return [normalise_fixture(provider='football_data',sport_key='soccer_epl',
            match_id='test:'+str(i),kickoff_epoch=(NOW-timedelta(days=10)).timestamp(),
            home='Home '+str(i),away='Away '+str(i),status='FINISHED',
            home_score=2,away_score=1,season='2026') for i in range(count)]

    def trace(self,queries):
        connect = self.store._connect
        def traced():
            conn = connect()
            conn.set_trace_callback(queries.append)
            return conn
        return patch.object(self.store,'_connect',side_effect=traced)

    def test_cold_season_import_uses_bounded_writes_and_replay_does_not_rewrite(self):
        rows = self.rows(715)
        queries = []
        with self.trace(queries):
            self.assertEqual(self.history.ingest(rows,observed_at=NOW),715)
        writes = [q for q in queries if q.startswith('INSERT INTO match_observations')]
        self.assertEqual(len(writes),math.ceil(len(rows)/100))
        self.assertEqual(len(self.history.results(['soccer_epl'],as_of=NOW)),715)
        queries.clear()
        with self.trace(queries):
            self.history.ingest(rows,observed_at=NOW+timedelta(minutes=5))
        self.assertFalse(any(q.startswith('INSERT INTO match_observations') for q in queries))

    def test_budget_expiry_between_batches_rolls_back_whole_import(self):
        with patch('lisa.match_history.check_budget',side_effect=[None,JobDeadlineExceeded()]):
            with self.assertRaises(JobDeadlineExceeded):
                self.history.ingest(self.rows(201),observed_at=NOW)
        with self.store._tx() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM match_observations').fetchone()[0],0)

    def test_basic_refresh_preserves_enrichment_while_correcting_goals(self):
        row = self.rows(1)[0]
        self.history.ingest([dict(row,home_corners=7,away_corners=4,
            home_first_half_score=1,away_first_half_score=0,
            source_statistics={'possession':58})],observed_at=NOW)
        self.history.ingest([dict(row,home_score=3)],observed_at=NOW+timedelta(minutes=5))
        saved = self.history.results(['soccer_epl'],as_of=NOW+timedelta(minutes=10))[0]
        self.assertEqual((saved['home_score'],saved['home_corners'],saved['away_corners']), (3,7,4))
        self.assertEqual(saved['source_statistics'],{'possession':58})
        self.assertEqual(saved['home_first_half_score'],1)

    def test_large_publication_is_batched_and_restart_does_not_duplicate_picks(self):
        from lisa import config,feed
        from lisa.daily_service import DailyService
        from test_daily_service_unittest import opportunity,report
        rows = [opportunity(match_id='test:'+str(i),kickoff=NOW+timedelta(hours=2)) for i in range(205)]
        cycle = report(rows)
        cycle.began = NOW
        service = DailyService(self.store,config.Settings(paper_mode=True),
            runner=Mock(return_value=cycle),providers=feed.ProviderSet())
        queries = []
        with self.trace(queries):
            first = service.tick(now=NOW)
        self.assertEqual(first['predictions_added'],207)
        writes = [q for q in queries if q.startswith('INSERT INTO picks')]
        self.assertLess(len(writes),10)
        self.assertEqual(service.tick(now=NOW+timedelta(minutes=1))['predictions_added'],0)
        self.assertEqual(self.store.count_picks()['total'],207)


class DeployedDataTests(unittest.TestCase):
    def payloads(self):
        stamp = NOW.isoformat()
        return {'/api/status':{'paper_mode':True,'storage_driver':'postgres','status':'healthy',
                    'daily_service':{'last_success':stamp}},
            '/api/daily-board':{'worker_observed_at':stamp,'board':{
                'today':[{'match_id':'observed:1'}],'this_week':[{'match_id':'observed:1'}]}},
            '/api/opportunity-board':{'generated_at':stamp,'board':{},'pick_feed':{'selected_count':0}},
            '/api/forecast':{'matches':[]}}

    def session(self,payloads):
        session = Mock()
        session.get.side_effect = lambda url,**kwargs: Mock(status_code=200,
            json=Mock(return_value=payloads[urlsplit(url).path]))
        return session

    def verify(self,payloads):
        return deployed.verify_data(self.session(payloads),'https://example.test',now=NOW)

    def test_http_200_without_collection_or_publication_fails(self):
        payloads = self.payloads()
        payloads['/api/status']['daily_service'] = {'state':'starting'}
        payloads['/api/daily-board'] = {'board':{}}
        payloads['/api/opportunity-board'] = {'board':None}
        checked = self.verify(payloads)
        self.assertTrue(checked['failed'])
        self.assertIn('generation_not_recorded',checked['problems'])
        self.assertIn('no_saved_calendar_fixtures',checked['problems'])
        self.assertIn('no_saved_publication',checked['problems'])

    def test_fresh_real_observations_can_pass_without_padding_picks(self):
        checked = self.verify(self.payloads())
        self.assertFalse(checked['failed'])
        self.assertEqual(checked['observed_fixtures'],1)
        self.assertEqual(checked['selected_picks'],0)

    def test_stale_publication_fails_despite_a_fresh_api_response(self):
        payloads = self.payloads()
        payloads['/api/opportunity-board']['generated_at'] = (NOW-timedelta(days=1)).isoformat()
        self.assertIn('publication_stale',self.verify(payloads)['problems'])

    def test_optional_provider_outage_does_not_erase_verified_publication(self):
        payloads = self.payloads()
        payloads['/api/status']['status'] = 'degraded'
        checked = self.verify(payloads)
        self.assertFalse(checked['failed'])
        self.assertTrue(checked['pipeline_degraded'])

    def test_missing_publication_http_error_fails_without_printing_response(self):
        session = self.session(self.payloads())
        session.get.side_effect = None
        session.get.return_value = Mock(status_code=503,text='private provider response')
        result = deployed.verify_data(session,'https://example.test',now=NOW)
        self.assertTrue(result['failed'])
        self.assertNotIn('private',json.dumps(result))

    def test_partial_job_preserves_safe_counters_and_withholds_error_text(self):
        session = Mock()
        session.post.return_value = Mock(status_code=503,json=Mock(return_value={
            'job':'history','state':'degraded','observations':50,'error_type':'TimeoutError',
            'error':'private credential','response':'private body'}))
        result = deployed.call_job(session,'https://example.test','history','x'*32)
        self.assertEqual(result['observations'],50)
        self.assertTrue(result['failed'])
        self.assertNotIn('private',json.dumps(result))
