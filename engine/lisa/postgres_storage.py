"""PostgreSQL connection boundary and versioned schema migrations."""
from contextlib import contextmanager
import re
from urllib.parse import parse_qsl, unquote, urlparse

from .postgres_schema import SCHEMA_V1
from .match_history import HISTORY_DDL
from .providers.api_football import BUDGET_DDL
from .odds_history import ODDS_HISTORY_DDL
from .pilot_schema import PILOT_DDL
from .provider_credentials import CREDENTIAL_DDL


# Only parameter markers change here; business SQL itself is portable.
# Preserve literal question marks and quoted identifiers/comments. All query
# values remain bound parameters; no user value is interpolated into SQL.
_SQL_TOKEN = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|--[^\n]*|/\*.*?\*/|\?", re.S)


def bind_postgres(sql):
    sql = sql.replace('%', '%%')
    return _SQL_TOKEN.sub(lambda m: '%s' if m.group() == '?' else m.group(), sql)


def require_isolated_database(dsn, suffix):
    """Validate isolation before any connection or schema modification."""
    parsed = urlparse(dsn)
    name = unquote(parsed.path).removeprefix('/')
    if parsed.scheme not in ('postgres','postgresql') or not name.endswith(suffix) or '/' in name:
        raise ValueError('Use an explicitly named isolated PostgreSQL database')
    # libpq query options can override the database named in the URL path.
    if any(key.lower() in ('dbname','service','options') for key, value in
           parse_qsl(parsed.query, strict_parsing=True)):
        raise ValueError('Isolation URLs cannot override the selected database or search path')
    return name


class PostgresConnection:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, parameters=None):
        return self.raw.execute(bind_postgres(sql) if parameters is not None else sql, parameters)

    def executescript(self, sql):
        # Application DDL contains no dollar-quoted functions or embedded
        # semicolons. Execute statements separately for driver compatibility.
        for statement in sql.split(';'):
            if statement.strip():
                self.raw.execute(statement)

    def commit(self):
        self.raw.commit()


class PostgresBackend:
    """Connection/migration mixin; repositories are in RelationalStorage."""
    lock_suffix = ' FOR UPDATE'

    def __init__(self, dsn=None, *, pool=None, serverless=False):
        self.serverless = serverless
        self.db_schema = 'lisa_private' if serverless else 'public'
        if pool is None:
            if not dsn or not dsn.startswith(('postgresql://', 'postgres://')):
                raise ValueError('PostgreSQL requires LISA_DATABASE_URL with a postgresql:// URL')
            try:
                from psycopg import IntegrityError
                from psycopg.rows import dict_row
                from psycopg_pool import ConnectionPool
            except ImportError as exc:
                raise RuntimeError('Install the postgres extra: pip install -e "./engine[postgres]"') from exc
            self.integrity_errors = (IntegrityError,)
            connection_options = {'row_factory': dict_row, 'connect_timeout': 10,
                                  'prepare_threshold': None}
            if serverless:
                configured_ssl = dict(parse_qsl(urlparse(dsn).query)).get('sslmode')
                connection_options['sslmode'] = configured_ssl if configured_ssl in (
                    'verify-ca','verify-full') else 'require'
            pool = ConnectionPool(dsn, min_size=0 if serverless else 1,
                max_size=1 if serverless else 10, max_waiting=50,
                max_idle=30 if serverless else 600, timeout=10, open=False,
                kwargs=connection_options)
            pool.open()
            try:
                pool.wait(timeout=15)
            except BaseException:
                pool.close()
                raise
        self.pool = pool
        try:
            self.ensure_schema()
        except BaseException:
            self.pool.close()
            raise

    @contextmanager
    def _tx(self):
        # Pool context commits/rolls back and returns this connection only after
        # this operation finishes. Concurrent requests never share transactions.
        with self.pool.connection() as conn:
            # Transaction-local settings survive transaction poolers correctly.
            conn.execute("SELECT set_config('statement_timeout','30000',true), "
                         "set_config('lock_timeout','5000',true), "
                         "set_config('idle_in_transaction_session_timeout','30000',true)")
            if self.serverless:
                conn.execute("SELECT set_config('search_path','lisa_private',true)")
            yield PostgresConnection(conn)

    def begin_write(self, conn):
        # PostgreSQL starts a transaction on first statement. Publication takes
        # a row lock on the worker lease instead of locking the whole database.
        pass

    def ensure_schema(self):
        with self._tx() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(1280525633)')
            if self.serverless:
                # Keep application identities, sessions and provider secrets
                # outside Supabase's default publicly exposed API schema.
                conn.execute('CREATE SCHEMA IF NOT EXISTS lisa_private')
                conn.execute('REVOKE ALL ON SCHEMA lisa_private FROM PUBLIC')
            conn.execute('CREATE TABLE IF NOT EXISTS schema_migrations '
                         '(version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())')
            versions = {r['version'] for r in conn.execute('SELECT version FROM schema_migrations')}
            if versions - {1, 2, 3, 4, 5, 6}:
                raise RuntimeError('Database schema is newer than this application')
            if 1 not in versions:
                existing = conn.execute('SELECT to_regclass(?) AS table_name',
                                        (self.db_schema+'.picks',)).fetchone()
                if existing['table_name']:
                    raise RuntimeError('Unversioned PostgreSQL schema detected; use a fresh database or an explicit migration')
                conn.executescript(SCHEMA_V1)
                conn.execute('INSERT INTO schema_migrations(version) VALUES (1)')
            if 2 not in versions:
                conn.executescript(HISTORY_DDL)
                conn.execute('INSERT INTO schema_migrations(version) VALUES (2)')
            if 3 not in versions:
                conn.executescript(BUDGET_DDL)
                conn.execute('INSERT INTO schema_migrations(version) VALUES (3)')
            if 4 not in versions:
                conn.executescript(ODDS_HISTORY_DDL)
                conn.execute('INSERT INTO schema_migrations(version) VALUES (4)')
            if 5 not in versions:
                conn.executescript(PILOT_DDL)
                conn.execute('INSERT INTO schema_migrations(version) VALUES (5)')
            if 6 not in versions:
                conn.executescript(CREDENTIAL_DDL)
                conn.execute('INSERT INTO schema_migrations(version) VALUES (6)')

    def admin_database_info(self):
        with self._tx() as conn:
            row = conn.execute('SELECT current_database() AS database, '
                'pg_database_size(current_database()) AS bytes, version() AS version').fetchone()
        return dict(row, driver='postgres', schema=self.db_schema, pool=self.pool.get_stats())

    def close(self):
        self.pool.close()
