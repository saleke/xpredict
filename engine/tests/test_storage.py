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


def test_pick_key_identity_and_ledger_columns():
    from lisa.storage import pick_to_row
    row = pick_to_row(_pick())
    assert row["dedupe_key"] == "m1::h2h::A"
    assert row["best_book"] == "pinnacle"
    assert row["best_ev"] == 0.008
    assert row["state"] == "TRIGGER_ALERT"
    assert row["result"] is None and row["settled_at"] is None


def test_postgres_schema_constant_is_sane():
    from lisa.storage import POSTGRES_DDL
    assert "CREATE TABLE IF NOT EXISTS picks" in POSTGRES_DDL
    assert "ON CONFLICT" not in POSTGRES_DDL  # PK is the dedupe mechanism