"""Run authenticated Vercel jobs through Python requests; print safe evidence.

Use --smoke to verify deployment wiring without spending provider credits.
"""
import argparse
import json
import os
import sys
from urllib.parse import urlsplit
from pathlib import Path

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
    if response.status_code != 200:
        return {'job':job,'state':'http_failed','status_code':response.status_code,'failed':True}
    payload = response.json()
    if not isinstance(payload,dict) or payload.get('job')!=job or not isinstance(payload.get('state'),str):
        raise ValueError('Deployment returned an unexpected job response')
    result = {key:payload[key] for key in ('job','state','failed','paper_mode','predictions_added',
               'settled','observations','statistics_requests','error_type') if key in payload}
    if result.get('state') in ('failed','broken','no_leagues') or payload.get('paper_mode') is False:
        result['failed'] = True
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
    parser.add_argument('--smoke',action='store_true')
    args = parser.parse_args(argv)
    from lisa.config import _load_dotenv
    _load_dotenv()
    try:
        import requests
        origin = deployment_url(os.environ.get('LISA_DEPLOYMENT_URL',''))
        bypass = os.environ.get('VERCEL_AUTOMATION_BYPASS_SECRET','')
        with requests.Session() as session:
            if args.smoke:
                results = [smoke(session,origin,bypass)]
            else:
                secret = os.environ.get('CRON_SECRET','')
                if not secret:
                    raise ValueError('Set CRON_SECRET in the calling environment')
                jobs = ('history','generation','settlement') if args.job=='all' else (args.job,)
                results = []
                for job in jobs:
                    try:
                        results.append(call_job(session,origin,job,secret,bypass))
                    except Exception as exc:
                        results.append({'job':job,'state':'request_failed','failed':True,
                                        'error_type':type(exc).__name__})
        print(json.dumps({'results':results},indent=2,allow_nan=False))
        return int(any(result.get('failed') for result in results))
    except Exception as exc:
        print(json.dumps({'state':'failed','error_type':type(exc).__name__,
            'detail':'Check deployment variables, protection and connectivity. Credentials, response bodies and exception text withheld.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
