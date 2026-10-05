"""Storage: TTL hot layer, write-once ledger semantics, state machine."""
from __future__ import annotations

import time

from lisa.gate import Execution, Pick
from lisa.odds import utcnow
from lisa.storage import InMemoryStorage, pick_key


def _pick(match_id: str = "m1", outcome: str = "A") -> Pick:
    return Pick(
        match_id=match_id, sport_key="basketball_nba",
        home_team="A", away_team="B", commence_time=utcnow(), market="h2h",
        outcome_name=outcome, p_true=0.8, fair_odds=1.25, n_books=5,
        stdev=0.01, cv=0.0125,
        best_execution=Execution("pinnacle", "Pinnacle", 1.26, 0.008),
        state="TRIGGER_ALERT", created_at=utcnow(),
    )


def test_live_ttl_expiry():
    s = InMemoryStorage()
    s.upsert_live("match:m1", {"home": "A"}, 0.05)  # 50 ms
    assert s.get_live("match:m1") == {"home": "A"}
    assert "match:m1" in list(s.scan_live_keys())
    time.sleep(0.06)
    assert s.get_live("match:m1") is None
    assert "match:m1" not in list(s.scan_live_keys())


def test_pick_dedupe_and_settlement():
    s = InMemoryStorage()
    assert s.insert_pick(_pick()) is True
    assert s.insert_pick(_pick()) is False      # dedupe
    assert len(s.list_pending_picks()) == 1

    key = pick_key("m1", "h2h", "A")
    assert s.settle_pick(key, "LOSS", utcnow()) is True
    assert s.list_pending_picks() == []
    assert s.settle_pick(key, "WIN", utcnow()) is False  # terminal is immutable


def test_void_state():
    s = InMemoryStorage()
    s.insert_pick(_pick(match_id="m2"))
    key = pick_key("m2", "h2h", "A")
    assert s.settle_pick(key, "VOID", utcnow(), state="VOID") is True
    assert s.list_pending_picks() == []
    row = s._picks[key]
    assert row["state"] == "VOID" and row["result"] == "VOID"


def test_list_settled_picks():
    s = InMemoryStorage()
    s.insert_pick(_pick(match_id="m1", outcome="A"))
    s.insert_pick(_pick(match_id="m2", outcome="B"))
    s.insert_pick(_pick(match_id="m3", outcome="C"))

    assert len(s.list_pending_picks()) == 3
    assert len(s.list_settled_picks()) == 0

    key1 = pick_key("m1", "h2h", "A")
    key2 = pick_key("m2", "h2h", "B")
    s.settle_pick(key1, "WIN", utcnow(), state="SETTLED")
    s.settle_pick(key2, "VOID", utcnow(), state="VOID")

    assert len(s.list_pending_picks()) == 1
    settled = s.list_settled_picks()
    assert len(settled) == 2
    settled_keys = {r["dedupe_key"] for r in settled}
    assert key1 in settled_keys
    assert key2 in settled_keys



def test_pick_key_identity_and_ledger_columns():
    from lisa.storage import pick_to_row
    row = pick_to_row(_pick())
    assert row["dedupe_key"] == "m1::h2h::A"
    assert row["best_book"] == "pinnacle"
    assert row["best_ev"] == 0.008
    assert row["state"] == "TRIGGER_ALERT"
    assert row["result"] is None and row["settled_at"] is None
    assert row["line"] is None

    # Totals/spreads pick with a line
    tot_pick = Pick(
        match_id="m1", sport_key="basketball_nba",
        home_team="A", away_team="B", commence_time=utcnow(), market="totals",
        outcome_name="Over", p_true=0.8, fair_odds=1.25, n_books=5,
        stdev=0.01, cv=0.0125,
        best_execution=Execution("pinnacle", "Pinnacle", 1.26, 0.008),
        state="TRIGGER_ALERT", created_at=utcnow(), line=220.5,
    )
    tot_row = pick_to_row(tot_pick)
    assert tot_row["line"] == 220.5


