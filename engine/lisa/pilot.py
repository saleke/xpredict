"""Recorded paper runs and sanitized operational acceptance evidence."""
import json
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .daily_service import DailyService
from .settlement_service import SettlementService
from .history_service import HistoryBackfillService
from .model_policy import MODEL_VERSION


class RecordedPaperJob:
    def tick(self, *, now=None):
        if not self.settings.paper_mode:
            raise ValueError('Recorded pilot jobs require paper mode')
        started = datetime.now(timezone.utc)
        cycle_id = uuid.uuid4().hex
        with self.storage._tx() as conn:
            conn.execute('INSERT INTO pilot_cycles VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                         (cycle_id,self.lease_name,started.isoformat(),None,None,'running',0,'{}'))
        try:
            status = super().tick(now=now)
            from .job_budget import check_budget
            check_budget()
        except Exception as exc:
            status = {'state': 'failed', 'error_type': type(exc).__name__, 'error': True}
            raise
        finally:
            finished = datetime.now(timezone.utc)
            if 'status' in locals():
                safe = {k: status[k] for k in ('state','predictions_added','settled',
                    'observations','statistics_requests','error_type') if k in status}
                failed = bool(status.get('error')) or status.get('state') in (
                    'failed','broken','unsupported','no_leagues')
                for name in ('supporting','offer_history'):
                    child = status.get(name)
                    if isinstance(child, dict):
                        safe[name+'_state'] = child.get('state')
                        failed |= bool(child.get('error')) or child.get('state') in (
                            'failed','broken','degraded')
                with self.storage._tx() as conn:
                    conn.execute('UPDATE pilot_cycles SET finished_at=?,duration_sec=?,state=?, '
                                 'has_error=?,payload=? WHERE id=? AND finished_at IS NULL',
                        (finished.isoformat(),(finished-started).total_seconds(),
                         str(status.get('state','unknown')),int(failed),
                         json.dumps(safe, allow_nan=False),cycle_id))
        return status


class PaperGeneration(RecordedPaperJob, DailyService):
    pass


class PaperSettlement(RecordedPaperJob, SettlementService):
    pass


class PaperHistory(RecordedPaperJob, HistoryBackfillService):
    pass


def pilot_report(storage, settings, *, days=14, now=None):
    if type(days) is not int or not 1 <= days <= 90:
        raise ValueError('Pilot reporting window must be 1 to 90 days')
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Report time must include timezone')
    local_day = now.astimezone(ZoneInfo(settings.product_timezone)).date()
    first = local_day - timedelta(days=days-1)
    start_utc = datetime.combine(first, datetime.min.time(), ZoneInfo(settings.product_timezone)).astimezone(timezone.utc)
    with storage._tx() as conn:
        publications = conn.execute('SELECT day,generated_at,payload FROM daily_publications '
            'WHERE day>=? AND day<=? ORDER BY day', (first.isoformat(),local_day.isoformat())).fetchall()
        jobs = conn.execute('SELECT job,COUNT(*) AS cycles,SUM(has_error) AS failures, '
            'MAX(finished_at) AS last_finished,MAX(duration_sec) AS longest_sec '
            "FROM pilot_cycles WHERE started_at>=? AND started_at<=? AND finished_at IS NOT NULL "
            "AND state NOT IN ('busy','another_worker') GROUP BY job",
            (start_utc.isoformat(),now.isoformat())).fetchall()
        unfinished = conn.execute('SELECT job,started_at FROM pilot_cycles WHERE finished_at IS NULL '
            'AND started_at>=? AND started_at<=? ORDER BY started_at',
            (start_utc.isoformat(),now.isoformat())).fetchall()
        grades = conn.execute('SELECT market,result,COUNT(*) AS picks FROM picks '
            'WHERE source=? AND created_at>=? AND created_at<=? GROUP BY market,result',
            (MODEL_VERSION,start_utc.isoformat(),now.isoformat())).fetchall()
        overdue = conn.execute('SELECT COUNT(*) AS count FROM picks WHERE source=? AND result IS NULL '
            'AND commence_time<?',(MODEL_VERSION,(now-timedelta(hours=24)).isoformat())).fetchone()['count']
        stakes = conn.execute('SELECT COUNT(*) AS count FROM picks WHERE source=? AND '
            '(is_recommendation<>0 OR recommended_stake_pct<>0 OR recommended_units<>0)',
            (MODEL_VERSION,)).fetchone()['count']
        first_cycle = conn.execute('SELECT MIN(started_at) AS started FROM pilot_cycles '
            'WHERE job=? AND started_at<=?',('generation',now.isoformat())).fetchone()['started']
        observations = conn.execute('SELECT source,COUNT(*) AS observations,MAX(observed_at) AS latest '
            'FROM odds_observations WHERE observed_at>=? AND observed_at<=? GROUP BY source',
            (start_utc.isoformat(),now.isoformat())).fetchall()
    by_day = {r['day']: r for r in publications}
    pilot_first_day = (datetime.fromisoformat(first_cycle).astimezone(ZoneInfo(settings.product_timezone)).date()
                       if first_cycle else local_day)
    calendar = []
    malformed = 0
    for offset in range(days):
        day = (first+timedelta(days=offset)).isoformat()
        row = by_day.get(day)
        entry = {'day':day, 'publication_present':bool(row)}
        if row:
            try:
                payload = json.loads(row['payload'])
                board = payload['board']
                entry.update(generated_at=row['generated_at'],
                    forecasts=len(payload.get('forecast',{}).get('matches',[])),
                    winning_picks=len(board.get('winning',[])), earning_picks=len(board.get('earning',[])),
                    micro_bets=len(board.get('micro_bets',[])), accumulators=len(board.get('accumulators',[])))
            except (ValueError,TypeError,KeyError):
                malformed += 1
                entry['state'] = 'malformed_publication'
        calendar.append(entry)
    sanitized_jobs = [dict(r) for r in jobs]
    db_driver = 'postgres' if storage.__class__.__name__ == 'PostgresStorage' else 'sqlite'
    blockers = []
    if db_driver != 'postgres': blockers.append('PostgreSQL execution is not verified by this report')
    if not settings.paper_mode: blockers.append('Paper mode is not enabled')
    if not publications: blockers.append('No daily publication in this reporting window')
    if any(r['failures'] for r in jobs): blockers.append('One or more recorded worker cycles failed')
    if not all(job in {r['job'] for r in jobs} for job in ('generation','settlement','history')):
        blockers.append('Not all three jobs have recorded cycles')
    if not observations: blockers.append('No dated odds observations in this reporting window')
    if overdue: blockers.append('Forecasts remain unsettled more than 24 hours after kickoff')
    if stakes: blockers.append('Paper ledger contains approved stakes')
    if malformed: blockers.append('Malformed saved publication')
    interrupted = [dict(row) for row in unfinished
                   if (now-datetime.fromisoformat(row['started_at'])).total_seconds() > 600]
    if interrupted: blockers.append('Recorded job starts have no completion evidence after ten minutes')
    missing_completed_days = [r['day'] for r in calendar if not r['publication_present'] and
        max(first,pilot_first_day).isoformat() <= r['day'] < local_day.isoformat()]
    if missing_completed_days: blockers.append('A completed pilot day has no saved publication')
    if storage.is_system_paused(): blockers.append('The pilot is paused')
    for job in jobs:
        threshold = 3 * (3600 if job['job'] == 'history' else settings.daily_interval_sec)
        if (now-datetime.fromisoformat(job['last_finished'])).total_seconds() > threshold:
            blockers.append(job['job']+' job evidence is stale')
    return {'schema_version':1,'checked_at':now.isoformat(),'storage_driver':db_driver,
        'paper_mode':settings.paper_mode,'window_days':days,'daily_publications':calendar,
        'jobs':sanitized_jobs,'settlement_counts':[dict(r) for r in grades],
        'running_cycles':len(unfinished),'unfinished_old_cycles':interrupted,
        'missing_completed_days':missing_completed_days,
        'overdue_unsettled_forecasts':overdue,'stake_violations':stakes,
        'odds_observations':[dict(r) for r in observations], 'blockers':blockers,
        'operational_state':'blocked' if blockers else 'observed',
        'profitability_approved':False,
        'limitations':['Operations evidence does not approve model profitability.',
            'An empty slate differs from a missed publication; counts preserve the distinction.',
            'Publication counts alone do not verify browser rendering or every market entitlement.',
            'No fabricated financial returns are assigned to zero-stake forecasts.']}
