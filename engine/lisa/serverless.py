"""Vercel adapter: saved web reads and authenticated, finite paper jobs.

No listening socket, permanent worker, local database, or writable secret file
is created here. Imports and unauthorized cron requests do not connect to DB.
"""
import hmac
import os
import threading
import urllib.parse
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from .server import LISAProductionHandler, configure_production_context
from .job_budget import execution_budget, check_budget

_context = None
_context_lock = threading.Lock()
JOBS = frozenset(('generation', 'settlement', 'history'))
JOB_INTERVALS = {'generation': 3600, 'settlement': 900, 'history': 3600}
FAILED_JOB_RETRY_SEC = 300


def bootstrap_owner(auth, environment=None):
    """Create the initial owner before public registration can claim the email.

    Existing users require matching bootstrap evidence or password proof. An
    environment password change does not silently reset an existing account.
    """
    environment = os.environ if environment is None else environment
    from .auth import EMAIL_REGEX
    owners = [email.strip().lower() for email in environment.get('LISA_ADMIN_EMAILS','').split(',') if email.strip()]
    password = environment.get('LISA_OWNER_PASSWORD','')
    if len(owners) != 1 or not EMAIL_REGEX.match(owners[0]) or len(password) < 16:
        raise ValueError('Configure one LISA_ADMIN_EMAILS owner and LISA_OWNER_PASSWORD of at least 16 characters')
    email = owners[0]
    key = 'bootstrap:owner'
    proof = auth.storage.get_telemetry(key) or {}
    account = auth.get_user_by_email(email)
    if account and proof == {'email':email,'user_id':account['id']}:
        return
    if account is None:
        try:
            account = auth.register_user(email,password,display_name='Owner',tier='admin')
        except ValueError:
            account = auth.get_user_by_email(email)
    if not account or not auth.authenticate_user(email,password):
        raise ValueError('Owner account requires verified bootstrap credentials')
    auth.storage.set_telemetry(key,{'email':email,'user_id':account['id']})


def cron_authorization(headers, *, environment=None):
    """Evaluate before connecting to shared storage or constructing providers."""
    environment = os.environ if environment is None else environment
    if environment.get('VERCEL_ENV') != 'production':
        return 403
    secret = environment.get('CRON_SECRET', '')
    if len(secret) < 32:
        return 503
    supplied = headers.get('Authorization', '')
    return 200 if hmac.compare_digest(supplied.encode(), ('Bearer '+secret).encode()) else 401


def routed_path(raw_path):
    """Preserve the route and query when a Vercel rewrite targets /api/index."""
    parsed = urllib.parse.urlsplit(raw_path)
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    routes = [value for key,value in pairs if key == '__lisa_path']
    if len(routes) > 1:
        raise ValueError('Invalid route')
    path = routes[0] if routes else parsed.path
    parts = path.split('/')
    if (not path.startswith('/api/') or len(path) > 512
            or any(part in ('.','..') for part in parts)
            or any(ord(c)<32 or c in ('?', '#', '\\') for c in path)):
        raise ValueError('Invalid route')
    query = urllib.parse.urlencode([(k,v) for k,v in pairs if k != '__lisa_path'])
    return path + ('?'+query if query else '')


def get_context():
    global _context
    if _context is not None:
        return _context
    with _context_lock:
        if _context is not None:
            return _context
        from . import config as cfg
        from .storage import PostgresStorage
        from .runtime import RuntimeConfig
        from .control import Control
        from .auth import AuthManager
        from .pilot import PaperGeneration, PaperSettlement, PaperHistory
        from .telegram_bot import registry
        settings = cfg.load_settings()
        if not settings.database_url.startswith(('postgres://', 'postgresql://')):
            raise ValueError('Vercel requires LISA_DATABASE_URL for managed PostgreSQL')
        settings = cfg.paper_settings(replace(settings, storage_driver='postgres',
            provider_credentials_backend='postgres', paper_mode=True,
            daily_interval_sec=3600, provider_timeout_sec=min(10., settings.provider_timeout_sec)))
        storage = PostgresStorage(settings.database_url, serverless=True)
        try:
            runtime = RuntimeConfig(settings, storage=storage)
            # Validate encryption configuration before accepting any requests.
            runtime.settings()
            auth = AuthManager(storage=storage)
            bootstrap_owner(auth)
            control = Control(storage, auth=auth, runtime=runtime)
            context = configure_production_context(SimpleNamespace(),
                web_dir=str(Path(__file__).resolve().parents[2]/'web'),
                storage=storage, settings=settings, auth=auth, control=control)
            context.jobs = {name: cls(storage, runtime.settings, settle_in_cycle=False)
                for name,cls in (('generation',PaperGeneration),('settlement',PaperSettlement),
                                 ('history',PaperHistory))}
            context.jobs['history'].season_batch_limit = 1
            context.jobs['history'].statistics_batch_limit = 3
            context.daily_service = context.jobs['generation']
            control.scheduler = context.daily_service
            storage.set_telemetry('daily:worker_mode','separate')
            registry.storage = storage
            registry.db_path = None
            _context = context
        except BaseException:
            storage.close()
            raise
    return _context


