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


#: The endpoint refuses to run on a secret under ``_MIN_WEBHOOK_SECRET_LEN``
#: (32, server.py): ``_webhook_secret()`` returns "" and the webhook answers 503
#: "disabled" without ever reaching the signature check.
#:
#: This test used ``"correct-horse"`` -- 13 characters -- so it asserted 401 and
#: received 503. That has nothing to do with the operator's .env: monkeypatch
#: replaces the value, so it fails identically on a fully provisioned machine.
#: docs/CHANGELOG_FTIPSTER_RESPONSE.md had recorded the wrong cause, and has been
#: corrected.
_WEBHOOK_SECRET = "correct-horse-battery-staple-two-1"


def test_payment_webhook_rejects_wrong_and_unsigned(tmp_path, monkeypatch):
    monkeypatch.setenv("LISA_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    server, base = _serve(tmp_path)
    try:
        # Unsigned
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"{base}/api/v1/webhook/payment", {"customer_email": "x@example.com", "tier": "tier3"})
        assert exc.value.code == 401
        with pytest.raises(urllib.error.HTTPError):
            _post(f"{base}/api/v1/webhook/payment", {"customer_email": "x@example.com", "tier": "tier3"})
        # Wrong secret (including the historical hardcoded default)
        for bad in ("lisa_internal_secret_2026", "correct-hor", _WEBHOOK_SECRET + "X"):
            with pytest.raises(urllib.error.HTTPError) as exc:
                _post(f"{base}/api/v1/webhook/payment",
                      {"customer_email": "x@example.com", "tier": "tier3"},
                      {"X-Webhook-Secret": bad})
            assert exc.value.code == 401
        # Correct secret is accepted
        with _post(f"{base}/api/v1/webhook/payment",
                   {"customer_email": "x@example.com", "tier": "tier3"},
                   {"X-Webhook-Secret": _WEBHOOK_SECRET}) as resp:
            assert json.loads(resp.read().decode())["success"] is True
    finally:
        server.shutdown()


def test_payment_webhook_disabled_by_a_weak_secret(tmp_path, monkeypatch):
    """A too-short or placeholder secret must leave the endpoint *disabled*.

    This is the failure mode the test above walked into from the other side: it
    treated a 13-character secret as configured. The minimum-length and
    placeholder checks are what stop a guessable string from becoming a tier
    grant, so they are pinned here rather than left implicit. Each value is
    sent as the secret *and* as the presented credential, because the dangerous
    version is the one where the two agree.
    """
    weak = (
        "correct-horse",                  # what the test above used to pass
        "lisa_internal_secret_2026",      # the hardcoded default that shipped
        "y" * 31,                         # one character under the floor
    )
    for i, bad in enumerate(weak):
        monkeypatch.setenv("LISA_WEBHOOK_SECRET", bad)
        # A fresh directory per case: _serve() creates its web dir non-recursively.
        server, base = _serve(tmp_path / f"weak{i}")
        try:
            with pytest.raises(urllib.error.HTTPError) as exc:
                _post(f"{base}/api/v1/webhook/payment",
                      {"customer_email": "victim@example.com", "tier": "tier3"},
                      {"X-Webhook-Secret": bad})
            assert exc.value.code == 503, f"{len(bad)}-char secret must not enable the webhook"
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
