"""Inspect or install xPredict's Vault-backed Supabase scheduler.

Read-only by default. --apply enables the existing PostgreSQL extensions,
stores deployment credentials in Vault and installs the named schedules.
"""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'engine'))
from run_deployed_jobs import deployment_url


def scheduler_status(db):
    extensions = dict(db.execute("SELECT name,installed_version FROM pg_available_extensions "
        "WHERE name IN ('pg_cron','pg_net','supabase_vault')").fetchall())
    jobs = []
    if extensions.get('pg_cron'):
        jobs = [dict(zip(('name','schedule','active'),row)) for row in db.execute(
            "SELECT jobname,schedule,active FROM cron.job "
            "WHERE jobname IN ('xpredict-history','xpredict-generation','xpredict-settlement') "
            'ORDER BY jobname').fetchall()]
    secrets = set()
    if extensions.get('supabase_vault'):
        secrets = {row[0] for row in db.execute("SELECT name FROM vault.secrets "
            "WHERE name IN ('xpredict_origin','xpredict_cron_secret')").fetchall()}
    ready = (all(extensions.get(k) for k in ('pg_cron','pg_net','supabase_vault'))
        and len(jobs)==3 and all(job['active'] for job in jobs)
        and secrets=={'xpredict_origin','xpredict_cron_secret'})
    return {'state':'configured' if ready else 'not_configured',
        'extensions':{name:bool(version) for name,version in extensions.items()},
        'required_vault_secrets_present':len(secrets)==2,'jobs':jobs,
        'detail':'Scheduler configuration is separate from completed data-job evidence.'}


def apply_scheduler(db,origin,secret,bypass=''):
    import requests
    from run_deployed_jobs import headers
    # Validate the destination before persisting any authentication secret.
    response = requests.get(origin+'/api/status',headers=headers(bypass=bypass),
        timeout=(10,30),allow_redirects=False)
    if response.status_code != 200:
        raise ValueError('Application status is unavailable')
    status = response.json()
    if status.get('paper_mode') is not True or status.get('storage_driver') != 'postgres':
        raise ValueError('Destination must be the PostgreSQL paper deployment')
    headers(secret,bypass)  # Check credential length/control characters too.
    if not secret:
        raise ValueError('Configure CRON_SECRET privately')
    if not db.execute("SELECT to_regclass('lisa_private.pilot_cycles')").fetchone()[0]:
        raise ValueError('Select the deployed application database')
    for extension in ('pg_cron','pg_net','supabase_vault'):
        # Static internal extension names; no user input enters SQL identifiers.
        db.execute('CREATE EXTENSION IF NOT EXISTS '+extension)
    for name,value in (('xpredict_origin',origin),('xpredict_cron_secret',secret),
                       ('xpredict_vercel_bypass',bypass)):
        row = db.execute('SELECT id FROM vault.secrets WHERE name=%s',(name,)).fetchone()
        if row:
            db.execute('SELECT vault.update_secret(%s,new_secret:=%s)',(row[0],value))
        elif value:
            db.execute('SELECT vault.create_secret(%s,%s)',(value,name))
    prepared = (ROOT/'deploy/supabase-scheduler.sql').read_text()
    # The connection owns the encompassing transaction, including Vault writes.
    db.execute(prepared.replace('BEGIN;','').replace('COMMIT;',''),prepare=False)


def main(argv=None):
    from lisa.config import load_settings
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true',help='install named schedules and update their Vault credentials')
    parser.add_argument('--origin',help='stable HTTPS application origin; defaults to LISA_DEPLOYMENT_URL')
    args = parser.parse_args(argv)
    try:
        import psycopg
        settings = load_settings()
        origin = deployment_url(args.origin or os.environ.get('LISA_DEPLOYMENT_URL','')) if args.apply else None
        if not settings.database_url.startswith(('postgres://','postgresql://')):
            raise ValueError('Configure the managed PostgreSQL URL privately')
        with psycopg.connect(settings.database_url,connect_timeout=10,prepare_threshold=None) as db:
            if args.apply:
                apply_scheduler(db,origin,os.environ.get('CRON_SECRET',''),
                    os.environ.get('VERCEL_AUTOMATION_BYPASS_SECRET',''))
            else:
                db.execute('SET TRANSACTION READ ONLY')
            result = scheduler_status(db)
        print(json.dumps(result,indent=2))
        return int(result['state']!='configured')
    except Exception as exc:
        print(json.dumps({'state':'failed','error_type':type(exc).__name__,
            'detail':'Check database extension privileges, application origin and private environment. No credentials or exception text returned.'}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
