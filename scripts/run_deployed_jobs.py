"""Run authenticated Vercel jobs through Python requests; print safe evidence.

Use --smoke for wiring, --verify-data for saved data, or --bootstrap to run
the initial paper jobs and verify their publication. Only bootstrap/jobs spend
provider credits; the two read-only checks never start collectors.
"""
import argparse
import json
import os
import sys
from urllib.parse import urlsplit
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'engine'))


def deployment_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/')
            or any(ord(c)<33 for c in value)):
        raise ValueError('Set an HTTPS deployment origin without credentials, paths or query parameters')
    return value.rstrip('/')


def headers(secret=None, bypass=None):
    result = {'Accept':'application/json'}
    if secret:
        if len(secret)<32 or any(ord(c)<32 for c in secret):
            raise ValueError('CRON_SECRET must contain at least 32 characters without control characters')
        result['Authorization'] = 'Bearer '+secret
    if bypass:
        result['x-vercel-protection-bypass'] = bypass
    return result


def call_job(session, origin, job, secret, bypass=None):
    if job not in ('generation','settlement','history'):
        raise ValueError('Unknown job')
    response = session.post(deployment_url(origin)+'/api/cron/'+job,
        headers=headers(secret,bypass), timeout=(10,320), allow_redirects=False)
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if response.status_code != 200:
        result = {'job':job,'state':'http_failed','status_code':response.status_code,'failed':True}
        # A partial job can save useful observations and still return 503.
        # Keep its safe counters; never print response bodies or error text.
        if isinstance(payload,dict):
            for key in ('predictions_added','settled','observations','statistics_requests'):
                if type(payload.get(key)) is int:
                    result[key] = payload[key]
            if payload.get('state') in ('failed','broken','partial','degraded','no_leagues'):
                result['state'] = payload['state']
            error_type = payload.get('error_type')
            if isinstance(error_type,str) and error_type.isidentifier() and len(error_type)<=80:
                result['error_type'] = error_type
        return result
    if not isinstance(payload,dict) or payload.get('job')!=job or not isinstance(payload.get('state'),str):
        raise ValueError('Deployment returned an unexpected job response')
    result = {key:payload[key] for key in ('job','state','failed','paper_mode','predictions_added',
               'settled','observations','statistics_requests','error_type') if key in payload}
    if result.get('state') in ('failed','broken','no_leagues') or payload.get('paper_mode') is False:
        result['failed'] = True
    return result


def verify_data(session, origin, bypass=None, *, now=None, max_age_sec=10800):
    """Verify actual saved collection/publication, independently of pick yield.

    An empty quality-filtered pick feed is allowed. An unstarted collector,
    missing publication, empty calendar or old observations fail this check.
    """
    now = now or datetime.now(timezone.utc)
    origin = deployment_url(origin)
    common = headers(bypass=bypass)
    payloads = {}
    for name,path in (('status','/api/status'),('calendar','/api/daily-board'),
                      ('publication','/api/opportunity-board'),('forecast','/api/forecast')):
        response = session.get(origin+path,headers=common,timeout=(10,30),allow_redirects=False)
        if response.status_code != 200:
            return {'state':'failed','failed':True,'reason':'saved_data_unavailable',
                    'endpoint':path,'status_code':response.status_code}
        value = response.json()
        if not isinstance(value,dict):
            raise ValueError('Deployment returned an unexpected saved-data response')
        payloads[name] = value
    status,calendar,publication,forecast = (payloads[k] for k in ('status','calendar','publication','forecast'))
    if status.get('paper_mode') is not True or status.get('storage_driver') != 'postgres':
        raise ValueError('Deployment must report paper mode and PostgreSQL')
    def age(value):
        try:
            stamp = datetime.fromisoformat(value.replace('Z','+00:00'))
            if stamp.tzinfo is None:
                return None
            seconds = (now-stamp).total_seconds()
            return seconds if seconds>=-60 else None
        except (AttributeError,TypeError,ValueError):
            return None
    problems = []
    service = status.get('daily_service') or {}
    generation_age = age(service.get('last_success'))
    publication_age = age(publication.get('generated_at'))
    observation_age = age(calendar.get('worker_observed_at'))
    for name,seconds in (('generation',generation_age),('publication',publication_age),('calendar',observation_age)):
        if seconds is None:
            problems.append(name+'_not_recorded')
        elif seconds>max_age_sec:
            problems.append(name+'_stale')
    board = calendar.get('board') or {}
    # The date buckets overlap, so count unique observed events only.
    fixtures = {row.get('match_id') for rows in board.values() if isinstance(rows,list)
                for row in rows if isinstance(row,dict) and row.get('match_id')}
    if not fixtures:
        problems.append('no_saved_calendar_fixtures')
    if not isinstance(publication.get('board'),dict):
        problems.append('no_saved_publication')
    matches = forecast.get('matches')
    if not isinstance(matches,list):
        problems.append('invalid_forecast_payload')
    result = {'state':'failed' if problems else 'passed','failed':bool(problems),
        'check':'saved_data','observed_fixtures':len(fixtures),
        'forecast_matches':len(matches) if isinstance(matches,list) else 0,
        'selected_picks':(publication.get('pick_feed') or {}).get('selected_count',0),
        'publication_age_sec':round(publication_age,1) if publication_age is not None else None,
        'pipeline_degraded':status.get('status')!='healthy','problems':problems,
        'detail':'Saved collection and publication checked; pick quality thresholds are unchanged.'}
    return result


