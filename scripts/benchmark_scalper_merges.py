"""Replay saved source captures against production merges in temporary databases."""
import argparse
from collections import Counter
from contextlib import contextmanager, closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'engine'))
from lisa.storage import SqliteStorage
from lisa.scalper.repository import ScalperRepository
from lisa.scalper.browser_sources import PinnacleBrowserSource, SportyBetBrowserSource


class Connection:
    def __init__(self, raw, counts):
        self.raw, self.counts = raw, counts

    def execute(self, sql, parameters=()):
        self.counts[sql.split()[0]] += 1
        return self.raw.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self.raw, name)


class CountedStorage(SqliteStorage):
    def __init__(self, path):
        self.counts = Counter()
        super().__init__(path)

    @contextmanager
    def _tx(self):
        with super()._tx() as connection:
            yield Connection(connection, self.counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True,
                        help='Existing SQLite database containing saved browser captures (read-only)')
    args = parser.parse_args()
    results = []
    with closing(sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True)) as origin:
        for source in (SportyBetBrowserSource(), PinnacleBrowserSource()):
            saved = origin.execute('SELECT payload FROM scalper_resources WHERE source=? AND resource=?',
                (source.name, 'browser:landing')).fetchone()
            if not saved or not saved[0]:
                continue
            capture = json.loads(saved[0])
            # Replay at the real capture time, not with a newly invented price clock.
            now = max(datetime.fromisoformat(value['received_at']) for value in capture['responses'].values())
            batch = source.parse(capture, now=now)
            with tempfile.TemporaryDirectory(prefix='scalper-sql-evaluation-') as folder:
                store = CountedStorage(str(Path(folder) / 'audit.db'))
                try:
                    repo = ScalperRepository(store)
                    measurements = []
                    for trial in range(4):
                        store.counts.clear()
                        started = time.monotonic()
                        repo.accept(batch, resource='browser:landing', payload=capture, now=now, ttl=0)
                        measurements.append({'milliseconds': round((time.monotonic() - started) * 1000, 2),
                            'statements': sum(store.counts.values()), 'by_operation': dict(store.counts)})
                    results.append({'source': source.name,
                        'fixtures': len(batch.fixtures), 'quotes': len(batch.quotes), 'first_commit': measurements[0],
                        'refresh_median_ms': statistics.median(value['milliseconds'] for value in measurements[1:]),
                        'refresh_statements': measurements[-1]['statements']})
                finally:
                    store.close()
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
