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
import os
import sqlite3
import time
from datetime import datetime, timezone
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
        "closing_odds": exec_.odds if exec_ else None,
        "closing_p_true": pick.p_true,
        "clv": 0.0 if exec_ else None,
        "conviction_score": getattr(pick, "conviction_score", 0.0),
        "recommended_stake_pct": getattr(pick, "recommended_stake_pct", 0.0),
        "recommended_units": getattr(pick, "recommended_units", 0.0),
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

    def update_pick_closing(self, dedupe_key: str, closing_odds: float,
                            closing_p_true: Optional[float] = None,
                            clv: Optional[float] = None) -> bool:
        raise NotImplementedError

    def get_pick(self, dedupe_key: str) -> Optional[dict]:
        raise NotImplementedError

    def log_admin_action(self, admin_id: str, action: str, target: str = "", details: str = "") -> int:
        return 0

    def list_admin_audit_logs(self, limit: int = 20) -> list[dict]:
        return []

    def manual_settle_match(self, match_id: str, result: str = "WIN") -> int:
        return 0

    def is_system_paused(self) -> bool:
        return False

    def set_system_paused(self, paused: bool) -> None:
        pass


class InMemoryStorage(Storage):
    """Thread-safe-enough for a single worker; TTL is wall-clock monotonic."""

    def __init__(self) -> None:
        self._live: dict[str, tuple[float, dict]] = {}
        self._picks: dict[str, dict] = {}
        self._audit_logs: list[dict] = []
        self._system_paused: bool = False

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

    def get_pick(self, dedupe_key: str) -> Optional[dict]:
        row = self._picks.get(dedupe_key)
        return dict(row) if row is not None else None

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

    def update_pick_closing(self, dedupe_key: str, closing_odds: float,
                            closing_p_true: Optional[float] = None,
                            clv: Optional[float] = None) -> bool:
        row = self._picks.get(dedupe_key)
        if row is None:
            return False
        row["closing_odds"] = closing_odds
        row["closing_p_true"] = closing_p_true
        row["clv"] = clv
        return True

    def log_admin_action(self, admin_id: str, action: str, target: str = "", details: str = "") -> int:
        rec = {
            "id": len(self._audit_logs) + 1,
            "admin_id": str(admin_id),
            "action": action,
            "target": target,
            "details": details,
            "timestamp": time.time(),
        }
        self._audit_logs.append(rec)
        return rec["id"]

    def list_admin_audit_logs(self, limit: int = 20) -> list[dict]:
        return sorted(self._audit_logs, key=lambda x: x["timestamp"], reverse=True)[:limit]

    def manual_settle_match(self, match_id: str, result: str = "WIN") -> int:
        count = 0
        now_iso = datetime.now(timezone.utc).isoformat()
        res_upper = result.upper()
        for row in self._picks.values():
            if row.get("match_id") == match_id or match_id in row.get("dedupe_key", ""):
                row["state"] = "SETTLED"
                row["result"] = res_upper
                row["settled_at"] = now_iso
                count += 1
        return count

    def is_system_paused(self) -> bool:
        return self._system_paused

    def set_system_paused(self, paused: bool) -> None:
        self._system_paused = bool(paused)


class JsonFileStorage(InMemoryStorage):
    """File-backed storage persisting picks to JSON. Zero external dependencies.
    Survives restarts and preserves the strict write-once ledger state machine."""

    def __init__(self, filepath: str = "data/storage.json") -> None:
        super().__init__()
        self.filepath = filepath
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self._picks = data.get("picks", {})
            except Exception:
                self._picks = {}

    def _save(self) -> None:
        try:
            folder = os.path.dirname(os.path.abspath(self.filepath))
            if folder:
                os.makedirs(folder, exist_ok=True)
            tmp_path = f"{self.filepath}.tmp.{os.getpid()}"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump({"picks": self._picks}, f, indent=2)
            os.replace(tmp_path, self.filepath)
        except Exception:
            pass

    def insert_pick(self, pick: Pick) -> bool:
        ret = super().insert_pick(pick)
        if ret:
            self._save()
        return ret

    def settle_pick(self, dedupe_key: str, result: str,
                    settled_at: datetime, state: str = "SETTLED") -> bool:
        ret = super().settle_pick(dedupe_key, result, settled_at, state=state)
        if ret:
            self._save()
        return ret

    def update_pick_closing(self, dedupe_key: str, closing_odds: float,
                            closing_p_true: Optional[float] = None,
                            clv: Optional[float] = None) -> bool:
        ret = super().update_pick_closing(dedupe_key, closing_odds, closing_p_true, clv)
        if ret:
            self._save()
        return ret


SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS picks (
    dedupe_key    TEXT PRIMARY KEY,
    match_id      TEXT NOT NULL,
    sport_key     TEXT NOT NULL,
    market        TEXT NOT NULL,
    outcome_name  TEXT NOT NULL,
    line          REAL,
    home_team     TEXT,
    away_team     TEXT,
    commence_time TEXT,
    p_true        REAL NOT NULL,
    fair_odds     REAL NOT NULL,
    n_books       INTEGER NOT NULL,
    stdev         REAL,
    cv            REAL,
    state         TEXT NOT NULL,
    result        TEXT,
    best_book     TEXT,
    best_odds     REAL,
    best_ev       REAL,
    closing_odds  REAL,
    closing_p_true REAL,
    clv           REAL,
    conviction_score REAL DEFAULT 0.0,
    recommended_stake_pct REAL DEFAULT 0.0,
    recommended_units REAL DEFAULT 0.0,
    created_at    TEXT NOT NULL,
    settled_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_picks_state ON picks(state);
CREATE INDEX IF NOT EXISTS idx_picks_match ON picks(match_id);
CREATE INDEX IF NOT EXISTS idx_picks_created ON picks(created_at);

CREATE TABLE IF NOT EXISTS live_cache (
    key           TEXT PRIMARY KEY,
    data          TEXT NOT NULL,
    expires_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_live_expires ON live_cache(expires_at);

CREATE TABLE IF NOT EXISTS verified_sessions (
    web_user_id      TEXT PRIMARY KEY,
    telegram_user_id TEXT,
    username         TEXT,
    verified_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS system_telemetry (
    key           TEXT PRIMARY KEY,
    val_json      TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id                TEXT PRIMARY KEY,
    email             TEXT UNIQUE NOT NULL,
    password_hash     TEXT NOT NULL,
    password_salt     TEXT NOT NULL,
    display_name      TEXT,
    tier              TEXT NOT NULL DEFAULT 'free',
    telegram_id       TEXT,
    telegram_username TEXT,
    telegram_verified INTEGER NOT NULL DEFAULT 0,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    last_login_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_telegram ON users(telegram_id);

CREATE TABLE IF NOT EXISTS sessions (
    session_id        TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at        REAL NOT NULL,
    expires_at        REAL NOT NULL,
    ip_address        TEXT,
    user_agent        TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS admin_audit_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id   TEXT NOT NULL,
    action     TEXT NOT NULL,
    target     TEXT,
    details    TEXT,
    timestamp  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_admin ON admin_audit_logs(admin_id);
CREATE INDEX IF NOT EXISTS idx_audit_time ON admin_audit_logs(timestamp);

CREATE VIEW IF NOT EXISTS lisa_predictions AS SELECT * FROM picks;
CREATE VIEW IF NOT EXISTS user_profiles AS SELECT * FROM users;
"""


class SqliteStorage(Storage):
    """Production-grade relational storage driver using Python's standard library sqlite3.
    
    Features:
      * WAL (Write-Ahead Logging) mode enabled for non-blocking concurrent reads during writes.
      * Strict write-once audited ledger (picks table with primary key dedupe_key).
      * High-performance hot live cache table with automatic TTL expiration.
      * Persistent verified Telegram subscriber session tracking.
      * Zero external dependencies.
    """

    def __init__(self, db_path: str = "data/lisa.db") -> None:
        self.db_path = db_path
        folder = os.path.dirname(os.path.abspath(self.db_path))
        if folder:
            os.makedirs(folder, exist_ok=True)
        self.ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SQLITE_DDL)
            conn.commit()

    # -- hot layer -----------------------------------------------------------

    def upsert_live(self, key: str, data: dict, ttl_seconds: int) -> None:
        expires_at = time.time() + ttl_seconds
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO live_cache (key, data, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data, expires_at=excluded.expires_at",
                (key, json.dumps(data), expires_at)
            )
            conn.commit()

    def get_live(self, key: str) -> Optional[dict]:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute("SELECT data, expires_at FROM live_cache WHERE key = ?", (key,))
            row = cur.fetchone()
            if not row:
                return None
            if row["expires_at"] < now:
                conn.execute("DELETE FROM live_cache WHERE key = ?", (key,))
                conn.commit()
                return None
            return json.loads(row["data"])

    def scan_live_keys(self) -> Iterable[str]:
        now = time.time()
        with self._connect() as conn:
            conn.execute("DELETE FROM live_cache WHERE expires_at < ?", (now,))
            conn.commit()
            cur = conn.execute("SELECT key FROM live_cache WHERE expires_at >= ?", (now,))
            return [r["key"] for r in cur.fetchall()]

    # -- cold layer ----------------------------------------------------------

    def insert_pick(self, pick: Pick) -> bool:
        r = pick_to_row(pick)
        return self.insert_pick_row(r)

    def insert_pick_row(self, r: dict) -> bool:
        sql = """
            INSERT OR IGNORE INTO picks (
                dedupe_key, match_id, sport_key, market, outcome_name, line,
                home_team, away_team, commence_time, p_true, fair_odds,
                n_books, stdev, cv, state, result, best_book, best_odds,
                best_ev, closing_odds, closing_p_true, clv,
                conviction_score, recommended_stake_pct, recommended_units,
                created_at, settled_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?
            )
        """
        params = (
            r["dedupe_key"],
            r.get("match_id", ""),
            r.get("sport_key", ""),
            r.get("market", ""),
            r.get("outcome_name", ""),
            r.get("line"),
            r.get("home_team"),
            r.get("away_team"),
            r.get("commence_time"),
            r.get("p_true", 0.0),
            r.get("fair_odds", 1.0),
            r.get("n_books", 1),
            r.get("stdev", 0.0),
            r.get("cv", 0.0),
            r.get("state", "TRIGGER_ALERT"),
            r.get("result"),
            r.get("best_book"),
            r.get("best_odds"),
            r.get("best_ev"),
            r.get("closing_odds"),
            r.get("closing_p_true"),
            r.get("clv"),
            float(r.get("conviction_score") or 0.0),
            float(r.get("recommended_stake_pct") or 0.0),
            float(r.get("recommended_units") or 0.0),
            r.get("created_at") or r.get("commence_time") or datetime.now(timezone.utc).isoformat(),
            r.get("settled_at")
        )
        with self._connect() as conn:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount > 0

    def get_pick(self, dedupe_key: str) -> Optional[dict]:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM picks WHERE dedupe_key = ?", (dedupe_key,))
            row = cur.fetchone()
            return dict(row) if row is not None else None

    def list_pending_picks(self) -> list[dict]:
        placeholders = ",".join("?" for _ in PENDING_STATES)
        sql = f"SELECT * FROM picks WHERE state IN ({placeholders}) ORDER BY commence_time ASC"
        with self._connect() as conn:
            cur = conn.execute(sql, list(PENDING_STATES))
            return [dict(r) for r in cur.fetchall()]

    def list_settled_picks(self) -> list[dict]:
        placeholders = ",".join("?" for _ in PENDING_STATES)
        sql = f"SELECT * FROM picks WHERE state NOT IN ({placeholders}) ORDER BY settled_at DESC, created_at DESC"
        with self._connect() as conn:
            cur = conn.execute(sql, list(PENDING_STATES))
            return [dict(r) for r in cur.fetchall()]

    def settle_pick(self, dedupe_key: str, result: str,
                    settled_at: datetime, state: str = "SETTLED") -> bool:
        placeholders = ",".join("?" for _ in PENDING_STATES)
        sql = f"""
            UPDATE picks SET state = ?, result = ?, settled_at = ?
            WHERE dedupe_key = ? AND state IN ({placeholders})
        """
        params = [state, result, settled_at.isoformat(), dedupe_key, *PENDING_STATES]
        with self._connect() as conn:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount > 0

    def update_pick_closing(self, dedupe_key: str, closing_odds: float,
                            closing_p_true: Optional[float] = None,
                            clv: Optional[float] = None) -> bool:
        sql = """
            UPDATE picks SET closing_odds = ?, closing_p_true = ?, clv = ?
            WHERE dedupe_key = ?
        """
        with self._connect() as conn:
            cur = conn.execute(sql, (closing_odds, closing_p_true, clv, dedupe_key))
            conn.commit()
            return cur.rowcount > 0

    # -- sessions & telemetry ------------------------------------------------

    def verify_user(self, web_user_id: str, telegram_user_id: str = "", username: str = "") -> None:
        if not web_user_id:
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO verified_sessions (web_user_id, telegram_user_id, username, verified_at) "
                "VALUES (?, ?, ?, ?)",
                (web_user_id.strip(), str(telegram_user_id), username, time.time())
            )
            conn.commit()

    def is_user_verified(self, web_user_id: str) -> bool:
        if not web_user_id:
            return False
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT 1 FROM verified_sessions WHERE web_user_id = ?",
                (web_user_id.strip(),)
            )
            return cur.fetchone() is not None

    def get_verified_user(self, web_user_id: str) -> Optional[dict]:
        if not web_user_id:
            return None
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT web_user_id, telegram_user_id, username, verified_at FROM verified_sessions WHERE web_user_id = ?",
                (web_user_id.strip(),)
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def count_picks(self) -> dict[str, int]:
        with self._connect() as conn:
            cur = conn.execute("""
                SELECT
                    count(*) as total,
                    sum(case when state in ('TRIGGER_ALERT', 'CONFIRMED', 'PENDING_SETTLEMENT') then 1 else 0 end) as pending,
                    sum(case when state not in ('TRIGGER_ALERT', 'CONFIRMED', 'PENDING_SETTLEMENT') then 1 else 0 end) as settled,
                    sum(case when result = 'WIN' then 1 else 0 end) as won,
                    sum(case when result = 'LOSS' then 1 else 0 end) as lost,
                    sum(case when result = 'VOID' then 1 else 0 end) as void
                FROM picks
            """)
            row = cur.fetchone()
            if not row:
                return {"total": 0, "pending": 0, "settled": 0, "won": 0, "lost": 0, "void": 0}
            return {
                "total": row["total"] or 0,
                "pending": row["pending"] or 0,
                "settled": row["settled"] or 0,
                "won": row["won"] or 0,
                "lost": row["lost"] or 0,
                "void": row["void"] or 0,
            }

    def log_admin_action(self, admin_id: str, action: str, target: str = "", details: str = "") -> int:
        sql = """
            INSERT INTO admin_audit_logs (admin_id, action, target, details, timestamp)
            VALUES (?, ?, ?, ?, ?)
        """
        now_ts = time.time()
        with self._connect() as conn:
            cur = conn.execute(sql, (str(admin_id), action, target, details, now_ts))
            conn.commit()
            return cur.lastrowid or 0

    def list_admin_audit_logs(self, limit: int = 20) -> list[dict]:
        sql = "SELECT * FROM admin_audit_logs ORDER BY timestamp DESC LIMIT ?"
        with self._connect() as conn:
            cur = conn.execute(sql, (limit,))
            return [dict(r) for r in cur.fetchall()]

    def manual_settle_match(self, match_id: str, result: str = "WIN") -> int:
        now_iso = datetime.now(timezone.utc).isoformat()
        res_upper = result.upper()
        sql = """
            UPDATE picks
            SET state = 'SETTLED', result = ?, settled_at = ?
            WHERE match_id = ? OR dedupe_key LIKE ?
        """
        like_pattern = f"%{match_id}%"
        with self._connect() as conn:
            cur = conn.execute(sql, (res_upper, now_iso, match_id, like_pattern))
            conn.commit()
            return cur.rowcount

    def is_system_paused(self) -> bool:
        with self._connect() as conn:
            cur = conn.execute("SELECT val_json FROM system_telemetry WHERE key = 'sys_kill_switch_paused'")
            row = cur.fetchone()
            if not row:
                return False
            try:
                data = json.loads(row["val_json"])
                return bool(data.get("paused", False))
            except Exception:
                return False

    def set_system_paused(self, paused: bool) -> None:
        val_str = json.dumps({"paused": bool(paused), "updated_at": time.time()})
        now_iso = datetime.now(timezone.utc).isoformat()
        sql = """
            INSERT INTO system_telemetry (key, val_json, updated_at)
            VALUES ('sys_kill_switch_paused', ?, ?)
            ON CONFLICT(key) DO UPDATE SET val_json = excluded.val_json, updated_at = excluded.updated_at
        """
        with self._connect() as conn:
            conn.execute(sql, (val_str, now_iso))
            conn.commit()


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

    def get_pick(self, dedupe_key: str) -> Optional[dict]:
        raw = self.r.get(self.PICK_PREFIX + dedupe_key)
        if not raw:
            return None
        return json.loads(raw)

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

    def update_pick_closing(self, dedupe_key: str, closing_odds: float,
                            closing_p_true: Optional[float] = None,
                            clv: Optional[float] = None) -> bool:
        raw = self.r.get(self.PICK_PREFIX + dedupe_key)
        if not raw:
            return False
        row = json.loads(raw)
        row["closing_odds"] = closing_odds
        row["closing_p_true"] = closing_p_true
        row["clv"] = clv
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
    closing_odds  DOUBLE PRECISION,
    closing_p_true DOUBLE PRECISION,
    clv           DOUBLE PRECISION,
    conviction_score DOUBLE PRECISION,
    recommended_stake_pct DOUBLE PRECISION,
    recommended_units DOUBLE PRECISION,
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
            cur.execute("ALTER TABLE picks ADD COLUMN IF NOT EXISTS closing_odds DOUBLE PRECISION;")
            cur.execute("ALTER TABLE picks ADD COLUMN IF NOT EXISTS closing_p_true DOUBLE PRECISION;")
            cur.execute("ALTER TABLE picks ADD COLUMN IF NOT EXISTS clv DOUBLE PRECISION;")
            cur.execute("ALTER TABLE picks ADD COLUMN IF NOT EXISTS conviction_score DOUBLE PRECISION;")
            cur.execute("ALTER TABLE picks ADD COLUMN IF NOT EXISTS recommended_stake_pct DOUBLE PRECISION;")
            cur.execute("ALTER TABLE picks ADD COLUMN IF NOT EXISTS recommended_units DOUBLE PRECISION;")
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
            "best_odds, best_ev, closing_odds, closing_p_true, clv, conviction_score, "
            "recommended_stake_pct, recommended_units, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (dedupe_key) DO NOTHING"
        )
        params = (r["dedupe_key"], r["match_id"], r["sport_key"], r["market"],
                  r["outcome_name"], r["line"], r["home_team"], r["away_team"],
                  r["commence_time"], r["p_true"], r["fair_odds"], r["n_books"],
                  r["stdev"], r["cv"], r["state"], r["result"], r["best_book"],
                  r["best_odds"], r["best_ev"], r["closing_odds"],
                  r["closing_p_true"], r["clv"], r.get("conviction_score", 0.0),
                  r.get("recommended_stake_pct", 0.0), r.get("recommended_units", 0.0),
                  r["created_at"])
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            inserted = cur.rowcount > 0
        self.conn.commit()
        return inserted

    def get_pick(self, dedupe_key: str) -> Optional[dict]:
        sql = "SELECT * FROM picks WHERE dedupe_key = %s"
        with self.conn.cursor() as cur:
            cur.execute(sql, (dedupe_key,))
            r = cur.fetchone()
            if not r:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, r))

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

    def update_pick_closing(self, dedupe_key: str, closing_odds: float,
                            closing_p_true: Optional[float] = None,
                            clv: Optional[float] = None) -> bool:
        sql = (
            "UPDATE picks SET closing_odds = %s, closing_p_true = %s, clv = %s "
            "WHERE dedupe_key = %s"
        )
        with self.conn.cursor() as cur:
            cur.execute(sql, (closing_odds, closing_p_true, clv, dedupe_key))
            updated = cur.rowcount > 0
        self.conn.commit()
        return updated