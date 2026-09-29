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
import re
import sqlite3
from contextlib import contextmanager
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .gate import Pick

PENDING_STATES = ("TRIGGER_ALERT", "CONFIRMED", "PENDING_SETTLEMENT")

#: A row counts as settled only once it carries a grade. Deriving "settled" as
#: "not pending" would let any unrecognised state inflate the win rate.
SETTLED_STATES = ("SETTLED", "VOID", "SETTLED_VOID")


class AdminReadModelUnavailable(RuntimeError):
    """A storage driver does not implement the console's analytics.

    Raised instead of ``NotImplementedError`` so the console can tell "this
    backend genuinely cannot answer that" (a 503 with a clear message) apart
    from "a method is missing because the code is broken".
    """

    def __init__(self, driver: str = "this"):
        super().__init__(f"the admin read model is not available on the {driver} driver")
        self.driver = driver


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
        "actual_score": None,
        "score_source": None,
        "best_book": exec_.book_key if exec_ else None,
        "best_odds": exec_.odds if exec_ else None,
        "best_ev": exec_.ev if exec_ else None,
        # closing_* and clv start as NULL, not a copy of the emit price. A pick
        # that is never re-priced before kickoff has *no* observed close; seeding
        # these from `best_odds` made an unrepriced pick score exactly 0.0 CLV,
        # which reads as "beat the close perfectly" in every mean that folds it in.
        "closing_odds": None,
        "closing_p_true": None,
        "clv": None,
        "conviction_score": getattr(pick, "conviction_score", 0.0),
        "recommended_stake_pct": getattr(pick, "recommended_stake_pct", 0.0),
        "recommended_units": getattr(pick, "recommended_units", 0.0),
        "created_at": pick.created_at.isoformat(),
        "settled_at": None,
    }


class Storage:
    """Interface — the pipeline depends on this, not on concrete drivers."""

    # hot layer
    def upsert_live(self, key: str, data: Any, ttl_seconds: int) -> None:
        raise NotImplementedError

    def get_live(self, key: str) -> Optional[Any]:
        raise NotImplementedError

    def get_live_stale(self, key: str) -> Optional[Any]:
        """Return a cached payload even after its TTL, so the UI can show the
        last real observation (with its age) instead of nothing. Returns None
        only when the key was never written."""
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
                    settled_at: datetime, state: str = "SETTLED",
                    actual_score: Optional[str] = None,
                    score_source: Optional[str] = None) -> bool:
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

    def manual_settle_match(self, match_id: str, result: str = "WIN",
                            actual_score: Optional[str] = None) -> int:
        return 0

    def is_system_paused(self) -> bool:
        return False

    def set_system_paused(self, paused: bool) -> None:
        pass

    # -- notification outbox -------------------------------------------------

    def enqueue_notification(self, dedupe_key: str, text: str) -> bool:
        raise NotImplementedError

    def list_pending_notifications(self, limit: int = 50) -> list[dict]:
        raise NotImplementedError

    def mark_notification_sent(self, dedupe_key: str) -> None:
        raise NotImplementedError

    def mark_notification_failed(self, dedupe_key: str, error: str) -> None:
        raise NotImplementedError



    # -- admin read model ----------------------------------------------------
    #
    # The console's analytics are implemented once, on the SQL drivers, because
    # they are aggregations over the whole ledger and re-implementing them over
    # an in-memory dict would be a second copy of the same logic to keep in
    # step. Drivers that do not support them inherit these, so the console gets
    # a clear "unavailable" instead of an AttributeError on a missing `_tx`.

    def admin_list_picks(self, *, state: str = "", market: str = "",
                         sport: str = "", search: str = "", page: int = 1,
                         limit: int = 50) -> dict[str, Any]:
        raise AdminReadModelUnavailable(self.__class__.__name__)

    def admin_pick_stats(self) -> dict[str, Any]:
        raise AdminReadModelUnavailable(self.__class__.__name__)

    def admin_pick_ledger_value(self) -> dict[str, Any]:
        raise AdminReadModelUnavailable(self.__class__.__name__)

    def admin_list_notifications(self, *, status: str = "", page: int = 1,
                                 limit: int = 50) -> dict[str, Any]:
        raise AdminReadModelUnavailable(self.__class__.__name__)

    def admin_list_users(self, *, term: str = "", page: int = 1,
                         limit: int = 50) -> dict[str, Any]:
        raise AdminReadModelUnavailable(self.__class__.__name__)

    def admin_list_audit(self, *, admin_id: str = "", page: int = 1,
                         limit: int = 50) -> dict[str, Any]:
        raise AdminReadModelUnavailable(self.__class__.__name__)

    def admin_get_telemetry(self) -> list[dict[str, Any]]:
        raise AdminReadModelUnavailable(self.__class__.__name__)

    def admin_set_telemetry(self, key: str, value: Any) -> None:
        raise AdminReadModelUnavailable(self.__class__.__name__)

    def requeue_notifications(self, limit: int = 100) -> int:
        raise AdminReadModelUnavailable(self.__class__.__name__)

    def admin_live_cache_overview(self) -> dict[str, Any]:
        raise AdminReadModelUnavailable(self.__class__.__name__)

    def admin_database_info(self) -> dict[str, Any]:
        raise AdminReadModelUnavailable(self.__class__.__name__)


