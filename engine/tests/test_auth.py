"""Unit tests for LISA production authentication, password hashing, and session management."""
from __future__ import annotations

import time
import pytest

from lisa.auth import (
    AuthManager,
    hash_password,
    verify_password,
)
from lisa.storage import SqliteStorage


def test_password_hashing_and_verification():
    pw = "SuperSecurePass123!"
    salt, hashed = hash_password(pw)
    assert len(salt) == 64  # 32 bytes in hex
    assert len(hashed) == 64  # SHA-256 output in hex

    assert verify_password(pw, salt, hashed) is True
    assert verify_password("WrongPassword", salt, hashed) is False
    assert verify_password(pw, "bad_salt", hashed) is False


def test_user_registration_and_validation(tmp_path):
    db_file = str(tmp_path / "auth_test.db")
    storage = SqliteStorage(db_file)
    auth = AuthManager(storage=storage)

    # 1. Invalid email
    with pytest.raises(ValueError, match="valid email"):
        auth.register_user("invalid-email", "Pass12345678")

    # 2. Short password (< 8 chars)
    with pytest.raises(ValueError, match="at least 8 characters"):
        auth.register_user("test@example.com", "short")

    # 3. Successful registration
    user = auth.register_user("alice@example.com", "SecretPass123!", display_name="Alice")
    assert user["email"] == "alice@example.com"
    assert user["display_name"] == "Alice"
    assert user["tier"] == "free"
    assert user["id"].startswith("usr_")
    assert "password_hash" not in user
    assert "password_salt" not in user

    # 4. Duplicate registration rejected
    with pytest.raises(ValueError, match="already exists"):
        auth.register_user("ALICE@example.com", "AnotherPass123!")


def test_authentication_flow(tmp_path):
    db_file = str(tmp_path / "auth_test2.db")
    storage = SqliteStorage(db_file)
    auth = AuthManager(storage=storage)

    auth.register_user("bob@example.com", "BobsPassWord2026!", display_name="Bob")

    # Wrong password
    assert auth.authenticate_user("bob@example.com", "WrongPass") is None

    # Unknown user
    assert auth.authenticate_user("unknown@example.com", "AnyPass1234") is None

    # Correct password
    user = auth.authenticate_user("BOB@EXAMPLE.COM", "BobsPassWord2026!")
    assert user is not None
    assert user["email"] == "bob@example.com"
    assert user["last_login_at"] is not None


def test_session_lifecycle(tmp_path):
    db_file = str(tmp_path / "auth_test3.db")
    storage = SqliteStorage(db_file)
    auth = AuthManager(storage=storage)

    user = auth.register_user("carol@example.com", "CarolPassword123!", tier="tier2")
    session = auth.create_session(user["id"], ip_address="127.0.0.1", user_agent="PyTest")

    assert session["session_id"].startswith("sess_")
    assert session["user_id"] == user["id"]

    # Validate active session
    valid = auth.validate_session(session["session_id"])
    assert valid is not None
    assert valid["user"]["email"] == "carol@example.com"
    assert valid["user"]["tier"] == "tier2"

    # Revoke session
    assert auth.revoke_session(session["session_id"]) is True
    assert auth.validate_session(session["session_id"]) is None


def test_session_expiry(tmp_path):
    db_file = str(tmp_path / "auth_test4.db")
    storage = SqliteStorage(db_file)
    auth = AuthManager(storage=storage)

    user = auth.register_user("dan@example.com", "DanPassWord123!")
    # Create session with 1 second TTL
    session = auth.create_session(user["id"], ttl_seconds=1)
    assert auth.validate_session(session["session_id"]) is not None

    time.sleep(1.1)
    assert auth.validate_session(session["session_id"]) is None


def test_telegram_linking_and_tier_update(tmp_path):
    db_file = str(tmp_path / "auth_test5.db")
    storage = SqliteStorage(db_file)
    auth = AuthManager(storage=storage)

    user = auth.register_user("eva@example.com", "EvaPassWord123!")
    assert user["telegram_verified"] is False

    # A bare link does not grant the verified privilege (server only passes
    # verified=True once unlock-code redemption or getChatMember has proven it)
    assert auth.link_telegram(user["id"], telegram_id="123456789", telegram_username="EvaTelegram") is True
    updated = auth.get_user_by_id(user["id"])
    assert updated["telegram_verified"] is False
    assert updated["telegram_id"] == "123456789"
    assert updated["telegram_username"] == "EvaTelegram"

    # Proof upgrades the same link
    assert auth.link_telegram(user["id"], telegram_id="123456789", verified=True) is True
    assert auth.get_user_by_id(user["id"])["telegram_verified"] is True

    # A Telegram ID owned by somebody else can never be claimed
    other = auth.register_user("mallory@example.com", "MalloryPassWord123!")
    assert auth.link_telegram(other["id"], telegram_id="123456789") is False
    assert auth.get_user_by_id(other["id"])["telegram_id"] in ("", None)
    assert auth.get_user_by_telegram_id("123456789")["id"] == user["id"]

    # Upgrade tier
    assert auth.update_user_tier(user["id"], "tier3") is True
    updated = auth.get_user_by_id(user["id"])
    assert updated["tier"] == "tier3"
