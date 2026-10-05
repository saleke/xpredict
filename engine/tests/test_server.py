"""Unit and integration tests for LISA production multi-threaded HTTP server."""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.parse
import urllib.request

from lisa.gate import Execution, Pick
from lisa.odds import utcnow
from lisa.server import make_production_server
from lisa.storage import InMemoryStorage, SqliteStorage
from lisa.telegram_bot import generate_unlock_token


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _make_sample_pick(match_id: str, outcome: str) -> Pick:
    return Pick(
        match_id=match_id, sport_key="basketball_nba",
        home_team="Team A", away_team="Team B", commence_time=utcnow(), market="h2h",
        outcome_name=outcome, p_true=0.85, fair_odds=1.17, n_books=5,
        stdev=0.01, cv=0.012,
        best_execution=Execution("pinnacle", "Pinnacle", 1.20, 0.02),
        state="TRIGGER_ALERT", created_at=utcnow(),
    )


def test_production_server_endpoints(tmp_path):
    port = _get_free_port()
    db_file = str(tmp_path / "server_test.db")
    storage = SqliteStorage(db_file)

    # Insert sample picks
    p1 = _make_sample_pick("m-open", "Team A")
    p2 = _make_sample_pick("m-locked-1", "Team C")
    p3 = _make_sample_pick("m-locked-2", "Team E")
    storage.insert_pick(p1)
    storage.insert_pick(p2)
    storage.insert_pick(p3)

    server = make_production_server(
        host="127.0.0.1",
        port=port,
        web_dir=str(tmp_path),
        storage=storage,
    )

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)

    try:
        base_url = f"http://127.0.0.1:{port}"

        # 1. /api/status and security headers
        req = urllib.request.Request(f"{base_url}/api/status")
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
            assert resp.headers.get("X-Content-Type-Options") == "nosniff"
            assert resp.headers.get("X-Frame-Options") == "DENY"
            data = json.loads(resp.read().decode("utf-8"))
            # No generation worker/publication exists in this endpoint fixture.
            assert data["status"] == "degraded"
            assert data["daily_service"]["ready"] is False
            assert data["ledger_counts"]["total"] == 3

        # 2. /api/verify-status for unknown user
        req = urllib.request.Request(f"{base_url}/api/verify-status?user_id=new_user_1")
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert data["user_id"] == "new_user_1"
            assert data["verified"] is False

        # 3. /api/picks for unverified user (server enforces masking)
        req = urllib.request.Request(f"{base_url}/api/picks?user_id=new_user_1")
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            picks = data["active_picks"]
            assert len(picks) == 3
            # Pick #0 is free and unmasked
            assert picks[0]["is_locked"] is False
            assert picks[0]["outcome_name"] == "Team A"
            # Pick #1 and #2 are strictly masked on the server
            assert picks[1]["is_locked"] is True
            assert "Unlock" in picks[1]["outcome_name"]
            assert picks[1]["best_odds"] is None
            assert picks[2]["is_locked"] is True
            assert picks[2]["best_odds"] is None

        # 4. /api/verify-token with cryptographic unlock code
        token = generate_unlock_token("user_seed_42")
        # Validate correct token
        verify_req = urllib.request.Request(
            f"{base_url}/api/verify-token",
            data=json.dumps({"user_id": "user_seed_42", "token": token}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(verify_req) as resp:
            ver_res = json.loads(resp.read().decode("utf-8"))
            assert ver_res["success"] is True

        # Check /api/verify-status now returns True
        req = urllib.request.Request(f"{base_url}/api/verify-status?user_id=user_seed_42")
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert data["verified"] is True

        req = urllib.request.Request(f"{base_url}/api/picks?user_id=user_seed_42")
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            picks = data["active_picks"]
            assert picks[1]["is_locked"] is False
            assert picks[1]["outcome_name"] == "Team C"
            assert picks[1]["best_odds"] == 1.20
            assert picks[2]["is_locked"] is True
            assert "Tier 1" in picks[2]["outcome_name"]

        # A `tier` query parameter must not grant a tier. The endpoint used to
        # honour it for seed/test user ids, so `?user_id=user_seed_42&tier=tier1`
        # unlocked every paid pick for any anonymous caller. It stays on the
        # free view regardless of what the parameter asks for.
        req_tier1 = urllib.request.Request(f"{base_url}/api/picks?user_id=user_seed_42&tier=tier1")
        with urllib.request.urlopen(req_tier1) as resp:
            data_t1 = json.loads(resp.read().decode("utf-8"))
            picks_t1 = data_t1["active_picks"]
            assert picks_t1[2]["is_locked"] is True
            assert "Tier 1" in picks_t1[2]["outcome_name"]

        # 6. /api/ledger
        storage.settle_pick("m-open::h2h::Team A", "WIN", utcnow())
        req = urllib.request.Request(f"{base_url}/api/ledger")
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert data["count"] == 1
            assert data["settled_ledger"][0]["result"] == "WIN"

    finally:
        server.shutdown()
