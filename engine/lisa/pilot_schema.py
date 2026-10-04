"""Portable schema for durable paper-run operational evidence."""
PILOT_DDL = '''CREATE TABLE IF NOT EXISTS pilot_cycles (
 id TEXT PRIMARY KEY, job TEXT NOT NULL, started_at TEXT NOT NULL,
 finished_at TEXT, duration_sec DOUBLE PRECISION,
 state TEXT NOT NULL, has_error INTEGER NOT NULL, payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pilot_cycle_time ON pilot_cycles(finished_at,job);
'''
