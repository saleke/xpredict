"""Guards against fabricated or static data reaching a customer surface.

The product promise is that every displayed number is derived from the odds
feed or the graded ledger. These tests fail if a static file fallback, an
invented booking code, or a placeholder statistic is reintroduced.
"""
from __future__ import annotations

import re
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lisa import config as cfg
from lisa.bulletin import NO_LIVE_DATA, build_live_bulletin, build_live_bulletin_from_payloads
from lisa.client import FixtureClient
from lisa.dashboard import (
    LIVE_ODDS_PREFIX,
    LIVE_SNAPSHOT_KEY,
    TRAPS_KEY,
    build_dashboard,
    live_odds_key,
)
from lisa.fixtures import FIXTURE_SPORTS, ODDS_PAYLOADS
from lisa.gate import Execution, Pick
from lisa.live_ingest import LiveIngestionDaemon
from lisa.storage import InMemoryStorage
from lisa.telegram_bot import (
    TelegramBot,
    format_booking_codes_html,
    format_parlay_html,
    format_stats_html,
)

REPO = Path(__file__).resolve().parents[2]
WEB = REPO / "web"

#: Invented strings that must never appear in code that a customer can see.
FABRICATED_MARKERS = (
    "BC792K", "FC82910", "W49TG", "B941K2", "BW44108",
    "BC2EEA", "FCADFAE", "BC99A1", "BC118F",
)


def _sample_pick(state: str = "TRIGGER_ALERT") -> Pick:
    now = datetime.now(timezone.utc)
    return Pick(
        match_id="real-match-1",
        sport_key="soccer_epl",
        home_team="Arsenal",
        away_team="Chelsea",
        commence_time=now + timedelta(hours=3),
        market="h2h",
        outcome_name="Arsenal",
        p_true=0.61,
        fair_odds=1.64,
        n_books=5,
        stdev=0.01,
        cv=0.02,
        best_execution=Execution(book_key="pinnacle", book_title="Pinnacle", odds=1.70, ev=0.037),
        state=state,
        created_at=now,
        conviction_score=7.0,
        recommended_stake_pct=1.0,
        recommended_units=1.0,
    )


class TestNoStaticDataOnDisk(unittest.TestCase):
    def test_web_data_directory_is_not_tracked(self):
        tracked = [
            p for p in (WEB / "data").glob("**/*") if p.is_file()
        ] if (WEB / "data").exists() else []
        self.assertEqual(
            tracked, [],
            f"static dashboard artifacts must not exist: {[p.name for p in tracked]}",
        )

    def test_gitignore_blocks_web_data(self):
        ignore = (REPO / ".gitignore").read_text()
        self.assertIn("web/data/", ignore)
        self.assertNotIn("!web/data/", ignore)

    def test_server_handlers_never_read_a_static_artifact(self):
        server = (REPO / "engine" / "lisa" / "server.py").read_text()
        # Every /api handler must build its answer from storage or the cache;
        # reading web/data/*.json as a fallback is the bug this prevents.
        self.assertNotIn('"data" / "dashboard.json"', server)
        self.assertNotIn("web/data", server)

    def test_frontend_fetches_api_not_static_json(self):
        app = (WEB / "js" / "app.js").read_text()
        for match in re.finditer(r"""fetch\(\s*['"]([^'"]+)['"]""", app):
            url = match.group(1)
            self.assertFalse(
                url.startswith("data/"),
                f"frontend still fetches a static artifact: {url}",
            )
        self.assertNotIn("fetch('data/", app)
        self.assertNotIn('fetch("data/', app)