class InMemoryStorage(Storage):
    """Thread-safe-enough for a single worker; TTL is wall-clock monotonic."""

    def __init__(self) -> None:
        self._live: dict[str, tuple[float, dict]] = {}
        self._picks: dict[str, dict] = {}
        self._audit_logs: list[dict] = []
        self._system_paused: bool = False
        self._outbox: dict[str, dict] = {}

    # -- hot layer -----------------------------------------------------------

    def upsert_live(self, key: str, data: Any, ttl_seconds: int) -> None:
        # The hot layer is a general JSON cache: dicts, lists and scalars all
        # round-trip, matching the SQLite driver.
        self._live[key] = (time.monotonic() + ttl_seconds, data)

    def get_live(self, key: str) -> Optional[Any]:
        entry = self._live.get(key)
        if entry is None:
            return None
        expires, data = entry
        if expires < time.monotonic():
            del self._live[key]
            return None
        return data

    def get_live_stale(self, key: str) -> Optional[Any]:
        return self._live.get(key, (0.0, None))[1]

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
        return [
            r for r in self._picks.values()
            if r["state"] in SETTLED_STATES or r.get("result")
        ]

    def settle_pick(self, dedupe_key: str, result: str,
                    settled_at: datetime, state: str = "SETTLED",
                    actual_score: Optional[str] = None,
                    score_source: Optional[str] = None) -> bool:
        row = self._picks.get(dedupe_key)
        if row is None or row["state"] not in PENDING_STATES:
            return False
        row["state"] = state
        row["result"] = result
        row["settled_at"] = settled_at.isoformat()
        if actual_score:
            row["actual_score"] = actual_score
        if score_source:
            row["score_source"] = score_source
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

    def manual_settle_match(self, match_id: str, result: str = "WIN",
                            actual_score: Optional[str] = None) -> int:
        count = 0
        now_iso = datetime.now(timezone.utc).isoformat()
        res_upper = result.upper()
        for row in self._picks.values():
            if row.get("state") in PENDING_STATES and (
                    row.get("match_id") == match_id
                    or match_id in row.get("dedupe_key", "")):
                row["state"] = "SETTLED"
                row["result"] = res_upper
                row["settled_at"] = now_iso
                if actual_score:
                    row["actual_score"] = actual_score
                count += 1
        return count

    def is_system_paused(self) -> bool:
        return self._system_paused

    def set_system_paused(self, paused: bool) -> None:
        self._system_paused = bool(paused)

    # -- notification outbox -------------------------------------------------

    def enqueue_notification(self, dedupe_key: str, text: str) -> bool:
        if dedupe_key in self._outbox:
            return False
        self._outbox[dedupe_key] = {
            "dedupe_key": dedupe_key,
            "text": text,
            "status": "PENDING",
            "attempts": 0,
            "last_error": None,
            "created_at": time.time(),
            "sent_at": None,
        }
        return True

    def list_pending_notifications(self, limit: int = 50) -> list[dict]:
        pending = [dict(r) for r in self._outbox.values() if r["status"] == "PENDING"]
        pending.sort(key=lambda r: r["created_at"])
        return pending[:limit]

    def mark_notification_sent(self, dedupe_key: str) -> None:
        row = self._outbox.get(dedupe_key)
        if row is not None:
            row["status"] = "SENT"
            row["sent_at"] = time.time()
            row["last_error"] = None

    def mark_notification_failed(self, dedupe_key: str, error: str) -> None:
        row = self._outbox.get(dedupe_key)
        if row is not None:
            row["attempts"] = int(row.get("attempts", 0)) + 1
            row["last_error"] = str(error)[:500]

    def requeue_notifications(self, limit: int = 100) -> int:
        """Clear the failure state on rows that failed, so the next flush retries."""
        cap = max(1, int(limit))
        failed = [r for r in self._outbox.values()
                  if r["status"] == "PENDING" and int(r.get("attempts", 0)) > 0]
        failed.sort(key=lambda r: r["created_at"])
        for row in failed[:cap]:
            row["attempts"] = 0
            row["last_error"] = None
        return min(len(failed), cap)



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
                    settled_at: datetime, state: str = "SETTLED",
                    actual_score: Optional[str] = None,
                    score_source: Optional[str] = None) -> bool:
        ret = super().settle_pick(dedupe_key, result, settled_at, state=state,
                                  actual_score=actual_score,
                                  score_source=score_source)
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
    actual_score  TEXT,
    score_source  TEXT,
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

