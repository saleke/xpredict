"""Admin console API.

Everything under ``/api/admin`` lives here rather than in ``server.py``. The
main server already carries the public product routes plus the static handler;
folding twenty more endpoints into it makes the security review of the public
surface harder rather than easier. And this module is exercisable without
binding a socket, which is what makes the authorisation matrix testable at all.

The checks run in this order, and each one is a reason on its own to refuse:

1. **Identity.** A session cookie validated against the ``sessions`` table.
   The id is never accepted from a query string. It is a *separate* cookie from
   the product's ``lisa_session`` so the two can differ in lifetime, SameSite
   and revocation without changing what the user-facing app sees.
2. **Role.** ``owner``/``admin``/``viewer``, resolved from the account's tier
   and its linked Telegram id on *every* request. Resolving per request rather
   than baking it into the session means demoting someone takes effect at once
   instead of whenever their cookie happens to expire.
3. **Permission.** Each route declares ``read``/``write``/``danger``. ``danger``
   additionally requires ``confirm: true`` in the body, so an irreversible
   action cannot be fired by a replayed request.
4. **CSRF.** An HMAC of the session id under a deployment secret. The browser
   cannot read it cross-origin and it is useless without the cookie, so neither
   half is sufficient alone.
5. **Rate limit.** Per-IP on sign-in, per-identity on writes.
6. **Audit.** Every mutation is recorded in ``admin_audit_logs`` with anything
   credential-shaped stripped out of the detail payload.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
from typing import Any, Callable, Optional

from . import __version__
from . import config as cfg
from .runtime import READONLY_FIELDS, OverrideError, RuntimeConfig
from .storage import AdminReadModelUnavailable, RelationalStorage

logger = logging.getLogger(__name__)

ADMIN_PREFIX = "/api/admin"
ADMIN_COOKIE = "lisa_admin"

#: Short by design. An operator console left open on a shared machine should
#: stop being a credential long before a 30-day product session would.
ADMIN_SESSION_TTL_SECONDS = 8 * 60 * 60

LOGIN_ATTEMPT_LIMIT = 5
LOGIN_WINDOW_SECONDS = 15 * 60
#: Per-identity write budget, so a runaway script cannot hammer the API.
WRITE_LIMIT = 240
WRITE_WINDOW_SECONDS = 60

READ = "read"
WRITE = "write"
DANGER = "danger"

#: Telemetry row holding the generated CSRF secret. Persisted so operator
#: sessions survive a restart instead of being silently invalidated by one.
CSRF_SECRET_KEY = "admin:csrf_secret"

_SPLIT = re.compile(r"[,\s]+")
_TRUE = ("1", "true", "yes", "on")


class AdminError(Exception):
    """A refusal that should become an HTTP response."""

    def __init__(self, status: int, message: str, *, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail


def _env_tuple(name: str) -> tuple[str, ...]:
    return tuple(p for p in _SPLIT.split(os.environ.get(name, "")) if p)


def _is_secret_key(name: str) -> bool:
    lowered = name.lower()
    return any(h in lowered for h in
               ("token", "secret", "password", "api_key", "key", "dsn",
                "url", "webhook"))


def scrub(value: Any, *, depth: int = 0) -> Any:
    """Strip anything credential-shaped out of a payload destined for a log.

    Applied to every audit detail. A settings diff is the most likely thing to
    carry a token by accident, and an audit log is the last place anyone would
    think to look for one.
    """
    if depth > 4:
        return "<truncated>"
    if isinstance(value, dict):
        return {str(k): ("<redacted>" if _is_secret_key(str(k))
                         else scrub(v, depth=depth + 1))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, depth=depth + 1) for v in list(value)[:50]]
    if isinstance(value, str) and len(value) > 300:
        return value[:300] + "..."
    return value


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_ts(value: Optional[float]) -> Optional[str]:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def _first(query: dict[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    return values[0] if values else default


def _search_term(query: dict[str, list[str]]) -> str:
    """The free-text filter, accepted under either a short or a readable name.

    ``q`` is the documented name and ``search`` is accepted as an alias because
    it is the one a reader of the console would reach for first. Both are read,
    with ``q`` winning, so a UI can migrate to the long name without breaking
    any bookmarked URL.
    """
    return _first(query, "q") or _first(query, "search")


def operator_role(user: Optional[dict]) -> Optional[str]:
    """Canonical operator/owner resolution for an account.

    This is the single source of truth, shared by the admin console and the
    public API, so the two can never disagree about who is an operator.

    ``owner`` is granted only by an explicit email allowlist: it is the one
    control that has to survive a compromised Telegram bot, and the only one
    that survives a tier change. ``admin`` is granted by the account's own
    tier or a linked Telegram id in the operator allowlist, matching how the
    bot already gates its ``admin:`` commands.

    Kept as a free function (not a method) so callers that have no control
    plane -- the public site, the HTTP handler -- can still resolve a role
    without depending on the console being wired up.
    """
    if not user:
        return None
    email = str(user.get("email") or "").strip().lower()
    if email and email in {e.lower() for e in _env_tuple("LISA_ADMIN_EMAILS")}:
        return "owner"
    telegram_id = str(user.get("telegram_id") or "").strip()
    if telegram_id and telegram_id in _env_tuple("ADMIN_TELEGRAM_IDS"):
        return "admin"
    if str(user.get("tier") or "") == "admin":
        return "admin"
    return None


class AdminAPI:
    """Request handling for the console. One instance per server."""

    def __init__(self, control: Any):
        self.control = control
        self._login_buckets: dict[str, list[float]] = {}
        self._write_buckets: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._csrf_secret: Optional[bytes] = None
        self._cycle_lock = threading.Lock()

    # -- collaborators -----------------------------------------------------

    @property
    def storage(self):
        return self.control.storage

    @property
    def auth(self):
        return self.control.auth

    @property
    def runtime(self) -> Optional[RuntimeConfig]:
        return self.control.runtime

    def settings(self) -> cfg.Settings:
        rt = self.runtime
        return rt.settings() if rt is not None else cfg.load_settings()

    # -- csrf --------------------------------------------------------------

    def _csrf_secret_bytes(self) -> bytes:
        if self._csrf_secret is not None:
            return self._csrf_secret
        secret: Any = None
        storage = self.storage
        if isinstance(storage, RelationalStorage):
            # Concurrent cold starts must select the same persisted secret.
            # Read-then-upsert allowed two instances to cache different secrets.
            candidate = secrets.token_urlsafe(48)
            with storage._tx() as conn:
                conn.execute('INSERT INTO system_telemetry VALUES (?, ?, ?) '
                    'ON CONFLICT(key) DO NOTHING',
                    (CSRF_SECRET_KEY, json.dumps(candidate), datetime.now(timezone.utc).isoformat()))
                row = conn.execute('SELECT val_json FROM system_telemetry WHERE key=?',
                                   (CSRF_SECRET_KEY,)).fetchone()
                secret = json.loads(row['val_json'])
            if not isinstance(secret, str) or len(secret) < 32:
                raise AdminError(503, 'Admin session protection is misconfigured')
            self._csrf_secret = secret.encode('utf-8')
            return self._csrf_secret
        if storage is not None and hasattr(storage, "admin_get_telemetry"):
            try:
                for row in storage.admin_get_telemetry():
                    if row.get("key") == CSRF_SECRET_KEY:
                        secret = row.get("value")
                        break
            except Exception:
                secret = None
        if not isinstance(secret, str) or len(secret) < 32:
            secret = secrets.token_urlsafe(48)
            if storage is not None and hasattr(storage, "admin_set_telemetry"):
                try:
                    storage.admin_set_telemetry(CSRF_SECRET_KEY, secret)
                except Exception:
                    logger.warning("admin CSRF secret could not be persisted")
        self._csrf_secret = secret.encode("utf-8")
        return self._csrf_secret

    def csrf_token(self, session_id: str) -> str:
        return hmac.new(self._csrf_secret_bytes(), session_id.encode("utf-8"),
                        hashlib.sha256).hexdigest()

    # -- audit -------------------------------------------------------------

    def audit(self, admin_id: str, action: str, target: str = "",
              details: Any = None) -> None:
        if self.storage is None or not hasattr(self.storage, "log_admin_action"):
            return
        try:
            payload = json.dumps(scrub(details if details is not None else {}),
                                 default=str)
        except (TypeError, ValueError):
            payload = "{}"
        try:
            self.storage.log_admin_action(str(admin_id), action,
                                          str(target)[:200], payload)
        except Exception:
            logger.exception("admin audit write failed")

    # -- identity ----------------------------------------------------------

    @staticmethod
    def _cookie(handler, name: str) -> Optional[str]:
        raw = handler.headers.get("Cookie", "") or ""
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith(name + "="):
                return part.split("=", 1)[1].strip() or None
        return None

    def _session_id(self, handler) -> Optional[str]:
        sid = self._cookie(handler, ADMIN_COOKIE)
        if sid:
            return sid
        header = (handler.headers.get("Authorization") or "").strip()
        if header.startswith("Bearer "):
            return header[7:].strip() or None
        return None

    def role_for(self, user: Optional[dict]) -> Optional[str]:
        """Resolve a role, or None when the account may not use the console."""
        return operator_role(user)

    def identify(self, handler) -> tuple[Optional[dict], Optional[dict], Optional[str]]:
        """Return ``(user, session, role)``. Any None means not permitted."""
        sid = self._session_id(handler)
        if not sid or self.auth is None:
            return None, None, None
        try:
            data = self.auth.validate_session(sid)
        except Exception:
            return None, None, None
        if not data:
            return None, None, None
        user = data.get("user") or {}
        role = self.role_for(user)
        if role is None:
            return None, None, None
        return user, data, role

    # -- request guards ----------------------------------------------------

    def _check_origin(self, handler) -> None:
        """Refuse a state-changing request whose Origin is not this deployment.

        Secondary to the CSRF token. It exists to catch a page on an allowlisted
        origin posting from somewhere else, which the token would also block but
        less obviously.
        """
        origin = (handler.headers.get("Origin") or "").strip().rstrip("/")
        if not origin:
            return
        host = (handler.headers.get("Host") or "").strip()
        if not host:
            return
        if origin in _server_allowed_origins():
            return
        try:
            origin_host = urlparse(origin).netloc
        except ValueError:
            raise AdminError(403, "Origin could not be verified")
        if origin_host and origin_host == host:
            return
        raise AdminError(403, "Cross-origin admin request refused")

    def _check_csrf(self, handler, session_id: str) -> None:
        supplied = (handler.headers.get("X-CSRF-Token") or "").strip()
        if not supplied:
            raise AdminError(403, "Missing CSRF token")
        if not hmac.compare_digest(supplied, self.csrf_token(session_id)):
            raise AdminError(403, "Invalid CSRF token")

    def _rate(self, buckets: dict[str, list[float]], key: str, limit: int,
              window: int) -> bool:
        now = time.time()
        with self._lock:
            hits = [t for t in buckets.get(key, []) if now - t < window]
            if len(hits) >= limit:
                buckets[key] = hits
                return True
            hits.append(now)
            buckets[key] = hits
            # Drop keys that can no longer be limited, so a long-lived process
            # does not accumulate one entry per source IP forever.
            if len(buckets) > 4096:
                for stale in [k for k, v in buckets.items()
                              if not any(now - t < window for t in v)]:
                    buckets.pop(stale, None)
            return False

    @staticmethod
    def client_ip(handler) -> str:
        try:
            return handler.client_address[0]
        except (TypeError, IndexError):
            return "unknown"

    @staticmethod
    def _queue_cookie(handler, value: str, max_age: int) -> None:
        """Register a Set-Cookie header for the response about to be sent.

        Scoped to ``/api/admin`` so the console's short-lived session is not
        sent on every product request, and Strict so it never rides along on a
        cross-site navigation.
        """
        parts = [f"{ADMIN_COOKIE}={value}", "Path=/api/admin",
                 f"Max-Age={int(max_age)}", "HttpOnly", "SameSite=Strict"]
        if _cookie_secure_enabled(handler):
            parts.append("Secure")
        pending = getattr(handler, "_pending_cookies", None)
        if pending is None:
            pending = []
            setattr(handler, "_pending_cookies", pending)
        pending.append("; ".join(parts))

    # -- dispatch ----------------------------------------------------------

    def resolve(self, method: str, path: str
                ) -> Optional[tuple[Callable, str, bool, dict[str, str]]]:
        """Find the handler for a method/path pair.

        Returns ``(handler, permission, is_public, path_params)``. Path
        parameters use ``{name}`` segments and are matched segment by segment,
        so a rule like ``/picks/{key}`` cannot accidentally match ``/picks/stats``
        -- which is a real route of its own.
        """
        if not path.startswith(ADMIN_PREFIX):
            return None
        rest = path[len(ADMIN_PREFIX):].strip("/")
        wanted = [seg for seg in rest.split("/") if seg]
        for (route_method, route_path), (func, permission, public) in _ROUTES.items():
            if route_method != method:
                continue
            pattern = [seg for seg in route_path.strip("/").split("/") if seg]
            params = _match_segments(pattern, wanted)
            if params is None:
                continue
            return func, permission, public, params
        return None

    def handle(self, handler, method: str, path: str,
               query: dict[str, list[str]], body: Optional[dict]) -> bool:
        """Handle an admin request. True when it has been answered here."""
        entry = self.resolve(method, path)
        if entry is None:
            if path.startswith(ADMIN_PREFIX):
                handler._send_json(
                    {"success": False, "error": "Unknown admin endpoint"}, status=404)
                return True
            return False
        func, permission, public, params = entry
        try:
            if public:
                result = func(self, handler, query, body or {}, None, None, params)
            else:
                user, session, role = self.identify(handler)
                if user is None:
                    raise AdminError(401, "Admin sign-in required")
                if permission != READ:
                    self._check_origin(handler)
                    self._check_csrf(
                        handler, (session or {}).get("session_id") or "")
                    ident = f"{role}:{user.get('id')}"
                    if self._rate(self._write_buckets, ident, WRITE_LIMIT,
                                  WRITE_WINDOW_SECONDS):
                        raise AdminError(429, "Too many admin writes; slow down")
                if permission == DANGER:
                    # Owner-only is enforced here rather than per route, so a
                    # route added later cannot accidentally ship a destructive
                    # action to the lower role: DANGER implies owner.
                    if role != "owner":
                        raise AdminError(403, "This action requires the owner role")
                    if not (body or {}).get("confirm"):
                        raise AdminError(400, "This action is irreversible; "
                                             "send confirm: true to proceed")
                elif func.__name__ in _OWNER_ONLY and role != "owner":
                    raise AdminError(403, "This action requires the owner role")
                result = func(self, handler, query, body or {}, user, role, params)
            if result is not None and not getattr(handler, "_admin_answered", False):
                handler._send_json(result, status=200)
        except AdminError as exc:
            handler._send_json({"success": False, "error": exc.message,
                                "detail": scrub(exc.detail) if exc.detail else None},
                               status=exc.status)
        except AdminReadModelUnavailable as exc:
            # The storage driver is real but has no console analytics. That is a
            # configuration problem, not a bug, and the operator needs to know
            # which driver is in use to fix it.
            handler._send_json(
                {"success": False, "error": "Storage driver does not support this view",
                 "detail": scrub(str(exc))}, status=503)
        except Exception as exc:  # a traceback must never reach the client
            logger.exception("admin request failed: %s %s", method, path)
            handler._send_json({"success": False,
                                "error": "Internal error handling admin request",
                                "detail": type(exc).__name__}, status=500)
        return True

    # -- session -----------------------------------------------------------

    def session(self, handler, query, body, user, role, params):
        """Report whether the caller already holds a valid operator session.

        Public on purpose: the console must be able to ask "am I signed in?"
        before it has one, and the answer is only ever the caller's own identity
        plus a yes/no. The CSRF token is returned so a page reload can resume a
        live session without a second credential round trip.
        """
        found, session, resolved = self.identify(handler)
        if not found:
            return {"authenticated": False, "role": None, "csrf_token": None}
        sid = (session or {}).get("session_id") or ""
        return {
            "authenticated": True,
            "role": resolved,
            "csrf_token": self.csrf_token(sid),
            "expires_at": (session or {}).get("expires_at"),
            "user": {"id": found.get("id"), "email": found.get("email"),
                     "display_name": found.get("display_name"),
                     "tier": found.get("tier"),
                     "telegram_id": found.get("telegram_id")},
        }

    def login(self, handler, query, body, user, role, params):
        email = str(body.get("email") or "").strip().lower()
        password = str(body.get("password") or "")
        ip = self.client_ip(handler)
        if self._rate(self._login_buckets, ip, LOGIN_ATTEMPT_LIMIT,
                      LOGIN_WINDOW_SECONDS):
            raise AdminError(429, "Too many sign-in attempts. Try again later.")
        if not email or not password:
            raise AdminError(400, "Email and password are required")
        if self.auth is None:
            raise AdminError(503, "Authentication backend unavailable")

        account = self.auth.authenticate_user(email, password)
        # A wrong password and a non-operator account are reported identically
        # so the endpoint cannot be used to enumerate which addresses exist.
        if not account or self.role_for(account) is None:
            self.audit("anonymous", "admin.login_denied", email,
                       {"reason": "bad_credentials_or_not_an_operator"})
            raise AdminError(401,
                             "Invalid credentials, or the account is not an operator")

        ttl = int(body.get("ttl_seconds") or ADMIN_SESSION_TTL_SECONDS)
        # An operator cannot extend their own session beyond the cap by asking.
        ttl = max(300, min(ttl, ADMIN_SESSION_TTL_SECONDS))
        session = self.auth.create_session(account["id"], ttl_seconds=ttl)
        sid = session["session_id"]
        with self._lock:
            self._login_buckets.pop(ip, None)
        resolved = self.role_for(account)
        self._queue_cookie(handler, sid, ttl)
        self.audit(account["id"], "admin.login", account["email"],
                   {"role": resolved, "ttl": ttl})
        return {
            "success": True,
            "role": resolved,
            "csrf_token": self.csrf_token(sid),
            "expires_at": session.get("expires_at"),
            "user": {"id": account["id"], "email": account["email"],
                     "display_name": account.get("display_name"),
                     "tier": account.get("tier")},
        }

    def logout(self, handler, query, body, user, role, params):
        if self.auth is not None:
            try:
                self.auth.revoke_session(self._session_id(handler) or "")
            except Exception:
                logger.warning("session revoke failed during admin logout")
        self._queue_cookie(handler, "", 0)
        self.audit((user or {}).get("id", "?"), "admin.logout")
        return {"success": True}

    # -- overview and health -----------------------------------------------

    def overview(self, handler, query, body, user, role, params):
        control = self.control
        settings = self.settings()
        storage = self.storage
        counts: Any = {}
        if storage is not None and hasattr(storage, "count_picks"):
            try:
                counts = storage.count_picks()
            except Exception as exc:
                counts = {"error": type(exc).__name__}
        scheduler = control.scheduler
        sched: dict[str, Any] = {"present": scheduler is not None}
        if scheduler is not None and hasattr(scheduler, "status"):
            sched.update(scheduler.status())
        elif scheduler is not None:
            stats = scheduler.stats
            sched.update({
                "ticks": stats.ticks, "cycles_run": stats.cycles_run,
                "settlements_run": stats.settlements_run,
                "cycles_skipped": stats.cycles_skipped,
                "total_errors": stats.total_errors,
                "next_cycle_at": _iso(scheduler.next_cycle_at),
                "next_settle_at": _iso(scheduler.next_settle_at),
                "tracked_fixtures": len(getattr(scheduler, "_commences", ())),
            })
        pool: dict[str, Any] = {"present": False}
        client = control.client
        if client is not None:
            pool["present"] = True
            try:
                pool["credits_remaining"] = client.remaining_credits
            except Exception:
                pass
            inner = getattr(client, "pool", None)
            if inner is not None and hasattr(inner, "status"):
                try:
                    pool.update(inner.status().to_dict())
                    pool["size"] = len(list(getattr(inner, "_states", ())))
                except Exception:
                    logger.warning("key pool status unavailable")
            # The console reads a small, stable surface regardless of how the
            # pool reports internally.
            pool.setdefault("remaining_credits", pool.get("credits_remaining")
                            or pool.get("total_remaining"))
            pool.setdefault("budget_daily", pool.get("daily_budget")
                            or getattr(inner, "budget_daily", 0)
                            if inner is not None else 0)
        paused = False
        if storage is not None and hasattr(storage, "is_system_paused"):
            try:
                paused = storage.is_system_paused()
            except Exception:
                pass
        rt = self.runtime
        stats: Any = {}
        if storage is not None and hasattr(storage, "admin_pick_stats"):
            try:
                stats = storage.admin_pick_stats()
            except Exception:
                logger.warning("overview pick stats unavailable")
        return {
            "version": __version__,
            "role": role,
            "pid": os.getpid(),
            "uptime_seconds": round(control.uptime_seconds(), 1),
            "started_at": _iso(control.started_at),
            "paused": paused,
            "stats": stats,
            "ledger_counts": counts,
            "scheduler": sched,
            "odds_pool": pool,
            "settings": {
                "sports_count": len(settings.sports),
                "sports": list(settings.sports),
                "markets": settings.markets,
                "regions": settings.regions,
                "cadence_prematch_sec": settings.cadence_prematch_sec,
                "cadence_live_sec": settings.cadence_live_sec,
                "cadence_settle_sec": settings.cadence_settle_sec,
                "credit_budget_daily": settings.credit_budget_daily,
                "forecast_horizon_hours": settings.forecast_horizon_hours,
                "forecast_min_matches": settings.forecast_min_matches,
                "forecast_max_matches": settings.forecast_max_matches,
                "enable_inplay": settings.enable_inplay,
                "enable_extra_markets": settings.enable_extra_markets,
                "enable_micro_predictions": settings.enable_micro_predictions,
                "require_positive_ev": settings.require_positive_ev,
                "inplay_max_leagues": settings.inplay_max_leagues,
            },
            "settings_revision": rt.revision if rt else 0,
            "overrides": rt.overrides() if rt else {},
            "capabilities": {
                "telegram_configured": bool(settings.telegram_token),
                "odds_keys": len(settings.odds_api_keys or ()),
                "storage_driver": settings.storage_driver,
                "scheduler_attached": scheduler is not None,
                "runtime_config": rt is not None,
            },
        }

    def health(self, handler, query, body, user, role, params):
        """Is the poller actually reaching the API and the ledger actually moving."""
        control = self.control
        storage = control.storage
        checks: list[dict[str, Any]] = []

        def add(name: str, fn: Callable[[], tuple[bool, Any]]) -> None:
            try:
                ok, detail = fn()
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        def db() -> tuple[bool, Any]:
            info = storage.admin_database_info()
            return bool(info.get("exists")) and info.get("integrity") == "ok", {
                "integrity": info.get("integrity"), "bytes": info.get("bytes"),
                "journal_mode": info.get("journal_mode")}
        add("database", db)

        def credits() -> tuple[bool, Any]:
            remaining = control.client.remaining_credits if control.client else None
            return remaining is None or remaining > 0, {"remaining": remaining}
        add("odds_credits", credits)

        def snapshot() -> tuple[bool, Any]:
            cache = storage.admin_live_cache_overview()
            return cache.get("live", 0) > 0, {"live": cache.get("live"),
                                              "stale": cache.get("stale")}
        add("odds_snapshot", snapshot)

        def ledger() -> tuple[bool, Any]:
            return True, storage.count_picks()
        add("ledger", ledger)

        def scheduler_check() -> tuple[bool, Any]:
            present = control.scheduler is not None
            detail: dict[str, Any] = {"present": present}
            if present and hasattr(control.scheduler, "status"):
                detail.update(control.scheduler.status())
                return detail["ready"], detail
            if present:
                detail["ticks"] = control.scheduler.stats.ticks
                detail["last_error_count"] = control.scheduler.stats.total_errors
            return present, detail
        add("scheduler", scheduler_check)

        return {"healthy": all(c["ok"] for c in checks), "checks": checks,
                "uptime_seconds": round(control.uptime_seconds(), 1),
                "checked_at": _utcnow_iso()}

    # -- keys --------------------------------------------------------------

    def keys(self, handler, query, body, user, role, params):
        client = self.control.client
        pool = getattr(client, "pool", None) if client else None
        if pool is None or not hasattr(pool, "status"):
            return {"configured": False, "keys": [], "note":
                    "No key pool is attached; the poller is not using rotation."}
        status = pool.status().to_dict()
        rows = []
        active_index: Optional[int] = None
        for i, state in enumerate(getattr(pool, "_states", ())):
            # KeyState.label() is already masked; the raw key is never read here.
            # The active key is the first one rotation would call now: not on
            # cooldown, and either unused (remaining unknown) or with credits
            # left. An exhausted key (remaining == 0) is skipped.
            available = not (state.disabled_until and state.disabled_until > time.time())
            has_credit = state.remaining is None or state.remaining > 0
            if active_index is None and available and has_credit:
                active_index = i
            rows.append({
                "index": i,
                "label": state.label(),
                "remaining": state.remaining,
                "used": state.used,
                "spent_today": state.spent_today,
                "budget_day": state.budget_day,
                # Aliases matching the console's column model, so the UI does
                # not have to know the pool's internal names.
                "requests_today": state.requests,
                "budget_daily": state.budget_day,
                "requests": state.requests,
                "errors": state.errors,
                "last_used_at": _iso_ts(state.last_used_at),
                "disabled_until": _iso_ts(state.disabled_until),
                "cooldown_until": _iso_ts(state.disabled_until),
                "cooldown_active": bool(state.disabled_until
                                        and state.disabled_until > time.time()),
            })
        if active_index is None:
            active_index = 0 if rows else None
        return {
            "configured": True,
            "credits_remaining": status.get("total_remaining"),
            "any_exhausted": bool(status.get("state") in ("exhausted", "paused")),
            "active_index": active_index,
            "keys": rows,
            "note": None,
        }

    def key_cooldown(self, handler, query, body, user, role, params):
        """Park one key for a while so rotation uses its siblings.

        Deliberately narrow: reacting to a key that is being rate limited or
        looks compromised is the legitimate use. It should not double as a way
        to reshuffle the pool arbitrarily.

        The console sends ``label`` plus ``cooldown_seconds``. ``index`` and
        ``minutes`` are accepted as aliases so an older client — or a hand-typed
        request — cannot silently no-op: both are translated into the same
        ``label``/seconds resolution below.
        """
        label = str(body.get("label") or "").strip()
        pool = getattr(self.control.client, "pool", None) if self.control.client else None
        if pool is None:
            raise AdminError(503, "No key pool attached")
        if not label:
            index = body.get("index")
            numeric = (isinstance(index, int) and not isinstance(index, bool)) or \
                (isinstance(index, str) and index.strip().isdigit())
            if numeric:
                states = list(getattr(pool, "_states", ()))
                i = int(index)
                if 0 <= i < len(states):
                    label = states[i].label()
        if not label:
            raise AdminError(400, "label is required")
        target = next((s for s in getattr(pool, "_states", ())
                       if s.label() == label), None)
        if target is None:
            raise AdminError(404, "No such key label")
        minutes = body.get("minutes")
        if minutes is not None:
            try:
                minutes = float(minutes)
            except (TypeError, ValueError):
                raise AdminError(400, "minutes must be a number")
            params_seconds = int(minutes * 60)
        else:
            try:
                params_seconds = int(body.get("cooldown_seconds") or 900)
            except (TypeError, ValueError):
                raise AdminError(400, "cooldown_seconds must be a number")
        seconds = max(60, min(params_seconds, 86400))
        until = time.time() + seconds
        target.disabled_until = until
        self.audit((user or {}).get("id"), "admin.key.cooldown", label,
                   {"cooldown_seconds": seconds, "until": _iso_ts(until)})
        return {"success": True, "label": label, "cooldown_until": _iso_ts(until)}

    # -- settings ----------------------------------------------------------

    def get_providers(self, handler, query, body, user, role, params):
        from .provider_credentials import credential_store, FIELDS
        settings = self.settings()
        overrides = credential_store(settings, self.storage).read()
        rows = []
        for provider, field in FIELDS.items():
            rows.append({'provider': provider, 'configured': bool(getattr(settings, field)),
                         'configuration_source': 'admin' if field in overrides else 'environment'})
        return {'providers': rows, 'coverage_plan': self.storage.get_telemetry('coverage:plan') or {},
                'settlement_coverage': self.storage.get_telemetry('coverage:settlement') or {},
                'oddspapi_status': self.storage.get_telemetry('provider:oddspapi:status') or {},
                'the_odds_api_status': self.storage.get_telemetry('provider:the_odds_api:status') or {},
                'credential_updates_allowed': role == 'owner'}

    def put_provider(self, handler, query, body, user, role, params):
        from .provider_credentials import credential_store
        if role != 'owner':
            raise AdminError(403, 'Credential changes require the owner role')
        provider = params.get('provider', '')
        operation = body.get('operation', 'replace')
        if operation not in ('replace', 'disable', 'inherit'):
            raise AdminError(400, 'operation must be replace, disable or inherit')
        credential = body.get('credential', '') if operation == 'replace' else ''
        if operation == 'replace' and (not isinstance(credential, str) or not credential.strip()):
            raise AdminError(400, 'A nonempty credential is required')
        try:
            credential_store(self.settings(), self.storage).update(
                provider, credential, inherit=operation == 'inherit')
        except ValueError as exc:
            raise AdminError(400, str(exc)) from None
        if self.runtime is not None:
            self.runtime.invalidate_credentials()
        self.audit((user or {}).get('id'), 'admin.provider.credential', provider,
                   {'operation': operation})
        return {'success': True, 'provider': provider, 'operation': operation,
                'detail': 'Applies on the next job cycle; in-flight requests finish with their previous settings.'}

    def operations(self, handler, query, body, user, role, params):
        from .pilot import pilot_report
        from .observability import FIXTURES_KEY, PERFORMANCE_KEY
        return {'pilot':pilot_report(self.storage,self.settings()),
                'performance':self.storage.get_telemetry(PERFORMANCE_KEY) or {},
                'generation_fixtures':self.storage.get_telemetry(FIXTURES_KEY) or {},
                'settlement_fixtures':self.storage.get_telemetry('settlement:'+FIXTURES_KEY) or {},
                'scheduler':'serverless' if getattr(self.control.server,'jobs',None) else 'worker'}

    def run_paper_job(self, handler, query, body, user, role, params):
        if role != 'owner':
            raise AdminError(403,'Job execution requires the owner role')
        context = self.control.server
        name = params.get('job')
        if not getattr(context,'jobs',None) or name not in context.jobs:
            raise AdminError(400,'Select generation, settlement or history on the serverless deployment')
        from .serverless import run_job
        self.audit(user.get('id'),'admin.paper_job',name)
        return run_job(context,name)

    def get_settings(self, handler, query, body, user, role, params):
        rt = self.runtime
        if rt is None:
            raise AdminError(503, "Runtime configuration unavailable")
        return {
            "fields": rt.describe(),
            "overrides": rt.overrides(),
            "revision": rt.revision,
            "secrets_present": rt.redacted_overview(),
            "readonly_fields": sorted(READONLY_FIELDS),
            "scope_leagues": list(cfg.SCOPE_LEAGUES),
            "effective": _effective_summary(rt.settings()),
        }

    def patch_settings(self, handler, query, body, user, role, params):
        rt = self.runtime
        if rt is None:
            raise AdminError(503, "Runtime configuration unavailable")
        updates = body.get("settings")
        if not isinstance(updates, dict) or not updates:
            raise AdminError(400, "settings must be a non-empty object")
        if len(updates) > 60:
            raise AdminError(400, "too many settings in one request")
        before = rt.overrides()
        try:
            after = rt.apply(updates)
        except OverrideError as exc:
            raise AdminError(400, str(exc))
        self.audit((user or {}).get("id"), "admin.settings.update", "",
                   {"settings": updates,
                    "changed": sorted(set(after) - set(before))})
        return {"success": True, "overrides": after, "revision": rt.revision,
                "effective": _effective_summary(rt.settings())}

    def reset_settings(self, handler, query, body, user, role, params):
        rt = self.runtime
        if rt is None:
            raise AdminError(503, "Runtime configuration unavailable")
        names = body.get("fields")
        if names is not None and not isinstance(names, list):
            raise AdminError(400, "fields must be a list of setting names")
        after = rt.reset([str(n) for n in names] if names is not None else None)
        self.audit((user or {}).get("id"), "admin.settings.reset",
                   ",".join(str(n) for n in (names or [])),
                   {"remaining": sorted(after)})
        return {"success": True, "overrides": after, "revision": rt.revision,
                "effective": _effective_summary(rt.settings())}

    # -- sports ------------------------------------------------------------

    def sports(self, handler, query, body, user, role, params):
        settings = self.settings()
        snapshot: dict[str, int] = {}
        storage = self.storage
        if storage is not None and hasattr(storage, "scan_live_keys"):
            from .dashboard import LIVE_ODDS_PREFIX
            try:
                for key in storage.scan_live_keys():
                    if not key.startswith(LIVE_ODDS_PREFIX):
                        continue
                    parts = key.split(":")
                    sport = parts[2] if len(parts) > 2 else ""
                    if not sport:
                        continue
                    entry = storage.get_live(key)
                    if not entry:
                        continue
                    payload = entry.get("payload") if isinstance(entry, dict) else entry
                    count = len(payload) if isinstance(payload, list) else 0
                    snapshot[sport] = max(snapshot.get(sport, 0), count)
            except Exception as exc:
                logger.warning("sport snapshot scan failed: %r", exc)
        return {
            "configured": list(settings.sports),
            "markets": settings.markets,
            "regions": settings.regions,
            "cost_note": ("1 credit per league per poll for h2h; 3 for "
                          "h2h+spreads+totals on one region; 6 across eu+us. "
                          "The /sports endpoint is free."),
            "leagues": [{"key": key, "configured": key in settings.sports,
                         "events_cached": snapshot.get(key, 0)}
                        for key in cfg.SCOPE_LEAGUES],
        }

    def put_sports(self, handler, query, body, user, role, params):
        rt = self.runtime
        if rt is None:
            raise AdminError(503, "Runtime configuration unavailable")
        sports = body.get("sports")
        if not isinstance(sports, (list, tuple)) or not sports:
            raise AdminError(400, "sports must be a non-empty list")
        if len(sports) > 60:
            raise AdminError(400, "too many leagues in one request")
        try:
            rt.apply({"sports": [str(s) for s in sports]})
        except OverrideError as exc:
            raise AdminError(400, str(exc))
        self.audit((user or {}).get("id"), "admin.sports.update", "",
                   {"sports": sorted(str(s) for s in sports)})
        return {"success": True, "sports": list(rt.settings().sports),
                "revision": rt.revision}

    # -- picks -------------------------------------------------------------

    def picks(self, handler, query, body, user, role, params):
        storage = self.storage
        if storage is None:
            raise AdminError(503, "This storage driver has no admin query support")
        try:
            result = storage.admin_list_picks(
                state=_first(query, "state"), market=_first(query, "market"),
                sport=_first(query, "sport"), outcome=_first(query, "outcome"),
                term=_search_term(query), page=_first(query, "page", "1"),
                limit=_first(query, "page_size", "50"),
                order=_first(query, "order", "created_at"))
        except Exception as exc:
            logger.exception("admin pick query failed")
            raise AdminError(500, f"Pick query failed: {type(exc).__name__}")
        result["stats"] = storage.admin_pick_stats()
        return result

    def pick_detail(self, handler, query, body, user, role, params):
        storage = self.storage
        if storage is None or not hasattr(storage, "get_pick"):
            raise AdminError(503, "Storage unavailable")
        row = storage.get_pick(params.get("key", ""))
        if not row:
            raise AdminError(404, "No such pick")
        return {"pick": row}

    def settle_pick(self, handler, query, body, user, role, params):
        storage = self.storage
        if storage is None or not hasattr(storage, "settle_pick"):
            raise AdminError(503, "Storage unavailable")
        result = str(body.get("result") or "").strip().upper()
        if result not in ("WIN", "LOSS", "VOID"):
            raise AdminError(400, "result must be WIN, LOSS or VOID")
        key = params.get("key", "")
        # The storage layer takes a datetime and formats it; passing the ISO
        # string produced by `_utcnow_iso` here raised inside the driver and
        # surfaced to the operator as a 500.
        if not storage.settle_pick(key, result, datetime.now(timezone.utc)):
            raise AdminError(409, "Pick is already settled, or not in a settlable state")
        self.audit((user or {}).get("id"), "admin.pick.settle", key,
                   {"result": result})
        return {"success": True, "dedupe_key": key, "result": result}

    def settle_match(self, handler, query, body, user, role, params):
        storage = self.storage
        if storage is None or not hasattr(storage, "manual_settle_match"):
            raise AdminError(503, "Storage unavailable")
        match_id = str(body.get("match_id") or "").strip()
        if not match_id:
            raise AdminError(400, "match_id is required")
        if len(match_id) > 200:
            raise AdminError(400, "match_id is too long")
        result = str(body.get("result") or "WIN").strip().upper()
        if result not in ("WIN", "LOSS", "VOID"):
            raise AdminError(400, "result must be WIN, LOSS or VOID")
        count = storage.manual_settle_match(match_id, result)
        if not count:
            raise AdminError(404, "No pending picks matched that id")
        self.audit((user or {}).get("id"), "admin.pick.settle_match", match_id,
                   {"result": result, "rows": count})
        return {"success": True, "rows_settled": count, "result": result}

    def pick_stats(self, handler, query, body, user, role, params):
        storage = self.storage
        if storage is None:
            raise AdminError(503, "Storage unavailable")
        return {"stats": storage.admin_pick_stats()}

    # -- notifications -----------------------------------------------------

    def notifications(self, handler, query, body, user, role, params):
        storage = self.storage
        if storage is None:
            raise AdminError(503, "This storage driver has no outbox")
        return storage.admin_list_notifications(
            status=_first(query, "status"), page=_first(query, "page", "1"),
            limit=_first(query, "page_size", "50"))

    def retry_notifications(self, handler, query, body, user, role, params):
        storage = self.storage
        if storage is None:
            raise AdminError(503, "Outbox unavailable")
        # Reset the failure state on rows that actually failed, so the next
        # pipeline flush picks them up. This must not mark anything sent: a row
        # that was never delivered is not a delivered row, and marking it sent
        # would silently drop the message for good.
        requeued = int(storage.requeue_notifications(
            limit=max(1, min(int(body.get("limit") or 100), 500))) or 0)
        self.audit((user or {}).get("id"), "admin.notifications.retry", "",
                   {"count": requeued})
        return {"success": True, "requeued": requeued,
                "note": "Failed messages were re-queued and will be sent on the "
                        "next ingestion cycle." if requeued else
                        "No failed messages were waiting."}

    def telegram_test(self, handler, query, body, user, role, params):
        settings = self.settings()
        if not settings.telegram_token:
            raise AdminError(503, "Telegram is not configured")
        chat_id = str(body.get("chat_id") or settings.telegram_chat_id or "").strip()
        if not chat_id:
            raise AdminError(400, "chat_id is required when no default is set")
        message = str(body.get("message") or "")[:900] or "LISA console test."
        ok, detail = send_telegram(settings.telegram_token, chat_id, message)
        self.audit((user or {}).get("id"), "admin.telegram.test", "",
                   {"chat_id": chat_id, "ok": ok})
        if not ok:
            raise AdminError(502, detail or "Telegram rejected the message")
        return {"success": True, "chat_id": chat_id, "detail": detail}

    # -- users -------------------------------------------------------------

    def users(self, handler, query, body, user, role, params):
        storage = self.storage
        if storage is None:
            raise AdminError(503, "This storage driver has no user queries")
        result = storage.admin_list_users(
            term=_search_term(query), page=_first(query, "page", "1"),
            limit=_first(query, "page_size", "50"))
        for row in result["rows"]:
            # The column list never includes password material; the operator flag
            # is computed from the same rules the request itself is gated on.
            row["is_operator"] = self.role_for(row) is not None
        return result

    def patch_user(self, handler, query, body, user, role, params):
        auth = self.auth
        if auth is None or not hasattr(auth, "update_user_tier"):
            raise AdminError(503, "Auth backend unavailable")
        tier = str(body.get("tier") or "").strip()
        if tier not in ("free", "tier1", "tier2", "tier3", "admin"):
            raise AdminError(400, "tier must be free, tier1, tier2, tier3 or admin")
        target_id = params.get("id", "")
        existing = auth.get_user_by_id(target_id)
        if not existing:
            raise AdminError(404, "No such user")
        # The console must never be able to lock itself out. An operator can
        # reach the console two ways: an email in LISA_ADMIN_EMAILS, or a
        # non-free tier. Checking only the tier column would let the last
        # email-owner demote the last tier-admin, leave nobody with a route in,
        # and strand the audit log behind it. So the guard asks the real
        # question: after this change, can any account still get in?
        if tier != "admin":
            if not self._any_operator_would_remain(target_id, existing):
                raise AdminError(409, "Cannot demote the last operator account; "
                                      "add an email to LISA_ADMIN_EMAILS or keep "
                                      "one admin-tier account")
        auth.update_user_tier(target_id, tier)
        self.audit((user or {}).get("id"), "admin.user.tier", target_id,
                   {"tier": tier})
        return {"success": True, "user_id": target_id, "tier": tier}

    def _any_operator_would_remain(self, target_id: str,
                                   target: Optional[dict]) -> bool:
        """Would at least one account still hold console access after a demotion?

        Email-allowlisted owners are counted by email, because the allowlist is
        read from the environment on every request and survives any tier change.
        """
        owner_emails = {e.lower() for e in _env_tuple("LISA_ADMIN_EMAILS")}
        if target and str(target.get("email") or "").strip().lower() in owner_emails:
            # The target is an owner by allowlist, so demoting them does not
            # actually remove their access. Nothing to protect here.
            return True
        if any(e.strip() for e in owner_emails):
            return True
        storage = self.storage
        if storage is None:
            # Without visibility into the account list, refuse rather than risk
            # an unrecoverable console.
            return False
        try:
            page = storage.admin_list_users(limit=ADMIN_MAX_PAGE_SIZE)
            return any(r.get("tier") == "admin" and r.get("id") != target_id
                       for r in page["rows"])
        except Exception:
            return False

    # -- audit, cache, database, telemetry, forecast -----------------------

    def audit_log(self, handler, query, body, user, role, params):
        storage = self.storage
        if storage is None:
            raise AdminError(503, "Audit log unavailable")
        return storage.admin_list_audit(
            admin_id=_first(query, "admin_id"), page=_first(query, "page", "1"),
            limit=_first(query, "page_size", "50"))

    def cache(self, handler, query, body, user, role, params):
        storage = self.storage
        if storage is None:
            raise AdminError(503, "Storage unavailable")
        return {"cache": storage.admin_live_cache_overview()}

    def purge_cache(self, handler, query, body, user, role, params):
        storage = self.storage
        if storage is None:
            raise AdminError(503, "Storage unavailable")
        # Only expired rows go: the live snapshot is the sole copy of the
        # current odds, and deleting it would blank the board until the next
        # poll. admin_live_cache_overview prunes as a side effect.
        result = storage.admin_live_cache_overview()
        self.audit((user or {}).get("id"), "admin.cache.purge", "",
                   {"pruned": result.get("pruned")})
        return {"success": True, "cache": result,
                "note": "Expired entries only; live snapshots preserved."}

    def database(self, handler, query, body, user, role, params):
        storage = self.storage
        if storage is None:
            raise AdminError(503, "This driver does not expose database info")
        return {"database": storage.admin_database_info()}

    def telemetry(self, handler, query, body, user, role, params):
        storage = self.storage
        if storage is None:
            raise AdminError(503, "Storage unavailable")
        rows = []
        for row in storage.admin_get_telemetry():
            key = str(row.get("key") or "")
            # The CSRF signing key lives in the same table as the operator's
            # settings, and a blanket dump handed it to every signed-in admin.
            # Redact it here rather than trusting the UI not to render it: the
            # console has no need for the value, only the fact that one is set.
            if _is_secret_key(key):
                rows.append({"key": key, "value": "***redacted***",
                             "updated_at": row.get("updated_at"),
                             "redacted": True})
            else:
                rows.append(row)
        return {"telemetry": rows}

    def forecast(self, handler, query, body, user, role, params):
        """The live board as users see it, from whatever snapshots exist now."""
        from .bulletin import build_live_bulletin_from_payloads
        from .dashboard import LIVE_ODDS_PREFIX
        storage = self.storage
        if storage is None or not hasattr(storage, "scan_live_keys"):
            raise AdminError(503, "Storage unavailable")
        settings = self.settings()
        payloads = []
        for key in storage.scan_live_keys():
            if not key.startswith(LIVE_ODDS_PREFIX):
                continue
            entry = storage.get_live(key)
            if isinstance(entry, dict) and entry.get("payload"):
                payloads.append((key, entry["payload"]))
        if not payloads:
            return {"board": None, "reason": "no live odds snapshot yet"}
        markets = tuple(m.strip() for m in str(settings.markets).split(",")
                        if m.strip()) or ("h2h",)
        return {"board": build_live_bulletin_from_payloads(
            payloads,
            max_matches=int(settings.forecast_max_matches),
            horizon_hours=float(settings.forecast_horizon_hours),
            min_matches=int(settings.forecast_min_matches),
            include_micro_markets=bool(settings.enable_extra_markets),
            include_in_play=bool(settings.enable_inplay),
            market_keys=markets,
        )}

    # -- system control ----------------------------------------------------

    def pause(self, handler, query, body, user, role, params):
        storage = self.storage
        if storage is None or not hasattr(storage, "set_system_paused"):
            raise AdminError(503, "Kill switch unavailable on this storage driver")
        paused = bool(body.get("paused", True))
        storage.set_system_paused(paused)
        self.audit((user or {}).get("id"),
                   "admin.system.pause" if paused else "admin.system.resume")
        return {"success": True, "paused": paused,
                "note": "Blocks new alerts. Settlement broadcasts still send."}

    def trigger(self, handler, query, body, user, role, params):
        scheduler = self.control.scheduler
        if scheduler is None:
            raise AdminError(503, "No scheduler attached to this process")
        what = str(body.get("what") or "cycle").strip()
        mode = str(body.get("mode") or "due").strip()
        if what not in ("cycle", "settlement"):
            raise AdminError(400, "what must be 'cycle' or 'settlement'")
        if mode not in ("due", "sync"):
            raise AdminError(400, "mode must be 'due' or 'sync'")

        if hasattr(scheduler, "request_refresh"):
            scheduler.request_refresh()
            self.audit((user or {}).get("id"), "admin.system.refresh_requested")
            return {"success": True, "queued": True,
                    "note": "The worker will refresh fixtures and retry overdue settlement."}

        if mode == "due":
            # Mark the work due and let the loop's own thread run it. Calling
            # tick() here would hold the request open for the length of a slow
            # upstream fetch, and could race the loop.
            if what == "cycle":
                scheduler.next_cycle_at = None
            else:
                scheduler.next_settle_at = None
            self.audit((user or {}).get("id"), f"admin.system.poll.{what}", "",
                       {"mode": "due"})
            return {"success": True, "queued": what, "mode": "due",
                    "note": "Marked due; the ingestion loop will pick it up."}

        if not self._cycle_lock.acquire(blocking=False):
            raise AdminError(409, "A cycle is already running in this process")
        try:
            summary = scheduler.tick(only=what)
        except Exception as exc:
            self.audit((user or {}).get("id"), f"admin.system.poll.{what}", "",
                       {"mode": "sync", "error": type(exc).__name__})
            raise AdminError(500, f"Cycle failed: {type(exc).__name__}")
        finally:
            self._cycle_lock.release()
        self.audit((user or {}).get("id"), f"admin.system.poll.{what}", "",
                   {"mode": "sync", "ran_cycle": summary.ran_cycle,
                    "ran_settlement": summary.ran_settlement})
        return {
            "success": True, "queued": what, "mode": "sync",
            "summary": {
                "mode": summary.mode,
                "ran_cycle": summary.ran_cycle,
                "ran_settlement": summary.ran_settlement,
                "skipped_reason": summary.skipped_reason,
                "errors": list(summary.errors)[:10],
                "cycle_reports": [
                    {"sport_key": r.sport_key, "matches": len(r.matches),
                     "errors": list(r.errors)}
                    for r in summary.cycle_reports
                ],
            },
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _match_segments(pattern: list[str], actual: list[str]
                    ) -> Optional[dict[str, str]]:
    """Match a route pattern against a concrete path.

    A ``{name}`` segment captures one path segment. Literal segments must match
    exactly, so ``/picks/stats`` cannot be captured by ``/picks/{key}``.
    """
    if len(pattern) != len(actual):
        return None
    params: dict[str, str] = {}
    for want, got in zip(pattern, actual):
        if want.startswith("{") and want.endswith("}"):
            if not got:
                return None
            params[want[1:-1]] = got
        elif want != got:
            return None
    return params


def _server_allowed_origins() -> tuple[str, ...]:
    return tuple(o.strip().rstrip("/")
                 for o in os.environ.get("LISA_ALLOWED_ORIGINS", "").split(",")
                 if o.strip())


def _cookie_secure_enabled(handler) -> bool:
    """Mark the cookie Secure only when the connection really is TLS.

    The deployment serves plain HTTP on :8080, so setting Secure unconditionally
    would make the cookie silently unusable and look like a broken login. A TLS
    front end can force it on, or advertise itself with X-Forwarded-Proto.
    """
    if os.environ.get("LISA_COOKIE_SECURE", "").strip().lower() in _TRUE:
        return True
    proto = (handler.headers.get("X-Forwarded-Proto") or "").strip().lower()
    return proto in ("https", "on")


def _effective_summary(settings: cfg.Settings) -> dict[str, Any]:
    return {
        "sports": list(settings.sports),
        "markets": settings.markets,
        "cadence_prematch_sec": settings.cadence_prematch_sec,
        "cadence_live_sec": settings.cadence_live_sec,
        "cadence_settle_sec": settings.cadence_settle_sec,
        "credit_budget_daily": settings.credit_budget_daily,
        "enable_inplay": settings.enable_inplay,
        "enable_extra_markets": settings.enable_extra_markets,
        "enable_micro_predictions": settings.enable_micro_predictions,
        "forecast_horizon_hours": settings.forecast_horizon_hours,
    }


def send_telegram(token: str, chat_id: str, text: str) -> tuple[bool, str]:
    """Send one message. Returns ``(ok, detail)``.

    A plain request rather than a call into the bot's own client, so the
    console can also be used to probe a chat id that the bot has never
    successfully written to.
    """
    import urllib.error
    import urllib.parse
    import urllib.request
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return bool(payload.get("ok")), str(payload.get("description") or "sent")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            return False, str(payload.get("description") or f"HTTP {exc.code}")
        except Exception:
            return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# route table: (method, path) -> (unbound method, permission, is_public)
# ---------------------------------------------------------------------------

_ROUTES: dict[tuple[str, str], tuple[Callable, str, bool]] = {
    ('GET', '/operations'): (AdminAPI.operations, READ, False),
    ('POST', '/jobs/{job}'): (AdminAPI.run_paper_job, WRITE, False),
    ("GET", "/session"): (AdminAPI.session, READ, True),
    ("POST", "/login"): (AdminAPI.login, READ, True),
    ("POST", "/logout"): (AdminAPI.logout, WRITE, False),
    ("GET", "/overview"): (AdminAPI.overview, READ, False),
    ("GET", "/health"): (AdminAPI.health, READ, False),
    ("GET", "/keys"): (AdminAPI.keys, READ, False),
    ('GET', '/providers'): (AdminAPI.get_providers, READ, False),
    ('PUT', '/providers/{provider}'): (AdminAPI.put_provider, WRITE, False),
    ("POST", "/keys/cooldown"): (AdminAPI.key_cooldown, WRITE, False),
    ("GET", "/settings"): (AdminAPI.get_settings, READ, False),
    ("PATCH", "/settings"): (AdminAPI.patch_settings, WRITE, False),
    ("POST", "/settings/reset"): (AdminAPI.reset_settings, WRITE, False),
    ("GET", "/sports"): (AdminAPI.sports, READ, False),
    ("PUT", "/sports"): (AdminAPI.put_sports, WRITE, False),
    ("GET", "/picks"): (AdminAPI.picks, READ, False),
    # Registered before the {key} rules only for readability; the matcher is
    # segment-exact, so literal routes always win over a capture regardless of
    # dictionary order.
    ("GET", "/picks/stats"): (AdminAPI.pick_stats, READ, False),
    ("POST", "/picks/settle-match"): (AdminAPI.settle_match, DANGER, False),
    ("GET", "/picks/{key}"): (AdminAPI.pick_detail, READ, False),
    ("POST", "/picks/{key}/settle"): (AdminAPI.settle_pick, DANGER, False),
    ("GET", "/notifications"): (AdminAPI.notifications, READ, False),
    ("POST", "/notifications/retry"): (AdminAPI.retry_notifications, WRITE, False),
    ("POST", "/telegram/test"): (AdminAPI.telegram_test, WRITE, False),
    ("GET", "/users"): (AdminAPI.users, READ, False),
    ("PATCH", "/users/{id}"): (AdminAPI.patch_user, WRITE, False),
    ("GET", "/audit"): (AdminAPI.audit_log, READ, False),
    ("GET", "/cache"): (AdminAPI.cache, READ, False),
    ("POST", "/cache/purge"): (AdminAPI.purge_cache, WRITE, False),
    ("GET", "/database"): (AdminAPI.database, READ, False),
    ("GET", "/telemetry"): (AdminAPI.telemetry, READ, False),
    ("GET", "/forecast"): (AdminAPI.forecast, READ, False),
    ("POST", "/system/pause"): (AdminAPI.pause, WRITE, False),
    ("POST", "/system/poll"): (AdminAPI.trigger, WRITE, False),
}

# Routes an `admin` may not call. The console has two operator levels and the
# distinction is about blast radius, not about UI polish:
#
#   * Access control. An `admin` can already be created by anyone with the
#     admin tier, so allowing an `admin` to grant tiers is self-escalation:
#     the ability to mint more admins must sit with the email allowlist only.
#   * Money and credentials. Key rotation and the global settings write both
#     decide what the product spends and reaches. A single compromised admin
#     session should not be able to redirect that.
#   * Irreversibility. Everything already marked DANGER, which is enforced by
#     the same check, so a settle or a bulk settle cannot be triggered by an
#     `admin`.
#
# `role_for` is re-evaluated on every request, so demoting an account in the
# database takes effect on its next request rather than at next sign-in.
_OWNER_ONLY = {
    'put_provider',
    "patch_user", "key_cooldown", "patch_settings", "reset_settings",
    "put_sports", "settle_pick", "settle_match", "retry_notifications",
    "purge_cache", "trigger", "pause",
}
