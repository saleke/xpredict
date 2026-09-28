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
from lisa.telegram_bot import generate_unlock_token


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


def test_auth_api_flow(tmp_path, monkeypatch):
<<<<<<< HEAD
=======
    # A real, high-entropy webhook secret. The old literal
    # "lisa_internal_secret_2026" shipped in server.py and is now rejected.
    webhook_secret = "test-secret-4f8a2c9e1b7d6035ae94c8f2b16d7e30"
    monkeypatch.setenv("LISA_WEBHOOK_SECRET", webhook_secret)

>>>>>>> 2bfd448 (environmental update on telegram_bot_admin access activation and user telegram interaction exprience)
    port = _get_free_port()
    db_file = str(tmp_path / "auth_api_test.db")
    storage = SqliteStorage(db_file)
    auth = AuthManager(storage=storage)
    webhook_secret = "test_webhook_secret_do_not_reuse"
    unlock_secret = "test_unlock_secret_do_not_reuse"
    monkeypatch.setenv("LISA_WEBHOOK_SECRET", webhook_secret)
    monkeypatch.setenv("LISA_UNLOCK_SECRET", unlock_secret)

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
        # NOTE: the payload asks for tier2, but self-service signup must always
        # provision the free tier. A caller-supplied tier used to be honoured,
        # which let anyone claim a paid tier with one unauthenticated POST.
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
<<<<<<< HEAD
            # A client-declared paid tier must never be honoured at signup.
            assert data["user"]["tier"] == "free"
            session_id = data["session_id"]
            user_id = data["user"]["id"]

        # 2b. Paid tier only via the authenticated webhook path
        promote_req = urllib.request.Request(
            f"{base_url}/api/auth/update-tier",
            data=json.dumps({"user_id": user_id, "tier": "tier2"}).encode(),
            headers={"Content-Type": "application/json", "X-Webhook-Secret": webhook_secret},
            method="POST",
        )
        with urllib.request.urlopen(promote_req) as resp:
            assert json.loads(resp.read().decode())["tier"] == "tier2"
=======
            assert data["user"]["tier"] == "free", "signup must not honour a requested tier"
            session_id = data["session_id"]
            user_id = data["user"]["id"]

        # 2b. Paid access is granted by an authorized actor, never by signup.
        elevate_req = urllib.request.Request(
            f"{base_url}/api/auth/update-tier",
            data=json.dumps({"user_id": user_id, "tier": "tier2"}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Secret": webhook_secret,
            },
            method="POST",
        )
        with urllib.request.urlopen(elevate_req) as resp:
            data = json.loads(resp.read().decode())
            assert data["success"] is True
            assert data["tier"] == "tier2"
>>>>>>> 2bfd448 (environmental update on telegram_bot_admin access activation and user telegram interaction exprience)

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

<<<<<<< HEAD
        # 6. /api/auth/link-telegram — a bare claim must not grant verification
=======
        # 6. /api/auth/link-telegram
        # Possession must be proven by a bot-side /start verify_<user_id>
        # handshake that named the same Telegram id. Self-asserting an id is
        # rejected, otherwise anyone could claim the operator's account.
        unverified_link = urllib.request.Request(
            f"{base_url}/api/auth/link-telegram",
            data=json.dumps({"telegram_id": "778899"}).encode(),
            headers={
                "Content-Type": "application/json",
                "Cookie": f"lisa_session={session_id}",
            },
            method="POST",
        )
        try:
            urllib.request.urlopen(unverified_link)
            raise AssertionError("link-telegram accepted an unproven telegram_id")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
            assert b"verification" in exc.read().lower()

        # Record the handshake the way the bot does, then linking succeeds.
        from lisa import telegram_bot as tb_mod
        tb_mod.registry.verify(user_id, telegram_user_id="778899", username="SarahTelegram")