-- Durable notification outbox: a pick is written to the ledger before its alert
-- is delivered, so an at-most-once dispatch silently drops alerts on failure.
-- Rows stay PENDING until a notifier confirms delivery, and are retried.
CREATE TABLE IF NOT EXISTS notification_outbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key    TEXT NOT NULL UNIQUE,
    text          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'PENDING',
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_at    REAL NOT NULL,
    sent_at       REAL
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON notification_outbox(status, id);

CREATE VIEW IF NOT EXISTS lisa_predictions AS SELECT * FROM picks;
CREATE VIEW IF NOT EXISTS user_profiles AS SELECT * FROM users;
"""


# ---------------------------------------------------------------------------
# Admin console read model
# ---------------------------------------------------------------------------
# The operational queries behind the admin panel. Three rules hold for all of
# them: every caller-supplied value is a bound parameter, every list is
# LIMIT/OFFSET bounded, and every one can return a total for pagination without
# re-running the page query.
#
# They live here rather than in the HTTP layer so the SQL stays with the
# schema it queries and stays testable without a server.

#: Hard ceiling on any admin page size, whatever the caller asks for.
ADMIN_MAX_PAGE_SIZE = 200
#: Upper bound on a search term. A LIKE with thousands of wildcards is a cheap
#: way to make the engine scan the whole table.
ADMIN_MAX_QUERY_LEN = 120


def _admin_page(limit: Any, page: Any) -> tuple[int, int]:
    """Coerce caller paging into a safe, bounded window.

    Anything unparseable, negative or absurd collapses to a sane default rather
    than raising, because this parses request parameters.
    """
    try:
        page_size = int(limit)
    except (TypeError, ValueError):
        page_size = 50
    if page_size < 1:
        page_size = 50
    page_size = min(page_size, ADMIN_MAX_PAGE_SIZE)
    try:
        page_no = int(page)
    except (TypeError, ValueError):
        page_no = 1
    if page_no < 1:
        page_no = 1
    return page_size, page_no


#: Table and column identifiers the application creates. Used to gate the one
#: place an identifier has to be interpolated into SQL, because identifiers
#: cannot be passed as bound parameters.
_SAFE_TABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _admin_term(term: Any) -> Optional[str]:
    """Escape LIKE wildcards in a search term and cap its length.

    Without this a search for ``%`` matches every row, and the panel would then
    stream the entire ledger to whoever typed it.
    """
    if term is None:
        return None
    text = str(term).strip()[:ADMIN_MAX_QUERY_LEN]
    if not text:
        return None
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
        # Off by default in SQLite, so sessions.user_id -> users(id) with
        # ON DELETE CASCADE never actually fired and deleting a user orphaned
        # its sessions. Enforcing it also rejects rows that violate the
        # declared relationships, which is the point of declaring them.
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    @contextmanager
    def _tx(self):
        """Transaction scope that also releases the connection.

        ``sqlite3.Connection.__exit__`` commits or rolls back but does not
        close, so the original ``with self._tx() as conn:`` pattern left
        every handle open until the garbage collector got round to it.
        """
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        with self._tx() as conn:
            conn.executescript(SQLITE_DDL)
            conn.commit()

    # -- hot layer -----------------------------------------------------------

    def upsert_live(self, key: str, data: Any, ttl_seconds: int) -> None:
        expires_at = time.time() + ttl_seconds
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO live_cache (key, data, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data, expires_at=excluded.expires_at",
                (key, json.dumps(data), expires_at)
            )
            conn.commit()

    def get_live(self, key: str) -> Optional[dict]:
        now = time.time()
        with self._tx() as conn:
            cur = conn.execute("SELECT data, expires_at FROM live_cache WHERE key = ?", (key,))
            row = cur.fetchone()
            if not row:
                return None
            if row["expires_at"] < now:
                conn.execute("DELETE FROM live_cache WHERE key = ?", (key,))
                conn.commit()
                return None
            return json.loads(row["data"])

    def get_live_stale(self, key: str) -> Optional[dict]:
        with self._tx() as conn:
            cur = conn.execute("SELECT data FROM live_cache WHERE key = ?", (key,))
            row = cur.fetchone()
            return json.loads(row["data"]) if row else None

    def scan_live_keys(self) -> Iterable[str]:
        now = time.time()
        with self._tx() as conn:
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
                n_books, stdev, cv, state, result, actual_score, score_source,
                best_book, best_odds,
                best_ev, closing_odds, closing_p_true, clv,
                conviction_score, recommended_stake_pct, recommended_units,
                created_at, settled_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
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
            r.get("actual_score"),
            r.get("score_source"),
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
        with self._tx() as conn:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount > 0

    def get_pick(self, dedupe_key: str) -> Optional[dict]:
        with self._tx() as conn:
            cur = conn.execute("SELECT * FROM picks WHERE dedupe_key = ?", (dedupe_key,))
            row = cur.fetchone()
            return dict(row) if row is not None else None

    def list_pending_picks(self) -> list[dict]:
        placeholders = ",".join("?" for _ in PENDING_STATES)
        sql = f"SELECT * FROM picks WHERE state IN ({placeholders}) ORDER BY commence_time ASC"
        with self._tx() as conn:
            cur = conn.execute(sql, list(PENDING_STATES))
            return [dict(r) for r in cur.fetchall()]

    def list_settled_picks(self) -> list[dict]:
        placeholders = ",".join("?" for _ in SETTLED_STATES)
        sql = (
            f"SELECT * FROM picks WHERE state IN ({placeholders}) "
            "OR result IS NOT NULL ORDER BY settled_at DESC, created_at DESC"
        )
        with self._tx() as conn:
            cur = conn.execute(sql, list(SETTLED_STATES))
            return [dict(r) for r in cur.fetchall()]

    def settle_pick(self, dedupe_key: str, result: str,
                    settled_at: datetime, state: str = "SETTLED",
                    actual_score: Optional[str] = None,
                    score_source: Optional[str] = None) -> bool:
        placeholders = ",".join("?" for _ in PENDING_STATES)
        sql = f"""
            UPDATE picks
            SET state = ?, result = ?, settled_at = ?,
                actual_score = COALESCE(?, actual_score),
                score_source  = COALESCE(?, score_source)
            WHERE dedupe_key = ? AND state IN ({placeholders})
        """
        params = [state, result, settled_at.isoformat(), actual_score, score_source,
                  dedupe_key, *PENDING_STATES]
        with self._tx() as conn:
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
        with self._tx() as conn:
            cur = conn.execute(sql, (closing_odds, closing_p_true, clv, dedupe_key))
            conn.commit()
            return cur.rowcount > 0

    # -- sessions & telemetry ------------------------------------------------

    def verify_user(self, web_user_id: str, telegram_user_id: str = "", username: str = "") -> None:
        if not web_user_id:
            return
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO verified_sessions (web_user_id, telegram_user_id, username, verified_at) "
                "VALUES (?, ?, ?, ?)",
                (web_user_id.strip(), str(telegram_user_id), username, time.time())
            )
            conn.commit()

    def is_user_verified(self, web_user_id: str) -> bool:
        if not web_user_id:
            return False
        with self._tx() as conn:
            cur = conn.execute(
                "SELECT 1 FROM verified_sessions WHERE web_user_id = ?",
                (web_user_id.strip(),)
            )
            return cur.fetchone() is not None

    def get_verified_user(self, web_user_id: str) -> Optional[dict]:
        if not web_user_id:
            return None
        with self._tx() as conn:
            cur = conn.execute(
                "SELECT web_user_id, telegram_user_id, username, verified_at FROM verified_sessions WHERE web_user_id = ?",
                (web_user_id.strip(),)
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def count_picks(self) -> dict[str, int]:
        with self._tx() as conn:
            cur = conn.execute("""
                SELECT
                    count(*) as total,
                    sum(case when state in ('TRIGGER_ALERT', 'CONFIRMED', 'PENDING_SETTLEMENT') then 1 else 0 end) as pending,
                    sum(case when state in ('SETTLED', 'VOID', 'SETTLED_VOID')
                              or result is not null then 1 else 0 end) as settled,
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

    # -- admin read model ----------------------------------------------------

    def admin_list_picks(self, *, state: str = "", market: str = "",
                         sport: str = "", outcome: str = "", term: str = "",
                         page: int = 1, limit: int = 50,
                         order: str = "created_at") -> dict[str, Any]:
        """Filtered, paginated pick rows plus the unpaginated total.

        ``state`` accepts a comma-separated list so the panel can show "pending
        or confirmed" in one query. Whitelist and ordering are validated here
        rather than interpolated: they cannot be parameters, so the only safe
        form is a lookup into a known-good set.
        """
        page_size, page_no = _admin_page(limit, page)
        where: list[str] = []
        params: list[Any] = []

        if state:
            allowed = [s for s in SETTLED_STATES + PENDING_STATES
                       if s in {x.strip().upper() for x in str(state).split(",")}]
            if allowed:
                where.append("state IN (%s)" % ",".join("?" for _ in allowed))
                params.extend(allowed)
        if market:
            where.append("market = ?")
            params.append(str(market)[:40])
        if sport:
            where.append("sport_key = ?")
            params.append(str(sport)[:80])
        if outcome:
            where.append("outcome_name = ?")
            params.append(str(outcome)[:120])
        like = _admin_term(term)
        if like:
            where.append(
                "(home_team LIKE ? ESCAPE '\\' OR away_team LIKE ? ESCAPE '\\'"
                " OR outcome_name LIKE ? ESCAPE '\\' OR match_id LIKE ? ESCAPE '\\')")
            params.extend([f"%{like}%"] * 4)

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        # ORDER BY cannot be a bound parameter, so it comes from a fixed map.
        order_sql = {
            "created_at": "created_at DESC",
            "created_at_asc": "created_at ASC",
            "commence_time": "commence_time ASC",
            "settled_at": "settled_at DESC",
            "best_ev": "best_ev DESC",
            "best_ev_asc": "best_ev ASC",
            "n_books": "n_books DESC",
            "p_true": "p_true DESC",
        }.get(str(order), "created_at DESC")

        with self._tx() as conn:
            total = conn.execute(
                f"SELECT count(*) AS n FROM picks{clause}", params).fetchone()["n"]
            rows = conn.execute(
                f"SELECT * FROM picks{clause} ORDER BY {order_sql} "
                f"LIMIT ? OFFSET ?",
                [*params, page_size, (page_no - 1) * page_size],
            ).fetchall()
        return {
            "rows": [dict(r) for r in rows],
            "total": int(total or 0),
            "page": page_no,
            "page_size": page_size,
            "pages": max(1, (int(total or 0) + page_size - 1) // page_size),
        }

    def admin_pick_stats(self) -> dict[str, Any]:
        """Accuracy and precision from settled rows only.

        Deliberately computed from the ledger rather than from the tracker
        jsonl, and deliberately restricted to rows that carry a grade. Including
        pending rows in the denominator is the single easiest way to make a
        model look better than it is, so the hit rate here is graded-only and
        the pending count is reported beside it rather than folded in.
        """
        with self._tx() as conn:
            graded = conn.execute("""
                SELECT count(*) AS n,
                       sum(case when result='WIN' then 1 else 0 end) AS wins,
                       sum(case when result='LOSS' then 1 else 0 end) AS losses,
                       sum(case when result='VOID' then 1 else 0 end) AS voids,
                       avg(p_true) AS avg_p,
                       avg(best_ev) AS avg_ev,
                       avg(best_odds) AS avg_odds,
                       avg(n_books) AS avg_books,
                       avg(clv) AS avg_clv,
                       count(clv) AS clv_n
                FROM picks
                WHERE result IN ('WIN','LOSS','VOID')
            """).fetchone()
            by_market = conn.execute("""
                SELECT market,
                       count(*) AS n,
                       sum(case when result='WIN' then 1 else 0 end) AS wins,
                       avg(p_true) AS avg_p,
                       avg(best_ev) AS avg_ev
                FROM picks WHERE result IN ('WIN','LOSS')
                GROUP BY market ORDER BY n DESC
            """).fetchall()
            by_sport = conn.execute("""
                SELECT sport_key,
                       count(*) AS n,
                       sum(case when result='WIN' then 1 else 0 end) AS wins,
                       avg(p_true) AS avg_p
                FROM picks WHERE result IN ('WIN','LOSS')
                GROUP BY sport_key ORDER BY n DESC LIMIT 50
            """).fetchall()
            # Calibration: bucket by the probability we claimed, then compare it
            # with the realised rate. A model whose 60% bucket wins 75% is
            # overconfident no matter what the headline hit rate says.
            calibration = conn.execute("""
                SELECT
                  min(10, cast(p_true*10 as int)) AS bucket,
                  count(*) AS n,
                  sum(case when result='WIN' then 1 else 0 end) AS wins,
                  avg(p_true) AS avg_p
                FROM picks WHERE result IN ('WIN','LOSS') AND p_true IS NOT NULL
                GROUP BY bucket ORDER BY bucket
            """).fetchall()
            pending = conn.execute(
                "SELECT count(*) AS n FROM picks WHERE result IS NULL"
            ).fetchone()["n"]
            awaiting = conn.execute(
                "SELECT count(*) AS n FROM picks "
                "WHERE state IN ('TRIGGER_ALERT','CONFIRMED','PENDING_SETTLEMENT')"
            ).fetchone()["n"]

        n = int(graded["n"] or 0)
        wins = int(graded["wins"] or 0)
        losses = int(graded["losses"] or 0)
        voided = int(graded["voids"] or 0)
        # VOID bets push no money, so the money denominator is WIN+LOSS.
        decided = wins + losses
        # Flat-stake return: each graded bet risks one unit at the quoted odds.
        profit = 0.0
        if decided:
            with self._tx() as conn:
                stakes = conn.execute("""
                    SELECT result, best_odds FROM picks
                    WHERE result IN ('WIN','LOSS') AND best_odds IS NOT NULL
                """).fetchall()
            for row in stakes:
                odds = float(row["best_odds"] or 0.0)
                if odds <= 1.0:
                    continue
                profit += (odds - 1.0) if row["result"] == "WIN" else -1.0
        roi = (profit / decided) if decided else None

        def _rate(win: int, total: int) -> Optional[float]:
            return round(win / total, 4) if total else None

        return {
            "graded": n,
            "wins": wins,
            "losses": losses,
            "voids": voided,
            "pending": int(pending or 0),
            "awaiting_settlement": int(awaiting or 0),
            "hit_rate": _rate(wins, decided),
            "void_rate": _rate(voided, n),
            "flat_stake_profit_units": round(profit, 4),
            "flat_stake_roi": round(roi, 4) if roi is not None else None,
            "avg_p_true": round(graded["avg_p"], 4) if graded["avg_p"] else None,
            "avg_best_ev": round(graded["avg_ev"], 5) if graded["avg_ev"] else None,
            "avg_odds": round(graded["avg_odds"], 3) if graded["avg_odds"] else None,
            "avg_books": round(graded["avg_books"], 2) if graded["avg_books"] else None,
            # avg(clv) ignores NULLs, so it is already a mean over the picks that
            # actually have an observed close. clv_n is published beside it so a
            # reader can see how much of the graded book that sample covers
            # instead of having to assume it is all of them.
            "avg_clv": round(graded["avg_clv"], 4) if graded["avg_clv"] else None,
            "clv_sample_size": int(graded["clv_n"] or 0),
            "by_market": [{
                "market": r["market"], "n": int(r["n"] or 0),
                "wins": int(r["wins"] or 0),
                "hit_rate": _rate(int(r["wins"] or 0), int(r["n"] or 0)),
                "avg_p": round(r["avg_p"], 4) if r["avg_p"] else None,
                "avg_ev": round(r["avg_ev"], 5) if r["avg_ev"] else None,
            } for r in by_market],
            "by_sport": [{
                "sport_key": r["sport_key"], "n": int(r["n"] or 0),
                "wins": int(r["wins"] or 0),
                "hit_rate": _rate(int(r["wins"] or 0), int(r["n"] or 0)),
                "avg_p": round(r["avg_p"], 4) if r["avg_p"] else None,
            } for r in by_sport],
            "calibration": [{
                "bucket": int(r["bucket"] or 0),
                "p_low": round((r["bucket"] or 0) / 10.0, 2),
                "p_high": round(((r["bucket"] or 0) + 1) / 10.0, 2),
                "n": int(r["n"] or 0),
                "wins": int(r["wins"] or 0),
                "realised": _rate(int(r["wins"] or 0), int(r["n"] or 0)),
                "claimed": round(r["avg_p"], 4) if r["avg_p"] else None,
            } for r in calibration],
        }

    def admin_pick_ledger_value(self) -> dict[str, Any]:
        """Realised P&L if every graded bet was staked flat at the best price."""
        stats = self.admin_pick_stats()
        return {
            "roi": stats["flat_stake_roi"],
            "profit_units": stats["flat_stake_profit_units"],
            "decided": stats["wins"] + stats["losses"],
        }

    def admin_list_notifications(self, *, status: str = "", page: int = 1,
                                 limit: int = 50) -> dict[str, Any]:
        page_size, page_no = _admin_page(limit, page)
        where, params = "", []
        if status and str(status).upper() in ("PENDING", "SENT", "FAILED"):
            where = " WHERE status = ?"
            params.append(str(status).upper())
        with self._tx() as conn:
            total = conn.execute(
                f"SELECT count(*) AS n FROM notification_outbox{where}",
                params).fetchone()["n"]
            rows = conn.execute(
                f"SELECT id, dedupe_key, status, attempts, last_error, "
                f"created_at, sent_at, substr(text, 1, 400) AS text "
                f"FROM notification_outbox{where} ORDER BY id DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page_no - 1) * page_size]).fetchall()
            counts = conn.execute(
                "SELECT status, count(*) AS n FROM notification_outbox "
                "GROUP BY status").fetchall()
        return {
            "rows": [dict(r) for r in rows],
            "total": int(total or 0),
            "page": page_no, "page_size": page_size,
            "pages": max(1, (int(total or 0) + page_size - 1) // page_size),
            "counts": {r["status"]: int(r["n"]) for r in counts},
        }

    def admin_list_users(self, *, term: str = "", page: int = 1,
                         limit: int = 50) -> dict[str, Any]:
        """Accounts for the panel. Never returns password material."""
        page_size, page_no = _admin_page(limit, page)
        where, params = "", []
        like = _admin_term(term)
        if like:
            where = (" WHERE (email LIKE ? ESCAPE '\\' OR display_name LIKE ? ESCAPE '\\'"
                     " OR telegram_id LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\')")
            params.extend([f"%{like}%"] * 4)
        cols = ("id, email, display_name, tier, telegram_id, telegram_username, "
                "telegram_verified, created_at, updated_at, last_login_at")
        with self._tx() as conn:
            total = conn.execute(
                f"SELECT count(*) AS n FROM users{where}", params).fetchone()["n"]
            rows = conn.execute(
                f"SELECT {cols} FROM users{where} ORDER BY created_at DESC "
                f"LIMIT ? OFFSET ?",
                [*params, page_size, (page_no - 1) * page_size]).fetchall()
        return {
            "rows": [dict(r) for r in rows],
            "total": int(total or 0),
            "page": page_no, "page_size": page_size,
            "pages": max(1, (int(total or 0) + page_size - 1) // page_size),
        }

    def admin_list_audit(self, *, admin_id: str = "", page: int = 1,
                         limit: int = 50) -> dict[str, Any]:
        page_size, page_no = _admin_page(limit, page)
        where, params = "", []
        if admin_id:
            where = " WHERE admin_id = ?"
            params.append(str(admin_id)[:80])
        with self._tx() as conn:
            total = conn.execute(
                f"SELECT count(*) AS n FROM admin_audit_logs{where}",
                params).fetchone()["n"]
            rows = conn.execute(
                f"SELECT * FROM admin_audit_logs{where} "
                f"ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page_no - 1) * page_size]).fetchall()
        return {
            "rows": [dict(r) for r in rows],
            "total": int(total or 0),
            "page": page_no, "page_size": page_size,
            "pages": max(1, (int(total or 0) + page_size - 1) // page_size),
        }

    def admin_get_telemetry(self) -> list[dict[str, Any]]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT key, val_json, updated_at FROM system_telemetry "
                "ORDER BY key").fetchall()
        out = []
        for r in rows:
            try:
                val = json.loads(r["val_json"])
            except (TypeError, ValueError):
                val = r["val_json"]
            out.append({"key": r["key"], "value": val, "updated_at": r["updated_at"]})
        return out

    def admin_set_telemetry(self, key: str, value: Any) -> None:
        with self._tx() as conn:
            conn.execute("""
                INSERT INTO system_telemetry (key, val_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE
                SET val_json=excluded.val_json, updated_at=excluded.updated_at
            """, (str(key), json.dumps(value), datetime.now().isoformat()))
            conn.commit()

    def admin_live_cache_overview(self) -> dict[str, Any]:
        now_ts = time.time()
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT key, length(data) AS bytes, expires_at FROM live_cache"
            ).fetchall()
            pruned = conn.execute(
                "DELETE FROM live_cache WHERE expires_at < ?", (now_ts,)).rowcount
            conn.commit()
        live = [r for r in rows if r["expires_at"] >= now_ts]
        by_prefix: dict[str, dict[str, Any]] = {}
        for r in live:
            head = r["key"].split(":")[0]
            entry = by_prefix.setdefault(head, {"count": 0, "bytes": 0, "live": 0,
                                                "stale": 0})
            entry["count"] += 1
            entry["bytes"] += int(r["bytes"] or 0)
            entry["live"] += 1
        stale = [r for r in rows if r["expires_at"] < now_ts]
        for r in stale:
            head = r["key"].split(":")[0]
            entry = by_prefix.setdefault(head, {"count": 0, "bytes": 0, "live": 0,
                                                "stale": 0})
            entry["count"] += 1
            entry["bytes"] += int(r["bytes"] or 0)
            entry["stale"] += 1
        return {
            "entries": len(rows),
            "live": len(live),
            "stale": len(stale),
            "bytes": sum(int(r["bytes"] or 0) for r in rows),
            "pruned": int(pruned or 0),
            "by_prefix": by_prefix,
        }

    def admin_database_info(self) -> dict[str, Any]:
        """Physical file facts, so the panel can show real growth not estimates."""
        info: dict[str, Any] = {"path": self.db_path, "exists": False}
        try:
            if os.path.exists(self.db_path):
                info["exists"] = True
                info["bytes"] = os.path.getsize(self.db_path)
                info["bytes_on_disk"] = info["bytes"]
                for suffix in ("-wal", "-shm"):
                    side = self.db_path + suffix
                    if os.path.exists(side):
                        info["bytes"] += os.path.getsize(side)
                        info.setdefault("sidecars", {})[suffix] = os.path.getsize(side)
        except OSError as exc:
            info["error"] = str(exc)
        try:
            with self._tx() as conn:
                info["page_count"] = conn.execute(
                    "PRAGMA page_count").fetchone()[0]
                info["page_size"] = conn.execute(
                    "PRAGMA page_size").fetchone()[0]
                info["journal_mode"] = conn.execute(
                    "PRAGMA journal_mode").fetchone()[0]
                info["integrity"] = conn.execute(
                    "PRAGMA quick_check").fetchone()[0]
                info["tables"] = {
                    r["name"]: r["n"] for r in conn.execute(
                        "SELECT name, (SELECT count(*) FROM pragma_table_info(name))"
                        " AS n FROM sqlite_master WHERE type='table'"
                        " ORDER BY name").fetchall()
                }
                info["row_counts"] = {
                    r["name"]: conn.execute(
                        f'SELECT count(*) AS c FROM "{r["name"]}"').fetchone()["c"]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
                    # A table name cannot be bound as a parameter, so it is
                    # interpolated. Everything the application creates matches
                    # this, and anything else (a table from a restored backup, a
                    # future SQLite internal) is reported by name without being
                    # counted rather than being pasted into the statement.
                    if _SAFE_TABLE_NAME.match(r["name"])
                }
        except sqlite3.Error as exc:
            info["error"] = str(exc)
        return info

    def log_admin_action(self, admin_id: str, action: str, target: str = "", details: str = "") -> int:
        sql = """
            INSERT INTO admin_audit_logs (admin_id, action, target, details, timestamp)
            VALUES (?, ?, ?, ?, ?)
        """
        now_ts = time.time()
        with self._tx() as conn:
            cur = conn.execute(sql, (str(admin_id), action, target, details, now_ts))
            conn.commit()
            return cur.lastrowid or 0

    def list_admin_audit_logs(self, limit: int = 20) -> list[dict]:
        sql = "SELECT * FROM admin_audit_logs ORDER BY timestamp DESC LIMIT ?"
        with self._tx() as conn:
            cur = conn.execute(sql, (limit,))
            return [dict(r) for r in cur.fetchall()]

    def manual_settle_match(self, match_id: str, result: str = "WIN",
                            actual_score: Optional[str] = None) -> int:
        """Operator-initiated settlement.

        Guarded exactly like :meth:`settle_pick` -- only rows still in a PENDING
        state are touched. Without this, ``POST /api/admin/picks/settle-match``
        and the Telegram ``/settle`` command could rewrite an already-SETTLED
        row, which made the "write-once immutable ledger" claim true of the
        automated path and false of the operator path.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        res_upper = result.upper()
        placeholders = ",".join("?" for _ in PENDING_STATES)
        sql = f"""
            UPDATE picks
            SET state = 'SETTLED', result = ?, settled_at = ?,
                actual_score = COALESCE(?, actual_score)
            WHERE (match_id = ? OR dedupe_key LIKE ?)
              AND state IN ({placeholders})
        """
        like_pattern = f"%{match_id}%"
        params = [res_upper, now_iso, actual_score, match_id, like_pattern,
                  *PENDING_STATES]
        with self._tx() as conn:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount

    def is_system_paused(self) -> bool:
        with self._tx() as conn:
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
        with self._tx() as conn:
            conn.execute(sql, (val_str, now_iso))
            conn.commit()

    # -- notification outbox -------------------------------------------------

    def enqueue_notification(self, dedupe_key: str, text: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO notification_outbox (dedupe_key, text, status, created_at) "
                "VALUES (?, ?, 'PENDING', ?)",
                (dedupe_key, text, time.time()),
            )
            conn.commit()
            return cur.rowcount > 0

    def list_pending_notifications(self, limit: int = 50) -> list[dict]:
        with self._tx() as conn:
            cur = conn.execute(
                "SELECT id, dedupe_key, text, attempts, created_at FROM notification_outbox "
                "WHERE status = 'PENDING' ORDER BY id LIMIT ?",
                (max(1, int(limit)),),
            )
            return [dict(row) for row in cur.fetchall()]

    def mark_notification_sent(self, dedupe_key: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE notification_outbox SET status = 'SENT', sent_at = ?, last_error = NULL "
                "WHERE dedupe_key = ?",
                (time.time(), dedupe_key),
            )
            conn.commit()

    def mark_notification_failed(self, dedupe_key: str, error: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE notification_outbox SET attempts = attempts + 1, last_error = ? "
                "WHERE dedupe_key = ?",
                (str(error)[:500], dedupe_key),
            )
            conn.commit()

    def requeue_notifications(self, limit: int = 100) -> int:
        limit = max(1, int(limit))
        with self._tx() as conn:
            # Bounded by the same row limit used to read the outbox. The id
            # subquery keeps this to a single statement so the reset cannot race
            # a concurrent flush between a SELECT and an UPDATE.
            cur = conn.execute(
                "UPDATE notification_outbox SET attempts = 0, last_error = NULL "
                "WHERE id IN ("
                "  SELECT id FROM notification_outbox "
                "  WHERE status = 'PENDING' AND attempts > 0 ORDER BY id LIMIT ?"
                ")",
                (limit,),
            )
            conn.commit()
            return int(cur.rowcount or 0)


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

    def upsert_live(self, key: str, data: Any, ttl_seconds: int) -> None:
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
            if row["state"] in SETTLED_STATES or row.get("result"):
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
            # Verification columns: a settled pick must carry the score that
            # graded it, and the feed that produced it, so a customer can check
            # the ledger against an independent scoreboard.
            cur.execute("ALTER TABLE picks ADD COLUMN IF NOT EXISTS actual_score VARCHAR(64);")
            cur.execute("ALTER TABLE picks ADD COLUMN IF NOT EXISTS score_source VARCHAR(255);")
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