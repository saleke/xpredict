"""Offline lifecycle release checks; runnable with stdlib unittest or pytest.

All fixtures and prices here are deliberately artificial test data. No provider
calls, production writes, or Telegram messages occur.
"""
from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlparse

from lisa import config, feed
from lisa.board import Board, BoardCoverage, Fixture, MarketPrice, Opportunity, OpportunityBoard
from lisa.daily_service import DailyService, BOARD_KEY, MODEL_VERSION
from lisa.dixon_coles import DixonColesModel, ScoredMatch
from lisa.odds import Score
from lisa.payments import key_store
from lisa.providers.base import FixturesResult
from lisa.server import LISAProductionHandler
from lisa.storage import SqliteStorage
from lisa.telegram_bot import TelegramBot

NOW = datetime.now(timezone.utc).replace(microsecond=0)
LEAGUE = "soccer_germany_bundesliga"


def opportunity(**kwargs):
    defaults = dict(match_id="test-match", sport_key=LEAGUE, kickoff=NOW + timedelta(hours=2),
        home="Test Home", away="Test Away", market="h2h", selection="Home",
        p_model=0.7, fair_odds=1 / 0.7, priced=True, best_odds=1.8,
        best_book="Test Book", ev=0.26, stake_fraction=0.02, basis="model_vs_market",
        price_updated_at=NOW)
    return Opportunity(**dict(defaults, **kwargs))


def report(rows=None, results=None, *, error=None):
    rows = rows if rows is not None else [opportunity()]
    board = Board(NOW, 24, False, {"sufficient": True}, winning=tuple(rows),
        micro_bets=(opportunity(market="btts", selection="Yes"),
                    opportunity(market="correct_score", selection="2-1")),
        coverage=BoardCoverage(24, len(rows), len(rows), len(rows), False, 12))
    return feed.FeedReport(began=NOW, board=board, results=results or [],
        forecast={"matches": [{"match_id": "test-match", "commence_at": opportunity().kickoff.isoformat()}], "count": 1},
        errors=[error] if error else [])


def final_result(**kwargs):
    return dict(match_id="test-match", sport_key=LEAGUE, completed=True, home_score=2,
        away_score=1, home_team="Test Home", away_team="Test Away", provider="test-official",
        status="FINISHED", kickoff=opportunity().kickoff.isoformat(), **kwargs)