>>>>>>> 2bfd448 (environmental update on telegram_bot_admin access activation and user telegram interaction exprience)
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
            assert data["telegram_verified"] is False
            assert data["user"]["telegram_verified"] is False
            assert data["user"]["telegram_username"] == "SarahTelegram"

        # 6b. The same link with a valid unlock code is proven and unlocks
        proven_link_req = urllib.request.Request(
            f"{base_url}/api/auth/link-telegram",
            data=json.dumps({
                "telegram_id": "778899",
                "telegram_username": "SarahTelegram",
                "token": generate_unlock_token("sarah@xpredict.ai"),
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "Cookie": f"lisa_session={session_id}",
            },
            method="POST",
        )
        with urllib.request.urlopen(proven_link_req) as resp:
            data = json.loads(resp.read().decode())
            assert data["telegram_verified"] is True
            assert data["user"]["telegram_verified"] is True

        # 6c. A forged code never establishes a new verified identity
        fraudster = auth.register_user("fraud@example.com", "FraudPassWord123!")
        fraud_sess = auth.create_session(user_id=fraudster["id"])["session_id"]
        forged_link_req = urllib.request.Request(
            f"{base_url}/api/auth/link-telegram",
            data=json.dumps({
                "telegram_id": "11223344",
                "token": "LISA-23456789AB",
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "Cookie": f"lisa_session={fraud_sess}",
            },
            method="POST",
        )
        with urllib.request.urlopen(forged_link_req) as resp:
            assert json.loads(resp.read().decode())["telegram_verified"] is False

        # And an already-proven identity is not demoted by a bogus retry
        retry_req = urllib.request.Request(
            f"{base_url}/api/auth/link-telegram",
            data=json.dumps({
                "telegram_id": "778899",
                "token": "LISA-23456789AB",
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "Cookie": f"lisa_session={session_id}",
            },
            method="POST",
        )
        with urllib.request.urlopen(retry_req) as resp:
            assert json.loads(resp.read().decode())["user"]["telegram_verified"] is True

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
                "X-Webhook-Secret": webhook_secret,
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
                "X-Webhook-Secret": webhook_secret,
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
                "X-Webhook-Secret": webhook_secret,
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


def _serve(tmp_path, monkeypatch, secret=None):
    """Boot a server on a free port; returns (base_url, auth, server)."""
    if secret is None:
        monkeypatch.delenv("LISA_WEBHOOK_SECRET", raising=False)
    else:
        monkeypatch.setenv("LISA_WEBHOOK_SECRET", secret)
    storage = SqliteStorage(str(tmp_path / "sec.db"))
    auth = AuthManager(storage=storage)
    port = _get_free_port()
    server = make_production_server(
        host="127.0.0.1", port=port, web_dir=str(tmp_path),
        storage=storage, auth=auth,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.15)
    return f"http://127.0.0.1:{port}", auth, server


def test_signup_cannot_self_assign_a_paid_tier(tmp_path, monkeypatch):
    """Regression: signup must never honour a caller-supplied tier.

    Previously POSTing {"tier": "tier3"} to /api/auth/signup created a tier3
    account with no payment and no admin approval.
    """
    base_url, auth, server = _serve(tmp_path, monkeypatch, secret="x" * 40)
    try:
        for tier in ("tier1", "tier2", "tier3"):
            email = f"greedy-{tier}@evil.example"
            req = urllib.request.Request(
                f"{base_url}/api/auth/signup",
                data=json.dumps({
                    "email": email,
                    "password": "Password123!",
                    "display_name": "Greedy",
                    "tier": tier,
                }).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode())
            assert data["user"]["tier"] == "free", f"{tier} was granted via signup"
            stored = auth.get_user_by_email(email)
            assert stored["tier"] == "free"
    finally:
        server.shutdown()


def test_payment_webhook_fails_closed_without_secret(tmp_path, monkeypatch):
    """Regression: with no secret set, the webhook must refuse, not accept.

    The old hardcoded default "lisa_internal_secret_2026" meant anyone who read
    the repository could provision any tier for any address.
    """
    base_url, auth, server = _serve(tmp_path, monkeypatch, secret=None)
    try:
        for headers in (
            {},
            {"X-Webhook-Secret": "lisa_internal_secret_2026"},
            {"X-Webhook-Secret": "anything"},
        ):
            req = urllib.request.Request(
                f"{base_url}/api/v1/webhook/payment",
                data=json.dumps({
                    "event": "checkout.session.completed",
                    "customer_email": "victim@target.example",
                    "tier": "tier3",
                }).encode(),
                headers={"Content-Type": "application/json", **headers},
                method="POST",
            )
            try:
                urllib.request.urlopen(req)
                raise AssertionError(f"webhook accepted unauthenticated call {headers}")
            except urllib.error.HTTPError as exc:
                assert exc.code in (401, 503), exc.code

        # Nothing was provisioned.
        assert auth.get_user_by_email("victim@target.example") is None
    finally:
        server.shutdown()


def test_link_telegram_requires_proven_possession(tmp_path, monkeypatch):
    """Regression: a caller must not be able to claim someone else's Telegram id.

    Previously /api/auth/link-telegram trusted the request body, so signing up
    and posting the operator's real Telegram id granted admin authority through
    is_admin() and unlocked self-service tier escalation.
    """
    from lisa.telegram_bot import TelegramBot

    monkeypatch.setenv("LISA_WEBHOOK_SECRET", "y" * 40)
    storage = SqliteStorage(str(tmp_path / "link.db"))
    auth = AuthManager(storage=storage)
    bot = TelegramBot(token="", channel_chat_id="@t", mock=True,
                      admin_telegram_ids=["8720543490"])
    port = _get_free_port()
    server = make_production_server(
        host="127.0.0.1", port=port, web_dir=str(tmp_path),
        storage=storage, auth=auth, bot=bot,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.15)
    base_url = f"http://127.0.0.1:{port}"

    def call(path, body=None, cookie=None):
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        req = urllib.request.Request(
            f"{base_url}{path}",
            data=json.dumps(body).encode() if body else None,
            headers=headers, method="POST",
        )
        try:
            resp = urllib.request.urlopen(req)
            return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    try:
        _, res = call("/api/auth/signup", {
            "email": "mallory@evil.example", "password": "Password123!",
        })
        session_id = res["session_id"]
        attacker_id = res["user"]["id"]
        cookie = f"lisa_session={session_id}"

        # Claiming the operator's id without a handshake is refused.
        code, res = call("/api/auth/link-telegram",
                         {"telegram_id": "8720543490"}, cookie=cookie)
        assert code == 403, res
        assert auth.get_user_by_id(attacker_id)["telegram_id"] is None

        # And that leaves no route to escalate.
        code, _ = call("/api/auth/update-tier",
                       {"user_id": attacker_id, "tier": "tier3"}, cookie=cookie)
        assert code == 403
        assert auth.get_user_by_id(attacker_id)["tier"] == "free"

        # A handshake recorded for a *different* id must not satisfy the claim.
        from lisa import telegram_bot as tb_mod
        tb_mod.registry.verify(attacker_id, telegram_user_id="111111", username="mallory")
        code, res = call("/api/auth/link-telegram",
                         {"telegram_id": "8720543490"}, cookie=cookie)
        assert code == 403, res
        assert auth.get_user_by_id(attacker_id)["telegram_id"] is None
    finally:
        server.shutdown()


def test_payment_webhook_rejects_placeholder_secret(tmp_path, monkeypatch):
    base_url, auth, server = _serve(
        tmp_path, monkeypatch, secret="lisa_internal_secret_2026"
    )
    try:
        req = urllib.request.Request(
            f"{base_url}/api/v1/webhook/payment",
            data=json.dumps({
                "event": "checkout.session.completed",
                "customer_email": "victim2@target.example",
                "tier": "tier3",
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Secret": "lisa_internal_secret_2026",
            },
            method="POST",
        )
        try:
            urllib.request.urlopen(req)
            raise AssertionError("placeholder secret was accepted")
        except urllib.error.HTTPError as exc:
            assert exc.code in (401, 503), exc.code
        assert auth.get_user_by_email("victim2@target.example") is None
    finally:
        server.shutdown()
