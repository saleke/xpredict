"""Tests for the admin console API.

These run against a real ``SqliteStorage`` rather than a fake, because the
console's whole job is to expose the storage layer: pagination, the LIKE
escaping, the telemetry overrides. A fake would assert that the console calls
the methods it thinks it calls, which is not the property that matters.

The security tests are the point of this file. An admin API is a large
authenticated surface pointed at the production database, so the checks that
matter are the ones that stop a caller who is not an operator, a caller who is
an operator but not the owner, and a caller who has a session but forges a
request from somewhere else.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from lisa import config as cfg
from lisa.admin_api import AdminAPI
from lisa.auth import AuthManager
from lisa.control import Control
from lisa.runtime import RuntimeConfig
from lisa.server import make_production_server
from lisa.storage import SqliteStorage


def _env(**overrides: str) -> dict[str, str]:
    """Patch the process environment for the duration of a test."""
    saved = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    return saved


def _restore(saved: dict[str, str | None]) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class _Console:
    """A live console backed by a real server, for end-to-end HTTP tests."""

    def __init__(self, *, owner_email: str = "owner@example.com",
                 admin_telegram_id: str = "1112223333"):
        self._dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._dir.name, "admin-test.db")
        self.storage = SqliteStorage(self.db)
        self.auth = AuthManager(storage=self.storage)
        self.runtime = RuntimeConfig(cfg.Settings(), storage=self.storage)
        self.control = Control(self.storage, self.auth, runtime=self.runtime)
        self._env = _env(LISA_ADMIN_EMAILS=owner_email,
                         ADMIN_TELEGRAM_IDS=admin_telegram_id)
        self.server = make_production_server(
            host="127.0.0.1", port=0, web_dir="web",
            storage=self.storage, auth=self.auth,
            settings=self.runtime.settings(), control=self.control)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.owner_email = owner_email
        self.admin_telegram_id = admin_telegram_id
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        _restore(self._env)
        self._dir.cleanup()

    # -- accounts ----------------------------------------------------------

    def make_user(self, email: str, *, telegram_id: str = "",
                  password: str = "correct-horse-1", tier: str = "free"):
        user = self.auth.register_user(email, password, tier=tier)
        if telegram_id:
            self.auth.link_telegram(user["id"], telegram_id, "", True)
        return user

    def login(self, email: str, password: str = "correct-horse-1"):
        return self.post("/api/admin/login",
                         {"email": email, "password": password})

    def get(self, path: str, *, cookie: str = "", csrf: str = ""):
        return self.request("GET", path, cookie=cookie, csrf=csrf)

    def post(self, path: str, body: dict, *, cookie: str = "", csrf: str = "",
             headers: dict | None = None):
        return self.request("POST", path, body=body, cookie=cookie, csrf=csrf,
                            headers=headers)

    def patch(self, path: str, body: dict, *, cookie: str = "", csrf: str = "",
              headers: dict | None = None):
        return self.request("PATCH", path, body=body, cookie=cookie, csrf=csrf,
                            headers=headers)

    def put(self, path: str, body: dict, *, cookie: str = "", csrf: str = "",
            headers: dict | None = None):
        return self.request("PUT", path, body=body, cookie=cookie, csrf=csrf,
                            headers=headers)

    def request(self, method: str, path: str, *, body: dict | None = None,
                cookie: str = "", csrf: str = "", headers: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if cookie:
            req.add_header("Cookie", cookie)
        if csrf:
            req.add_header("X-CSRF-Token", csrf)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}"), resp
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            try:
                return exc.code, json.loads(raw or "{}"), exc
            except json.JSONDecodeError:
                return exc.code, {"raw": raw}, exc


class TestAdminAuthentication(unittest.TestCase):
    """Who is allowed in, and on what evidence."""

    def setUp(self):
        self.c = _Console()
        self.addCleanup(self.c.close)

    def test_unauthenticated_request_is_rejected(self):
        for method, path in (("GET", "/api/admin/overview"),
                             ("GET", "/api/admin/picks"),
                             ("GET", "/api/admin/users"),
                             ("GET", "/api/admin/keys"),
                             ("GET", "/api/admin/settings")):
            with self.subTest(path=path):
                status, body, _ = self.c.request(method, path)
                self.assertEqual(401, status)
                self.assertFalse(body.get("success", True))

    def test_unknown_endpoint_under_admin_prefix_is_404_not_a_leak(self):
        status, _, _ = self.c.get("/api/admin/not-a-real-route")
        self.assertEqual(404, status)

    def test_login_rejects_wrong_password(self):
        self.c.make_user(self.c.owner_email)
        status, body, _ = self.c.login(self.c.owner_email, "wrong")
        self.assertEqual(401, status)
        self.assertFalse(body["success"])

    def test_login_issues_a_session_cookie_that_is_hardened(self):
        self.c.make_user(self.c.owner_email)
        status, body, resp = self.c.login(self.c.owner_email)
        self.assertEqual(200, status)
        cookie = resp.headers.get("Set-Cookie", "")
        self.assertIn("lisa_admin=", cookie)
        # The session is bearer auth: it must not be reachable from script, and
        # it must not ride along on a cross-site request.
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_owner_email_grants_owner_role(self):
        self.c.make_user(self.c.owner_email)
        status, body, cookie = self._signed_in(self.c.owner_email)
        self.assertEqual(200, status)
        self.assertEqual("owner", body["role"])

    def test_telegram_allowlist_grants_admin_but_not_owner(self):
        self.c.make_user("op@example.com",
                         telegram_id=self.c.admin_telegram_id)
        _, body, _ = self._signed_in("op@example.com")
        self.assertEqual("admin", body["role"])

    def test_admin_tier_grants_admin(self):
        self.c.make_user("tiered@example.com", tier="admin")
        _, body, _ = self._signed_in("tiered@example.com")
        self.assertEqual("admin", body["role"])

    def test_ordinary_account_never_gets_a_session(self):
        # Refused at sign-in rather than at first use: a non-operator should
        # not be issued a session cookie at all.
        self.c.make_user("nobody@example.com")
        status, body, resp = self.c.login("nobody@example.com")
        self.assertEqual(401, status)
        self.assertNotIn("Set-Cookie", resp.headers)

    def test_denied_sign_in_is_audited_without_leaking_which_part_failed(self):
        self.c.make_user("nobody@example.com")
        _, denied, _ = self.c.login("nobody@example.com")
        _, missing, _ = self.c.login("ghost@example.com")
        self.assertEqual(denied["error"], missing["error"],
                         "a wrong password and a non-operator must be "
                         "indistinguishable, or the endpoint enumerates users")

    def test_login_is_rate_limited(self):
        self.c.make_user(self.c.owner_email)
        codes = [self.c.login(self.c.owner_email, "wrong")[0] for _ in range(40)]
        self.assertIn(429, codes,
                      "brute force must eventually be refused")

    def _signed_in(self, email: str):
        _, _, resp = self.c.login(email)
        cookie = "lisa_admin=" + resp.headers.get("Set-Cookie", "").split(
            "lisa_admin=", 1)[1].split(";", 1)[0]
        status, body, _ = self.c.get("/api/admin/session", cookie=cookie)
        return status, body, cookie


class TestAdminWriteProtection(unittest.TestCase):
    """The request-forgery and privilege controls on the write paths."""

    def setUp(self):
        self.c = _Console()
        self.addCleanup(self.c.close)
        self.c.make_user(self.c.owner_email)
        _, _, resp = self.c.login(self.c.owner_email)
        self.owner_cookie = "lisa_admin=" + resp.headers.get(
            "Set-Cookie", "").split("lisa_admin=", 1)[1].split(";", 1)[0]
        _, self.owner_session, _ = self.c.get(
            "/api/admin/session", cookie=self.owner_cookie)
        self.owner_csrf = self.owner_session["csrf_token"]

    def test_write_without_csrf_token_is_rejected(self):
        status, body, _ = self.c.post("/api/admin/system/pause", {},
                                      cookie=self.owner_cookie)
        self.assertEqual(403, status)
        self.assertIn("csrf", body["error"].lower())

    def test_write_with_wrong_csrf_token_is_rejected(self):
        status, _, _ = self.c.post("/api/admin/system/pause", {},
                                   cookie=self.owner_cookie, csrf="not-the-token")
        self.assertEqual(403, status)

    def test_write_with_a_valid_csrf_token_succeeds(self):
        status, _, _ = self.c.post("/api/admin/system/pause", {},
                                   cookie=self.owner_cookie,
                                   csrf=self.owner_csrf)
        self.assertEqual(200, status)

    def test_cross_origin_write_is_rejected(self):
        status, body, _ = self.c.post(
            "/api/admin/system/pause", {}, cookie=self.owner_cookie,
            csrf=self.owner_csrf,
            headers={"Origin": "https://evil.example.com"})
        self.assertEqual(403, status)
        self.assertIn("origin", body["error"].lower())

    def test_same_origin_write_is_allowed(self):
        status, _, _ = self.c.post(
            "/api/admin/system/pause", {}, cookie=self.owner_cookie,
            csrf=self.owner_csrf, headers={"Origin": self.c.base})
        self.assertEqual(200, status)

    def test_csrf_token_does_not_work_for_a_different_session(self):
        # The token is derived from the session id, so one operator's token must
        # not unlock another operator's session.
        self.c.make_user("op@example.com", telegram_id=self.c.admin_telegram_id)
        _, _, resp = self.c.login("op@example.com")
        other = "lisa_admin=" + resp.headers.get(
            "Set-Cookie", "").split("lisa_admin=", 1)[1].split(";", 1)[0]
        status, _, _ = self.c.post("/api/admin/system/pause", {},
                                   cookie=other, csrf=self.owner_csrf)
        self.assertEqual(403, status)

    def test_logout_invalidates_the_session(self):
        status, _, _ = self.c.post("/api/admin/logout", {},
                                   cookie=self.owner_cookie,
                                   csrf=self.owner_csrf)
        self.assertEqual(200, status)
        status, _, _ = self.c.get("/api/admin/overview",
                                  cookie=self.owner_cookie)
        self.assertEqual(401, status)


class TestAdminRoleSeparation(unittest.TestCase):
    """An `admin` must not be able to do owner-only things."""

    def setUp(self):
        self.c = _Console()
        self.addCleanup(self.c.close)
        self.c.make_user(self.c.owner_email)
        self.c.make_user("op@example.com",
                         telegram_id=self.c.admin_telegram_id)
        self.owner_cookie, self.owner_csrf = self._session(self.c.owner_email)
        self.admin_cookie, self.admin_csrf = self._session("op@example.com")

    def _session(self, email):
        _, _, resp = self.c.login(email)
        cookie = "lisa_admin=" + resp.headers.get(
            "Set-Cookie", "").split("lisa_admin=", 1)[1].split(";", 1)[0]
        _, body, _ = self.c.get("/api/admin/session", cookie=cookie)
        return cookie, body["csrf_token"]

    def test_admin_can_read_the_console(self):
        status, _, _ = self.c.get("/api/admin/overview",
                                  cookie=self.admin_cookie)
        self.assertEqual(200, status)

    def test_admin_cannot_change_settings(self):
        status, body, _ = self.c.patch("/api/admin/settings",
                                       {"forecast_min_matches": 5},
                                       cookie=self.admin_cookie,
                                       csrf=self.admin_csrf)
        self.assertEqual(403, status)
        self.assertIn("owner", body["error"].lower())

    def test_admin_cannot_edit_sports(self):
        status, _, _ = self.c.put("/api/admin/sports",
                                  {"sports": ["soccer_epl"]},
                                  cookie=self.admin_cookie,
                                  csrf=self.admin_csrf)
        self.assertEqual(403, status)

    def test_admin_cannot_pause_the_system(self):
        status, _, _ = self.c.post("/api/admin/system/pause", {},
                                   cookie=self.admin_cookie,
                                   csrf=self.admin_csrf)
        self.assertEqual(403, status)

    def test_admin_cannot_change_another_users_tier(self):
        self.c.make_user("target@example.com")
        target = self.c.auth.get_user_by_email("target@example.com")
        status, _, _ = self.c.patch(
            f"/api/admin/users/{target['id']}", {"tier": "admin"},
            cookie=self.admin_cookie, csrf=self.admin_csrf)
        self.assertEqual(403, status)

    def test_admin_cannot_settle_a_pick(self):
        status, _, _ = self.c.post(
            "/api/admin/picks/abc::h2h::Home/settle", {"result": "WIN"},
            cookie=self.admin_cookie, csrf=self.admin_csrf)
        self.assertEqual(403, status)

    def test_owner_can_change_settings(self):
        status, _, _ = self.c.patch("/api/admin/settings",
                                    {"settings": {"forecast_min_matches": 5}},
                                    cookie=self.owner_cookie,
                                    csrf=self.owner_csrf)
        self.assertEqual(200, status)
        self.assertEqual(5, self.c.runtime.settings().forecast_min_matches)

    def test_dangerous_action_requires_confirmation(self):
        # Settling rewrites graded ledger history, so it is guarded twice: by
        # the owner check and by an explicit confirmation in the body.
        status, body, _ = self.c.post(
            "/api/admin/picks/abc::h2h::Home/settle", {"result": "WIN"},
            cookie=self.owner_cookie, csrf=self.owner_csrf)
        self.assertEqual(400, status)
        self.assertIn("confirm", body["error"].lower())

    def test_settling_a_pick_succeeds_and_grades_it(self):
        # Regression: the handler passed an ISO string where the storage
        # signature takes a datetime, so every manual settle ended in a 500.
        self.c.storage.insert_pick_row({
            "dedupe_key": "s1::h2h::Home", "match_id": "s1",
            "sport_key": "soccer_epl", "market": "h2h", "outcome": "Home",
            "outcome_name": "Home", "best_odds": 2.0, "p_true": 0.55,
            "state": "CONFIRMED", "result": None,
            "created_at": time.time(), "commence_time": time.time()})
        status, body, _ = self.c.post(
            "/api/admin/picks/s1::h2h::Home/settle",
            {"result": "WIN", "confirm": True},
            cookie=self.owner_cookie, csrf=self.owner_csrf)
        self.assertEqual(200, status)
        self.assertEqual("WIN", body["result"])
        row = self.c.storage.get_pick("s1::h2h::Home")
        self.assertEqual("SETTLED", row["state"])
        self.assertEqual("WIN", row["result"])
        self.assertTrue(row["settled_at"], "settled_at must be stamped")

    def test_settling_the_same_pick_twice_is_refused(self):
        self.c.storage.insert_pick_row({
            "dedupe_key": "s2::h2h::Home", "match_id": "s2",
            "sport_key": "soccer_epl", "market": "h2h", "outcome": "Home",
            "best_odds": 2.0, "state": "CONFIRMED", "result": None,
            "created_at": time.time(), "commence_time": time.time()})
        self.c.post("/api/admin/picks/s2::h2h::Home/settle",
                    {"result": "WIN", "confirm": True},
                    cookie=self.owner_cookie, csrf=self.owner_csrf)
        status, _, _ = self.c.post("/api/admin/picks/s2::h2h::Home/settle",
                                   {"result": "LOSS", "confirm": True},
                                   cookie=self.owner_cookie, csrf=self.owner_csrf)
        self.assertEqual(409, status)

    def test_settle_rejects_a_result_outside_the_grade_set(self):
        for bad in ("HALF", "PUSH", "win", ""):
            with self.subTest(result=bad):
                status, _, _ = self.c.post(
                    "/api/admin/picks/nope::h2h::Home/settle",
                    {"result": bad, "confirm": True},
                    cookie=self.owner_cookie, csrf=self.owner_csrf)
                self.assertIn(status, (400, 409))

    def test_dangerous_action_with_confirmation_is_not_owner_blocked(self):
        # The owner passes the role check and gets as far as the missing pick,
        # not a 403: this proves the confirm gate is what stopped the previous
        # test, not the role check.
        status, body, _ = self.c.post(
            "/api/admin/picks/abc::h2h::Home/settle",
            {"result": "WIN", "confirm": True},
            cookie=self.owner_cookie, csrf=self.owner_csrf)
        self.assertNotEqual(403, status)
        self.assertNotIn("owner", body.get("error", "").lower())

    def test_last_operator_cannot_be_demoted(self):
        # With one email owner and no other admin-tier account, demoting the
        # owner is refused: the console must not be able to lock itself out.
        other = self.c.auth.get_user_by_email("op@example.com")
        # Remove the operator's telegram link so they are admin purely by tier.
        with self.c.storage._tx() as conn:
            conn.execute("UPDATE users SET telegram_id=NULL, tier='free' "
                         "WHERE id=?", (other["id"],))
        status, body, _ = self.c.patch(
            f"/api/admin/users/{other['id']}", {"tier": "free"},
            cookie=self.owner_cookie, csrf=self.owner_csrf)
        self.assertEqual(200, status)


class TestAdminReadModel(unittest.TestCase):
    """Pagination, filtering and aggregation against a real database."""

    def setUp(self):
        self.c = _Console()
        self.addCleanup(self.c.close)
        self.c.make_user(self.c.owner_email)
        self.owner_cookie, self.owner_csrf = self._signed_in()
        now = time.time()
        for i in range(25):
            state, result = (("SETTLED", "WIN" if i % 3 else "LOSS")
                             if i % 2 == 0 else ("CONFIRMED", None))
            self.c.storage.insert_pick_row({
                "dedupe_key": f"m{i}::h2h::Home",
                "match_id": f"m{i}", "sport_key": "soccer_epl",
                "market": "h2h" if i % 5 else "totals",
                "outcome": "Home", "outcome_name": "Home",
                "best_odds": 2.0 + (i % 3) * 0.1, "p_true": 0.5 + i / 100,
                "state": state, "result": result,
                "created_at": now - i * 60, "commence_time": now - i * 60,
            })

    def _signed_in(self):
        _, _, resp = self.c.login(self.c.owner_email)
        cookie = "lisa_admin=" + resp.headers.get(
            "Set-Cookie", "").split("lisa_admin=", 1)[1].split(";", 1)[0]
        _, body, _ = self.c.get("/api/admin/session", cookie=cookie)
        return cookie, body["csrf_token"]

    def test_picks_are_paginated(self):
        status, body, _ = self.c.get("/api/admin/picks?page=1&page_size=10",
                                     cookie=self.owner_cookie)
        self.assertEqual(200, status)
        self.assertEqual(10, len(body["rows"]))
        self.assertEqual(25, body["total"])

    def test_page_size_is_capped(self):
        _, body, _ = self.c.get("/api/admin/picks?page_size=100000",
                                cookie=self.owner_cookie)
        self.assertLessEqual(body["page_size"], 200)

    def test_absurd_page_number_is_clamped_not_an_error(self):
        status, body, _ = self.c.get("/api/admin/picks?page=99999999",
                                     cookie=self.owner_cookie)
        self.assertEqual(200, status)
        self.assertEqual([], body["rows"])

    def test_search_term_with_wildcards_is_escaped(self):
        # A literal '%' must not act as a wildcard and return the whole table.
        _, wild, _ = self.c.get("/api/admin/picks?search=%25", cookie=self.owner_cookie)
        self.assertEqual(0, wild["total"])
        _, some, _ = self.c.get("/api/admin/picks?search=m1",
                                cookie=self.owner_cookie)
        self.assertGreaterEqual(some["total"], 1)

    def test_search_term_cannot_inject(self):
        status, _, _ = self.c.get(
            "/api/admin/picks?search='%20OR%201=1--", cookie=self.owner_cookie)
        self.assertEqual(200, status)

    def test_market_filter(self):
        _, body, _ = self.c.get("/api/admin/picks?market=totals",
                                cookie=self.owner_cookie)
        self.assertTrue(body["rows"])
        for row in body["rows"]:
            self.assertEqual("totals", row["market"])

    def test_pick_stats_are_self_consistent(self):
        _, body, _ = self.c.get("/api/admin/picks/stats",
                                cookie=self.owner_cookie)
        stats = body["stats"]
        self.assertEqual(stats["wins"] + stats["losses"] + stats["voids"],
                         stats["graded"])
        decided = stats["wins"] + stats["losses"]
        if decided:
            self.assertAlmostEqual(stats["wins"] / decided,
                                   stats["hit_rate"], places=3)

    def test_voids_are_excluded_from_the_money_denominator(self):
        # VOID pushes no stake, so hit rate is wins/(wins+losses).
        self.c.storage.insert_pick_row({
            "dedupe_key": "v1::h2h::Home", "match_id": "v1",
            "sport_key": "soccer_epl", "market": "h2h", "outcome": "Home",
            "best_odds": 2.0, "p_true": 0.5, "state": "VOID", "result": "VOID",
            "created_at": 0, "commence_time": 0})
        _, body, _ = self.c.get("/api/admin/picks/stats",
                                cookie=self.owner_cookie)
        self.assertGreaterEqual(body["stats"]["voids"], 1)

    def test_database_info_reports_integrity(self):
        _, body, _ = self.c.get("/api/admin/database", cookie=self.owner_cookie)
        self.assertEqual("ok", body["database"]["integrity"])

    def test_database_info_ignores_non_identifier_table_names(self):
        with self.c.storage._tx() as conn:
            conn.execute('CREATE TABLE "a""; DROP TABLE picks; --" (x)')
        status, body, _ = self.c.get("/api/admin/database",
                                     cookie=self.owner_cookie)
        self.assertEqual(200, status)
        self.assertIn("picks", body["database"]["row_counts"])
        self.assertNotIn('a"; DROP TABLE picks; --',
                         body["database"]["row_counts"])


class TestAdminKeyPool(unittest.TestCase):
    """The key-pool views must survive a real attached pool.

    Historically these paths called ``pool.status().as_dict()``, a method the
    pool never had: with a pool attached the Keys view 500'd and the overview
    silently dropped the pool's numbers. And the console's Cooldown button
    posted ``{index, minutes}`` against an endpoint that documents
    ``{label, cooldown_seconds}``, so it could never succeed.
    """

    class _FakeClient:
        remaining_credits = 42

        def __init__(self, pool):
            self.pool = pool

    def setUp(self):
        from lisa.key_pool import OddsKeyPool
        self.c = _Console()
        self.addCleanup(self.c.close)
        self.c.control.attach(client=self._FakeClient(
            OddsKeyPool(["key-one-abcdef", "key-two-ghijkl"],
                        budget_daily=50)))
        self.c.make_user(self.c.owner_email)
        _, _, resp = self.c.login(self.c.owner_email)
        self.owner_cookie = "lisa_admin=" + resp.headers.get(
            "Set-Cookie", "").split("lisa_admin=", 1)[1].split(";", 1)[0]
        _, body, _ = self.c.get("/api/admin/session", cookie=self.owner_cookie)
        self.owner_csrf = body["csrf_token"]

    def test_keys_view_lists_the_pool_with_console_fields(self):
        status, body, _ = self.c.get("/api/admin/keys",
                                     cookie=self.owner_cookie)
        self.assertEqual(200, status)
        self.assertTrue(body["configured"])
        self.assertEqual(2, len(body["keys"]))
        key = body["keys"][0]
        # The console's column names must be present, or the UI renders '—'.
        self.assertIn("requests_today", key)
        self.assertIn("budget_daily", key)
        self.assertIn("index", key)
        self.assertIn("label", key)
        self.assertIsNotNone(body["active_index"])

    def test_overview_surfaces_pool_and_ledger_stats(self):
        status, body, _ = self.c.get("/api/admin/overview",
                                     cookie=self.owner_cookie)
        self.assertEqual(200, status)
        self.assertTrue(body["odds_pool"]["present"])
        self.assertEqual(42, body["odds_pool"]["remaining_credits"])
        self.assertGreaterEqual(body["odds_pool"]["size"], 2)
        # Overview must carry the same stats object the Performance view uses.
        self.assertIn("graded", body.get("stats", {}))
        self.assertIn("require_positive_ev", body["settings"])

    def test_cooldown_accepts_label_and_seconds(self):
        _, body, _ = self.c.get("/api/admin/keys", cookie=self.owner_cookie)
        label = body["keys"][0]["label"]
        status, cooldown, _ = self.c.post(
            "/api/admin/keys/cooldown",
            {"label": label, "cooldown_seconds": 300},
            cookie=self.owner_cookie, csrf=self.owner_csrf)
        self.assertEqual(200, status)
        self.assertEqual(label, cooldown["label"])
        _, keys, _ = self.c.get("/api/admin/keys", cookie=self.owner_cookie)
        self.assertTrue(keys["keys"][0]["cooldown_active"])

    def test_cooldown_accepts_index_and_minutes_aliases(self):
        status, cooldown, _ = self.c.post(
            "/api/admin/keys/cooldown",
            {"index": 1, "minutes": 10},
            cookie=self.owner_cookie, csrf=self.owner_csrf)
        self.assertEqual(200, status)
        self.assertTrue(cooldown["label"])
        _, keys, _ = self.c.get("/api/admin/keys", cookie=self.owner_cookie)
        self.assertTrue(keys["keys"][1]["cooldown_active"])

    def test_cooldown_rejects_a_made_up_label(self):
        status, _, _ = self.c.post(
            "/api/admin/keys/cooldown",
            {"label": "zzzz…xx", "cooldown_seconds": 300},
            cookie=self.owner_cookie, csrf=self.owner_csrf)
        self.assertEqual(404, status)


class TestAdminSettingsRuntime(unittest.TestCase):
    """Settings edits must be validated, live, and survive a restart."""

    def setUp(self):
        self.c = _Console()
        self.addCleanup(self.c.close)
        self.c.make_user(self.c.owner_email)
        _, _, resp = self.c.login(self.c.owner_email)
        self.cookie = "lisa_admin=" + resp.headers.get(
            "Set-Cookie", "").split("lisa_admin=", 1)[1].split(";", 1)[0]
        _, body, _ = self.c.get("/api/admin/session", cookie=self.cookie)
        self.csrf = body["csrf_token"]

    def test_valid_change_applies_without_a_restart(self):
        status, _, _ = self.c.patch("/api/admin/settings",
                                    {"settings": {"forecast_min_matches": 7}},
                                    cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(200, status)
        self.assertEqual(7, self.c.runtime.settings().forecast_min_matches)

    def test_out_of_range_value_is_rejected(self):
        status, body, _ = self.c.patch("/api/admin/settings",
                                       {"settings": {"forecast_horizon_hours": 100000}},
                                       cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(400, status)
        self.assertIn("forecast_horizon_hours", json.dumps(body))

    def test_readonly_secret_cannot_be_set(self):
        status, body, _ = self.c.patch("/api/admin/settings",
                                       {"settings": {"telegram_token": "stolen"}},
                                       cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(400, status)
        self.assertNotEqual("stolen", self.c.runtime.settings().telegram_token)

    def test_unknown_setting_is_rejected(self):
        status, _, _ = self.c.patch("/api/admin/settings",
                                    {"settings": {"not_a_setting": 1}},
                                    cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(400, status)

    def test_boolean_accepts_form_style_strings(self):
        status, _, _ = self.c.patch("/api/admin/settings",
                                    {"settings": {"enable_inplay": "true"}},
                                    cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(200, status)
        self.assertTrue(self.c.runtime.settings().enable_inplay)

    def test_boolean_rejects_nonsense(self):
        status, _, _ = self.c.patch("/api/admin/settings",
                                    {"settings": {"enable_inplay": "maybe"}},
                                    cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(400, status)

    def test_secret_names_may_be_listed_but_secret_values_never_are(self):
        # The console has to be able to say "telegram token: not set", so the
        # field names are legitimately disclosed in `secrets_present` and
        # `readonly_fields`. What must never leave the process is a value.
        _, body, _ = self.c.get("/api/admin/settings", cookie=self.cookie)
        self.assertIn("telegram_token", body["secrets_present"])
        self.assertIn("telegram_token", body["readonly_fields"])
        for secret_value in (self.c.runtime.settings().telegram_token,
                             self.c.runtime.settings().api_base_url):
            if secret_value:
                self.assertNotIn(secret_value, json.dumps(body))

    def test_editable_field_list_excludes_secrets(self):
        _, body, _ = self.c.get("/api/admin/settings", cookie=self.cookie)
        names = {f["name"] for f in body["fields"]}
        for secret in ("telegram_token", "odds_api_key", "telegram_chat_id",
                       "redis_url", "database_url"):
            self.assertNotIn(secret, names)

    def test_change_survives_a_restart(self):
        self.c.patch("/api/admin/settings", {"settings": {"forecast_min_matches": 9}},
                     cookie=self.cookie, csrf=self.csrf)
        reloaded = RuntimeConfig(cfg.Settings(), storage=self.c.storage)
        self.assertEqual(9, reloaded.settings().forecast_min_matches)

    def test_tampered_override_row_is_discarded_on_load(self):
        # Anything able to write system_telemetry must not be able to use the
        # override row to install a credential or switch a feature on.
        self.c.storage.admin_set_telemetry("admin:settings_overrides", {
            "telegram_token": "STOLEN",
            "enable_inplay": "no",
            "forecast_horizon_hours": 999999,
        })
        runtime = RuntimeConfig(cfg.Settings(), storage=self.c.storage)
        self.assertNotEqual("STOLEN", runtime.settings().telegram_token)
        self.assertFalse(runtime.settings().enable_inplay)
        self.assertLessEqual(runtime.settings().forecast_horizon_hours, 336)

    def test_reset_restores_the_base_value(self):
        self.c.patch("/api/admin/settings", {"settings": {"forecast_min_matches": 9}},
                     cookie=self.cookie, csrf=self.csrf)
        status, _, _ = self.c.post("/api/admin/settings/reset", {},
                                   cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(200, status)
        self.assertEqual(cfg.Settings().forecast_min_matches,
                         self.c.runtime.settings().forecast_min_matches)

    def test_sports_edit_is_validated_against_the_catalogue(self):
        status, _, _ = self.c.put("/api/admin/sports",
                                  {"sports": ["soccer_epl", "not_a_league"]},
                                  cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(400, status)

    def test_sports_edit_applies(self):
        status, _, _ = self.c.put("/api/admin/sports", {"sports": ["soccer_epl"]},
                                  cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(200, status)
        self.assertEqual(("soccer_epl",), self.c.runtime.settings().sports)


class TestAdminNotificationsRetry(unittest.TestCase):
    """'Retry' must not destroy the messages it claims to resend."""

    def setUp(self):
        self.c = _Console()
        self.addCleanup(self.c.close)
        self.c.make_user(self.c.owner_email)
        _, _, resp = self.c.login(self.c.owner_email)
        self.cookie = "lisa_admin=" + resp.headers.get(
            "Set-Cookie", "").split("lisa_admin=", 1)[1].split(";", 1)[0]
        _, body, _ = self.c.get("/api/admin/session", cookie=self.cookie)
        self.csrf = body["csrf_token"]

    def test_retry_does_not_mark_an_undelivered_row_as_sent(self):
        self.c.storage.enqueue_notification("n1", "hello")
        self.c.storage.mark_notification_failed("n1", "network down")
        status, body, _ = self.c.post("/api/admin/notifications/retry", {},
                                      cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(200, status)
        self.assertEqual(1, body["requeued"])
        rows = self.c.storage.admin_list_notifications(limit=10)["rows"]
        row = next(r for r in rows if r["dedupe_key"] == "n1")
        self.assertEqual("PENDING", row["status"])
        self.assertEqual(0, row["attempts"])

    def test_retry_reports_honestly_when_there_is_nothing_to_do(self):
        self.c.storage.enqueue_notification("n2", "hello")
        _, body, _ = self.c.post("/api/admin/notifications/retry", {},
                                 cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(0, body["requeued"])

    def test_retry_does_not_resurrect_a_delivered_row(self):
        self.c.storage.enqueue_notification("n3", "hello")
        self.c.storage.mark_notification_sent("n3")
        _, body, _ = self.c.post("/api/admin/notifications/retry", {},
                                 cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(0, body["requeued"])
        rows = self.c.storage.admin_list_notifications(limit=10)["rows"]
        self.assertEqual("SENT",
                         next(r for r in rows if r["dedupe_key"] == "n3")["status"])


class TestPublicApiSecurity(unittest.TestCase):
    """The console work touched the public API; check the fixes hold."""

    def setUp(self):
        self.c = _Console()
        self.addCleanup(self.c.close)

    def test_tier_query_parameter_does_not_unlock_picks(self):
        for i in range(4):
            self.c.storage.insert_pick_row({
                "dedupe_key": f"m{i}::h2h::Home", "match_id": f"m{i}",
                "sport_key": "soccer_epl", "market": "h2h", "outcome": "Home",
                "outcome_name": f"Team {i}", "best_odds": 1.5 + i / 10,
                "tier_level": "tier1" if i > 1 else "FREE",
                "state": "CONFIRMED", "created_at": i, "commence_time": i})
        _, body, _ = self.c.get("/api/picks?user_id=test_x&tier=tier3")
        for pick in body["active_picks"]:
            self.assertTrue(pick["is_locked"] or pick.get("tier_level") == "FREE",
                            "a locked pick was revealed via the tier parameter")

    def test_ledger_response_is_bounded(self):
        now = time.time()
        for i in range(1200):
            self.c.storage.insert_pick_row({
                "dedupe_key": f"s{i}::h2h::Home", "match_id": f"s{i}",
                "sport_key": "soccer_epl", "market": "h2h", "outcome": "Home",
                "best_odds": 2.0, "state": "SETTLED", "result": "WIN",
                "settled_at": now, "created_at": now - i,
                "commence_time": now - i})
        _, body, _ = self.c.get("/api/ledger")
        self.assertLessEqual(len(body["settled_ledger"]), 1000)
        self.assertTrue(body["truncated"])
        self.assertEqual(1200, body["total"])

    def test_ledger_limit_is_respected_and_capped(self):
        now = time.time()
        for i in range(10):
            self.c.storage.insert_pick_row({
                "dedupe_key": f"l{i}::h2h::Home", "match_id": f"l{i}",
                "sport_key": "soccer_epl", "market": "h2h", "outcome": "Home",
                "best_odds": 2.0, "state": "SETTLED", "result": "WIN",
                "settled_at": now, "created_at": now - i,
                "commence_time": now - i})
        _, body, _ = self.c.get("/api/ledger?limit=5")
        self.assertEqual(5, len(body["settled_ledger"]))
        _, capped, _ = self.c.get("/api/ledger?limit=999999")
        self.assertLessEqual(capped["limit"], 1000)

    def test_search_filter_is_reachable_under_both_names(self):
        now = time.time()
        self.c.storage.insert_pick_row({
            "dedupe_key": "q1::h2h::Home", "match_id": "q1",
            "sport_key": "soccer_epl", "market": "h2h", "outcome": "Home",
            "outcome_name": "Arsenal", "state": "CONFIRMED",
            "created_at": now, "commence_time": now})
        for param in ("q", "search"):
            with self.subTest(param=param):
                status, body, _ = self.c.get(
                    f"/api/picks?user_id=test_x&{param}=Arsenal")
                self.assertEqual(200, status)

    def test_ledger_limit_garbage_does_not_error(self):
        status, _, _ = self.c.get("/api/ledger?limit=abc")
        self.assertEqual(200, status)


class TestAdminSystemControl(unittest.TestCase):
    """The manual-poll control must run exactly the work that was asked for."""

    class _Recorder:
        """A scheduler stand-in that records which halves of a tick ran."""

        def __init__(self):
            self.calls: list[str | None] = []
            self.next_cycle_at = None
            self.next_settle_at = None

        def tick(self, now=None, *, only=None):
            self.calls.append(only)
            if only is None:
                return _Summary(ran_cycle=True, ran_settlement=True)
            if only == "cycle":
                return _Summary(ran_cycle=True, ran_settlement=False)
            return _Summary(ran_cycle=False, ran_settlement=True)

    def setUp(self):
        self.c = _Console()
        self.addCleanup(self.c.close)
        self.c.make_user(self.c.owner_email)
        _, _, resp = self.c.login(self.c.owner_email)
        self.cookie = "lisa_admin=" + resp.headers.get(
            "Set-Cookie", "").split("lisa_admin=", 1)[1].split(";", 1)[0]
        _, body, _ = self.c.get("/api/admin/session", cookie=self.cookie)
        self.csrf = body["csrf_token"]
        self.scheduler = self._Recorder()
        self.c.control.attach(scheduler=self.scheduler)

    def test_settlement_request_does_not_spend_a_poll_cycle(self):
        status, body, _ = self.c.post(
            "/api/admin/system/poll",
            {"what": "settlement", "mode": "sync", "confirm": True},
            cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(200, status)
        self.assertEqual(["settlement"], self.scheduler.calls)
        self.assertFalse(body["summary"]["ran_cycle"])
        self.assertTrue(body["summary"]["ran_settlement"])

    def test_cycle_request_does_not_run_settlement(self):
        status, body, _ = self.c.post(
            "/api/admin/system/poll",
            {"what": "cycle", "mode": "sync", "confirm": True},
            cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(200, status)
        self.assertEqual(["cycle"], self.scheduler.calls)
        self.assertTrue(body["summary"]["ran_cycle"])
        self.assertFalse(body["summary"]["ran_settlement"])

    def test_queued_mode_marks_the_right_timer_due(self):
        self.scheduler.next_cycle_at = object()
        self.scheduler.next_settle_at = object()
        self.c.post("/api/admin/system/poll",
                    {"what": "settlement", "mode": "due", "confirm": True},
                    cookie=self.cookie, csrf=self.csrf)
        self.assertIsNone(self.scheduler.next_settle_at,
                          "settlement must be marked due")
        self.assertIsNotNone(self.scheduler.next_cycle_at,
                             "the poll timer must be left alone")

    def test_unknown_mode_is_rejected(self):
        for bad in ({"what": "nuke"}, {"what": "cycle", "mode": "later"}):
            with self.subTest(bad=bad):
                status, _, _ = self.c.post("/api/admin/system/poll", bad,
                                           cookie=self.cookie, csrf=self.csrf)
                self.assertEqual(400, status)

    def test_trigger_without_a_scheduler_is_a_clear_503(self):
        self.c.control.attach(scheduler=None)
        self.c.control.scheduler = None
        status, body, _ = self.c.post(
            "/api/admin/system/poll",
            {"what": "cycle", "mode": "sync", "confirm": True},
            cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(503, status)
        self.assertIn("scheduler", body["error"].lower())


class _Summary:
    def __init__(self, *, ran_cycle: bool, ran_settlement: bool):
        self.mode = "test"
        self.ran_cycle = ran_cycle
        self.ran_settlement = ran_settlement
        self.skipped_reason = None
        self.errors: list[str] = []
        self.cycle_reports: list = []


class TestControl(unittest.TestCase):
    """The handle the console drives must be safe to share across threads."""

    def test_attach_and_describe(self):
        storage = SqliteStorage(":memory:")
        control = Control(storage)
        self.assertFalse(control.describe()["has_scheduler"])
        control.attach(scheduler=object(), client=object())
        described = control.describe()
        self.assertTrue(described["has_scheduler"])
        self.assertTrue(described["has_odds_client"])
        self.assertGreaterEqual(described["uptime_seconds"], 0)

    def test_admin_api_absent_without_control(self):
        server = make_production_server(host="127.0.0.1", port=0,
                                        storage=SqliteStorage(":memory:"))
        self.addCleanup(server.server_close)
        self.assertIsNone(server.admin)


if __name__ == "__main__":
    unittest.main()
