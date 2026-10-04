"""Immutable price observations, distinct from forecasts and recommendations."""
import hashlib
import json
import math
from datetime import datetime, timezone

ODDS_HISTORY_DDL = '''CREATE TABLE IF NOT EXISTS odds_observations (
 id TEXT PRIMARY KEY, source TEXT NOT NULL, event_id TEXT NOT NULL,
 bookmaker TEXT NOT NULL, observed_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_odds_event_time ON odds_observations(source,event_id,observed_at);
CREATE INDEX IF NOT EXISTS idx_odds_time ON odds_observations(observed_at);
'''


class OddsHistoryRepository:
    def __init__(self, storage):
        self.storage = storage

    def ingest(self, rows):
        staged = {}
        for row in rows:
            try:
                if not all(isinstance(row.get(k), str) and row[k] for k in
                           ('source', 'event_id', 'bookmaker', 'observed_at')):
                    continue
                observed = datetime.fromisoformat(row['observed_at'].replace('Z', '+00:00'))
                if observed.tzinfo is None or observed > datetime.now(timezone.utc):
                    continue
                if type(row.get('odds')) not in (float, int) or not math.isfinite(row['odds']) or row['odds'] <= 1:
                    continue
                encoded = json.dumps(row, sort_keys=True, separators=(',', ':'), allow_nan=False)
                identity = hashlib.sha256(encoded.encode()).hexdigest()
                staged[identity] = (identity, row['source'], row['event_id'], row['bookmaker'],
                                    observed.astimezone(timezone.utc).isoformat(), encoded)
            except (ValueError, TypeError, KeyError):
                continue
        added = 0
        values = list(staged.values())
        with self.storage._tx() as conn:
            for start in range(0, len(values), 100):
                batch = values[start:start+100]
                added += conn.execute('INSERT INTO odds_observations VALUES ' +
                    ','.join('(?, ?, ?, ?, ?, ?)' for _ in batch) + ' ON CONFLICT(id) DO NOTHING',
                    tuple(v for row in batch for v in row)).rowcount
        return added

    def observations(self, source, event_id, *, before=None, limit=1000):
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError('History limit must be between 1 and 10000')
        cutoff = before or datetime.now(timezone.utc)
        if cutoff.tzinfo is None:
            raise ValueError('History cutoff must include timezone')
        with self.storage._tx() as conn:
            rows = conn.execute('SELECT payload FROM odds_observations WHERE source=? AND event_id=? '
                'AND observed_at<=? ORDER BY observed_at DESC,id LIMIT ?',
                (source, event_id, cutoff.astimezone(timezone.utc).isoformat(), limit)).fetchall()
        return [json.loads(row['payload']) for row in rows]
