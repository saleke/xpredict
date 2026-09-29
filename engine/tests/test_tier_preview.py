"""Regression coverage for the operator tier-preview work.

The public site can now show an operator what each tier is given. That must
stay strictly a *view* concern:

* ``/api/auth/me`` reports operator status using the console's own rule, so the
  two interfaces cannot disagree about who is an operator.
* An operator picking a tier from the public UI must not have their real
  account tier rewritten. For anyone whose operator access derives from the
  tier column (rather than the email allowlist) that rewrite removed their own
  console access, so the write is refused.
* An email-allowlisted owner is unaffected: the allowlist survives a tier
  change by design.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lisa.auth import AuthManager  # noqa: E402
from lisa.server import make_production_server  # noqa: E402
from lisa.storage import SqliteStorage  # noqa: E402
from lisa.telegram_bot import TelegramBot  # noqa: E402


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve(tmp_path, monkeypatch, *, admin_emails: str, admin_telegram_ids: str = ""):
    """Start a production server and return (base_url, auth, storage, server)."""
    monkeypatch.setenv("LISA_WEBHOOK_SECRET", "s" * 40)
    monkeypatch.setenv("LISA_ADMIN_EMAILS", admin_emails)
    monkeypatch.setenv("ADMIN_TELEGRAM_IDS", admin_telegram_ids)

    storage = SqliteStorage(str(tmp_path / "preview.db"))
    auth = AuthManager(storage=storage)
    bot = TelegramBot(token="", channel_chat_id="@t", mock=True, storage=storage,
                      admin_telegram_ids=[i for i in admin_telegram_ids.split(",") if i])
    port = _free_port()
    server = make_production_server(
        host="127.0.0.1", port=port, web_dir=str(tmp_path),
        storage=storage, auth=auth, bot=bot,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.15)
    return f"http://127.0.0.1:{port}", auth, storage, server


def _get(base_url, path, cookie=None):
    headers = {"Cookie": cookie} if cookie else {}
    req = urllib.request.Request(f"{base_url}{path}", headers=headers)
    with urllib.request.urlopen(req) as resp:
        return resp.status, json.loads(resp.read().decode())


def _post(base_url, path, body, cookie=None):
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(
        f"{base_url}{path}", data=json.dumps(body).encode(),
        headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def _signup(base_url, email):
    _, res = _post(base_url, "/api/auth/signup",
                   {"email": email, "password": "Password123!"})
    return res["user"]["id"], f"lisa_session={res['session_id']}"


def test_auth_me_reports_operator_status(tmp_path, monkeypatch):
    """is_operator must follow the console's rule, and never leak secrets."""
    base_url, auth, _storage, server = _serve(
        tmp_path, monkeypatch, admin_emails="owner@example.com")
    try:
        owner_id, owner_cookie = _signup(base_url, "owner@example.com")
        auth.update_user_tier(owner_id, "admin")

        plain_id, plain_cookie = _signup(base_url, "member@example.com")

        _, res = _get(base_url, "/api/auth/me", owner_cookie)
        assert res["user"]["is_operator"] is True
        assert res["user"]["role"] == "owner"

        _, res = _get(base_url, "/api/auth/me", plain_cookie)
        assert res["user"]["is_operator"] is False
        assert res["user"]["role"] is None

        # The status flag must not widen what the payload exposes.
        assert "password_hash" not in res["user"]
        assert "password_salt" not in res["user"]
    finally:
        server.shutdown()


def test_operator_tier_admin_cannot_self_demote_via_public_api(tmp_path, monkeypatch):
    """A tier-derived operator must keep console access after browsing tiers.

    The account tier IS the operator credential for anyone not on the email
    allowlist, so letting the public API rewrite it turns a tier click into a
    self-lockout with no route back.
    """
    base_url, auth, _storage, server = _serve(
        tmp_path, monkeypatch, admin_emails="")
    try:
        uid, cookie = _signup(base_url, "tierboss@example.com")
        auth.update_user_tier(uid, "admin")

        code, res = _post(base_url, "/api/auth/update-tier", {"tier": "tier3"}, cookie=cookie)
        assert code == 403, res
        assert auth.get_user_by_id(uid)["tier"] == "admin"

        # Still an operator afterwards.
        _, me = _get(base_url, "/api/auth/me", cookie)
        assert me["user"]["is_operator"] is True
    finally:
        server.shutdown()


def test_email_allowlisted_owner_may_still_change_own_tier(tmp_path, monkeypatch):
    """The allowlist is the control that must survive a tier change."""
    base_url, auth, _storage, server = _serve(
        tmp_path, monkeypatch, admin_emails="owner@example.com")
    try:
        uid, cookie = _signup(base_url, "owner@example.com")
        auth.update_user_tier(uid, "admin")

        code, res = _post(base_url, "/api/auth/update-tier", {"tier": "tier3"}, cookie=cookie)
        assert code == 200, res
        assert auth.get_user_by_id(uid)["tier"] == "tier3"

        # Access survives, because the email allowlist is what grants it.
        _, me = _get(base_url, "/api/auth/me", cookie)
        assert me["user"]["is_operator"] is True
    finally:
        server.shutdown()


def test_member_still_cannot_change_tier_without_authorization(tmp_path, monkeypatch):
    """A normal member has no self-service tier path at all."""
    base_url, auth, _storage, server = _serve(
        tmp_path, monkeypatch, admin_emails="")
    try:
        uid, cookie = _signup(base_url, "member@example.com")

        code, _ = _post(base_url, "/api/auth/update-tier", {"tier": "tier3"}, cookie=cookie)
        assert code == 403
        assert auth.get_user_by_id(uid)["tier"] == "free"
    finally:
        server.shutdown()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