class TestNoFabricatedCodesInCode(unittest.TestCase):
    def test_python_sources_have_no_invented_codes(self):
        offenders: list[str] = []
        for path in (REPO / "engine" / "lisa").glob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for marker in FABRICATED_MARKERS:
                if marker in text:
                    offenders.append(f"{path.name}:{marker}")
        self.assertEqual(offenders, [], f"invented booking codes remain: {offenders}")

    def test_frontend_has_no_invented_codes(self):
        app = (WEB / "js" / "app.js").read_text()
        for marker in FABRICATED_MARKERS:
            self.assertNotIn(marker, app)

    def test_visualizer_is_built_from_the_ledger_not_canned_examples(self):
        app = (WEB / "js" / "app.js").read_text()
        html = (WEB / "index.html").read_text()
        # No hardcoded "Bet365 1.17"-style quote strings anywhere.
        self.assertNotRegex(app, r"quotes:\s*[\"']\w+\s\d")
        self.assertNotIn("Bet365 1.17", html)
        self.assertNotIn("Oklahoma City Thunder vs Washington Wizards", html)
        self.assertNotIn("88.7%", html)
        self.assertIn("visualizerScenarios()", app)
        self.assertIn("state.dashboard", app)

    def test_feed_controlled_text_is_escaped_before_innerhtml(self):
        app = (WEB / "js" / "app.js").read_text()
        self.assertIn("function esc(", app, "an HTML escaper must exist for feed text")
        # cleanText() strips formatting but does not encode markup, so it must
        # not be interpolated into innerHTML on its own.
        self.assertNotRegex(app, r"\$\{cleanText\(")
        for field in ("home_team", "away_team", "outcome_name", "best_book", "match_id"):
            self.assertNotRegex(app, r"\$\{[a-zA-Z_$][\w$]*\.%s\}" % field)

    def test_teaser_has_no_placeholder_numbers(self):
        app = (WEB / "js" / "app.js").read_text()
        teaser = app[app.index("// 5. Dynamic Featured Slate Preview Teaser"):
                     app.index("function renderKPIs")]
        for pattern in (r"picks\.length \|\| \d", r"p_true \|\| 0\.\d",
                        r"best_odds \?[^:\n]*:\s*[\"']\d",
                        r"Predictions Analyzed Today"):
            self.assertNotRegex(teaser, pattern)


