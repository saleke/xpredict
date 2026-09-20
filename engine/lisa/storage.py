"""Stage 4 — storage layer.

Dual-layer architecture:
  * hot layer: transient live state (Redis with TTL, or in-memory for local);
  * cold layer: the audited ledger (Postgres), WRITE-ONCE with a strict
    state machine — a settled pick can never be rewritten.

The whole engine runs on ``InMemoryStorage`` with zero infrastructure; the
Redis and Postgres drivers activate via ``LISA_STORAGE=redis|postgres``.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Iterable, Optional

from .gate import Pick

PENDING_STATES = ("TRIGGER_ALERT", "CONFIRMED", "PENDING_SETTLEMENT")


def pick_key(match_id: str, market: str, outcome: str) -> str:
    return f"{match_id}::{market}::{outcome}"


def pick_to_row(pick: Pick) -> dict:
    exec_ = pick.best_execution
    return {
        "dedupe_key": pick_key(pick.match_id, pick.market, pick.outcome_name),
        "match_id": pick.match_id,
        "sport_key": pick.sport_key,
        "market": pick.market,
        "outcome_name": pick.outcome_name,
        "line": pick.line,
        "home_team": pick.home_team,
        "away_team": pick.away_team,
        "commence_time": pick.commence_time.isoformat(),
        "p_true": pick.p_true,
        "fair_odds": pick.fair_odds,
        "n_books": pick.n_books,
        "stdev": pick.stdev,
        "cv": pick.cv,
        "state": pick.state,
        "result": None,
        "best_book": exec_.book_key if exec_ else None,
        "best_odds": exec_.odds if exec_ else None,
        "best_ev": exec_.ev if exec_ else None,
        "created_at": pick.created_at.isoformat(),
        "settled_at": None,
    }


class Storage:
    """Interface — the pipeline depends on this, not on concrete drivers."""

    # hot layer
    def upsert_live(self, key: str, data: dict, ttl_seconds: int) -> None:
        raise NotImplementedError

    def get_live(self, key: str) -> Optional[dict]:
        raise NotImplementedError

    def scan_live_keys(self) -> Iterable[str]:
        raise NotImplementedError

    # cold layer
    def insert_pick(self, pick: Pick) -> bool:
        raise NotImplementedError

    def list_pending_picks(self) -> list[dict]:
        raise NotImplementedError

    def list_settled_picks(self) -> list[dict]:
        raise NotImplementedError

    def settle_pick(self, dedupe_key: str, result: str,
                    settled_at: datetime, state: str = "SETTLED") -> bool:
        raise NotImplementedError


class InMemoryStorage(Storage):
    """Thread-safe-enough for a single worker; TTL is wall-clock monotonic."""

    def __init__(self) -> None:
        self._live: dict[str, tuple[float, dict]] = {}
        self._picks: dict[str, dict] = {}

    # -- hot layer -----------------------------------------------------------

    def upsert_live(self, key: str, data: dict, ttl_seconds: int) -> None:
        self._live[key] = (time.monotonic() + ttl_seconds, dict(data))

    def get_live(self, key: str) -> Optional[dict]:
        entry = self._live.get(key)
        if entry is None:
            return None
        expires, data = entry
        if expires < time.monotonic():
            del self._live[key]
            return None
        return data

    def scan_live_keys(self) -> Iterable[str]:
        now = time.monotonic()
        return [k for k, (exp, _) in self._live.items() if exp >= now]

    # -- cold layer ----------------------------------------------------------

    def insert_pick(self, pick: Pick) -> bool:
        row = pick_to_row(pick)
        if row["dedupe_key"] in self._picks:
            return False
        self._picks[row["dedupe_key"]] = row
        return True

    def list_pending_picks(self) -> list[dict]:
        return [r for r in self._picks.values() if r["state"] in PENDING_STATES]

    def list_settled_picks(self) -> list[dict]:
        return [r for r in self._picks.values() if r["state"] not in PENDING_STATES]

    def settle_pick(self, dedupe_key: str, result: str,
                    settled_at: datetime, state: str = "SETTLED") -> bool:
        row = self._picks.get(dedupe_key)
        if row is None or row["state"] not in PENDING_STATES:
            return False
        row["state"] = state
        row["result"] = result
        row["settled_at"] = settled_at.isoformat()
        return True


class RedisStorage(Storage):
    """Hot + cold in Redis. Hot keys carry TTLs; pick rows are JSON strings.
    Requires the optional ``redis`` package."""

    LIVE_PREFIX = "lisa:live:"
    LIVE_INDEX = "lisa:live:index"
    PICK_PREFIX = "lisa:pick:"
    PICK_INDEX = "lisa:picks"

    def __init__(self, url: str | None = None, *, client=None):
        if client is None:
            try:
                import redis
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "LISA_STORAGE=redis requires the 'redis' package") from exc
            client = redis.Redis.from_url(
                url or "redis://localhost:6379", decode_responses=True)
        self.r = client

    # -- hot layer -----------------------------------------------------------

    def upsert_live(self, key: str, data: dict, ttl_seconds: int) -> None:
        self.r.set(self.LIVE_PREFIX + key, json.dumps(data), ex=ttl_seconds)
        self.r.sadd(self.LIVE_INDEX, key)

    def get_live(self, key: str) -> Optional[dict]:
        raw = self.r.get(self.LIVE_PREFIX + key)
        return json.loads(raw) if raw else None

    def scan_live_keys(self) -> Iterable[str]:
        alive: list[str] = []
        dead: list[str] = []
        for key in self.r.smembers(self.LIVE_INDEX):
            if self.r.exists(self.LIVE_PREFIX + key):
                alive.append(key)
            else:
                dead.append(key)  # TTL expired: prune from the index
        if dead:
            self.r.srem(self.LIVE_INDEX, *dead)
        return alive

    # -- cold layer ----------------------------------------------------------

    def insert_pick(self, pick: Pick) -> bool:
        row = pick_to_row(pick)
        created = self.r.set(self.PICK_PREFIX + row["dedupe_key"],
                             json.dumps(row), nx=True)
        if created:
            self.r.sadd(self.PICK_INDEX, row["dedupe_key"])
        return bool(created)

    def list_pending_picks(self) -> list[dict]:
        out: list[dict] = []
        for key in self.r.smembers(self.PICK_INDEX):
            raw = self.r.get(self.PICK_PREFIX + key)
            if not raw:
                continue
            row = json.loads(raw)
            if row["state"] in PENDING_STATES:
                out.append(row)
        return out

    def list_settled_picks(self) -> list[dict]:
        out: list[dict] = []
        for key in self.r.smembers(self.PICK_INDEX):
            raw = self.r.get(self.PICK_PREFIX + key)
            if not raw:
                continue
            row = json.loads(raw)
            if row["state"] not in PENDING_STATES:
                out.append(row)
        return out

    def settle_pick(self, dedupe_key: str, result: str,
                    settled_at: datetime, state: str = "SETTLED") -> bool:
        raw = self.r.get(self.PICK_PREFIX + dedupe_key)
        if not raw:
            return False
        row = json.loads(raw)
        if row["state"] not in PENDING_STATES:
            return False
        row["state"] = state
        row["result"] = result
        row["settled_at"] = settled_at.isoformat()
        self.r.set(self.PICK_PREFIX + dedupe_key, json.dumps(row))
        return True


POSTGRES_DDL = """
CREATE TABLE IF NOT EXISTS picks (
    dedupe_key    TEXT PRIMARY KEY,
    match_id      TEXT NOT NULL,
    sport_key     TEXT NOT NULL,
    market        TEXT NOT NULL,
    outcome_name  TEXT NOT NULL,
    line          DOUBLE PRECISION,
    home_team     TEXT,
    away_team     TEXT,
    commence_time TIMESTAMPTZ,
    p_true        DOUBLE PRECISION NOT NULL,
    fair_odds     DOUBLE PRECISION NOT NULL,
    n_books       INTEGER NOT NULL,
    stdev         DOUBLE PRECISION,
    cv            DOUBLE PRECISION,
    state         TEXT NOT NULL,
    result        TEXT,
    best_book     TEXT,
    best_odds     DOUBLE PRECISION,
    best_ev       DOUBLE PRECISION,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_picks_state ON picks(state);
CREATE INDEX IF NOT EXISTS idx_picks_match ON picks(match_id);
"""


class PostgresStorage(Storage):
    """Cold, audited ledger. Not a hot cache — live telemetry must use the
    Redis or in-memory driver. Requires the optional ``psycopg`` package."""

    def __init__(self, dsn: str | None = None, *, conn=None):
        if conn is None:
            try:
                import psycopg
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "LISA_STORAGE=postgres requires the 'psycopg' package") from exc
            conn = psycopg.connect(dsn or "postgresql://localhost:5432/lisa")
        self.conn = conn

    def ensure_schema(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(POSTGRES_DDL)
            cur.execute("ALTER TABLE picks ADD COLUMN IF NOT EXISTS line DOUBLE PRECISION;")
        self.conn.commit()

    # -- hot layer (not supported: Postgres is the cold layer) ---------------

    def upsert_live(self, key: str, data: dict, ttl_seconds: int) -> None:
        raise NotImplementedError(
            "PostgresStorage is the cold ledger; use RedisStorage/in-memory "
            "for the hot live cache.")

    def get_live(self, key: str) -> Optional[dict]:
        raise NotImplementedError(
            "PostgresStorage is the cold ledger; use RedisStorage/in-memory "
            "for the hot live cache.")

    def scan_live_keys(self) -> Iterable[str]:
        raise NotImplementedError(
            "PostgresStorage is the cold ledger; use RedisStorage/in-memory "
            "for the hot live cache.")

    # -- cold layer ----------------------------------------------------------

    def insert_pick(self, pick: Pick) -> bool:
        r = pick_to_row(pick)
        sql = (
            "INSERT INTO picks (dedupe_key, match_id, sport_key, market, "
            "outcome_name, line, home_team, away_team, commence_time, p_true, "
            "fair_odds, n_books, stdev, cv, state, result, best_book, "
            "best_odds, best_ev, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (dedupe_key) DO NOTHING"
        )
        params = (r["dedupe_key"], r["match_id"], r["sport_key"], r["market"],
                  r["outcome_name"], r["line"], r["home_team"], r["away_team"],
                  r["commence_time"], r["p_true"], r["fair_odds"], r["n_books"],
                  r["stdev"], r["cv"], r["state"], r["result"], r["best_book"],
                  r["best_odds"], r["best_ev"], r["created_at"])
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            inserted = cur.rowcount > 0
        self.conn.commit()
        return inserted

    def list_pending_picks(self) -> list[dict]:
        sql = "SELECT * FROM picks WHERE state = ANY(%s)"
        with self.conn.cursor() as cur:
            cur.execute(sql, (list(PENDING_STATES),))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return rows

    def list_settled_picks(self) -> list[dict]:
        sql = "SELECT * FROM picks WHERE state NOT IN ('PENDING')"
        with self.conn.cursor() as cur:
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return rows

    def settle_pick(self, dedupe_key: str, result: str,
                    settled_at: datetime, state: str = "SETTLED") -> bool:
        sql = (
            "UPDATE picks SET state = %s, result = %s, settled_at = %s "
            "WHERE dedupe_key = %s AND state = ANY(%s)"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql, (state, result, settled_at, dedupe_key,
                              list(PENDING_STATES)))
            updated = cur.rowcount > 0
        self.conn.commit()
        return updated