"""Run the local paper web server and all three workers without Docker."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'engine'))


def local_owner(path):
    path = Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    try:
        descriptor = os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor,'w') as stream:
            stream.write('LISA_ADMIN_EMAILS=owner@localhost.test\n')
            stream.write('LISA_OWNER_PASSWORD='+secrets.token_urlsafe(32)+'\n')
            stream.flush()
            os.fsync(stream.fileno())
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077 or path.stat().st_size>2048:
        raise ValueError('Local owner file must be a small private regular file')
    values = dict(line.split('=',1) for line in path.read_text().splitlines() if line)
    if (set(values)!={'LISA_ADMIN_EMAILS','LISA_OWNER_PASSWORD'}
            or len(values['LISA_OWNER_PASSWORD'])<16):
        raise ValueError('Local owner file is incomplete')
    return values


def check_port(port):
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        listener.bind(('127.0.0.1',port))


def environment(database, postgres):
    from lisa.config import _load_dotenv
    _load_dotenv()
    result = dict(os.environ)
    result['PYTHONPATH'] = str(ROOT/'engine')
    result['LISA_PAPER_MODE'] = '1'
    if postgres:
        if not result.get('LISA_DATABASE_URL','').startswith(('postgres://','postgresql://')):
            raise ValueError('PostgreSQL mode needs LISA_DATABASE_URL')
        if importlib.util.find_spec('psycopg') is None:
            raise ValueError('Install ./engine[postgres,cloud] before using PostgreSQL')
        result['LISA_STORAGE'] = 'postgres'
    else:
        result['LISA_STORAGE'] = 'sqlite'
        result['LISA_DATABASE_URL'] = str(Path(database).resolve())
        result['LISA_PROVIDER_CREDENTIALS_BACKEND'] = 'file'
    return result


def wait_for_web(process, port):
    deadline = time.monotonic()+45
    # This is local readiness checking; no provider calls happen on /api/health.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic()<deadline and process.poll() is None:
        try:
            with opener.open('http://127.0.0.1:'+str(port)+'/api/health',timeout=2) as response:
                if response.status==200:
                    return
        except OSError:
            pass
        time.sleep(.25)
    raise RuntimeError('Local web server did not become available')


def diagnose(database):
    """Read-only local evidence; never reads or prints credentials or errors."""
    import sqlite3
    from datetime import datetime, timezone
    path = Path(database).resolve()
    result = {'database': str(path), 'exists': path.is_file()}
    if not path.is_file():
        result['state'] = 'database_not_created'
        return result
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'picks','system_telemetry'} <= tables:
            return dict(result,state='schema_not_initialized')
        now = datetime.now(timezone.utc).isoformat()
        counts = conn.execute('SELECT COUNT(*),SUM(result IS NOT NULL),'
            'SUM(result IS NULL AND commence_time>?),SUM(result IS NULL AND commence_time<=?) FROM picks',
            (now,now)).fetchone()
        result['predictions'] = dict(zip(('saved','settled','upcoming','awaiting_results'),
                                        (int(n or 0) for n in counts)))
        result['match_observations'] = conn.execute('SELECT COUNT(*) FROM match_observations').fetchone()[0] if 'match_observations' in tables else 0
        allowed_sources = {'football_data','openligadb','openfootball','sportsdb','sharpapi',
                           'api_football','allsports','the_odds_api','oddspapi'}
        if 'match_observations' in tables:
            result['history_by_source'] = {source: count for source, count in conn.execute(
                'SELECT source,COUNT(*) FROM match_observations GROUP BY source') if source in allowed_sources}
        result['jobs'] = {}
        for name,key in (('generation','daily:status'),('history','history:status'),('settlement','daily:settlement_status')):
            row = conn.execute('SELECT val_json FROM system_telemetry WHERE key=?',(key,)).fetchone()
            status = json.loads(row[0]) if row else {}
            state = status.get('state','not_started')
            result['jobs'][name] = {'state': state if state in ('ok','ready','running','failed','broken','partial','degraded','unproven','paused','not_started') else 'unknown',
                                    'has_errors': bool(status.get('error'))}
            for field in ('last_attempt','last_finished'):
                try:
                    stamp = datetime.fromisoformat(status[field].replace('Z','+00:00'))
                    if stamp.tzinfo is not None:
                        result['jobs'][name][field] = stamp.astimezone(timezone.utc).isoformat()
                except (KeyError,AttributeError,TypeError,ValueError):
                    pass
        row = conn.execute('SELECT val_json FROM system_telemetry WHERE key=?',('daily:board',)).fetchone()
        publication = json.loads(row[0]) if row else {}
        board = publication.get('board') or {}
        result['published'] = bool(row)
        result['categories'] = {name:len(board.get(name) or []) for name in ('winning','micro_bets','earning','accumulators')}
        failed = {p['name'] for p in publication.get('providers',[]) if p.get('error') and p.get('name') in allowed_sources}
        failed.update(name for name, source in (publication.get('prices', {}).get('sources') or {}).items()
                      if name in allowed_sources and source.get('error'))
        result['failed_sources'] = sorted(failed)
    alternate = ROOT/'data/lisa.db'
    if path != alternate.resolve() and alternate.is_file():
        with sqlite3.connect(alternate.resolve().as_uri()+'?mode=ro',uri=True) as conn:
            if conn.execute("SELECT name FROM sqlite_master WHERE name='picks'").fetchone():
                result['default_database_predictions'] = conn.execute('SELECT COUNT(*) FROM picks').fetchone()[0]
                result['database_note'] = 'Starting lisa start separately may use data/lisa.db. This launcher uses the same explicit database for web and worker.'
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8080)
    parser.add_argument('--database',default=str(ROOT/'data/localhost-paper.db'))
    parser.add_argument('--postgres',action='store_true',help='use the managed/native PostgreSQL URL in .env')
    parser.add_argument('--unlock-tiers',action='store_true',help='temporarily expose every tier on this paper verification server')
    parser.add_argument('--check',action='store_true',help='check prerequisites without starting jobs')
    parser.add_argument('--diagnose',action='store_true',help='print a credential-safe read-only SQLite pipeline report')
    args = parser.parse_args(argv)
    children = []
    stopping = [False]
    def stop(signum,frame):
        stopping[0] = True
    try:
        if not 1024<=args.port<=65535:
            raise ValueError('Choose a port from 1024 to 65535')
        if not args.postgres and not args.database.endswith('.db'):
            raise ValueError('Local SQLite path must end in .db')
        if args.diagnose:
            if args.postgres:
                raise ValueError('Use the admin Operations page for PostgreSQL diagnostics')
            print(json.dumps(diagnose(args.database),indent=2))
            return 0
        check_port(args.port)
        child_environment = environment(args.database,args.postgres)
        child_environment['LISA_PAPER_TIERS_UNLOCKED'] = '1' if args.unlock_tiers else '0'
        if args.check:
            print('Local port and storage prerequisites passed. Provider authentication still needs a live run.')
            return 0
        if not child_environment.get('LISA_OWNER_PASSWORD'):
            child_environment.update(local_owner(ROOT/'data/localhost-owner.env'))
            print('Admin sign-in credentials saved privately in data/localhost-owner.env.',flush=True)
        for sig in (signal.SIGINT,signal.SIGTERM):
            signal.signal(sig,stop)
        web = subprocess.Popen([sys.executable,'-m','lisa','start','--port',str(args.port),
                '--no-ingest','--no-bot'],cwd=ROOT,env=child_environment)
        children.append(web)
        wait_for_web(web,args.port)
        children.append(subprocess.Popen([sys.executable,'-m','lisa','worker'],cwd=ROOT,env=child_environment))
        if not args.postgres:
            print('Web and worker database: '+child_environment['LISA_DATABASE_URL'],flush=True)
        print('Dashboard: http://localhost:'+str(args.port)+' · admin: /admin · paper stakes: zero',flush=True)
        if args.unlock_tiers:
            print('Paper verification: all prediction tiers unlocked. Restart without --unlock-tiers to restore access limits.',flush=True)
        while not stopping[0]:
            if any(child.poll() is not None for child in children):
                raise RuntimeError('A local service stopped; both services will be stopped')
            time.sleep(.25)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        if isinstance(exc,PermissionError):
            print('This environment denied local socket or filesystem access; run this launcher in an ordinary terminal on your computer.')
        elif isinstance(exc,ValueError):
            print(str(exc))
        else:
            print('Local launch unavailable ('+type(exc).__name__+'). Check port availability and child service output. Private exception text withheld.')
        return 1
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=45)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


if __name__ == '__main__':
    raise SystemExit(main())
