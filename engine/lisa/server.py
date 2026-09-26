"""High-concurrency multi-threaded production HTTP server for LISA.

Features:
  * Uses Python standard library http.server.ThreadingHTTPServer for concurrent non-blocking requests.
  * REST API endpoints (/api/status, /api/picks, /api/ledger, /api/verify-status, /api/verify-token).
  * Server-enforced prediction masking (unverified users cannot access locked picks via API inspection).
  * Security headers enforced across all responses (X-Content-Type-Options, X-Frame-Options, Cache-Control).
  * Serves static web assets (HTML, CSS, JS, data) directly with directory isolation.
  * Zero external dependencies.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import time
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from . import __version__
from . import config as cfg
from .auth import AuthManager
from .dashboard import LIVE_ODDS_PREFIX
from .gate import Pick
from .storage import InMemoryStorage, SqliteStorage, Storage
from .telegram_bot import TelegramBot, generate_unlock_token, registry, verify_unlock_token

logger = logging.getLogger(__name__)

FAILED_SIGNIN_ATTEMPTS: dict[str, list[float]] = {}
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_WINDOW_SECONDS = 900

#: Per-IP budget for unlock-code redemption attempts.
MAX_VERIFY_TOKEN_ATTEMPTS = 10
VERIFY_TOKEN_LOCKOUT_SECONDS = 900

#: Per-IP budget for Telegram identity linking attempts.
MAX_LINK_ATTEMPTS = 10
LINK_LOCKOUT_SECONDS = 900

#: Fallback limiter state for handlers constructed without a server object.
RATE_LIMIT_BUCKETS: dict[str, list[float]] = {}

#: Hard ceiling on request bodies accepted by the JSON API (bytes).
MAX_BODY_BYTES = 64 * 1024

#: Directories inside ``web/`` that must never be served as static assets:
#: they hold user emails, Telegram IDs, and other non-public data.
STATIC_DENY_DIRS = ("data",)

#: Extra origins allowed to send credentialed cross-origin requests. Same-origin
#: browser traffic never needs CORS, so this stays empty by default.
ALLOWED_ORIGINS_ENV = "LISA_ALLOWED_ORIGINS"


def _allowed_origins() -> tuple:
    return tuple(
        o.strip().rstrip("/")
        for o in os.environ.get(ALLOWED_ORIGINS_ENV, "").split(",")
        if o.strip()
    )


class LISAProductionHandler(SimpleHTTPRequestHandler):
    """Production HTTP request handler combining REST API routing and static file serving."""

    def __init__(self, *args, **kwargs):
        # Python 3.7+ supports directory kwarg in SimpleHTTPRequestHandler
        server = kwargs.get("server") or (args[2] if len(args) > 2 else None)
        web_dir = getattr(server, "web_dir", Path("web").resolve())
        super().__init__(*args, directory=str(web_dir), **kwargs)

    @property
    def storage(self) -> Storage:
        return getattr(self.server, "storage", None)

    @property
    def auth(self) -> Optional[AuthManager]:
        return getattr(self.server, "auth", None)

    @property
    def settings(self) -> cfg.Settings:
        return getattr(self.server, "settings", None)

    @property
    def bot(self) -> Any:
        return getattr(self.server, "bot", None)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-XSS-Protection", "1; mode=block")
        super().end_headers()

    def _send_cors_headers(self):
        """Credentialed CORS is allowlist-only.

        Reflecting the caller's ``Origin`` together with
        ``Access-Control-Allow-Credentials`` lets any site on the internet make
        authenticated calls with a visitor's session cookie, so unlisted
        origins get no CORS headers at all.
        """
        req_origin = (self.headers.get("Origin") or "").strip().rstrip("/")
        if req_origin and req_origin in _allowed_origins():
            self.send_header("Access-Control-Allow-Origin", req_origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Cookie, X-Webhook-Secret, X-Admin-Secret")
            self.send_header("Vary", "Origin")
        elif not req_origin:
            # No Origin header: not a browser cross-origin request.
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Cookie, X-Webhook-Secret, X-Admin-Secret")

    def _send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, indent=2 if status != 200 else None).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _send_json_with_cookie(self, data: Any, cookie_val: str, max_age: int = 2592000, status: int = 200):
        cookie_header = f"lisa_session={cookie_val}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"
        body = json.dumps(data, indent=2 if status != 200 else None).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", cookie_header)
        self._send_cors_headers()
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _get_session_id(self) -> Optional[str]:
        cookie_header = self.headers.get("Cookie", "")
        if cookie_header:
            for part in cookie_header.split(";"):
                part = part.strip()
                if part.startswith("lisa_session="):
                    return part.split("=", 1)[1].strip()
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:].strip()
        return None

    def _get_current_user_and_session(self) -> tuple[Optional[dict], Optional[dict]]:
        sess_id = self._get_session_id()
        if not sess_id or not self.auth:
            return None, None
        session_data = self.auth.validate_session(sess_id)
        if not session_data:
            return None, None
        return session_data["user"], session_data

    def _read_json_body(self) -> Optional[dict]:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self._send_json({"success": False, "error": "Invalid Content-Length"}, status=400)
            return None
        if length <= 0:
            return None
        if length > MAX_BODY_BYTES:
            self._send_json({"success": False, "error": "Request body too large"}, status=413)
            return None
        try:
            raw_body = self.rfile.read(length).decode("utf-8")
            data = json.loads(raw_body)
        except Exception:
            self._send_json({"success": False, "error": "Invalid JSON body"}, status=400)
            return None
        return data if isinstance(data, dict) else None

    @property
    def _rate_limit_buckets(self) -> dict:
        """Per-server limiter state, so restarts and tests start clean."""
        server = getattr(self, "server", None)
        if server is None:
            return RATE_LIMIT_BUCKETS
        buckets = getattr(server, "rate_limit_buckets", None)
        if buckets is None:
            buckets = {}
            setattr(server, "rate_limit_buckets", buckets)
        return buckets

    def _rate_limited(self, bucket: str, limit: int, window: int) -> bool:
        """Per-IP fixed-window limiter shared by credentialed endpoints."""
        buckets = self._rate_limit_buckets
        ip = self.client_address[0] if self.client_address else "unknown"
        now_ts = time.time()
        key = f"{bucket}:{ip}"
        attempts = [t for t in buckets.get(key, []) if now_ts - t < window]
        if len(attempts) >= limit:
            buckets[key] = attempts
            return True
        attempts.append(now_ts)
        buckets[key] = attempts
        return False

    def _clear_rate_limit(self, bucket: str) -> None:
        ip = self.client_address[0] if self.client_address else "unknown"
        self._rate_limit_buckets.pop(f"{bucket}:{ip}", None)

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def _is_denied_static_path(self, path: str) -> bool:
        """Block static access to non-public directories under ``web/``."""
        parts = [p for p in urllib.parse.urlparse(path).path.split("/") if p]
        return any(p in STATIC_DENY_DIRS for p in parts[:-1])

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if self._is_denied_static_path(path):
            self._send_json({"error": "Not found"}, status=404)
            return

        if path in ("/api/status", "/api/health", "/api/telemetry"):
            self._handle_status()
            return

        if path in ("/api/dashboard", "/api/data"):
            self._handle_dashboard()
            return

        if path == "/api/auth/me":
            self._handle_auth_me()
            return

        if path == "/api/verify-status":
            self._handle_verify_status(parsed)
            return

        if path == "/api/picks":
            self._handle_picks(parsed)
            return

        if path == "/api/tiers":
            self._handle_tiers()
            return

        if path == "/api/forecast":
            self._handle_forecast()
            return

        if path == "/api/backtest":
            self._handle_backtest()
            return

        if path == "/api/ledger":
            self._handle_ledger(parsed)
            return

        super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/api/v1/webhook/payment", "/api/webhook/payment", "/api/webhook/stripe"):
            self._handle_payment_webhook()
            return

        if path == "/api/auth/signup":
            self._handle_auth_signup()
            return

        if path == "/api/auth/signin":
            self._handle_auth_signin()
            return

        if path == "/api/auth/signout":
            self._handle_auth_signout()
            return

        if path in ("/api/auth/update-tier", "/api/auth/tier"):
            self._handle_auth_update_tier()
            return

        if path == "/api/auth/link-telegram":
            self._handle_auth_link_telegram()
            return

        if path == "/api/verify-token":
            self._handle_verify_token()
            return

        self._send_json({"error": "Endpoint not found"}, status=404)

    def _handle_auth_me(self):
        user, sess = self._get_current_user_and_session()
        if user:
            self._send_json({
                "authenticated": True,
                "user": user,
                "session_id": sess["session_id"] if sess else None,
            })
        else:
            self._send_json({
                "authenticated": False,
                "user": None,
            })

    def _handle_auth_signup(self):
        if not self.auth:
            self._send_json({"success": False, "error": "Auth service unavailable"}, status=500)
            return

        data = self._read_json_body()
        if not data:
            self._send_json({"success": False, "error": "JSON payload required"}, status=400)
            return

        email = str(data.get("email", "")).strip()
        password = str(data.get("password", ""))
        name = str(data.get("display_name", "")).strip()
        # Signup always provisions the free tier. Paid tiers are only granted by
        # a verified payment webhook or an admin, never by the request body.
        requested_tier = str(data.get("tier", "")).strip().lower()
        if requested_tier and requested_tier != "free":
            logger.warning(
                "Ignoring client-supplied tier %r on signup for %s",
                requested_tier,
                email,
            )

        try:
            user = self.auth.register_user(email=email, password=password, display_name=name, tier="free")
            ip_addr = self.client_address[0] if self.client_address else ""
            ua = self.headers.get("User-Agent", "")
            sess = self.auth.create_session(user_id=user["id"], ip_address=ip_addr, user_agent=ua)
            self._send_json_with_cookie(
                {"success": True, "user": user, "session_id": sess["session_id"]},
                cookie_val=sess["session_id"],
                status=201,
            )
        except ValueError as err:
            self._send_json({"success": False, "error": str(err)}, status=400)
        except Exception as exc:
            self._send_json({"success": False, "error": f"Registration failed: {exc}"}, status=500)

    def _handle_auth_signin(self):
        if not self.auth:
            self._send_json({"success": False, "error": "Auth service unavailable"}, status=500)
            return

        ip_addr = self.client_address[0] if self.client_address else "127.0.0.1"
        now_ts = time.time()
        attempts = [t for t in FAILED_SIGNIN_ATTEMPTS.get(ip_addr, []) if now_ts - t < LOCKOUT_WINDOW_SECONDS]
        FAILED_SIGNIN_ATTEMPTS[ip_addr] = attempts
        if len(attempts) >= MAX_FAILED_ATTEMPTS:
            self._send_json({
                "success": False,
                "error": "Too many failed sign-in attempts. For security, please wait 15 minutes before trying again."
            }, status=429)
            return

        data = self._read_json_body()
        if not data:
            self._send_json({"success": False, "error": "JSON payload required"}, status=400)
            return

        email = str(data.get("email", "")).strip()
        password = str(data.get("password", ""))

        user = self.auth.authenticate_user(email=email, password=password)
        if not user:
            attempts.append(now_ts)
            FAILED_SIGNIN_ATTEMPTS[ip_addr] = attempts
            remaining = MAX_FAILED_ATTEMPTS - len(attempts)
            warn = f" ({remaining} attempts remaining before temporary lockout)" if remaining > 0 else ""
            self._send_json({"success": False, "error": f"Invalid email or password.{warn}"}, status=401)
            return

        # Successful login: reset failed attempts
        FAILED_SIGNIN_ATTEMPTS.pop(ip_addr, None)

        ua = self.headers.get("User-Agent", "")
        sess = self.auth.create_session(user_id=user["id"], ip_address=ip_addr, user_agent=ua)
        self._send_json_with_cookie(
            {"success": True, "user": user, "session_id": sess["session_id"]},
            cookie_val=sess["session_id"],
            status=200,
        )

    def _handle_auth_signout(self):
        sess_id = self._get_session_id()
        if sess_id and self.auth:
            self.auth.revoke_session(sess_id)
        self._send_json_with_cookie(
            {"success": True, "message": "Signed out successfully"},
            cookie_val="",
            max_age=0,
            status=200,
        )

    def _expected_webhook_secret(self) -> str:
        return os.environ.get("LISA_WEBHOOK_SECRET", "").strip()

    def _webhook_authorized(self, data: dict) -> bool:
        """Constant-time check of the shared payment/webhook secret.

        With no ``LISA_WEBHOOK_SECRET`` configured there is no way to tell a real
        payment event from a forged one, so the endpoint fails closed instead of
        accepting unsigned (or hardcoded-secret) requests.
        """
        expected = self._expected_webhook_secret()
        if not expected:
            logger.error(
                "LISA_WEBHOOK_SECRET is not configured: rejecting privileged request"
            )
            return False
        provided = self.headers.get("X-Webhook-Secret") or self.headers.get("X-Admin-Secret") or data.get("secret")
        if not provided:
            return False
        return secrets.compare_digest(str(provided), expected)

    def _handle_auth_update_tier(self):
        user, _ = self._get_current_user_and_session()
        data = self._read_json_body() or {}
        is_authorized = self._webhook_authorized(data)
        if not is_authorized and user:
            user_tg = str(user.get("telegram_id", "")).strip()
            if (self.bot and self.bot.is_admin(user_tg)) or user.get("tier") == "admin":
                is_authorized = True

        if not is_authorized:
            self._send_json({"success": False, "error": "Unauthorized: tier modification requires payment webhook or administrative authorization"}, status=403)
            return

        target_user_id = str(data.get("user_id") or (user["id"] if user else "")).strip()
        tier = str(data.get("tier", "")).strip().lower()
        valid_tiers = ("free", "tier1", "tier2", "tier3")
        if tier not in valid_tiers:
            self._send_json({"success": False, "error": f"Invalid tier: {tier}. Must be one of {valid_tiers}"}, status=400)
            return

        try:
            if self.auth:
                self.auth.update_user_tier(target_user_id, tier)
                updated = self.auth.get_user_by_id(target_user_id)
            else:
                updated = None
            if self.bot:
                self.bot.invalidate_user_cache(target_user_id)
            self._send_json({"success": True, "user": updated, "tier": tier, "message": f"Tier updated to {tier.upper()}"})
        except Exception as exc:
            self._send_json({"success": False, "error": str(exc)}, status=500)

    def _handle_payment_webhook(self):
        data = self._read_json_body()
        if not data:
            self._send_json({"success": False, "error": "JSON payload required"}, status=400)
            return

        if not self._expected_webhook_secret():
            self._send_json({
                "success": False,
                "error": "Payment webhook disabled: LISA_WEBHOOK_SECRET is not configured",
            }, status=503)
            return
        if not self._webhook_authorized(data):
            self._send_json({"success": False, "error": "Invalid webhook secret signature"}, status=401)
            return

        event_type = str(data.get("event") or data.get("type", "checkout.session.completed")).lower()
        obj = data.get("data", {})
        inner = obj.get("object", obj) if isinstance(obj, dict) else {}

        cust_details = inner.get("customer_details", {}) if isinstance(inner, dict) else {}
        metadata = inner.get("metadata", {}) if isinstance(inner, dict) else {}

        email = str(
            cust_details.get("email")
            or inner.get("customer_email")
            or inner.get("email")
            or metadata.get("email")
            or data.get("customer_email", "")
        ).strip().lower()

        tier_raw = str(
            metadata.get("tier")
            or inner.get("tier")
            or data.get("tier", "tier2")
        ).strip().lower()
        clean_tier = "tier2" if "2" in tier_raw else "tier3" if "3" in tier_raw else "tier1" if "1" in tier_raw else "free"
        telegram_id = str(
            metadata.get("telegram_id")
            or inner.get("telegram_id")
            or data.get("telegram_id", "")
        ).strip()

        if event_type in ("checkout.session.completed", "subscription.created", "payment.succeeded", "invoice.paid"):
            user = None
            if self.auth and email:
                user = self.auth.get_user_by_email(email)
                if user:
                    self.auth.update_user_tier(user["id"], clean_tier)
                    if telegram_id and not self.auth.link_telegram(user["id"], telegram_id=telegram_id):
                        logger.warning(
                            "Payment for %s could not link Telegram ID %s: already owned by another account",
                            email,
                            telegram_id,
                        )
                else:
                    user = self.auth.register_user(email=email, password=secrets.token_urlsafe(16), tier=clean_tier)
                    if telegram_id and not self.auth.link_telegram(user["id"], telegram_id=telegram_id):
                        logger.warning(
                            "Provisioning for %s could not link Telegram ID %s: already owned by another account",
                            email,
                            telegram_id,
                        )

            invite = None
            if self.bot:
                channel = self.bot.tier2_channel_chat_id if clean_tier in ("tier2", "tier3") else self.bot.channel_chat_id
                invite = self.bot.create_single_use_invite(channel, member_limit=1, expire_seconds=86400)
                if email:
                    self.bot.invalidate_user_cache(email)
                if telegram_id:
                    self.bot.invalidate_user_cache(telegram_id)

            unlock_code = generate_unlock_token(email or telegram_id)
            if self.storage and hasattr(self.storage, "verify_user") and email:
                self.storage.verify_user(email, telegram_user_id=telegram_id)
            registry.verify(email or telegram_id)

            audit_id = 0
            if self.storage and hasattr(self.storage, "log_admin_action"):
                audit_id = self.storage.log_admin_action(
                    admin_id="AUTOMATED_PAYMENT_GATEWAY",
                    action="WEBHOOK_SUBSCRIPTION_PROVISION",
                    target=email or telegram_id,
                    details=f"Granted {clean_tier.upper()} via {event_type}. Invite: {invite}",
                )

            self._send_json({
                "success": True,
                "action": "PROVISIONED",
                "email": email,
                "tier": clean_tier,
                "invite_link": invite,
                "unlock_code": unlock_code,
                "audit_id": audit_id,
            })
            return

        if event_type in ("customer.subscription.deleted", "subscription.canceled", "payment.failed", "invoice.payment_failed"):
            if self.auth and email:
                user = self.auth.get_user_by_email(email)
                if user:
                    self.auth.update_user_tier(user["id"], "free")

            if self.bot:
                if email:
                    self.bot.invalidate_user_cache(email)
                if telegram_id:
                    self.bot.invalidate_user_cache(telegram_id)
                    channel = self.bot.tier2_channel_chat_id if clean_tier in ("tier2", "tier3") else self.bot.channel_chat_id
                    self.bot.kick_member(channel, telegram_id, temporary=True)

            audit_id = 0
            if self.storage and hasattr(self.storage, "log_admin_action"):
                audit_id = self.storage.log_admin_action(
                    admin_id="AUTOMATED_PAYMENT_GATEWAY",
                    action="WEBHOOK_CHURN_DOWNGRADE",
                    target=email or telegram_id,
                    details=f"Downgraded to FREE via {event_type}",
                )

            self._send_json({
                "success": True,
                "action": "DOWNGRADED",
                "email": email,
                "tier": "free",
                "audit_id": audit_id,
            })
            return

        self._send_json({"success": True, "action": "IGNORED", "event": event_type})

    def _handle_auth_link_telegram(self):
        user, _ = self._get_current_user_and_session()
        if not user or not self.auth:
            self._send_json({"success": False, "error": "Authentication required"}, status=401)
            return

        data = self._read_json_body() or {}
        tg_id = str(data.get("telegram_id", "")).strip()
        tg_username = str(data.get("telegram_username", "")).strip()
        if not tg_id:
            self._send_json({"success": False, "error": "telegram_id required"}, status=400)
            return

        # A self-declared Telegram ID is not evidence. Unlocking requires either
        # a valid unlock code (issued after payment) or a live bot-side
        # getChatMember check against the official channel.
        seeds = self._unlock_seed_candidates(user["id"])
        token = str(data.get("token", "")).strip()
        proven = bool(token) and verify_unlock_token(token, seeds)

        proof_source = "unlock_code"
        if not proven:
            proof_source = "channel_membership"
            proven = bool(
                self.bot
                and getattr(self.bot, "token", "")
                and self.bot.is_channel_member(self.bot.channel_chat_id, tg_id)
            )

        if self._rate_limited("link_telegram", MAX_LINK_ATTEMPTS, LINK_LOCKOUT_SECONDS):
            self._send_json({"success": False, "error": "Too many link attempts. Try again later."}, status=429)
            return

        self.auth.link_telegram(user["id"], telegram_id=tg_id, telegram_username=tg_username, verified=proven)
        updated = self.auth.get_user_by_id(user["id"])
        if proven:
            self._clear_rate_limit("link_telegram")
            if self.storage and hasattr(self.storage, "verify_user"):
                self.storage.verify_user(user["id"], telegram_user_id=tg_id, username=tg_username)
            registry.verify(user["id"], telegram_user_id=tg_id, username=tg_username)
        self._send_json({
            "success": True,
            "user": updated,
            "telegram_verified": bool(proven),
            "message": (
                "Telegram identity verified."
                if proven
                else "Telegram ID recorded but not verified. Complete the unlock-code "
                     "flow in Telegram (or join the channel) to unlock premium content."
            ),
        })

    def _handle_backtest(self):
        """Archive replay, computed on demand and cached for a day.

        This is real historical odds from the packaged archive, not a live feed,
        so the response is labelled with its provenance and compute time.
        """
        cache_key = "cache:backtest"
        cached = None
        if self.storage is not None and hasattr(self.storage, "get_live"):
            cached = self.storage.get_live(cache_key)
        if cached:
            self._send_json(cached)
            return

        try:
            from .backtest import BacktestEngine

            started = time.time()
            payload = BacktestEngine().run().to_web_dict()
            payload["meta"] = {
                "provenance": "packaged_historical_archive",
                "is_live": False,
                "statement": (
                    "Replay of real historical odds from the packaged archive. "
                    "Not a live feed; it measures the model, not today's board."
                ),
                "computed_in_sec": round(time.time() - started, 2),
            }
        except Exception as exc:
            logger.warning("backtest compute failed: %r", exc)
            self._send_json({"error": "backtest unavailable", "detail": str(exc)}, status=503)
            return

        if self.storage is not None and hasattr(self.storage, "upsert_live"):
            try:
                self.storage.upsert_live(cache_key, payload, 24 * 60 * 60)
            except Exception:
                pass
        self._send_json(payload)

    def _handle_dashboard(self):
        """Real dashboard payload: ledger + last live observation only."""
        from .dashboard import build_dashboard

        settings = self.settings or cfg.load_settings()
        try:
            payload = build_dashboard(self.storage, settings)
        except Exception as exc:
            logger.warning("dashboard build failed: %r", exc)
            self._send_json({"error": "dashboard unavailable", "detail": str(exc)}, status=503)
            return
        self._send_json(payload)

    def _handle_status(self):
        start_time = getattr(self.server, "start_time", time.time())
        uptime = round(time.time() - start_time, 1)

        counts = {"total": 0, "pending": 0, "settled": 0, "won": 0, "lost": 0, "void": 0}
        if self.storage and hasattr(self.storage, "count_picks"):
            counts = self.storage.count_picks()

        settings = self.settings or cfg.load_settings()
        payload = {
            "status": "healthy",
            "version": __version__,
            "uptime_seconds": uptime,
            "storage_driver": getattr(settings, "storage_driver", "sqlite"),
            "ledger_counts": counts,
            "telegram_bot": {
                "configured": bool(settings.telegram_token),
                "bot_username": settings.telegram_bot_username or "XpredictPremiumBot",
                "channel_configured": bool(settings.telegram_chat_id),
            },
            "odds_feed": {
                "configured": bool(settings.odds_api_key),
                "base_url": settings.api_base_url,
                "sports_count": len(settings.sports),
            }
        }
        self._send_json(payload)

    def _handle_tiers(self):
        """Subscription tier value ladder (feature matrix + reveal timing)."""
        from .tiers import upgrade_path

        self._send_json(upgrade_path())

    def _handle_forecast(self):
        """Forecast board for real upcoming fixtures from the live cache.

        Never falls back to the packaged archive: if there is no live snapshot
        the response is an explicit "no live data" board.
        """
        from .bulletin import NO_LIVE_DATA, build_live_bulletin_from_payloads

        cached = getattr(self, "_forecast_cache", None)
        if cached and time.time() - cached[0] <= 120:
            self._send_json(cached[1])
            return

        payloads: list[tuple[str, list]] = []
        if self.storage is not None and hasattr(self.storage, "scan_live_keys"):
            try:
                keys = [k for k in self.storage.scan_live_keys() if k.startswith(LIVE_ODDS_PREFIX)]
            except Exception:
                keys = []
            for key in keys:
                entry = self.storage.get_live(key) if hasattr(self.storage, "get_live") else None
                if isinstance(entry, dict) and entry.get("payload"):
                    payloads.append((str(entry.get("sport_key") or key), entry["payload"]))

        if not payloads:
            self._send_json(dict(NO_LIVE_DATA))
            return

        try:
            board = build_live_bulletin_from_payloads(payloads)
        except Exception as exc:
            logger.warning("live bulletin failed: %r", exc)
            self._send_json(dict(NO_LIVE_DATA))
            return

        self._forecast_cache = (time.time(), board)
        self._send_json(board)

    def _handle_verify_status(self, parsed: urllib.parse.ParseResult):
        qs = urllib.parse.parse_qs(parsed.query)
        user_id = qs.get("user_id", [""])[0].strip()

        is_ver = False
        if user_id:
            if self.storage and hasattr(self.storage, "is_user_verified"):
                is_ver = self.storage.is_user_verified(user_id)
            if not is_ver:
                is_ver = registry.is_verified(user_id)

            # Reconcile the auth row only for an account that has no Telegram
            # identity yet, and never overwrite an existing one. This endpoint is
            # unauthenticated, so it must not be able to grant privileges.
            if is_ver and user_id.startswith("usr_") and self.auth:
                u = self.auth.get_user_by_id(user_id)
                if u and not u.get("telegram_verified") and not str(u.get("telegram_id") or "").strip():
                    try:
                        self._sync_verified_identity(user_id)
                    except Exception:
                        logger.debug("verify-status identity sync failed", exc_info=True)

        self._send_json({"user_id": user_id, "verified": bool(is_ver)})

    def _sync_verified_identity(self, user_id: str) -> None:
        """Copy the registry's proven Telegram identity onto the auth record."""
        if not self.auth:
            return
        session = registry.get_session(user_id) or {}
        telegram_id = str(session.get("telegram_user_id") or "").strip()
        username = str(session.get("username") or "")
        if not telegram_id:
            return
        if not self.auth.link_telegram(user_id, telegram_id=telegram_id, telegram_username=username, verified=True):
            logger.warning("Refused identity sync for %s: Telegram ID already owned", user_id)

    def _unlock_seed_candidates(self, user_id: str) -> list:
        """Seeds an unlock code may legitimately be bound to for this request."""
        seeds = []
        for candidate in (user_id,):
            candidate = str(candidate or "").strip()
            if candidate:
                seeds.append(candidate)
        auth_user, _ = self._get_current_user_and_session()
        if auth_user:
            for key in ("email", "id", "telegram_id"):
                value = str(auth_user.get(key) or "").strip()
                if value and value not in seeds:
                    seeds.append(value)
        if self.auth and user_id:
            try:
                record = self.auth.get_user_by_id(user_id) or {}
            except Exception:
                record = {}
            for key in ("email", "id", "telegram_id"):
                value = str(record.get(key) or "").strip()
                if value and value not in seeds:
                    seeds.append(value)
        return seeds

    def _handle_verify_token(self):
        data = self._read_json_body()
        if not data:
            self._send_json({"success": False, "error": "JSON payload required"}, status=400)
            return

        user_id = str(data.get("user_id", "")).strip()
        token = str(data.get("token", "")).strip()

        if not user_id or not token:
            self._send_json({"success": False, "error": "user_id and token required"}, status=400)
            return

        # Bound online guessing: the code is ~50 bits of HMAC, and this keeps the
        # remaining search space off the table cheaply.
        if self._rate_limited("verify_token", MAX_VERIFY_TOKEN_ATTEMPTS, VERIFY_TOKEN_LOCKOUT_SECONDS):
            self._send_json({"success": False, "error": "Too many attempts. Try again later."}, status=429)
            return

        if verify_unlock_token(token, self._unlock_seed_candidates(user_id)):
            if self.storage and hasattr(self.storage, "verify_user"):
                self.storage.verify_user(user_id)
            registry.verify(user_id)
            self._clear_rate_limit("verify_token")
            self._send_json({"success": True, "user_id": user_id, "verified": True})
        else:
            self._send_json({"success": False, "error": "Invalid or expired verification token"}, status=403)

    def _handle_picks(self, parsed: urllib.parse.ParseResult):
        qs = urllib.parse.parse_qs(parsed.query)
        user_id = qs.get("user_id", [""])[0].strip()
        requested_tier = qs.get("tier", ["free"])[0].strip().lower()

        auth_user, _ = self._get_current_user_and_session()
        is_ver = False
        tier = "free"

        if auth_user:
            user_id = auth_user["id"]
            if auth_user.get("telegram_verified"):
                is_ver = True
            is_admin = auth_user.get("tier") == "admin" or (self.bot and self.bot.is_admin(str(auth_user.get("telegram_id", ""))))
            if is_admin:
                tier = requested_tier if requested_tier in ("free", "tier1", "tier2", "tier3") else "tier3"
            else:
                tier = auth_user.get("tier", "free")
        elif user_id.startswith("user_seed_") or user_id.startswith("test_"):
            tier = requested_tier
        else:
            tier = "free"

        if not is_ver and user_id:
            if self.storage and hasattr(self.storage, "is_user_verified"):
                is_ver = self.storage.is_user_verified(user_id)
            if not is_ver:
                is_ver = registry.is_verified(user_id)

        # 1. Pending picks come from the ledger only. There is no static-file
        # fallback: an empty ledger means "no live picks yet", not demo data.
        raw_picks: list[dict] = []
        if self.storage:
            raw_picks = self.storage.list_pending_picks()

        processed_picks = []
        for idx, pick in enumerate(raw_picks):
            p = dict(pick)
            tier_level = p.get("tier_level")

            if idx == 0:
                p["is_locked"] = False
                p["tier_level"] = "FREE"
            elif idx == 1:
                p["tier_level"] = "TELEGRAM_UNLOCK"
                if is_ver or tier in ("tier1", "tier2", "tier3", "all"):
                    p["is_locked"] = False
                else:
                    p["is_locked"] = True
                    p["outcome_name"] = "🔒 Join Telegram to Unlock Match #2"
                    p["best_odds"] = None
                    p["fair_odds"] = None
                    p["best_ev"] = None
                    p["gauge_text"] = "Telegram Unlock Required"
                    p["booking_codes"] = {}
                    p["deep_links"] = {}
            elif idx in (2, 3, 4):
                p["tier_level"] = "TIER_1"
                if tier in ("tier1", "tier2", "tier3", "all"):
                    p["is_locked"] = False
                else:
                    p["is_locked"] = True
                    p["outcome_name"] = "🔒 Sharp Starter (Tier 1 Required)"
                    p["best_odds"] = None
                    p["fair_odds"] = None
                    p["best_ev"] = None
                    p["gauge_text"] = "Tier 1 Subscription Required"
                    p["booking_codes"] = {}
                    p["deep_links"] = {}
            else:
                p["tier_level"] = tier_level or "TIER_2"
                if tier in ("tier2", "tier3", "all"):
                    p["is_locked"] = False
                else:
                    p["is_locked"] = True
                    p["outcome_name"] = "🔒 Pro Trader (Tier 2 Required)"
                    p["best_odds"] = None
                    p["fair_odds"] = None
                    p["best_ev"] = None
                    p["gauge_text"] = "Tier 2 Subscription Required"
                    p["booking_codes"] = {}
                    p["deep_links"] = {}

            processed_picks.append(p)

        self._send_json({
            "active_picks": processed_picks,
            "count": len(processed_picks),
            "user_id": user_id,
            "is_telegram_verified": is_ver,
            "tier": tier,
            "is_authenticated": auth_user is not None,
        })

    def _handle_ledger(self, parsed: urllib.parse.ParseResult):
        settled: list[dict] = []
        if self.storage:
            settled = self.storage.list_settled_picks()

        if not settled:
            dash_file = Path(getattr(self.server, "web_dir", Path("web"))) / "data" / "dashboard.json"
            if dash_file.exists():
                try:
                    with open(dash_file, "r", encoding="utf-8") as f:
                        dash_data = json.load(f)
                        settled = dash_data.get("settled_ledger", [])
                except Exception:
                    pass

        self._send_json({
            "settled_ledger": settled,
            "count": len(settled),
        })


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server with SO_REUSEADDR enabled for instant restart without port binding errors."""
    allow_reuse_address = True
    daemon_threads = True


def make_production_server(
    host: str = "0.0.0.0",
    port: int = 8080,
    web_dir: str = "web",
    storage: Optional[Storage] = None,
    settings: Optional[cfg.Settings] = None,
    bot: Optional[TelegramBot] = None,
    auth: Optional[AuthManager] = None,
) -> ReusableThreadingHTTPServer:
    """Factory creating and configuring the multi-threaded production server."""
    server = ReusableThreadingHTTPServer((host, port), LISAProductionHandler)
    server.web_dir = Path(web_dir).resolve()
    server.storage = storage or SqliteStorage()
    server.auth = auth or AuthManager(storage=server.storage if isinstance(server.storage, SqliteStorage) else None)
    server.settings = settings or cfg.load_settings()
    server.bot = bot
    server.start_time = time.time()
    return server
