"""Unit tests for LISA Interactive Telegram Bot & Live Ingestion Dispatch."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from lisa import config as cfg
from lisa.client import FixtureClient
from lisa.fixtures import FIXTURE_SPORTS, ODDS_PAYLOADS, SCORES_PAYLOADS
from lisa.gate import Execution, Pick
from lisa.live_ingest import LiveIngestionDaemon
from lisa.telegram_bot import (
    TelegramBot,
    TelegramUpdate,
    format_diamond_alert_html,
    format_free_picks_html,
    format_stats_html,
    format_trap_advisory_html,
    generate_unlock_token,
    verify_unlock_token,
)


class TestTelegramBot(unittest.TestCase):
    def setUp(self):
        self.bot = TelegramBot(token="", channel_chat_id="@test_channel", mock=True)
        self.now = datetime.now(timezone.utc)
        self.sample_pick = Pick(
            match_id="test-match-101",
            sport_key="soccer_epl",
            home_team="Arsenal",
            away_team="Wolverhampton",
            commence_time=self.now,
            market="h2h",
            outcome_name="Arsenal",
            p_true=0.8105,
            fair_odds=1.2338,
            n_books=5,
            stdev=0.008,
            cv=0.012,
            best_execution=Execution(
                book_key="pinnacle",
                book_title="Pinnacle",
                odds=1.25,
                ev=0.013,
            ),
            state="ACTIVE",
            created_at=self.now,
            conviction_score=8.5,
            recommended_stake_pct=1.5,
            recommended_units=1.5,
        )

    def test_unlock_token_generation_and_verification(self):
        token1 = generate_unlock_token("user_12345")
        self.assertTrue(token1.startswith("LISA-"))
        self.assertEqual(len(token1), 11)
        self.assertTrue(verify_unlock_token(token1))

        # Consistent generation with same seed
        token2 = generate_unlock_token("user_12345")
        self.assertEqual(token1, token2)

        # Verification of 6-digit code
        self.assertTrue(verify_unlock_token("AB12CD"))
        self.assertFalse(verify_unlock_token(""))
        self.assertFalse(verify_unlock_token("invalid-too-long-string-code"))

    def test_format_diamond_alert_html(self):
        html = format_diamond_alert_html(self.sample_pick)
        self.assertIn("LISA INSTITUTIONAL DIAMOND ALERT", html)
        self.assertIn("Arsenal vs Wolverhampton", html)
        self.assertIn("81.0%", html)
        self.assertIn("Pinnacle", html)
        self.assertIn("8.5/10.0", html)
        self.assertIn("1.5u", html)

    def test_format_trap_advisory_html(self):
        html = format_trap_advisory_html(
            home_team="Manchester United",
            away_team="Tottenham",
            sport_key="soccer_epl",
            public_favorite="Manchester United",
            reason="High cross-bookmaker variance",
            cv=0.084,
        )
        self.assertIn("LISA TRAP ADVISORY", html)
        self.assertIn("Manchester United vs Tottenham", html)
        self.assertIn("8.4%", html)
        self.assertIn("30 Traps avoided this month", html)

    def test_format_stats_html(self):
        summary = {
            "win_rate": 0.840,
            "brier_score": 0.1305,
            "ece": 0.0361,
            "mean_clv": 0.0312,
            "traps_avoided_month": 30,
            "settled_picks_count": 50,
        }
        html = format_stats_html(summary)
        self.assertIn("84.0%", html)
        self.assertIn("0.1305", html)
        self.assertIn("3.61%", html)
        self.assertIn("+3.12%", html)
        self.assertIn("30 sucker bets avoided", html)

    def test_format_free_picks_html(self):
        picks = [
            {
                "home_team": "Arsenal",
                "away_team": "Wolverhampton",
                "outcome_name": "Arsenal",
                "p_true": 0.81,
                "best_odds": 1.23,
                "recommended_units": 1.0,
            },
            {
                "home_team": "Man City",
                "away_team": "Ipswich",
                "outcome_name": "Man City",
                "p_true": 0.85,
                "best_odds": 1.18,
                "recommended_units": 1.0,
            },
        ]
        html = format_free_picks_html(picks)
        self.assertIn("LISA TODAY'S TOP SELECTIONS", html)
        self.assertIn("Arsenal vs Wolverhampton", html)
        self.assertIn("Man City vs Ipswich", html)

    def test_command_start_default_and_deeplink(self):
        # Default /start
        reply, markup = self.bot.handle_command("/start", "user_999", "chat_999")
        self.assertIn("Welcome to LISA", reply)
        self.assertIn("Your Backup Unlock Code:", reply)

        # Deeplink /start unlock_7749
        reply_deeplink, _ = self.bot.handle_command("/start unlock_7749", "user_999", "chat_999")
        self.assertIn("Welcome to LISA", reply_deeplink)

    def test_gatekeeper_verification_handshake(self):
        # 1. Verification with member status
        reply_ok, markup_ok = self.bot.handle_command("/start verify_usr_web_42", "tg_user_1", "chat_1", username="trader_joe")
        self.assertIn("VERIFICATION SUCCESSFUL", reply_ok)
        self.assertIn("usr_web_42", reply_ok)
        self.assertTrue(self.bot.registry.is_verified("usr_web_42"))
        self.assertIsNotNone(markup_ok)
        self.assertIn("inline_keyboard", markup_ok)

        # 2. Verification with non-member status
        reply_fail, markup_fail = self.bot.handle_command("/start verify_usr_unjoined", "unjoined_user", "chat_1")
        self.assertIn("CHANNEL MEMBERSHIP REQUIRED", reply_fail)
        self.assertFalse(self.bot.registry.is_verified("usr_unjoined"))
        self.assertIsNotNone(markup_fail)
        self.assertIn("Join", markup_fail["inline_keyboard"][0][0]["text"])

    def test_gatekeeper_paid_subscriber_invite(self):
        reply, markup = self.bot.handle_command("/start sub_tier2", "paid_user_1", "chat_1")
        self.assertIn("WELCOME PREMIUM SUBSCRIBER", reply)
        self.assertIn("single-use invite link", reply)
        self.assertIsNotNone(markup)
        self.assertIn("https://t.me/+lisa_mock_invite_", markup["inline_keyboard"][0][0]["url"])

    def test_single_use_invite_generation_and_churn_kicking(self):
        link = self.bot.create_single_use_invite("@lisa_sports_alpha", member_limit=1, expire_seconds=300)
        self.assertTrue(link.startswith("https://t.me/"))

        # Kicking member on churn
        kicked = self.bot.kick_member("@lisa_sports_alpha", "churned_user_99")
        self.assertTrue(kicked)

    def test_make_execution_buttons(self):
        from lisa.telegram_bot import make_execution_buttons
        buttons = make_execution_buttons(self.sample_pick)
        self.assertIn("inline_keyboard", buttons)
        row = buttons["inline_keyboard"][0]
        self.assertTrue(any("Pinnacle" in b["text"] for b in row))
        self.assertTrue(any("Bet365" in b["text"] for b in row))

    def test_command_picks_stats_traps_vip(self):
        reply_picks, _ = self.bot.handle_command("/picks", "user_1", "chat_1")
        self.assertIn("LISA TODAY'S TOP SELECTIONS", reply_picks)

        reply_stats, _ = self.bot.handle_command("/stats", "user_1", "chat_1")
        self.assertIn("LISA AUDITED PERFORMANCE AUDIT", reply_stats)

        reply_traps, _ = self.bot.handle_command("/traps", "user_1", "chat_1")
        self.assertIn("LISA CAPITAL PRESERVATION DESK", reply_traps)

        reply_vip, _ = self.bot.handle_command("/vip", "user_1", "chat_1")
        self.assertIn("LISA INSTITUTIONAL MEMBERSHIP TIERS", reply_vip)

    def test_command_unlock(self):
        reply_gen, _ = self.bot.handle_command("/unlock", "user_1", "chat_1")
        self.assertIn("Your Web Terminal Unlock Code:", reply_gen)
        self.assertIn("LISA-", reply_gen)

        code = generate_unlock_token("user_1")
        reply_ver, _ = self.bot.handle_command(f"/unlock {code}", "user_1", "chat_1")
        self.assertIn("Unlock Code Verified!", reply_ver)

        reply_bad, _ = self.bot.handle_command("/unlock invalid-token-xyz-12345", "user_1", "chat_1")
        self.assertIn("Invalid code format", reply_bad)

    def test_broadcast_and_mock_outbox(self):
        self.bot.broadcast_diamond(self.sample_pick)
        self.assertEqual(len(self.bot.outbox), 1)
        self.assertIn("LISA INSTITUTIONAL DIAMOND ALERT", self.bot.outbox[0]["text"])
        self.assertTrue(self.bot.outbox[0]["protect_content"])
        self.assertIsNotNone(self.bot.outbox[0]["reply_markup"])

        self.bot.broadcast_trap(
            home_team="Man Utd",
            away_team="Spurs",
            sport_key="soccer_epl",
            public_favorite="Man Utd",
            reason="High CV",
        )
        self.assertEqual(len(self.bot.outbox), 2)
        self.assertIn("LISA TRAP ADVISORY", self.bot.outbox[1]["text"])

    def test_broadcast_settlement(self):
        # WIN settlement
        pick_win = {
            "match_id": "m1",
            "sport_key": "soccer_epl",
            "home_team": "Arsenal",
            "away_team": "Wolves",
            "outcome_name": "Arsenal",
            "best_odds": 1.25,
            "recommended_units": 2.0,
            "clv": 0.015,
            "result": "WIN",
        }
        self.bot.broadcast_settlement(pick_win)
        self.assertIn("LISA INSTITUTIONAL SETTLEMENT — WIN!", self.bot.outbox[-1]["text"])
        self.assertIn("+0.50u", self.bot.outbox[-1]["text"])
        self.assertIn("+1.50% CLV", self.bot.outbox[-1]["text"])

        # LOSS settlement
        pick_loss = {
            "match_id": "m2",
            "sport_key": "soccer_epl",
            "home_team": "Chelsea",
            "away_team": "Fulham",
            "outcome_name": "Chelsea",
            "best_odds": 1.80,
            "recommended_units": 1.0,
            "result": "LOSS",
        }
        self.bot.broadcast_settlement(pick_loss)
        self.assertIn("LISA INSTITUTIONAL SETTLEMENT — LOSS", self.bot.outbox[-1]["text"])
        self.assertIn("-1.00u", self.bot.outbox[-1]["text"])

    def test_process_one_update(self):
        update = TelegramUpdate(
            update_id=1,
            message_id=101,
            chat_id="chat_42",
            user_id="user_42",
            username="investor_bob",
            text="/stats",
        )
        reply = self.bot.process_one_update(update)
        self.assertIn("LISA AUDITED PERFORMANCE AUDIT", reply)
        self.assertEqual(self.bot.outbox[-1]["chat_id"], "chat_42")


class TestLiveIngest(unittest.TestCase):
    def test_live_ingest_cycle_with_fixtures(self):
        settings = cfg.Settings(sports=FIXTURE_SPORTS)
        client = FixtureClient(ODDS_PAYLOADS, SCORES_PAYLOADS)
        bot = TelegramBot(mock=True)
        daemon = LiveIngestionDaemon(
            client=client,
            settings=settings,
            telegram_bot=bot,
        )

        result = daemon.run_cycle()
        self.assertEqual(result.leagues_polled, len(FIXTURE_SPORTS))
        self.assertGreaterEqual(result.matches_seen, 1)

    def test_live_ingest_with_storage_and_settlement(self):
        from lisa.storage import InMemoryStorage
        settings = cfg.Settings(sports=FIXTURE_SPORTS)
        client = FixtureClient(ODDS_PAYLOADS, SCORES_PAYLOADS)
        storage = InMemoryStorage()
        bot = TelegramBot(mock=True)

        daemon = LiveIngestionDaemon(
            client=client,
            settings=settings,
            telegram_bot=bot,
            storage=storage,
            auto_settle=True,
            notify_settle=True,
            dashboard_path="",
        )

        result = daemon.run_cycle()
        self.assertGreaterEqual(result.matches_seen, 1)
        self.assertEqual(result.settled_count, 0)


if __name__ == "__main__":
    unittest.main()
