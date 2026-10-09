"""Research candidate only: portable batching with existing merge semantics."""
import hashlib
import json

from lisa.scalper.repository import ScalperRepository, encoded
from lisa.scalper.contracts import timestamp
from lisa.providers.base import ParseError
from lisa.providers.calendar import team_identity

def chunks(values, size):
    for start in range(0, len(values), size):
        yield values[start:start + size]

def insert(conn, table, values, suffix=''):
    for batch in chunks(values, 100):
        placeholders = '(' + ','.join('?' for value in batch[0]) + ')'
        conn.execute('INSERT INTO ' + table + ' VALUES ' + ','.join(placeholders for row in batch) + suffix,
            tuple(value for row in batch for value in row))

class BatchedRepository(ScalperRepository):
    def accept(self, batch, *, resource, payload, now, ttl, etag=None, last_modified=None):
        body = encoded(payload)
        fixtures = {row['source_event_id']: row for row in batch.fixtures}
        offers = {}
        for quote in batch.quotes:
            identity = hashlib.sha256(encoded([quote[key] for key in
                ('market', 'selection', 'line', 'period', 'settlement_contract')]).encode()).hexdigest()
            update = timestamp(quote['updated_at'], optional=True)
            offers.setdefault((quote['event_id'], quote['book_key']), []).append(
                (identity, update.timestamp() if update else None, encoded(quote)))
        accepted, event_values, set_values, quote_values = [], [], [], []
        replacements = []
        with self.storage._tx() as conn:
            self.storage.begin_write(conn)
            if self.storage.lock_suffix:
                source_lock = int.from_bytes(hashlib.sha256(batch.source.encode()).digest()[:4], 'big') % 2147483647
                conn.execute('SELECT pg_advisory_xact_lock(1280525636, ?)', (source_lock,))
            old_events, old_sets = {}, {}
            for ids in chunks(list(fixtures), 400):
                rows = conn.execute('SELECT event_id, payload, observed_at FROM scalper_events WHERE source=? '
                    'AND event_id IN (' + ','.join('?' for value in ids) + ')' + self.storage.lock_suffix,
                    (batch.source, *ids)).fetchall()
                old_events.update({row['event_id']: row for row in rows})
            for pairs in chunks(list(batch.replace_books), 200):
                rows = conn.execute('SELECT event_id, book_key, observed_at FROM scalper_quote_sets WHERE source=? '
                    'AND (event_id,book_key) IN (' + ','.join('(?,?)' for pair in pairs) + ')' + self.storage.lock_suffix,
                    (batch.source, *(value for pair in pairs for value in pair))).fetchall()
                old_sets.update({(row['event_id'], row['book_key']): row for row in rows})
            for row in batch.fixtures:
                observed = timestamp(row['observed_at']).timestamp()
                previous = old_events.get(row['source_event_id'])
                if previous and previous['observed_at'] > observed:
                    continue
                value = dict(row)
                if previous:
                    old = json.loads(previous['payload'])
                    if (old['sport_key'] != value['sport_key'] or
                            team_identity(old['home_team']) != team_identity(value['home_team']) or
                            team_identity(old['away_team']) != team_identity(value['away_team'])):
                        raise ParseError('scalper: source event identity changed')
                    if old.get('completed') and not value.get('completed') and value['status'] not in ('CANCELED', 'ABANDONED'):
                        continue
                    if (not value.get('ended_after_extra_time') and old.get('home_score') == value.get('home_score')
                            and old.get('away_score') == value.get('away_score')):
                        for key, old_value in old.items():
                            if (key.startswith(('home_', 'away_')) or key in ('source_player_statistics',
                                    'statistics_observed_at')) and value.get(key) is None:
                                value[key] = old_value
                event_values.append((batch.source, value['source_event_id'], value['sport_key'], value['epoch'], observed, encoded(value)))
                accepted.append(value)
            for event, book in batch.replace_books:
                observed = timestamp(fixtures[event]['observed_at']).timestamp()
                previous = old_sets.get((event, book))
                if previous and previous['observed_at'] > observed:
                    continue
                replacements.append((event, book))
                set_values.append((batch.source, event, book, observed))
                quote_values.extend((batch.source, event, book, identity, observed, update, data)
                    for identity, update, data in offers.get((event, book), ()))
            insert(conn, 'scalper_events', event_values, ' ON CONFLICT(source, event_id) DO UPDATE SET '
                'sport_key=excluded.sport_key, kickoff=excluded.kickoff, observed_at=excluded.observed_at, payload=excluded.payload')
            for pairs in chunks(replacements, 200):
                conn.execute('DELETE FROM scalper_quotes WHERE source=? AND (event_id,book_key) IN ('
                    + ','.join('(?,?)' for pair in pairs) + ')',
                    (batch.source, *(value for pair in pairs for value in pair)))
            insert(conn, 'scalper_quote_sets', set_values, ' ON CONFLICT(source, event_id, book_key) '
                'DO UPDATE SET observed_at=excluded.observed_at')
            insert(conn, 'scalper_quotes', quote_values)
            conn.execute('INSERT INTO scalper_resources VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, 1) '
                'ON CONFLICT(source, resource) DO UPDATE SET fetched_at=excluded.fetched_at, '
                'expires_at=excluded.expires_at, next_attempt=0, failures=0, payload=excluded.payload, '
                'content_hash=excluded.content_hash, etag=excluded.etag, last_modified=excluded.last_modified, '
                "error='', parser_version=1", (batch.source, resource, now.timestamp(), now.timestamp() + ttl,
                body, hashlib.sha256(body.encode()).hexdigest(), etag, last_modified, ''))
            conn.execute("INSERT INTO scalper_sources VALUES (?, 0, 0, '') ON CONFLICT(source) "
                         "DO UPDATE SET next_attempt=0, failures=0, error=''", (batch.source,))
        return tuple(accepted)