def run_job(context, name):
    """Only the authenticated scheduler calls this; leases/quotas are shared."""
    context.control.runtime.load()
    context.control.runtime.invalidate_credentials()
    job = context.jobs[name]
    # A distinct short dispatch lease prevents concurrent scheduler retries from
    # passing the cadence gate together. The service has its own execution lease.
    from .daily_service import DailyService
    dispatch = DailyService(context.storage, context.control.runtime.settings)
    dispatch.lease_name = 'dispatch:'+name
    now = datetime.now(timezone.utc)
    if not dispatch._claim(now):
        return {'job':name,'state':'another_worker'}
    try:
        key = 'serverless:last_dispatch:'+name
        last = context.storage.get_telemetry(key) or {}
        interval = (FAILED_JOB_RETRY_SEC if last.get('failed') or last.get('state') == 'running'
                    else JOB_INTERVALS[name])
        if now.timestamp()-float(last.get('timestamp',0)) < interval:
            return {'job':name,'state':'not_due'}
        context.storage.set_telemetry(key,{'timestamp':now.timestamp(),'state':'running'})
        try:
            with execution_budget(seconds=210):
                status = job.tick()
                check_budget()
        except Exception:
            context.storage.set_telemetry(key,{'timestamp':now.timestamp(),'state':'failed','failed':True})
            raise
        # Provider exception strings and bodies never enter scheduler responses.
        safe = {key:status[key] for key in ('state','predictions_added','settled',
                    'observations','statistics_requests','error_type') if key in status}
        failed = bool(status.get('error')) or status.get('state') in ('failed','broken','no_leagues')
        for child in ('supporting','offer_history'):
            detail = status.get(child)
            if isinstance(detail,dict):
                failed |= bool(detail.get('error')) or detail.get('state') in ('failed','broken','degraded')
        safe.update(job=name, failed=failed, paper_mode=True)
        context.storage.set_telemetry(key,{'timestamp':now.timestamp(),
            'state':'completed','failed':bool(failed)})
        return safe
    finally:
        dispatch._release()


class VercelHandler(LISAProductionHandler):
    def _dispatch(self, method):
        try:
            self.path = routed_path(self.path)
        except ValueError:
            self._send_json({'error':'Unknown API route'},status=404)
            return
        path = urllib.parse.urlsplit(self.path).path
        name = path.removeprefix('/api/cron/') if path.startswith('/api/cron/') else None
        if name is not None:
            if name not in JOBS:
                self._send_json({'error':'Unknown scheduled job'},status=404)
                return
            if method not in ('GET','POST'):
                self._send_json({'error':'Method not allowed'},status=405)
                return
            status = cron_authorization(self.headers)
            if status != 200:
                self._send_json({'error':'Scheduled job authorization unavailable' if status==503
                                 else 'Scheduled job authorization required'},status=status)
                return
        try:
            self.server = get_context()
            self.server.control.runtime.load()
            if name is not None:
                result = run_job(self.server,name)
                self._send_json(result,status=503 if result.get('failed') else 200)
            else:
                # Runtime settings are read on each request by the underlying
                # API. No ordinary visitor request starts a provider job.
                getattr(super(), 'do_'+method)()
        except Exception as exc:
            self._send_json({'error':'Service unavailable','error_type':type(exc).__name__},status=503)

    def do_GET(self):
        self._dispatch('GET')

    def do_POST(self):
        self._dispatch('POST')

    def do_PUT(self):
        self._dispatch('PUT')

    def do_PATCH(self):
        self._dispatch('PATCH')

    def do_DELETE(self):
        self._dispatch('DELETE')

    def do_HEAD(self):
        self._send_json({'error':'Method not allowed'},status=405)

    def log_message(self, format, *args):
        # Request URLs can contain tokens; Vercel already records route/status.
        pass