class TestDashboardIsRealOnly(unittest.TestCase):
    def test_empty_ledger_yields_null_metrics_not_defaults(self):
        payload = build_dashboard(InMemoryStorage())
        summary = payload["summary"]
        self.assertIsNone(summary["win_rate"])
        self.assertIsNone(summary["brier_score"])
        self.assertIsNone(summary["mean_clv"])
        self.assertEqual(summary["settled_picks_count"], 0)
        self.assertEqual(payload["active_picks"], [])
        self.assertFalse(payload["meta"]["data_provenance"]["synthetic"])

    def test_metrics_come_from_graded_rows(self):
        storage = InMemoryStorage()
        pick = _sample_pick()
        storage.insert_pick(pick)
        key = f"{pick.match_id}::{pick.market}::{pick.outcome_name}"
        storage.settle_pick(key, "WIN", datetime.now(timezone.utc))

        payload = build_dashboard(storage)
        self.assertEqual(payload["summary"]["settled_picks_count"], 1)
        self.assertEqual(payload["summary"]["win_rate"], 1.0)
        self.assertEqual(payload["summary"]["won_count"], 1)
        self.assertEqual(payload["active_picks"], [])

    def test_ungraded_row_is_not_counted_as_settled(self):
        storage = InMemoryStorage()
        storage.insert_pick(_sample_pick(state="TRIGGER_ALERT"))
        payload = build_dashboard(storage)
        self.assertEqual(payload["summary"]["settled_picks_count"], 0)
        self.assertIsNone(payload["summary"]["win_rate"])
        self.assertEqual(len(payload["active_picks"]), 1)

    def test_quotes_and_traps_come_from_the_live_cache(self):
        storage = InMemoryStorage()
        daemon = LiveIngestionDaemon(
            client=FixtureClient(ODDS_PAYLOADS), settings=cfg.Settings(sports=FIXTURE_SPORTS),
            storage=storage, auto_settle=False,
        )
        daemon.run_cycle()
        match_ids = []
        for key in storage.scan_live_keys():
            if key.startswith(LIVE_ODDS_PREFIX):
                from lisa.parsing import parse_odds_payload
                entry = storage.get_live(key)
                match_ids += [m.id for m in parse_odds_payload(entry["payload"])]
        self.assertTrue(match_ids)

        pick = replace(
            _sample_pick(), match_id=match_ids[0],
            sport_key="basketball_nba", home_team="Celtics", away_team="Knicks",
        )
        storage.insert_pick(pick)
        storage.upsert_live(TRAPS_KEY, [
            {"home_team": "Roma", "away_team": "Lazio", "public_favorite": "Roma",
             "cv": 0.11, "detected_at": "2026-01-02T00:00:00Z"},
            {"home_team": "NoMetric", "away_team": "X", "cv": None},
        ], 3600)

        payload = build_dashboard(storage)
        quotes = payload["active_picks"][0]["quotes"]
        self.assertIsNotNone(quotes, "a cached fixture must expose its real book prices")
        self.assertGreaterEqual(quotes["n_books"], 1)
        self.assertGreater(quotes["margin"], 0)
        self.assertTrue(all(b["book_title"] for b in quotes["books"]))

        traps = payload["traps"]
        self.assertEqual(len(traps), 1, "traps without a metric are not reportable")
        self.assertEqual(traps[0]["home_team"], "Roma")

    def test_live_state_reports_never_without_snapshot(self):
        payload = build_dashboard(InMemoryStorage())
        self.assertEqual(payload["live"]["state"], "never")
        self.assertIsNone(payload["live"]["age_sec"])

    def test_cards_carry_no_booking_codes(self):
        storage = InMemoryStorage()
        storage.insert_pick(_sample_pick())
        payload = build_dashboard(storage)
        for card in payload["active_picks"]:
            self.assertNotIn("booking_codes", card)
            self.assertNotIn("accumulator_booking_codes", payload)


class TestLiveFeedOnlyBulletin(unittest.TestCase):
    def test_no_snapshot_returns_explicit_empty_board(self):
        board = build_live_bulletin_from_payloads([])
        self.assertEqual(board["mode"], "no_live_data")
        self.assertEqual(board["matches"], [])
        self.assertEqual(board["count"], 0)
        self.assertIn("No live fixture snapshot", board["disclaimer"])

    def test_no_live_data_constant_is_empty(self):
        self.assertEqual(NO_LIVE_DATA["count"], 0)
        self.assertEqual(NO_LIVE_DATA["matches"], [])

    def test_board_built_from_real_payload_has_live_mode(self):
        settings = cfg.Settings(sports=FIXTURE_SPORTS)
        daemon_client = FixtureClient(ODDS_PAYLOADS)
        storage = InMemoryStorage()
        daemon = LiveIngestionDaemon(
            client=daemon_client, settings=settings, storage=storage, auto_settle=False
        )
        daemon.run_cycle()

        payloads = []
        for key in storage.scan_live_keys():
            if key.startswith("live:odds:"):
                entry = storage.get_live(key)
                payloads.append((key, entry["payload"]))
        self.assertTrue(payloads, "daemon must cache the raw odds payloads")
        self.assertTrue(storage.get_live_stale(LIVE_SNAPSHOT_KEY))

        # The fixture kickoffs are fixed dates, so evaluate just before them.
        before_fixtures = datetime(2026, 9, 19, tzinfo=timezone.utc)
        board = build_live_bulletin_from_payloads(
            [(str(k), v) for k, v in payloads], now=before_fixtures
        )
        self.assertEqual(board["mode"], "live")
        for row in board["matches"]:
            self.assertIn("home", row)
            self.assertIn("uncertainty", row)
            if row["market"]:
                self.assertGreaterEqual(row["market"]["n_books"], 1)

    def test_empty_bulletin_helper_returns_no_live_data(self):
        self.assertEqual(build_live_bulletin([])["mode"], "no_live_data")