def smoke(session, origin, bypass=None):
    origin = deployment_url(origin)
    common = headers(bypass=bypass)
    response = session.get(origin+'/api/status', headers=common, timeout=(10,30),allow_redirects=False)
    if response.status_code != 200:
        raise ValueError('Deployment API did not return HTTP 200; check environment configuration and deployment protection')
    payload = response.json()
    if payload.get('paper_mode') is not True or payload.get('storage_driver') != 'postgres':
        raise ValueError('Deployment must report paper mode and PostgreSQL')
    page = session.get(origin+'/',headers=common,timeout=(10,30),allow_redirects=False)
    if page.status_code != 200 or '<html' not in page.text.lower():
        raise ValueError('Public dashboard HTML was not served')
    for path in ('/.env','/data/provider-credentials.json','/web/data/backtest_report.json'):
        check = session.get(origin+path,headers=common,timeout=(10,30),allow_redirects=False)
        if check.status_code != 404:
            raise ValueError('Private path is reachable or is redirected by deployment configuration')
    blocked = session.get(origin+'/api/cron/generation',headers=common,timeout=(10,30),allow_redirects=False)
    if blocked.status_code != 401:
        raise ValueError('Scheduled endpoint must reject unauthenticated access with HTTP 401')
    return {'state':'passed','paper_mode':True,'storage_driver':'postgres',
        'detail':'API, HTML, private-path isolation and cron authorization checked; no provider jobs run.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job',choices=('all','generation','settlement','history'),default='all')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--smoke',action='store_true')
    mode.add_argument('--verify-data',action='store_true',help='read-only check of fresh saved fixtures and publication')
    mode.add_argument('--bootstrap',action='store_true',help='run initial paper jobs, then require fresh saved data')
    args = parser.parse_args(argv)
    if args.bootstrap and args.job!='all':
        parser.error('--bootstrap requires all three paper jobs; use --job alone for an individual job')
    from lisa.config import _load_dotenv
    _load_dotenv()
    try:
        import requests
        origin = deployment_url(os.environ.get('LISA_DEPLOYMENT_URL',''))
        bypass = os.environ.get('VERCEL_AUTOMATION_BYPASS_SECRET','')
        with requests.Session() as session:
            if args.smoke:
                results = [smoke(session,origin,bypass)]
            elif args.verify_data:
                results = [verify_data(session,origin,bypass)]
            else:
                secret = os.environ.get('CRON_SECRET','')
                if not secret:
                    raise ValueError('Set CRON_SECRET in the calling environment')
                jobs = ('history','generation','settlement') if args.job=='all' else (args.job,)
                results = []
                for job in jobs:
                    print('Running paper job: '+job,file=sys.stderr,flush=True)
                    try:
                        results.append(call_job(session,origin,job,secret,bypass))
                    except Exception as exc:
                        results.append({'job':job,'state':'request_failed','failed':True,
                                        'error_type':type(exc).__name__})
                if args.bootstrap:
                    # Optional provider failures remain visible, but do not
                    # invalidate independently verified useful publication.
                    verified = verify_data(session,origin,bypass)
                    print(json.dumps({'results':results,'data':verified},indent=2,allow_nan=False))
                    return int(verified['failed'])
        print(json.dumps({'results':results},indent=2,allow_nan=False))
        return int(any(result.get('failed') for result in results))
    except Exception as exc:
        print(json.dumps({'state':'failed','error_type':type(exc).__name__,
            'detail':'Check deployment variables, protection and connectivity. Credentials, response bodies and exception text withheld.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
