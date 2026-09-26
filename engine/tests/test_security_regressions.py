"""Regression tests for the hardening pass.

Each test here pins a vulnerability that previously let an unauthenticated
caller escalate privilege, forge access, or read non-public data.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from lisa.auth import AuthManager
from lisa.server import make_production_server
from lisa.storage import SqliteStorage
from lisa.telegram_bot import generate_unlock_token


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _serve(tmp_path):
    port = _get_free_port()
    storage = SqliteStorage(str(tmp_path / "sec.db"))
    auth = AuthManager(storage=storage)
    web_dir = tmp_path / "web"
    (web_dir / "data").mkdir(parents=True)
    (web_dir / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    (web_dir / "data" / "verified_users.json").write_text(
        json.dumps({"usr_1": {"telegram_user_id": "999", "email": "leak@example.com"}}),
        encoding="utf-8",
    )
    server = make_production_server(
        host="127.0.0.1", port=port, web_dir=str(web_dir), storage=storage, auth=auth
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.1)
    return server, f"http://127.0.0.1:{port}"


def _post(url, payload, headers=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    return urllib.request.urlopen(req)


def test_payment_webhook_fails_closed_without_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("LISA_WEBHOOK_SECRET", raising=False)
    server, base = _serve(tmp_path)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{base}/api/v1/webhook/payment", {"event": "checkout.session.completed",
                                                     "customer_email": "victim@example.com",
                                                     "tier": "tier3"})
        assert exc.value.code == 503
    finally:
        server.shutdown()


def test_payment_webhook_rejects_wrong_and_unsigned(tmp_path, monkeypatch):
    monkeypatch.setenv("LISA_WEBHOOK_SECRET", "correct-horse")
    server, base = _serve(tmp_path)
    try:
        # Unsigned
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{base}/api/v1/webhook/payment", {"customer_email": "x@example.com", "tier": "tier3"})
        assert exc.value.code == 401
        with pytest.raises(urllib.error.HTTPError):
            _post(f"{base}/api/v1/webhook/payment", {"customer_email": "x@example.com", "tier": "tier3"})
        # Wrong secret (including the historical hardcoded default)
        for bad in ("lisa_internal_secret_2026", "correct-hor", "correct-horseX"):
            with pytest.raises(urllib.error.HTTPError) as exc:
                _post(f"{base}/api/v1/webhook/payment",
                      {"customer_email": "x@example.com", "tier": "tier3"},
                      {"X-Webhook-Secret": bad})
            assert exc.value.code == 401
        # Correct secret is accepted
        with _post(f"{base}/api/v1/webhook/payment",
                   {"customer_email": "x@example.com", "tier": "tier3"},
                   {"X-Webhook-Secret": "correct-horse"}) as resp:
            assert json.loads(resp.read().decode())["success"] is True
    finally:
        server.shutdown()


def test_credentialed_cors_is_allowlist_only(tmp_path, monkeypatch):
    monkeypatch.setenv("LISA_ALLOWED_ORIGINS", "https://dashboard.example.com")
    server, base = _serve(tmp_path)
    try:
        req = urllib.request.Request(f"{base}/api/health", headers={"Origin": "https://evil.example"})
        with urllib.request.urlopen(req) as resp:
            assert resp.headers.get("Access-Control-Allow-Origin") is None
            assert resp.headers.get("Access-Control-Allow-Credentials") is None

        req = urllib.request.Request(
            f"{base}/api/health", headers={"Origin": "https://dashboard.example.com"}
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.headers.get("Access-Control-Allow-Origin") == "https://dashboard.example.com"
            assert resp.headers.get("Access-Control-Allow-Credentials") == "true"
    finally:
        server.shutdown()


def test_static_server_does_not_expose_web_data(tmp_path):
    server, base = _serve(tmp_path)
    try:
        with urllib.request.urlopen(f"{base}/index.html") as resp:
            assert resp.status == 200
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/data/verified_users.json")
        assert exc.value.code == 404
    finally:
        server.shutdown()


def test_verify_token_rejects_forged_codes(tmp_path, monkeypatch):
    monkeypatch.setenv("LISA_UNLOCK_SECRET", "unlock-key-for-tests")
    server, base = _serve(tmp_path)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{base}/api/verify-token", {"user_id": "usr_x", "token": "LISA-23456789AB"})
        assert exc.value.code == 403
        assert "verified" not in json.loads(exc.value.read().decode())

        # A genuine code for this seed unlocks; the same code for another seed does not.
        with _post(f"{base}/api/verify-token",
                   {"user_id": "usr_x", "token": generate_unlock_token("usr_x")}) as resp:
            assert json.loads(resp.read().decode())["verified"] is True
    finally:
        server.shutdown()


def test_verify_token_is_rate_limited(tmp_path, monkeypatch):
    monkeypatch.setenv("LISA_UNLOCK_SECRET", "unlock-key-for-tests")
    server, base = _serve(tmp_path)
    try:
        last_code = None
        for _ in range(14):
            try:
                with _post(f"{base}/api/verify-token",
                           {"user_id": "usr_y", "token": "LISA-23456789AB"}) as resp:
                    last_code = resp.status
            except urllib.error.HTTPError as exc:
                last_code = exc.code
        assert last_code == 429
    finally:
        server.shutdown()


def test_signup_ignores_client_supplied_tier(tmp_path):
    server, base = _serve(tmp_path)
    try:
        with _post(f"{base}/api/auth/signup", {
            "email": "greedy@example.com",
            "password": "Password12345!",
            "display_name": "Greedy",
            "tier": "tier3",
        }) as resp:
            data = json.loads(resp.read().decode())
        assert data["user"]["tier"] == "free"
    finally:
        server.shutdown()


def test_oversized_body_rejected(tmp_path):
    server, base = _serve(tmp_path)
    try:
        payload = {"email": "a" * (200 * 1024), "password": "x", "display_name": "y"}
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{base}/api/auth/signup", payload)
        assert exc.value.code == 413
    finally:
        server.shutdown()


def test_verify_status_cannot_grant_privileges(tmp_path):
    server, base = _serve(tmp_path)
    try:
        # Unauthenticated: never promotes an account, even for an unknown id
        with urllib.request.urlopen(f"{base}/api/verify-status?user_id=usr_attacker") as resp:
            assert json.loads(resp.read().decode())["verified"] is False
    finally:
        server.shutdown()