class TestBotSurfacesAreReal(unittest.TestCase):
    def test_stats_page_has_no_default_numbers(self):
        html = format_stats_html({})
        self.assertIn("No settled picks yet", html)
        self.assertNotIn("84.0%", html)

    def test_parlay_without_legs_refuses_to_invent(self):
        text, _ = format_parlay_html([], user_tier="tier3")
        self.assertIn("Not enough live selections", text)
        for marker in FABRICATED_MARKERS:
            self.assertNotIn(marker, text)

    def test_parlay_computes_from_supplied_legs(self):
        picks = [
            {
                "home_team": "A", "away_team": "B", "outcome_name": "A",
                "p_true": 0.60, "best_odds": 1.80, "best_ev": 0.08, "best_book": "pinnacle",
                "commence_time": "2026-01-01T18:00:00Z",
            },
            {
                "home_team": "C", "away_team": "D", "outcome_name": "C",
                "p_true": 0.55, "best_odds": 1.95, "best_ev": 0.07, "best_book": "bet365",
                "commence_time": "2026-01-01T20:00:00Z",
            },
        ]
        text, _ = format_parlay_html(picks, user_tier="tier3")
        self.assertIn("LISA LIVE ACCUMULATOR", text)
        self.assertIn("3.51", text)          # 1.80 * 1.95
        self.assertIn("33.0%", text)         # 0.60 * 0.55
        for marker in FABRICATED_MARKERS:
            self.assertNotIn(marker, text)

    def test_codes_page_states_there_is_no_integration(self):
        text, _ = format_booking_codes_html()
        self.assertIn("does not generate bookmaker booking codes", text)
        for marker in FABRICATED_MARKERS:
            self.assertNotIn(marker, text)

    def test_bot_picks_come_from_ledger(self):
        storage = InMemoryStorage()
        bot = TelegramBot(token="", channel_chat_id="@t", mock=True, storage=storage)
        self.assertIn("No active signals", bot._handle_active_top_picks("u")[0])

        storage.insert_pick(_sample_pick())
        bot._picks_cache = ({}, 0.0)
        text, _ = bot._handle_active_top_picks("u")
        self.assertIn("Arsenal vs Chelsea", text)
        for marker in FABRICATED_MARKERS:
            self.assertNotIn(marker, text)

    def test_traps_page_reads_recorded_history(self):
        storage = InMemoryStorage()
        bot = TelegramBot(token="", channel_chat_id="@t", mock=True, storage=storage)
        self.assertIn("No trap advisories", bot._handle_traps()[0])

        storage.upsert_live("live:traps", [
            {"home_team": "Roma", "away_team": "Lazio", "public_favorite": "Roma", "cv": 0.09},
        ], 3600)
        text, _ = bot._handle_traps()
        self.assertIn("Roma vs Lazio", text)
        self.assertIn("9.0%", text)
        self.assertNotIn("Capital Preserved", text)


