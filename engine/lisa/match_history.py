"""Durable, source-attributed observations shared by web and worker processes."""
import json
from datetime import datetime, timedelta, timezone
from .providers.calendar import dedupe_fixtures

HISTORY_DDL = '''
CREATE TABLE IF NOT EXISTS match_observations (
 source TEXT NOT NULL, match_id TEXT NOT NULL, sport_key TEXT NOT NULL,
 kickoff TEXT NOT NULL, observed_at TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(source, match_id)
);
CREATE INDEX IF NOT EXISTS idx_observations_league_time
 ON match_observations(sport_key, kickoff);
'''


class HistoryRepository:
    def __init__(self, storage):
        self.storage = storage

    def ingest(self, rows, *, observed_at=None):
        observed_at = observed_at or datetime.now(timezone.utc)
        staged = []
        for row in rows:
            if not row.get('completed') or row.get('status') not in ('FINISHED', 'final'):
                continue
            if any(type(row.get(k)) is not int or row[k] < 0
                   for k in ('home_score', 'away_score')):
                continue
            try:
                kickoff = datetime.fromisoformat(row['kickoff'].replace('Z', '+00:00'))
                if kickoff.tzinfo is None or kickoff > observed_at:
                    continue
                if not all(row.get(k) for k in ('provider', 'match_id', 'sport_key', 'home_team', 'away_team')):
                    continue
                staged.append((row['provider'], row['match_id'], row['sport_key'],
                    kickoff.astimezone(timezone.utc).isoformat(), observed_at.isoformat(),
                    json.dumps(row, allow_nan=False, sort_keys=True, separators=(',', ':'))))
            except (ValueError, TypeError, KeyError):
                continue
        # One transaction and bounded bulk lookups. Unchanged season snapshots
        # do not rewrite thousands of rows on every five-minute scan.
        staged = {values[:2]: values for values in staged}
        if not staged:
            return 0
        with self.storage._tx() as conn:
            self.storage.begin_write(conn)
            if self.storage.lock_suffix:
                # Serialise only short observation merges across worker jobs;
                # network calls and model fitting never hold this lock.
                conn.execute('SELECT pg_advisory_xact_lock(1280525634)')
            existing = {}
            sources = sorted({source for source, _ in staged})
            for source in sources:
                ids = sorted(match_id for src, match_id in staged if src == source)
                for start in range(0, len(ids), 400):
                    batch = ids[start:start + 400]
                    found = conn.execute('SELECT match_id, payload FROM match_observations WHERE source=? '
                        'AND match_id IN (' + ','.join('?' for _ in batch) + ')', (source, *batch)).fetchall()
                    existing.update({(source, r['match_id']): r['payload'] for r in found})
            for key, values in sorted(staged.items()):
                previous = existing.get(key)
                if previous is not None:
                    old, new = json.loads(previous), json.loads(values[-1])
                    # A basic refresh must not erase richer historical observations.
                    for name in ('source_statistics', 'source_player_statistics',
                                 'statistics_observed_at', 'player_statistics_observed_at', 'source_referee'):
                        if new.get(name) is None and old.get(name) is not None:
                            new[name] = old[name]
                    for side in ('home', 'away'):
                        name = side + '_first_half_score'
                        if new.get(name) is None and type(old.get(name)) is int and old[name] <= new[side + '_score']:
                            new[name] = old[name]
                    if not new.get('ended_after_extra_time') and not new.get('corner_conflict'):
                        for name in ('home_corners', 'away_corners'):
                            if new.get(name) is None and old.get(name) is not None:
                                new[name] = old[name]
                    encoded = json.dumps(new, allow_nan=False, sort_keys=True, separators=(',', ':'))
                    if encoded == previous:
                        continue
                    values = (*values[:-1], encoded)
                conn.execute('INSERT INTO match_observations VALUES (?, ?, ?, ?, ?, ?) '
                    'ON CONFLICT(source, match_id) DO UPDATE SET sport_key=excluded.sport_key, '
                    'kickoff=excluded.kickoff, observed_at=excluded.observed_at, payload=excluded.payload', values)
        return len(staged)

    def results(self, leagues, *, as_of, years=4):
        if not leagues:
            return []
        as_of = as_of.astimezone(timezone.utc)
        lower = as_of - timedelta(days=365.25 * years)
        with self.storage._tx() as conn:
            rows = conn.execute('SELECT payload FROM match_observations WHERE sport_key IN ('
                + ','.join('?' for _ in leagues) + ') AND kickoff < ? AND kickoff >= ? ORDER BY kickoff, source',
                (*leagues, as_of.isoformat(), lower.isoformat())).fetchall()
        from .data_quality import reconcile_results
        eligible = []
        for row in rows:
            value = json.loads(row['payload'])
            if value.get('result_available_after'):
                try:
                    available = datetime.fromisoformat(value['result_available_after'])
                    if available.tzinfo is None or available > as_of:
                        continue
                except (TypeError, ValueError):
                    continue
            eligible.append(value)
        accepted, _ = reconcile_results(eligible)
        return dedupe_fixtures(accepted)
