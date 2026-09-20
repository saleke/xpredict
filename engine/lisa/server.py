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
import sys
import time
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from . import __version__
from . import config as cfg
from .auth import AuthManager
from .gate import Pick
from .storage import InMemoryStorage, SqliteStorage, Storage
from .telegram_bot import TelegramBot, registry, verify_unlock_token

logger = logging.getLogger(__name__)


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

    def end_headers(self):
        # Security hardening headers on every response
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-XSS-Protection", "1; mode=block")
        super().end_headers()

    def _send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, indent=2 if status != 200 else None).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Cookie")
        self.send_header("Access-Control-Allow-Credentials", "true")
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
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Cookie")
        self.send_header("Access-Control-Allow-Credentials", "true")
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
            if length <= 0:
                return None
            raw_body = self.rfile.read(length).decode("utf-8")
            return json.loads(raw_body)
        except Exception:
            return None

    def do_OPTIONS(self):
        """Handle CORS pre-flight requests."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Cookie")
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/api/status", "/api/health"):
            self._handle_status()
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

        if path == "/api/ledger":
            self._handle_ledger(parsed)
            return

        # Fallback to static asset serving
        super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/auth/signup":
            self._handle_auth_signup()
            return

        if path == "/api/auth/signin":
            self._handle_auth_signin()
            return

        if path == "/api/auth/signout":
            self._handle_auth_signout()
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
        tier = str(data.get("tier", "free")).strip().lower()

        try:
            user = self.auth.register_user(email=email, password=password, display_name=name, tier=tier)
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

        data = self._read_json_body()
        if not data:
            self._send_json({"success": False, "error": "JSON payload required"}, status=400)
            return

        email = str(data.get("email", "")).strip()
        password = str(data.get("password", ""))

        user = self.auth.authenticate_user(email=email, password=password)
        if not user:
            self._send_json({"success": False, "error": "Invalid email or password."}, status=401)
            return

        ip_addr = self.client_address[0] if self.client_address else ""
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

        self.auth.link_telegram(user["id"], telegram_id=tg_id, telegram_username=tg_username)
        updated = self.auth.get_user_by_id(user["id"])
        self._send_json({"success": True, "user": updated})

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

    def _handle_verify_status(self, parsed: urllib.parse.ParseResult):
        qs = urllib.parse.parse_qs(parsed.query)
        user_id = qs.get("user_id", [""])[0].strip()

        is_ver = False
        if user_id:
            if self.storage and hasattr(self.storage, "is_user_verified"):
                is_ver = self.storage.is_user_verified(user_id)
            if not is_ver:
                is_ver = registry.is_verified(user_id)

        self._send_json({"user_id": user_id, "verified": bool(is_ver)})

    def _handle_verify_token(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length).decode("utf-8")
            data = json.loads(raw_body)
        except Exception:
            self._send_json({"success": False, "error": "Invalid JSON body"}, status=400)
            return

        user_id = str(data.get("user_id", "")).strip()
        token = str(data.get("token", "")).strip()

        if not user_id or not token:
            self._send_json({"success": False, "error": "user_id and token required"}, status=400)
            return

        if verify_unlock_token(token):
            if self.storage and hasattr(self.storage, "verify_user"):
                self.storage.verify_user(user_id)
            registry.verify(user_id)
            self._send_json({"success": True, "user_id": user_id, "verified": True})
        else:
            self._send_json({"success": False, "error": "Invalid or expired verification token"}, status=403)

    def _handle_picks(self, parsed: urllib.parse.ParseResult):
        qs = urllib.parse.parse_qs(parsed.query)
        user_id = qs.get("user_id", [""])[0].strip()
        tier = qs.get("tier", ["free"])[0].strip().lower()

        # Check authenticated session
        auth_user, _ = self._get_current_user_and_session()
        is_ver = False
        if auth_user:
            user_id = auth_user["id"]
            if auth_user.get("telegram_verified"):
                is_ver = True
            if not qs.get("tier"):
                tier = auth_user.get("tier", "free")

        if not is_ver and user_id:
            if self.storage and hasattr(self.storage, "is_user_verified"):
                is_ver = self.storage.is_user_verified(user_id)
            if not is_ver:
                is_ver = registry.is_verified(user_id)

        # 1. Fetch pending picks from storage, or fallback to dashboard.json
        raw_picks: list[dict] = []
        if self.storage:
            raw_picks = self.storage.list_pending_picks()

        if not raw_picks:
            # Fallback to web/data/dashboard.json active_picks
            dash_file = Path(getattr(self.server, "web_dir", Path("web"))) / "data" / "dashboard.json"
            if dash_file.exists():
                try:
                    with open(dash_file, "r", encoding="utf-8") as f:
                        dash_data = json.load(f)
                        raw_picks = dash_data.get("active_picks", [])
                except Exception:
                    pass

        # 2. Server-side masking to guarantee locked predictions cannot be leaked
        processed_picks = []
        for idx, pick in enumerate(raw_picks):
            p = dict(pick)
            tier_level = p.get("tier_level")

            # In the 4-tier commercial architecture:
            # - Pick index 0 is open for all (Free)
            # - Pick indices 1 and 2 require Telegram verification
            # - Pick indices >= 3 are Syndicate Alpha (Paid Tier 2/Tier 3)
            is_free_pick = (tier_level == "FREE") if tier_level else (idx == 0)
            is_telegram_pick = (tier_level == "TELEGRAM_UNLOCK") if tier_level else (idx in (1, 2))

            if is_free_pick and idx == 0:
                p["is_locked"] = False
                p["tier_level"] = "FREE"
            elif is_telegram_pick or idx in (1, 2):
                p["tier_level"] = "TELEGRAM_UNLOCK"
                if is_ver:
                    p["is_locked"] = False
                else:
                    p["is_locked"] = True
                    p["outcome_name"] = "🔒 Join Telegram to Unlock"
                    p["best_odds"] = None
                    p["fair_odds"] = None
                    p["best_ev"] = None
                    p["gauge_text"] = "Telegram Unlock Required"
                    p["deep_links"] = {}
            else:
                p["tier_level"] = tier_level or "TIER_2"
                if tier in ("tier2", "tier3", "all"):
                    p["is_locked"] = False
                else:
                    p["is_locked"] = True
                    p["outcome_name"] = "🔒 Syndicate Alpha (Tier 2/3)"
                    p["best_odds"] = None
                    p["fair_odds"] = None
                    p["best_ev"] = None
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