class Handler(LISAProductionHandler):
    def __init__(self, server, user=None):
        self.server = server
        self.user = user

    def _get_current_user_and_session(self):
        return self.user, None

    def _send_json(self, data, status=200):
        self.response = data
        self.response_status = status


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / "test.db")
        self.storage = SqliteStorage(self.path)
        self.settings = config.Settings(board_leagues=(LEAGUE,), enable_sharpapi=False,
                                        enable_sportsdb=False)
        self.runner = Mock(return_value=report())
        self.service = DailyService(self.storage, self.settings, runner=self.runner,
                                   providers=feed.ProviderSet())

    def test_changed_provider_id_settles_only_exact_fixture(self):
        self.service.tick(now=NOW)
        result = final_result()
        result["match_id"] = "alternate-provider-id"
        self.runner.return_value = report([], [result])
        self.assertEqual(self.service.tick(now=NOW + timedelta(days=2))["settled"], 3)

    def test_invalid_final_score_never_settles(self):
        self.service.tick(now=NOW)
        for value in (-1, True, "2"):
            result = final_result()
            result["home_score"] = value
            self.runner.return_value = report([], [result])
            self.assertEqual(self.service.tick(now=NOW + timedelta(days=2))["settled"], 0)

    def test_explicit_cancellation_voids_saved_forecasts(self):
        self.service.tick(now=NOW)
        result = final_result()
        result.update(status="CANCELLED", completed=False, home_score=None, away_score=None)
        self.runner.return_value = report([], [result])
        self.assertEqual(self.service.tick(now=NOW + timedelta(days=2))["settled"], 3)
        self.assertTrue(all(r["result"] == "VOID" for r in self.storage.list_settled_picks()))

    def test_boot_worker_generates_without_http_request(self):
        import threading
        ran = threading.Event()
        self.runner.side_effect = lambda *a, **k: (ran.set(), report())[1]
        self.service.start()
        try:
            self.assertTrue(ran.wait(2))
        finally:
            self.service.stop()
        self.assertTrue(self.runner.called)

    def test_forecasts_do_not_fabricate_financial_returns(self):
        from lisa.accuracy_tracker import AccuracyTracker
        tracker = AccuracyTracker(storage=self.storage)
        rows = [{"result": "WIN", "fair_odds": 9, "is_recommendation": False},
                {"result": "WIN", "best_odds": 2, "is_recommendation": True},
                {"result": "LOSS", "best_odds": 2, "is_recommendation": True}]
        self.assertEqual(tracker._calculate_roi(rows), 0)
        self.assertEqual(tracker._calculate_roi([rows[0]]), 0)

    def test_generation_without_visitors_and_restart_persistence(self):
        self.assertEqual(self.service.tick(now=NOW)["predictions_added"], 3)
        restarted = DailyService(SqliteStorage(self.path), self.settings)
        self.assertIsNotNone(restarted.read())
        self.assertEqual(len(restarted.storage.list_pending_picks()), 3)

    def test_repeated_generation_keeps_first_probability_price_and_stake(self):
        self.service.tick(now=NOW)
        before = self.storage.list_pending_picks()
        self.runner.return_value = report([opportunity(p_model=0.5, best_odds=2.2)])
        self.assertEqual(self.service.tick(now=NOW)["predictions_added"], 0)
        self.assertEqual(before, self.storage.list_pending_picks())

    def test_different_totals_lines_do_not_collide(self):
        self.runner.return_value = report([opportunity(market="totals", selection="Over", line=x)
                                           for x in (2.5, 3.5)])
        self.service.tick(now=NOW)
        self.assertEqual({p["line"] for p in self.storage.list_pending_picks() if p["market"] == "totals"}, {2.5, 3.5})

    def test_official_results_grade_all_micro_and_match_markets(self):
        self.service.tick(now=NOW)
        self.runner.return_value = report([], [final_result()])
        self.assertEqual(self.service.tick(now=NOW + timedelta(days=2))["settled"], 3)
        rows = self.storage.list_settled_picks()
        self.assertTrue(all(p["result"] == "WIN" and p["actual_score"] == "2-1" for p in rows))
        self.assertTrue(all(p["result_source"] == "test-official" for p in rows))
        self.assertEqual(self.service.tick(now=NOW + timedelta(days=3))["settled"], 0)

    def test_overdue_recovery_uses_saved_leagues_even_after_configuration_change(self):
        self.service.tick(now=NOW)
        self.service._settings = replace(self.settings, board_leagues=("soccer_epl",))
        self.runner.return_value = report([], [final_result()])
        self.service.tick(now=NOW + timedelta(days=5))
        self.assertIn(LEAGUE, self.runner.call_args.args[0].board_leagues)
        self.assertEqual(len(self.storage.list_settled_picks()), 3)

    def test_missing_or_nonfinal_scores_stay_pending(self):
        self.service.tick(now=NOW)
        r = final_result()
        r.update(home_score=None, away_score=None)
        self.runner.return_value = report([], [r])
        self.assertEqual(self.service.tick(now=NOW + timedelta(days=2))["settled"], 0)
        self.assertEqual(len(self.storage.list_pending_picks()), 3)

    def test_pause_blocks_publication_but_continues_overdue_settlement(self):
        self.service.tick(now=NOW)
        self.storage.set_system_paused(True)
        self.runner.return_value = report([opportunity(match_id="new-match")], [final_result()])
        result = self.service.tick(now=NOW + timedelta(days=2))
        self.assertEqual(result["predictions_added"], 0)
        self.assertEqual(result["settled"], 3)
        self.assertEqual(self.service.status()["state"], "paused")

    def test_quiet_day_publishes_a_dated_shortfall_report(self):
        r = report([])
        r.board.micro_bets = ()
        r.forecast = {"matches": [], "count": 0}
        self.runner.return_value = r
        self.service.tick(now=NOW)
        self.assertEqual(self.service.read()["forecast"]["count"], 0)
        with self.storage._tx() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM daily_publications").fetchone()[0], 1)

    def test_provider_failure_keeps_previous_publication_and_marks_not_ready(self):
        self.service.tick(now=NOW)
        self.runner.side_effect = RuntimeError("test outage")
        with self.assertLogs("lisa.daily_service", level="ERROR"):
            self.service.tick(now=NOW)
        self.assertIsNotNone(self.service.read())
        self.assertFalse(self.service.status(now=NOW)["ready"])
        self.assertIn("test outage", self.service.status(now=NOW)["error"])

    def test_expired_lease_cannot_publish_and_concurrent_worker_cannot_fetch(self):
        self.assertTrue(self.service._claim(NOW))
        second_runner = Mock(return_value=report())
        second = DailyService(self.storage, self.settings, runner=second_runner)
        self.assertEqual(second.tick(now=NOW)["state"], "another_worker")
        second_runner.assert_not_called()
        with self.assertRaises(RuntimeError):
            self.service._publish(report(), NOW + timedelta(minutes=10))
        self.assertEqual(self.storage.count_picks()["total"], 0)

    def test_publication_transaction_rolls_back_if_payload_is_invalid(self):
        r = report()
        r.model = {"bad": float("nan")}
        self.assertTrue(self.service._claim(NOW))
        with self.assertRaises(ValueError):
            self.service._publish(r, NOW)
        self.assertEqual(self.storage.count_picks()["total"], 0)
        self.assertIsNone(self.storage.get_telemetry(BOARD_KEY))

    def test_no_late_prediction_is_inserted(self):
        self.service.tick(now=NOW + timedelta(hours=3))
        self.assertEqual(self.storage.count_picks()["total"], 0)

    def test_http_reads_do_not_fetch_and_free_view_only_exposes_its_featured_pick(self):
        self.service.tick(now=NOW)
        h = Handler(SimpleNamespace(storage=self.storage, daily_service=self.service, settings=self.settings))
        h._handle_opportunity_board(urlparse("/api/opportunity-board"))
        self.assertEqual(self.runner.call_count, 1)
        self.assertEqual(len(h.response["board"]["winning"]), 1)
        self.assertEqual(h.response["board"]["winning"][0]["best_odds"], 1.8)
        self.assertEqual(h.response["board"]["micro_bets"], [])
        self.assertFalse(h.response["board"]["research_access"]["available"])
        h._handle_opportunity_board(urlparse("/api/opportunity-board?refresh=1"))
        self.assertEqual(h.response_status, 403)
        self.assertEqual(self.runner.call_count, 1)
        paid = Handler(h.server, {"tier": "tier1"})
        paid._handle_opportunity_board(urlparse("/api/opportunity-board"))
        self.assertEqual(paid.response["board"]["earning"][0]["best_odds"], 1.8)

    def test_forecast_and_result_endpoints_use_publications_and_ledger(self):
        self.service.tick(now=NOW)
        h = Handler(SimpleNamespace(storage=self.storage, daily_service=self.service, settings=self.settings))
        h._handle_forecast()
        self.assertEqual(h.response["count"], 1)
        self.runner.return_value = report([], [final_result()])
        self.service.tick(now=NOW + timedelta(days=2))
        h._handle_ledger(urlparse("/api/ledger"))
        self.assertEqual(h.response["count"], 3)

    def test_health_distinguishes_starting_current_and_stale(self):
        self.assertFalse(self.service.status(now=NOW)["ready"])
        self.service.tick(now=NOW)
        self.assertTrue(self.service.status(now=NOW)["ready"])
        self.assertFalse(self.service.status(now=NOW + timedelta(hours=2))["ready"])

    def test_worker_shutdown_interrupts_long_interval_wait(self):
        self.service._settings = replace(self.settings, daily_interval_sec=86400)
        with patch.object(self.service, "tick", return_value={}):
            self.service.start()
            self.service.stop()
        self.assertFalse(self.service._thread.is_alive())

    def test_user_payment_confirmation_never_issues_an_activation_key(self):
        bot = TelegramBot(mock=True, storage=self.storage)
        before = set(key_store._keys)
        message, _ = bot._handle_payment_confirmation("usdt_trc20", "tier3", "123456789")
        self.assertEqual(before, set(key_store._keys))
        self.assertIn("No activation key", message)


class ModelIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.training = [ScoredMatch(LEAGUE, NOW - timedelta(days=200 - i // 2),
            f"Test Team {i % 8}", f"Test Team {(i + 3) % 8}", i % 4, (i + 1) % 3) for i in range(240)]
        cls.model = DixonColesModel()
        cls.model.fit(cls.training, as_of=NOW)
        cls.fixtures = [Fixture(f"test-{i}", LEAGUE, NOW + timedelta(hours=12 + i),
            f"Test Team {i % 8}", f"Test Team {(i + 3) % 8}", "openligadb") for i in range(12)]

    def test_double_chance_probabilities_are_consistent_with_score_distribution(self):
        p = self.model.predict(self.fixtures[0].home, self.fixtures[0].away)
        self.assertAlmostEqual(p['double_chance']['1X'], 1 - p['p_away'])
        self.assertAlmostEqual(p['double_chance']['X2'], 1 - p['p_home'])
        self.assertAlmostEqual(p['double_chance']['12'], 1 - p['p_draw'])
        self.assertAlmostEqual(sum(p['double_chance'].values()), 2)

    def test_team_goal_totals_match_score_matrix_marginals(self):
        f = self.fixtures[0]
        prediction = self.model.predict(f.home, f.away)
        grid = self.model.score_matrix(f.home, f.away)
        expected_home = sum(w for x, row in enumerate(grid) for w in row if x > 1.5)
        expected_away = sum(w for row in grid for y, w in enumerate(row) if y > 1.5)
        self.assertAlmostEqual(prediction['home_team_over']['1.5'], expected_home)
        self.assertAlmostEqual(prediction['away_team_over']['1.5'], expected_away)
        expected_total = sum(w for x, row in enumerate(grid) for y, w in enumerate(row) if x + y > 2.5)
        self.assertAlmostEqual(prediction['over']['2.5'], expected_total)
        self.assertIn('7.5', prediction['over'])

    def test_winning_selection_is_not_forced_to_1x2(self):
        candidates = [opportunity(p_model=0.6),
            opportunity(market='double_chance', selection='1X', p_model=0.75, fair_odds=1 / 0.75)]
        self.assertEqual(OpportunityBoard(self.model)._winning_ladder(candidates)[0].market, 'double_chance')

    def test_accumulator_can_mix_markets_but_never_repeat_a_match(self):
        candidates = [opportunity(match_id='first', market='double_chance', selection='1X', p_model=0.75),
                      opportunity(match_id='second', market='totals', selection='Over', line=1.5, p_model=0.7)]
        board = OpportunityBoard(self.model, min_accumulator_prob=0.1)
        accas = board._accumulators(self.fixtures, {}, candidates=candidates)
        self.assertTrue(accas)
        self.assertEqual({leg.market for leg in accas[0].legs}, {'double_chance', 'totals'})
        self.assertEqual(len({leg.match_id for leg in accas[0].legs}), len(accas[0].legs))

    def test_new_markets_settle_draws_and_team_goal_pushes(self):
        score = Score('test', LEAGUE, NOW, True, 1, 1, 'final')
        self.assertEqual(score.grade_pick('double_chance', '1X'), 'WIN')
        self.assertEqual(score.grade_pick('double_chance', 'X2'), 'WIN')
        self.assertEqual(score.grade_pick('double_chance', '12'), 'LOSS')
        self.assertEqual(score.grade_pick('home_team_totals', 'Over', 1.5), 'LOSS')
        self.assertEqual(score.grade_pick('away_team_totals', 'Under', 1.5), 'WIN')
        self.assertEqual(score.grade_pick('home_team_totals', 'Over', 1), 'VOID')
        self.assertEqual(score.grade_pick('home_team_totals', 'Over', 1.25), 'HALF_LOSS')

    def test_settings_reach_selection_and_risk_sizing(self):
        base = OpportunityBoard(self.model).build(self.fixtures, now=NOW)
        prices = {o.match_id: [MarketPrice(o.match_id, o.selection, 1.2 / o.p_model, "testbook", market=o.market, line=o.line)]
                  for o in base.winning}
        strict = feed.build_board(self.model, config.Settings(board_min_ev=9)).build(self.fixtures, prices, now=NOW)
        self.assertEqual(len(strict.earning), 0)
        self.assertTrue(all(o.stake_fraction == 0 for o in strict.winning))

    def test_accumulators_never_invent_a_bookmaker_parlay_offer(self):
        base = OpportunityBoard(self.model).build(self.fixtures, now=NOW)
        prices = {o.match_id: [MarketPrice(o.match_id, o.selection, 1.2 / o.p_model, f"book{i%2}", market=o.market)]
                  for i, o in enumerate(base.winning)}
        accas = OpportunityBoard(self.model).build(self.fixtures, prices, now=NOW).accumulators
        self.assertTrue(accas)
        self.assertTrue(all(not a.priced and a.ev is None and a.stake_fraction == 0 for a in accas))

    def test_credentials_expand_default_calendar_coverage(self):
        self.assertIn("soccer_epl", feed._default_leagues(config.Settings()))
        self.assertNotIn("soccer_epl", feed._default_leagues(config.Settings(enable_openfootball=False)))
        self.assertIn("soccer_epl", feed._default_leagues(config.Settings(football_data_token="test-token")))

    def test_aliases_correct_score_and_pushes_are_graded(self):
        score = Score("test", LEAGUE, NOW, True, 2, 1, "final", "Real Home", "Real Away")
        self.assertEqual(score.grade_pick("h2h", "Home"), "WIN")
        self.assertEqual(score.grade_pick("h2h", "Away"), "LOSS")
        self.assertEqual(score.grade_pick("correct_score", "2-1"), "WIN")
        self.assertEqual(score.grade_pick("correct_score", "1-2"), "LOSS")
        self.assertIsNone(score.grade_pick("correct_score", "broken"))
        self.assertEqual(score.grade_pick("totals", "Over", 3), "VOID")


if __name__ == "__main__":
    unittest.main()
