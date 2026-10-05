"""Portable SQL staging, source circuits and atomic quote replacement."""
import hashlib
import json
from datetime import timedelta

from ..storage import RelationalStorage

DDL = '''
CREATE TABLE IF NOT EXISTS scalper_resources (
 source TEXT NOT NULL, resource TEXT NOT NULL, fetched_at DOUBLE PRECISION NOT NULL,
 expires_at DOUBLE PRECISION NOT NULL, next_attempt DOUBLE PRECISION NOT NULL,
 failures INTEGER NOT NULL, payload TEXT, content_hash TEXT, etag TEXT,
 last_modified TEXT, error TEXT NOT NULL, parser_version INTEGER NOT NULL,
 PRIMARY KEY(source, resource)
);
CREATE TABLE IF NOT EXISTS scalper_events (
 source TEXT NOT NULL, event_id TEXT NOT NULL, sport_key TEXT NOT NULL,
 kickoff DOUBLE PRECISION NOT NULL, observed_at DOUBLE PRECISION NOT NULL,
 payload TEXT NOT NULL, PRIMARY KEY(source, event_id)
);
CREATE INDEX IF NOT EXISTS idx_scalper_events_league
 ON scalper_events(sport_key, kickoff);
CREATE TABLE IF NOT EXISTS scalper_quotes (
 source TEXT NOT NULL, event_id TEXT NOT NULL, book_key TEXT NOT NULL,
 identity TEXT NOT NULL, observed_at DOUBLE PRECISION NOT NULL,
 updated_at DOUBLE PRECISION, payload TEXT NOT NULL,
 PRIMARY KEY(source, event_id, book_key, identity)
);
CREATE TABLE IF NOT EXISTS scalper_sources (
 source TEXT PRIMARY KEY, next_attempt DOUBLE PRECISION NOT NULL,
 failures INTEGER NOT NULL, error TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scalper_quote_sets (
 source TEXT NOT NULL, event_id TEXT NOT NULL, book_key TEXT NOT NULL,
 observed_at DOUBLE PRECISION NOT NULL, PRIMARY KEY(source, event_id, book_key)
);
'''


def encoded(value):
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(',', ':'))


