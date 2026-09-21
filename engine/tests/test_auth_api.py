"""Integration tests for LISA authentication REST API endpoints, cookies, and tier permissions."""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from lisa.auth import AuthManager
from lisa.gate import Execution, Pick
from lisa.odds import utcnow
from lisa.server import make_production_server
from lisa.storage import SqliteStorage


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _make_sample_pick(match_id: str, outcome: str) -> Pick:
    return Pick(
        match_id=match_id, sport_key="soccer_epl",
        home_team="Man City", away_team="Arsenal", commence_time=utcnow(), market="h2h",
        outcome_name=outcome, p_true=0.88, fair_odds=1.14, n_books=5,
        stdev=0.01, cv=0.012,
        best_execution=Execution("pinnacle", "Pinnacle", 1.18, 0.035),
        state="TRIGGER_ALERT", created_at=utcnow(),
    )


def test_auth_api_flow(tmp_path):
    port = _get_free_port()
    db_file = str(tmp_path / "auth_api_test.db")
    storage = SqliteStorage(db_file)
    auth = AuthManager(storage=storage)

    # Insert 4 picks: #0 (free), #1 (telegram), #2 (telegram), #3 (tier2 syndicate alpha)
    for i in range(4):
        storage.insert_pick(_make_sample_pick(f"m-{i}", f"Outcome-{i}"))

    server = make_production_server(
        host="127.0.0.1",
        port=port,
        web_dir=str(tmp_path),
        storage=storage,
        auth=auth,
    )

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.1)

    try:
        base_url = f"http://127.0.0.1:{port}"

        # 1. Unauthenticated /api/auth/me
        req = urllib.request.Request(f"{base_url}/api/auth/me")
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            assert data["authenticated"] is False
            assert data["user"] is None

        # 2. Signup /api/auth/signup
        signup_payload = {
            "email": "sarah@xpredict.ai",
            "password": "SecurePassword2026!",
            "display_name": "Sarah Alpha",
            "tier": "tier2",
        }
        req = urllib.request.Request(
            f"{base_url}/api/auth/signup",
            data=json.dumps(signup_payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 201
            cookie_header = resp.headers.get("Set-Cookie")
            assert cookie_header is not None
            assert "lisa_session=" in cookie_header
            assert "HttpOnly" in cookie_header
            data = json.loads(resp.read().decode())
            assert data["success"] is True
            assert data["user"]["email"] == "sarah@xpredict.ai"
            assert data["user"]["tier"] == "tier2"
            session_id = data["session_id"]
            user_id = data["user"]["id"]

        # 3. /api/auth/me using Cookie
        req = urllib.request.Request(
            f"{base_url}/api/auth/me",
            headers={"Cookie": f"lisa_session={session_id}"},
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            assert data["authenticated"] is True
            assert data["user"]["display_name"] == "Sarah Alpha"
            assert data["user"]["tier"] == "tier2"

        # 4. /api/auth/me using Bearer header
        req = urllib.request.Request(
            f"{base_url}/api/auth/me",
            headers={"Authorization": f"Bearer {session_id}"},
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            assert data["authenticated"] is True
            assert data["user"]["email"] == "sarah@xpredict.ai"

        # 5. /api/picks for Tier 2 authenticated subscriber
        req = urllib.request.Request(
            f"{base_url}/api/picks",
            headers={"Cookie": f"lisa_session={session_id}"},
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            assert data["is_authenticated"] is True
            assert data["tier"] == "tier2"
            picks = data["active_picks"]
            # Pick #3 (Paid Tier 2) is unmasked because user is Tier 2!
            assert picks[3]["is_locked"] is False
            assert picks[3]["outcome_name"] == "Outcome-3"

        # 6. /api/auth/link-telegram
        link_req = urllib.request.Request(
            f"{base_url}/api/auth/link-telegram",
            data=json.dumps({"telegram_id": "778899", "telegram_username": "SarahTelegram"}).encode(),
            headers={
                "Content-Type": "application/json",
                "Cookie": f"lisa_session={session_id}",
            },
            method="POST",
        )
        with urllib.request.urlopen(link_req) as resp:
            data = json.loads(resp.read().decode())
            assert data["success"] is True
            assert data["user"]["telegram_verified"] is True
            assert data["user"]["telegram_username"] == "SarahTelegram"

        # 7. /api/auth/signin
        signin_req = urllib.request.Request(
            f"{base_url}/api/auth/signin",
            data=json.dumps({"email": "sarah@xpredict.ai", "password": "SecurePassword2026!"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(signin_req) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["success"] is True
            assert data["user"]["telegram_verified"] is True

        # 8. Signout /api/auth/signout
        signout_req = urllib.request.Request(
            f"{base_url}/api/auth/signout",
            headers={"Cookie": f"lisa_session={session_id}"},
            method="POST",
        )
        with urllib.request.urlopen(signout_req) as resp:
            data = json.loads(resp.read().decode())
            assert data["success"] is True
            cookie = resp.headers.get("Set-Cookie")
            assert "Max-Age=0" in cookie

        # Check /api/auth/me now returns authenticated: False
        req = urllib.request.Request(
            f"{base_url}/api/auth/me",
            headers={"Cookie": f"lisa_session={session_id}"},
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            assert data["authenticated"] is False

        picks_bypass_req = urllib.request.Request(f"{base_url}/api/picks?tier=tier3")
        with urllib.request.urlopen(picks_bypass_req) as resp:
            data = json.loads(resp.read().decode())
            assert data["tier"] == "free"
            assert data["active_picks"][3]["is_locked"] is True

        unauth_update_req = urllib.request.Request(
            f"{base_url}/api/auth/update-tier",
            data=json.dumps({"tier": "tier3"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(unauth_update_req)
            assert False, "Expected 403 Forbidden"
        except urllib.error.HTTPError as exc:
            assert exc.code == 403

        auth_update_req = urllib.request.Request(
            f"{base_url}/api/auth/update-tier",
            data=json.dumps({"user_id": user_id, "tier": "tier3"}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Secret": "lisa_internal_secret_2026",
            },
            method="POST",
        )
        with urllib.request.urlopen(auth_update_req) as resp:
            data = json.loads(resp.read().decode())
            assert data["success"] is True
            assert data["tier"] == "tier3"

        webhook_provision_req = urllib.request.Request(
            f"{base_url}/api/v1/webhook/payment",
            data=json.dumps({
                "event": "checkout.session.completed",
                "customer_email": "pro_trader@xpredict.ai",
                "tier": "tier2",
                "telegram_id": "99887766",
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Secret": "lisa_internal_secret_2026",
            },
            method="POST",
        )
        with urllib.request.urlopen(webhook_provision_req) as resp:
            data = json.loads(resp.read().decode())
            assert data["success"] is True
            assert data["action"] == "PROVISIONED"
            assert data["tier"] == "tier2"

        provisioned_user = auth.get_user_by_email("pro_trader@xpredict.ai")
        assert provisioned_user is not None
        assert provisioned_user["tier"] == "tier2"
        assert provisioned_user["telegram_id"] == "99887766"

        webhook_churn_req = urllib.request.Request(
            f"{base_url}/api/v1/webhook/payment",
            data=json.dumps({
                "event": "customer.subscription.deleted",
                "customer_email": "pro_trader@xpredict.ai",
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Secret": "lisa_internal_secret_2026",
            },
            method="POST",
        )
        with urllib.request.urlopen(webhook_churn_req) as resp:
            data = json.loads(resp.read().decode())
            assert data["success"] is True
            assert data["action"] == "DOWNGRADED"
            assert data["tier"] == "free"

        churned_user = auth.get_user_by_email("pro_trader@xpredict.ai")
        assert churned_user["tier"] == "free"

    finally:
        server.shutdown()
