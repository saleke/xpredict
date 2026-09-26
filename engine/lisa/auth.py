"""Production-grade Authentication & Session Management for LISA.

Features:
  * Zero external dependencies: uses Python standard library hashlib, hmac, secrets, sqlite3.
  * OWASP-compliant password hashing: PBKDF2-HMAC-SHA256 with 600,000 iterations.
  * 32-byte cryptographic random salt per user (secrets.token_bytes(32)).
  * Timing-attack resistant verification (hmac.compare_digest).
  * High-entropy 256-bit cryptographically secure session tokens.
  * Multi-tier subscription role tracking ('free', 'tier1', 'tier2', 'tier3').
  * Native Telegram identity linking and verification bridge.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from .storage import SqliteStorage

logger = logging.getLogger(__name__)

# RFC 5322 simplified email regex
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
PBKDF2_ITERATIONS = 600_000
DEFAULT_SESSION_TTL_SECONDS = 30 * 24 * 3600  # 30 days
VALID_TIERS = ("free", "tier1", "tier2", "tier3")


def hash_password(password: str) -> tuple[str, str]:
    """Hash password using PBKDF2-HMAC-SHA256 with 600,000 iterations and a 32-byte random salt."""
    salt_bytes = secrets.token_bytes(32)
    hash_bytes = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt_bytes,
        PBKDF2_ITERATIONS,
    )
    return salt_bytes.hex(), hash_bytes.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    """Verify password against stored salt and PBKDF2 hash using constant-time comparison."""
    try:
        salt_bytes = bytes.fromhex(salt_hex)
        expected_hash = bytes.fromhex(hash_hex)
        actual_hash = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt_bytes,
            PBKDF2_ITERATIONS,
        )
        return hmac.compare_digest(actual_hash, expected_hash)
    except Exception:
        return False


def sanitize_user(user_row: dict[str, Any]) -> dict[str, Any]:
    """Strip sensitive fields (password_hash, password_salt) before exposing user data."""
    u = dict(user_row)
    u.pop("password_hash", None)
    u.pop("password_salt", None)
    u["telegram_verified"] = bool(u.get("telegram_verified", False))
    return u


class AuthManager:
    """Authentication and session management engine backed by SQLite."""

    def __init__(self, storage: Optional[SqliteStorage] = None, db_path: str = "data/lisa.db"):
        self.storage = storage or SqliteStorage(db_path)
        self.db_path = self.storage.db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    # -- User Registration & Authentication ----------------------------------

    def register_user(
        self,
        email: str,
        password: str,
        display_name: str = "",
        tier: str = "free",
    ) -> dict[str, Any]:
        """Register a new user account with validated credentials."""
        clean_email = (email or "").strip().lower()
        if not clean_email or not EMAIL_REGEX.match(clean_email):
            raise ValueError("A valid email address is required.")

        if not password or len(password) < 8:
            raise ValueError("Password must be at least 8 characters in length.")

        clean_tier = tier.strip().lower() if tier else "free"
        if clean_tier not in VALID_TIERS:
            clean_tier = "free"

        salt_hex, hash_hex = hash_password(password)
        user_id = f"usr_{secrets.token_hex(8)}"
        clean_name = display_name.strip() if display_name else clean_email.split("@")[0]
        now = time.time()

        sql = """
            INSERT INTO users (
                id, email, password_hash, password_salt, display_name,
                tier, telegram_id, telegram_username, telegram_verified,
                created_at, updated_at, last_login_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            user_id, clean_email, hash_hex, salt_hex, clean_name,
            clean_tier, None, None, 0, now, now, None
        )

        with self._connect() as conn:
            try:
                conn.execute(sql, params)
                conn.commit()
            except sqlite3.IntegrityError:
                raise ValueError("An account with this email address already exists.")

        return self.get_user_by_id(user_id) or {}

    def authenticate_user(self, email: str, password: str) -> Optional[dict[str, Any]]:
        """Validate email and password, returning sanitized user record if valid."""
        clean_email = (email or "").strip().lower()
        if not clean_email or not password:
            return None

        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM users WHERE email = ?", (clean_email,))
            row = cur.fetchone()
            if not row:
                return None

            user_data = dict(row)
            if not verify_password(password, user_data["password_salt"], user_data["password_hash"]):
                return None

            now = time.time()
            conn.execute("UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?", (now, now, user_data["id"]))
            conn.commit()
            user_data["last_login_at"] = now
            return sanitize_user(user_data)

    def get_user_by_id(self, user_id: str) -> Optional[dict[str, Any]]:
        """Retrieve sanitized user by user ID."""
        if not user_id:
            return None
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM users WHERE id = ?", (user_id.strip(),))
            row = cur.fetchone()
            return sanitize_user(dict(row)) if row else None

    def get_user_by_email(self, email: str) -> Optional[dict[str, Any]]:
        """Retrieve sanitized user by email."""
        clean_email = (email or "").strip().lower()
        if not clean_email:
            return None
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM users WHERE email = ?", (clean_email,))
            row = cur.fetchone()
            return sanitize_user(dict(row)) if row else None

    def get_user_by_telegram_id(self, telegram_id: str) -> Optional[dict[str, Any]]:
        """Retrieve sanitized user by linked Telegram ID.

        Used to stop one account from claiming an identity (in particular an
        admin's) that already belongs to somebody else.
        """
        clean_tg = str(telegram_id or "").strip()
        if not clean_tg:
            return None
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (clean_tg,))
            row = cur.fetchone()
            return sanitize_user(dict(row)) if row else None

    def update_user_tier(self, user_id: str, tier: str) -> bool:
        """Update subscription tier for a user."""
        clean_tier = tier.strip().lower()
        if clean_tier not in VALID_TIERS:
            raise ValueError(f"Invalid tier: {tier}. Must be one of {VALID_TIERS}")
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE users SET tier = ?, updated_at = ? WHERE id = ?",
                (clean_tier, time.time(), user_id)
            )
            conn.commit()
            return cur.rowcount > 0

    def link_telegram(
        self, user_id: str, telegram_id: str, telegram_username: str = "", verified: bool = False
    ) -> bool:
        """Attach a Telegram identity to an account.

        ``verified`` must only be set by a caller that has proven control of the
        Telegram account (unlock-code redemption or a bot-side
        ``getChatMember`` check). Recording an unverified claim never grants the
        ``telegram_verified`` privilege.

        A Telegram ID already owned by another account is never reassigned:
        claiming someone else's ID would inherit their privileges. An unproven
        claim also never demotes an identity that was already proven.
        """
        now = time.time()
        clean_tg = str(telegram_id or "").strip()
        if clean_tg:
            owner = self.get_user_by_telegram_id(clean_tg)
            if owner and owner.get("id") != user_id:
                return False

        already_proven = False
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT telegram_id, telegram_verified FROM users WHERE id = ?", (user_id,)
            )
            row = cur.fetchone()
            if row:
                already_proven = bool(row["telegram_verified"]) and str(row["telegram_id"] or "") == clean_tg
        new_verified = 1 if (verified or already_proven) else 0

        with self._connect() as conn:
            cur = conn.execute("""
                UPDATE users
                SET telegram_id = ?, telegram_username = ?, telegram_verified = ?, updated_at = ?
                WHERE id = ?
            """, (str(telegram_id), telegram_username, new_verified, now, user_id))
            conn.commit()
            return cur.rowcount > 0

    # -- Session Management --------------------------------------------------

    def create_session(
        self,
        user_id: str,
        ip_address: str = "",
        user_agent: str = "",
        ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
    ) -> dict[str, Any]:
        """Issue a 256-bit cryptographically secure session token."""
        token = f"sess_{secrets.token_urlsafe(32)}"
        now = time.time()
        expires_at = now + ttl_seconds

        sql = """
            INSERT INTO sessions (session_id, user_id, created_at, expires_at, ip_address, user_agent)
            VALUES (?, ?, ?, ?, ?, ?)
        """
        with self._connect() as conn:
            conn.execute(sql, (token, user_id, now, expires_at, ip_address, user_agent))
            conn.commit()

        return {
            "session_id": token,
            "user_id": user_id,
            "created_at": now,
            "expires_at": expires_at,
        }

    def validate_session(self, session_id: str) -> Optional[dict[str, Any]]:
        """Validate session token, returning user details if active and unexpired."""
        if not session_id or not isinstance(session_id, str):
            return None

        clean_token = session_id.strip()
        now = time.time()

        sql = """
            SELECT
                s.session_id, s.user_id, s.created_at as session_created_at, s.expires_at,
                u.id, u.email, u.display_name, u.tier, u.telegram_id, u.telegram_username,
                u.telegram_verified, u.created_at, u.last_login_at
            FROM sessions s
            JOIN users u ON s.user_id = u.id
            WHERE s.session_id = ?
        """
        with self._connect() as conn:
            cur = conn.execute(sql, (clean_token,))
            row = cur.fetchone()
            if not row:
                return None

            data = dict(row)
            if data["expires_at"] < now:
                # Expired: remove from database
                conn.execute("DELETE FROM sessions WHERE session_id = ?", (clean_token,))
                conn.commit()
                return None

            user_obj = {
                "id": data["id"],
                "email": data["email"],
                "display_name": data["display_name"],
                "tier": data["tier"],
                "telegram_id": data["telegram_id"],
                "telegram_username": data["telegram_username"],
                "telegram_verified": bool(data["telegram_verified"]),
                "created_at": data["created_at"],
                "last_login_at": data["last_login_at"],
            }
            return {
                "session_id": clean_token,
                "user": user_obj,
                "expires_at": data["expires_at"],
            }

    def revoke_session(self, session_id: str) -> bool:
        """Revoke a session (logout)."""
        if not session_id:
            return False
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id.strip(),))
            conn.commit()
            return cur.rowcount > 0

    def revoke_all_user_sessions(self, user_id: str) -> bool:
        """Revoke all sessions for a user (security reset)."""
        if not user_id:
            return False
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id.strip(),))
            conn.commit()
            return cur.rowcount > 0