def test_pick_storage_preserves_line():
    s = InMemoryStorage()
    p = Pick(
        match_id="m-tot", sport_key="basketball_nba",
        home_team="A", away_team="B", commence_time=utcnow(), market="totals",
        outcome_name="Over", p_true=0.8, fair_odds=1.25, n_books=5,
        stdev=0.01, cv=0.0125,
        best_execution=Execution("pinnacle", "Pinnacle", 1.26, 0.008),
        state="TRIGGER_ALERT", created_at=utcnow(), line=220.5,
    )
    assert s.insert_pick(p) is True
    key = pick_key("m-tot", "totals", "Over", 220.5)
    saved = s._picks[key]
    assert saved["line"] == 220.5


def test_postgres_schema_constant_is_sane():
    from lisa.storage import POSTGRES_DDL
    assert "CREATE TABLE IF NOT EXISTS picks" in POSTGRES_DDL
    assert "line" in POSTGRES_DDL and "DOUBLE PRECISION" in POSTGRES_DDL
    assert "ON CONFLICT" not in POSTGRES_DDL  # PK is the dedupe mechanism


def test_json_file_storage(tmp_path):
    from lisa.storage import JsonFileStorage
    fpath = str(tmp_path / "storage.json")
    store = JsonFileStorage(fpath)

    p1 = _pick(match_id="m-file-1", outcome="TeamA")
    assert store.insert_pick(p1) is True
    assert store.insert_pick(p1) is False  # dedupe

    pending = store.list_pending_picks()
    assert len(pending) == 1
    assert pending[0]["match_id"] == "m-file-1"

    # Settle pick
    k = pick_key("m-file-1", "h2h", "TeamA")
    assert store.settle_pick(k, "WIN", utcnow()) is True
    assert len(store.list_pending_picks()) == 0
    assert len(store.list_settled_picks()) == 1

    # Reload from disk into fresh instance
    store2 = JsonFileStorage(fpath)
    assert len(store2.list_settled_picks()) == 1
    reloaded = store2.get_pick(k)
    assert reloaded is not None
    assert reloaded["result"] == "WIN"


def test_sqlite_storage(tmp_path):
    from lisa.storage import SqliteStorage
    db_file = str(tmp_path / "test_lisa.db")
    store = SqliteStorage(db_file)

    # Hot layer TTL
    store.upsert_live("match:live-1", {"score": "2-1"}, ttl_seconds=1)
    assert store.get_live("match:live-1") == {"score": "2-1"}
    assert "match:live-1" in list(store.scan_live_keys())

    # Cold layer write-once pick insert
    p1 = _pick(match_id="m-sql-1", outcome="Lakers")
    assert store.insert_pick(p1) is True
    assert store.insert_pick(p1) is False  # dedupe

    pending = store.list_pending_picks()
    assert len(pending) == 1
    assert pending[0]["outcome_name"] == "Lakers"

    # Settle pick
    k = pick_key("m-sql-1", "h2h", "Lakers")
    assert store.settle_pick(k, "WIN", utcnow()) is True
    assert store.settle_pick(k, "LOSS", utcnow()) is False  # write-once guarantee
    assert len(store.list_pending_picks()) == 0
    settled = store.list_settled_picks()
    assert len(settled) == 1
    assert settled[0]["result"] == "WIN"

    # Closing odds & CLV
    assert store.update_pick_closing(k, closing_odds=1.15, closing_p_true=0.85, clv=0.095) is True
    updated = store.get_pick(k)
    assert updated is not None
    assert updated["closing_odds"] == 1.15
    assert updated["clv"] == 0.095

    # Verification session
    assert store.is_user_verified("user-xyz") is False
    store.verify_user("user-xyz", telegram_user_id="998877", username="testuser")
    assert store.is_user_verified("user-xyz") is True
    session = store.get_verified_user("user-xyz")
    assert session is not None
    assert session["username"] == "testuser"

    # Count stats
    counts = store.count_picks()
    assert counts["total"] == 1
    assert counts["settled"] == 1
    assert counts["won"] == 1


def test_sqlite_concurrent_access(tmp_path):
    import threading
    from lisa.storage import SqliteStorage

    db_file = str(tmp_path / "concurrent_lisa.db")
    store = SqliteStorage(db_file)
    errors = []

    def writer(idx: int):
        try:
            p = _pick(match_id=f"m-conc-{idx}", outcome=f"Team-{idx}")
            store.insert_pick(p)
            store.verify_user(f"session-{idx}", username=f"user_{idx}")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    counts = store.count_picks()
    assert counts["total"] == 20
    assert counts["pending"] == 20