class TestCreditDiscipline(unittest.TestCase):
    def test_daemon_stops_polling_at_the_credit_floor(self):
        settings = cfg.Settings(sports=FIXTURE_SPORTS, credit_stop=20, credit_warn=100)
        client = FixtureClient(ODDS_PAYLOADS)
        client.last_remaining = 5
        storage = InMemoryStorage()
        daemon = LiveIngestionDaemon(
            client=client, settings=settings, storage=storage, auto_settle=False
        )
        result = daemon.run_cycle()
        self.assertEqual(result.leagues_polled, 0)
        self.assertEqual(result.matches_seen, 0)
        self.assertTrue(any("quota floor" in e for e in result.errors))
        # The last observation is still visible, and marked as not live.
        snapshot = storage.get_live_stale(LIVE_SNAPSHOT_KEY)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["matches_observed"], 0)

    def test_quota_block_preserves_the_last_observation(self):
        settings = cfg.Settings(sports=FIXTURE_SPORTS, credit_stop=20, credit_warn=100)
        client = FixtureClient(ODDS_PAYLOADS)
        storage = InMemoryStorage()
        daemon = LiveIngestionDaemon(
            client=client, settings=settings, storage=storage, auto_settle=False
        )
        daemon.run_cycle()
        first = storage.get_live_stale(LIVE_SNAPSHOT_KEY)
        self.assertGreater(int(first["matches_observed"]), 0)

        client.last_remaining = 3
        daemon.run_cycle()
        after = storage.get_live_stale(LIVE_SNAPSHOT_KEY)
        self.assertEqual(
            int(after["matches_observed"]), int(first["matches_observed"]),
            "a blocked cycle must not erase the last real observation",
        )
        self.assertIsNotNone(after["observed_at"])
        self.assertTrue(after["sports"])
        self.assertTrue(any("quota floor" in (e or "") for e in
                            [v.get("last_error") for v in [after]]))

    def test_failed_league_is_not_refetched(self):
        """A failed prefetch must not become a second billable request."""
        settings = cfg.Settings(sports=FIXTURE_SPORTS)
        attempts: list[str] = []

        class FlakyClient(FixtureClient):
            def get_odds(self, sport_key, **kwargs):
                attempts.append(sport_key)
                if sport_key == FIXTURE_SPORTS[0]:
                    raise RuntimeError("upstream 503")
                return super().get_odds(sport_key, **kwargs)

        storage = InMemoryStorage()
        daemon = LiveIngestionDaemon(
            client=FlakyClient(ODDS_PAYLOADS), settings=settings,
            storage=storage, auto_settle=False,
        )
        daemon.run_cycle()
        self.assertEqual(
            len(attempts), len(set(attempts)),
            f"a league was fetched more than once: {attempts}",
        )

    def test_one_upstream_request_per_league(self):
        settings = cfg.Settings(sports=FIXTURE_SPORTS)
        calls: list[str] = []

        class CountingClient(FixtureClient):
            def get_odds(self, sport_key, **kwargs):
                calls.append(sport_key)
                return super().get_odds(sport_key, **kwargs)

        client = CountingClient(ODDS_PAYLOADS)
        storage = InMemoryStorage()
        daemon = LiveIngestionDaemon(
            client=client, settings=settings, storage=storage, auto_settle=False
        )
        daemon.run_cycle()
        self.assertEqual(
            sorted(calls), sorted(FIXTURE_SPORTS),
            "a cycle must not double-fetch a league (it burns API credits)",
        )

    def test_raw_payload_is_cached_for_the_board(self):
        settings = cfg.Settings(sports=FIXTURE_SPORTS)
        storage = InMemoryStorage()
        daemon = LiveIngestionDaemon(
            client=FixtureClient(ODDS_PAYLOADS), settings=settings,
            storage=storage, auto_settle=False,
        )
        daemon.run_cycle()
        keys = [k for k in storage.scan_live_keys() if k.startswith("live:odds:")]
        self.assertTrue(keys)
        entry = storage.get_live(live_odds_key(FIXTURE_SPORTS[0]))
        self.assertIsInstance(entry["payload"], list)
        self.assertIn("observed_at", entry)


if __name__ == "__main__":
    unittest.main()


def test_start_does_not_seed_the_operational_ledger_from_backtest():
    """A fresh install must show an empty ledger, not simulated history.

    `lisa start` used to seed the picks table from the backtest archive when it
    was empty, inventing the fields the archive lacks (fixed book count,
    fabricated dispersion, closing odds = best odds * 0.96). The dashboard would
    then present simulated accuracy and CLV as results.
    """
    import inspect
    from lisa import cli

    source = inspect.getsource(cli._cmd_start)
    assert "BacktestEngine" not in source, (
        "_cmd_start must not build backtest rows into the operational ledger"
    )
    assert "best_odds * 0.96" not in source
    assert "insert_pick_row" not in source