class ScalperRepository:
    def __init__(self, storage):
        if not isinstance(storage, RelationalStorage):
            raise ValueError('Scalper requires shared SQLite or PostgreSQL storage')
        self.storage = storage
        with storage._tx() as conn:
            if storage.lock_suffix:
                conn.execute('SELECT pg_advisory_xact_lock(1280525635)')
            conn.executescript(DDL)

    def resource(self, source, resource):
        with self.storage._tx() as conn:
            row = conn.execute('SELECT * FROM scalper_resources WHERE source=? AND resource=?',
                               (source, resource)).fetchone()
        return dict(row) if row else None

    def source_state(self, source):
        with self.storage._tx() as conn:
            row = conn.execute('SELECT * FROM scalper_sources WHERE source=?', (source,)).fetchone()
        return dict(row) if row else {'next_attempt': 0, 'failures': 0, 'error': ''}

    def claim(self, owner, now, seconds, *, name='scalper'):
        with self.storage._tx() as conn:
            self.storage.begin_write(conn)
            conn.execute("INSERT INTO worker_leases VALUES (?, '', 0) ON CONFLICT(name) DO NOTHING", (name,))
            return bool(conn.execute("UPDATE worker_leases SET owner=?, expires=? "
                "WHERE name=? AND expires<=?", (owner, now.timestamp() + seconds, name, now.timestamp())).rowcount)

    def release(self, owner, *, name='scalper'):
        with self.storage._tx() as conn:
            conn.execute("UPDATE worker_leases SET expires=0 WHERE name=? AND owner=?", (name, owner))

    def failure(self, source, resource, now, delay, error, *, host=False):
        """Retain the last good resource. Only classification is public."""
        with self.storage._tx() as conn:
            self.storage.begin_write(conn)
            conn.execute('INSERT INTO scalper_resources VALUES (?, ?, 0, 0, ?, 1, '
                'NULL, NULL, NULL, NULL, ?, 1) ON CONFLICT(source, resource) DO UPDATE SET '
                'next_attempt=excluded.next_attempt, failures=scalper_resources.failures+1, error=excluded.error',
                (source, resource, now.timestamp() + delay, error))
            if host:
                conn.execute('INSERT INTO scalper_sources VALUES (?, ?, 1, ?) ON CONFLICT(source) DO UPDATE SET '
                    'next_attempt=excluded.next_attempt, failures=scalper_sources.failures+1, error=excluded.error',
                    (source, now.timestamp() + delay, error))

    def accept(self, batch, *, resource, payload, now, ttl, etag=None, last_modified=None):
        """Commit resource, events and quote revocations as one transaction."""
        from .contracts import timestamp
        body = encoded(payload)
        fixtures = {r['source_event_id']: r for r in batch.fixtures}
        offers = {}
        for q in batch.quotes:
            offers.setdefault((q['event_id'], q['book_key']), []).append(q)
        accepted = []
        with self.storage._tx() as conn:
            self.storage.begin_write(conn)
            if self.storage.lock_suffix:
                # Serialize only this source's short merge, including races
                # where event/quote rows do not exist yet. No network under lock.
                source_lock = int.from_bytes(hashlib.sha256(batch.source.encode()).digest()[:4], 'big') % 2147483647
                conn.execute('SELECT pg_advisory_xact_lock(1280525636, ?)', (source_lock,))
            for row in batch.fixtures:
                observed = timestamp(row['observed_at']).timestamp()
                # Summary enrichment may not erase richer observations from the
                # same source. A final correction CAN change an actual score.
                previous = conn.execute('SELECT payload, observed_at FROM scalper_events '
                    'WHERE source=? AND event_id=?' + self.storage.lock_suffix,
                    (batch.source, row['source_event_id'])).fetchone()
                if previous and previous['observed_at'] > observed:
                    continue
                value = dict(row)
                if previous:
                    old = json.loads(previous['payload'])
                    from ..providers.calendar import team_identity
                    if (old['sport_key'] != value['sport_key'] or
                            team_identity(old['home_team']) != team_identity(value['home_team']) or
                            team_identity(old['away_team']) != team_identity(value['away_team'])):
                        from ..providers.base import ParseError
                        raise ParseError('scalper: source event identity changed')
                    # Never regress a final observation to live/scheduled on a
                    # delayed scoreboard. Corrections of final scores are kept.
                    if old.get('completed') and not value.get('completed') and value['status'] not in ('CANCELED', 'ABANDONED'):
                        continue
                    if (not value.get('ended_after_extra_time') and old.get('home_score') == value.get('home_score')
                            and old.get('away_score') == value.get('away_score')):
                        for key, old_value in old.items():
                            if (key.startswith(('home_', 'away_')) or key in ('source_player_statistics',
                                    'statistics_observed_at')) and value.get(key) is None:
                                value[key] = old_value
                conn.execute('INSERT INTO scalper_events VALUES (?, ?, ?, ?, ?, ?) '
                    'ON CONFLICT(source, event_id) DO UPDATE SET sport_key=excluded.sport_key, '
                    'kickoff=excluded.kickoff, observed_at=excluded.observed_at, payload=excluded.payload',
                    (batch.source, value['source_event_id'], value['sport_key'], value['epoch'], observed, encoded(value)))
                accepted.append(value)
            for event, book in batch.replace_books:
                # An older capture cannot revoke a newer quote snapshot.
                relevant = offers.get((event, book), ())
                row = fixtures[event]
                observed = timestamp(row['observed_at']).timestamp()
                previous = conn.execute('SELECT observed_at FROM scalper_quote_sets '
                    'WHERE source=? AND event_id=? AND book_key=?' + self.storage.lock_suffix,
                    (batch.source, event, book)).fetchone()
                if previous and previous['observed_at'] > observed:
                    continue
                conn.execute('DELETE FROM scalper_quotes WHERE source=? AND event_id=? AND book_key=?',
                             (batch.source, event, book))
                conn.execute('INSERT INTO scalper_quote_sets VALUES (?, ?, ?, ?) '
                    'ON CONFLICT(source, event_id, book_key) DO UPDATE SET observed_at=excluded.observed_at',
                    (batch.source, event, book, observed))
                for q in relevant:
                    # Suspensions are retained for audit; the bridge rejects them.
                    identity = hashlib.sha256(encoded([q[k] for k in
                        ('market', 'selection', 'line', 'period', 'settlement_contract')]).encode()).hexdigest()
                    update = timestamp(q['updated_at'], optional=True)
                    conn.execute('INSERT INTO scalper_quotes VALUES (?, ?, ?, ?, ?, ?, ?)',
                        (batch.source, event, book, identity, observed,
                         update.timestamp() if update else None, encoded(q)))
            conn.execute('INSERT INTO scalper_resources VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, 1) '
                'ON CONFLICT(source, resource) DO UPDATE SET fetched_at=excluded.fetched_at, '
                'expires_at=excluded.expires_at, next_attempt=0, failures=0, payload=excluded.payload, '
                'content_hash=excluded.content_hash, etag=excluded.etag, last_modified=excluded.last_modified, '
                "error='', parser_version=1", (batch.source, resource, now.timestamp(), now.timestamp() + ttl,
                body, hashlib.sha256(body.encode()).hexdigest(), etag, last_modified, ''))
            conn.execute("INSERT INTO scalper_sources VALUES (?, 0, 0, '') ON CONFLICT(source) "
                         "DO UPDATE SET next_attempt=0, failures=0, error=''", (batch.source,))
        return tuple(accepted)

    def fixtures(self, leagues, *, now, max_age=900, include_stale=False):
        if not leagues:
            return []
        lower = (now - timedelta(days=4 * 366)).timestamp()
        upper = (now + timedelta(days=31)).timestamp()
        with self.storage._tx() as conn:
            rows = conn.execute('SELECT payload, observed_at FROM scalper_events WHERE sport_key IN ('
                + ','.join('?' for _ in leagues) + ') AND kickoff>=? AND kickoff<=? ORDER BY kickoff LIMIT 20000',
                (*leagues, lower, upper)).fetchall()
        out = []
        for r in rows:
            value = json.loads(r['payload'])
            age = now.timestamp() - r['observed_at']
            if value['provider'] in ('pinnacle_browser', 'sportybet_browser'):
                from .contracts import timestamp
                confirmed = timestamp(value.get('calendar_confirmed_at'), optional=True)
                age = max(age, (now - confirmed).total_seconds()) if confirmed else max(age, max_age + 1)
            value['collection_age_sec'] = max(0., age)
            value['stale'] = age > max_age
            if include_stale or value.get('completed') or -60 <= age <= max_age:
                out.append(value)
        return out

    def quotes(self, leagues, *, now, max_age=300):
        if not leagues:
            return []
        with self.storage._tx() as conn:
            rows = conn.execute('SELECT q.payload, e.payload AS event_payload FROM scalper_quotes q JOIN scalper_events e '
                'ON e.source=q.source AND e.event_id=q.event_id WHERE e.sport_key IN ('
                + ','.join('?' for _ in leagues) + ') AND q.observed_at>=? AND q.observed_at<=? '
                'AND e.kickoff>? ORDER BY q.source, q.event_id LIMIT 20000',
                (*leagues, now.timestamp() - max_age, now.timestamp() + 60, now.timestamp())).fetchall()
        return [json.loads(r['payload']) for r in rows if
                json.loads(r['event_payload'])['status'] in ('SCHEDULED', 'UNKNOWN')]

    def health(self, *, now):
        with self.storage._tx() as conn:
            resources = conn.execute('SELECT source, COUNT(*) AS resources, MAX(fetched_at) AS last_success, '
                'SUM(CASE WHEN error<>? THEN 1 ELSE 0 END) AS failed FROM scalper_resources GROUP BY source', ('',)).fetchall()
            circuits = {r['source']: dict(r) for r in conn.execute('SELECT * FROM scalper_sources')}
            counts = {name: conn.execute('SELECT COUNT(*) AS n FROM ' + name).fetchone()['n']
                      for name in ('scalper_events', 'scalper_quotes')}
        return dict(counts, sources=[dict(r, **{k: v for k, v in circuits.get(r['source'], {}).items() if k != 'source'})
                                    for r in resources], observed_at=now.isoformat(),
                    browser_workers=self.storage.get_telemetry('scalper:browser:status') or {},
                    worker=self.storage.get_telemetry('scalper:status') or {'state': 'not_started'})

    def prune(self, now):
        with self.storage._tx() as conn:
            self.storage.begin_write(conn)
            conn.execute('DELETE FROM scalper_resources WHERE fetched_at<? AND next_attempt<?',
                         (now.timestamp() - 30 * 86400, now.timestamp()))
            conn.execute('DELETE FROM scalper_quotes WHERE observed_at<?', (now.timestamp() - 2 * 86400,))
            conn.execute('DELETE FROM scalper_events WHERE kickoff<?', (now.timestamp() - 4 * 366 * 86400,))
            conn.execute('DELETE FROM scalper_quote_sets WHERE observed_at<?', (now.timestamp() - 30 * 86400,))
