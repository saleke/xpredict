#!/usr/bin/env python3
"""Portable launcher for the isolated local Docker paper pilot."""
import argparse
import json
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'engine'))


def report_path(filename):
    directory = ROOT/'data/reports'
    directory.mkdir(parents=True, exist_ok=True)
    return directory/filename


def preflight():
    """Read local prerequisites without requesting providers or exposing keys."""
    from dataclasses import replace
    from lisa import config
    from lisa.runtime import RuntimeConfig
    config._load_dotenv(str(ROOT/'.env'))
    settings=config.load_settings()
    credential_path=Path(settings.provider_credentials_path)
    if not credential_path.is_absolute():
        credential_path=ROOT/credential_path
    settings=RuntimeConfig(replace(settings,provider_credentials_path=str(credential_path))).settings()
    docker_available=shutil.which('docker') is not None
    daemon_accessible=False
    if docker_available:
        try:
            result=subprocess.run(['docker','info','--format','{{.ServerVersion}}'],
                                  capture_output=True,text=True,timeout=5)
            daemon_accessible=result.returncode==0
        except (OSError,subprocess.TimeoutExpired):
            pass
    try:
        dns=subprocess.run([sys.executable,'-c',
            "import socket; socket.getaddrinfo('api.oddspapi.io',443,type=socket.SOCK_STREAM)"],
            capture_output=True,timeout=5)
        hostname_resolved=dns.returncode==0
    except (OSError,subprocess.TimeoutExpired):
        hostname_resolved=False
    validation={}
    safe_states={'missing_credential','blocked_network','failed','sample_failed',
                 'account_sample_received','price_sample_received','sample_received','no_price_sample'}
    for provider,filename in (('oddspapi','oddspapi-validation-local.json'),
                              ('the_odds_api','the-odds-api-validation-local.json')):
        source=ROOT/'data/reports'/filename
        state='missing_report'
        checked_at=None
        quotes=0
        dated=0
        if source.is_file():
            try:
                saved=json.loads(source.read_text())
                state=saved.get('state')
                if state not in safe_states:
                    state='unrecognized_report'
                checked_at=datetime.fromisoformat(saved['checked_at']).isoformat()
                count=saved.get('quotes',0)
                dated_count=saved.get('quotes_with_bookmaker_update_time',saved.get('quotes_with_update_time',0))
                quotes=count if type(count) is int and count>=0 else 0
                dated=dated_count if type(dated_count) is int and 0<=dated_count<=quotes else 0
            except (OSError,ValueError,KeyError,TypeError,AttributeError):
                state='malformed_report'
                checked_at=None
        validation[provider]={'state':state,'checked_at':checked_at,'quotes':quotes,'dated_quotes':dated}
    blockers=[]
    if not docker_available: blockers.append('Docker CLI is not available')
    elif not daemon_accessible: blockers.append('Docker daemon is not accessible from this environment')
    if not hostname_resolved: blockers.append('OddsPapi hostname is not resolvable from this environment')
    if validation['oddspapi']['state']!='price_sample_received' or not validation['oddspapi']['dated_quotes']:
        blockers.append('Direct OddsPapi price access is not verified by a local validation report')
    if validation['the_odds_api']['state']!='sample_received' or not validation['the_odds_api']['dated_quotes']:
        blockers.append('Supporting The Odds API price access is not verified by a local validation report')
    report={'schema_version':1,'checked_at':datetime.now(timezone.utc).isoformat(),
        'state':'local_execution_required','docker_cli_available':docker_available,
        'docker_daemon_accessible':daemon_accessible,
        'host_postgres_driver_installed':importlib.util.find_spec('psycopg') is not None,
        'host_postgres_pool_installed':importlib.util.find_spec('psycopg_pool') is not None,
        'provider_credentials_configured':{name:bool(getattr(settings,field)) for name,field in (
            ('oddspapi','oddspapi_key'),('the_odds_api','odds_api_key'),
            ('football_data','football_data_token'),('api_football','api_football_key'),
            ('allsports','allsports_api_key'),('sharpapi','sharpapi_key'))},
        'oddspapi_hostname_resolved':hostname_resolved,'local_live_validation':validation,
        'blockers':blockers,'profitability_approved':False,
        'limitations':['Configured keys do not prove authentication, market entitlement or quota availability.',
            'DNS resolution does not verify outbound HTTPS access.',
            'Docker builds install PostgreSQL dependencies; host drivers are needed only for host execution.',
            'Run PostgreSQL checks and collect the separate live pilot report before accepting operations.']}
    encoded=json.dumps(report,indent=2,allow_nan=False)+'\n'
    report_path('pilot-readiness.json').write_text(encoded)
    print(encoded,end='')
    print('Saved sanitized prerequisites: data/reports/pilot-readiness.json')
    return 1 if blockers else 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','preflight','start','checks','report','stop'))
    args=parser.parse_args()
    if args.action=='preflight':
        return preflight()
    from lisa.pilot_setup import prepare_pilot
    if args.action in ('prepare','start'):
        prepare_pilot(ROOT/'.env.pilot')
    if args.action=='prepare':
        print('Private .env.pilot ready; existing passwords preserved.')
        return 0
    if not (ROOT/'.env').is_file() or not (ROOT/'.env.pilot').is_file():
        print('Keep provider credentials in private .env and run python scripts/pilot.py prepare first.')
        return 1
    command=['docker','compose','--env-file',str(ROOT/'.env'),'--env-file',str(ROOT/'.env.pilot'),
             '-f',str(ROOT/'docker-compose.pilot.yml')]
    commands={'start':['up','--build','-d','pilot-db','web','worker'],
        'checks':['run','--rm','--build','checks'],
        'report':['run','--rm','--no-deps','report'], 'stop':['stop']}
    try:
        if args.action=='report':
            result=subprocess.run(command+commands[args.action],cwd=ROOT,capture_output=True,text=True)
            try:
                report=json.loads(result.stdout)
            except ValueError:
                print('Pilot report unavailable. Start the pilot and confirm Docker can run locally.')
                return 1
            destination=report_path('pilot-live-report.json')
            encoded=json.dumps(report,indent=2,allow_nan=False)+'\n'
            destination.write_text(encoded)
            print(encoded,end='')
            print('Saved sanitized report: data/reports/pilot-live-report.json')
            return result.returncode
        return subprocess.run(command+commands[args.action],cwd=ROOT).returncode
    except FileNotFoundError:
        print('Docker with Compose is required on the local machine.')
        return 1


if __name__=='__main__':
    raise SystemExit(main())
