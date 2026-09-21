"""Unit tests for LISA Interactive Telegram Bot & Live Ingestion Dispatch."""
from __future__ import annotations

import secrets
import unittest
from datetime import datetime, timezone

from lisa import config as cfg
from lisa.client import FixtureClient
from lisa.fixtures import FIXTURE_SPORTS, ODDS_PAYLOADS, SCORES_PAYLOADS
from lisa.gate import Execution, Pick
from lisa.live_ingest import LiveIngestionDaemon
from lisa.telegram_bot import (
    MAIN_REPLY_KEYBOARD,
    BankrollFSMManager,
    TelegramBot,
    TelegramUpdate,
    format_active_top_picks_contract,
    format_booking_codes_html,
    format_diamond_alert_html,
    format_free_picks_html,
    format_parlay_html,
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

    def test_persistent_reply_keyboard_structure(self):
        keyboard = MAIN_REPLY_KEYBOARD
        self.assertTrue(keyboard.get("is_persistent"))
        self.assertTrue(keyboard.get("resize_keyboard"))
        buttons = [btn["text"] for row in keyboard.get("keyboard", []) for btn in row]
        self.assertIn("📊 Active Top Picks", buttons)
        self.assertIn("🏦 My Bankroll", buttons)
        self.assertIn("📈 Accuracy Ledger", buttons)
        self.assertIn("⚡ 5-Fold Parlay", buttons)
        self.assertIn("🎟️ Bookmaker Codes", buttons)
        self.assertIn("🛡️ Trap Advisories", buttons)

    def test_bankroll_fsm_onboarding_lifecycle(self):
        user_id = f"trader_alpha_{secrets.token_hex(4)}"
        chat_id = f"chat_alpha_{secrets.token_hex(4)}"

        update_init = TelegramUpdate(1, 10, chat_id, user_id, "trader", text="🏦 My Bankroll")
        reply_init = self.bot.process_one_update(update_init)
        self.assertIn("LISA BANKROLL ONBOARDING (Step 1/3)", reply_init)
        self.assertEqual(self.bot.fsm.get_state(user_id), BankrollFSMManager.STATE_AWAITING_AMOUNT)

        update_bad = TelegramUpdate(2, 11, chat_id, user_id, "trader", text="invalid_amount")
        reply_bad = self.bot.process_one_update(update_bad)
        self.assertIn("Invalid Bankroll Amount", reply_bad)
        self.assertEqual(self.bot.fsm.get_state(user_id), BankrollFSMManager.STATE_AWAITING_AMOUNT)

        update_amt = TelegramUpdate(3, 12, chat_id, user_id, "trader", text="$2,500.00")
        reply_amt = self.bot.process_one_update(update_amt)
        self.assertIn("SELECT RISK TOLERANCE (Step 2/3)", reply_amt)
        self.assertIn("$2,500.00", reply_amt)
        self.assertEqual(self.bot.fsm.get_state(user_id), BankrollFSMManager.STATE_AWAITING_RISK)

        update_risk = TelegramUpdate(4, 13, chat_id, user_id, "trader", callback_query_id="cb_risk", callback_data="fsm:risk:balanced")
        reply_risk = self.bot.process_one_update(update_risk)
        self.assertIn("PREFERRED BOOKMAKER (Step 3/3)", reply_risk)
        self.assertEqual(self.bot.fsm.get_state(user_id), BankrollFSMManager.STATE_AWAITING_BOOK)

        update_book = TelegramUpdate(5, 14, chat_id, user_id, "trader", callback_query_id="cb_book", callback_data="fsm:book:Football.com")
        reply_book = self.bot.process_one_update(update_book)
        self.assertIn("ONBOARDING COMPLETE — PROFILE SAVED", reply_book)
        self.assertIn("Football.com", reply_book)
        self.assertEqual(self.bot.fsm.get_state(user_id), BankrollFSMManager.STATE_IDLE)

        saved = self.bot.fsm.get_profile(user_id)
        self.assertIsNotNone(saved)
        self.assertEqual(saved["bankroll_amount"], 2500.0)
        self.assertEqual(saved["risk_profile"], "balanced")
        self.assertEqual(saved["kelly_fraction"], 0.50)
        self.assertEqual(saved["preferred_bookmaker"], "Football.com")

        update_view = TelegramUpdate(6, 15, chat_id, user_id, "trader", text="🏦 My Bankroll")
        reply_view = self.bot.process_one_update(update_view)
        self.assertIn("LISA BANKROLL REFINERY PROFILE", reply_view)
        self.assertIn("$2,500.00", reply_view)
        self.assertIn("Football.com", reply_view)

    def test_live_cache_picks_with_personalized_bankroll(self):
        user_id = "trader_beta_2"
        chat_id = "chat_beta_2"

        self.bot.fsm.save_profile(user_id, "beta", 5000.0, "balanced", 0.50, "SportyBet")

        update = TelegramUpdate(10, 20, chat_id, user_id, "beta", text="📊 Active Top Picks")
        reply = self.bot.process_one_update(update)

        self.assertIn("LISA TODAY'S TOP SELECTIONS", reply)
        self.assertIn("$5,000.00", reply)
        self.assertIn("SportyBet", reply)
        self.assertIn("P(true)", reply)
        self.assertIn("Fair Odds", reply)
        self.assertIn("Conviction", reply)

        last_msg = self.bot.outbox[-1]
        self.assertIsNotNone(last_msg["reply_markup"])
        inline_rows = last_msg["reply_markup"].get("inline_keyboard", [])
        self.assertTrue(len(inline_rows) >= 2)
        urls = [b.get("url", "") for row in inline_rows for b in row if b.get("url")]
        self.assertTrue(any("sportybet" in u for u in urls))

    def test_parlay_and_booking_codes_features(self):
        update_parlay = TelegramUpdate(20, 30, "chat_1", "user_1", "user", text="⚡ 5-Fold Parlay")
        reply_parlay = self.bot.process_one_update(update_parlay)
        self.assertIn("LISA HIGH-CONVICTION 5-FOLD PARLAY", reply_parlay)
        self.assertIn("BC792K", reply_parlay)
        self.assertIn("FC82910", reply_parlay)

        update_codes = TelegramUpdate(21, 31, "chat_1", "user_1", "user", text="🎟️ Bookmaker Codes")
        reply_codes = self.bot.process_one_update(update_codes)
        self.assertIn("LISA VERIFIED BOOKMAKER BOOKING CODES", reply_codes)
        self.assertIn("SportyBet", reply_codes)
        self.assertIn("Football.com", reply_codes)

    def test_unexpected_input_and_natural_team_search(self):
        update_search = TelegramUpdate(30, 40, "chat_1", "user_1", "user", text="Arsenal")
        reply_search = self.bot.process_one_update(update_search)
        self.assertIn("Arsenal", reply_search)

        update_unexpected = TelegramUpdate(31, 41, "chat_1", "user_1", "user", text="hello random unknown query 12345")
        reply_fallback = self.bot.process_one_update(update_unexpected)
        self.assertIn("LISA Sports Intelligence Desk", reply_fallback)
        self.assertIn("Received query", reply_fallback)

    def test_global_exception_fallback_handler(self):
        class ExplodingBot(TelegramBot):
            def handle_message(self, update):
                raise RuntimeError("Simulated transient memory fault")

        exploding_bot = ExplodingBot(mock=True)
        update = TelegramUpdate(99, 999, "chat_fail", "user_fail", "fail", text="crash test")
        res = exploding_bot.process_one_update(update)

        self.assertIn("LISA System Notification", res)
        self.assertIn("An unexpected operational exception occurred", res)
        self.assertEqual(exploding_bot.outbox[-1]["reply_markup"], MAIN_REPLY_KEYBOARD)

    def test_admin_identity_gate(self):
        admin_id = "super_admin_777"
        non_admin_id = "regular_user_123"
        admin_bot = TelegramBot(mock=True, admin_telegram_ids=[admin_id])

        self.assertTrue(admin_bot.is_admin(admin_id))
        self.assertFalse(admin_bot.is_admin(non_admin_id))

        unauth_update = TelegramUpdate(1, 101, "chat_1", non_admin_id, "user", text="/admin")
        unauth_reply = admin_bot.process_one_update(unauth_update)
        self.assertIn("Unknown command", unauth_reply)

        unauth_settle = TelegramUpdate(2, 102, "chat_1", non_admin_id, "user", text="/settle match-99 WIN")
        self.assertIn("Unknown command", admin_bot.process_one_update(unauth_settle))

        unauth_grant = TelegramUpdate(3, 103, "chat_1", non_admin_id, "user", text="/grant test@vip.com tier2")
        self.assertIn("Unknown command", admin_bot.process_one_update(unauth_grant))

        unauth_pause = TelegramUpdate(4, 104, "chat_1", non_admin_id, "user", text="/sys_pause")
        self.assertIn("Unknown command", admin_bot.process_one_update(unauth_pause))

        auth_update = TelegramUpdate(5, 105, "chat_admin", admin_id, "boss", text="/admin")
        auth_reply = admin_bot.process_one_update(auth_update)
        self.assertIn("LISA MOBILE ADMIN CONSOLE", auth_reply)
        self.assertIn(admin_id, auth_reply)

    def test_admin_manual_settle(self):
        from lisa.storage import InMemoryStorage
        admin_id = "super_admin_777"
        storage = InMemoryStorage()
        storage.insert_pick(self.sample_pick)

        admin_bot = TelegramBot(mock=True, admin_telegram_ids=[admin_id], storage=storage)

        update = TelegramUpdate(10, 201, "chat_admin", admin_id, "boss", text=f"/settle {self.sample_pick.match_id} WIN")
        reply = admin_bot.process_one_update(update)

        self.assertIn("MANUAL MATCH SETTLEMENT SYNCHRONIZED", reply)
        self.assertIn(self.sample_pick.match_id, reply)

        key = f"{self.sample_pick.match_id}::{self.sample_pick.market}::{self.sample_pick.outcome_name}"
        saved = storage.get_pick(key)
        self.assertIsNotNone(saved)
        self.assertEqual(saved["state"], "SETTLED")
        self.assertEqual(saved["result"], "WIN")

        logs = storage.list_admin_audit_logs()
        self.assertTrue(len(logs) >= 1)
        self.assertEqual(logs[0]["action"], "MANUAL_SETTLEMENT")
        self.assertEqual(logs[0]["admin_id"], admin_id)

    def test_admin_grant_user_provisioning(self):
        from lisa.storage import InMemoryStorage
        admin_id = "super_admin_777"
        storage = InMemoryStorage()
        admin_bot = TelegramBot(mock=True, admin_telegram_ids=[admin_id], storage=storage)

        update = TelegramUpdate(20, 301, "chat_admin", admin_id, "boss", text="/grant partner@influencer.com tier2")
        reply = admin_bot.process_one_update(update)

        self.assertIn("CUSTOMER PROVISIONING COMPLETED", reply)
        self.assertIn("partner@influencer.com", reply)
        self.assertIn("TIER2", reply)
        self.assertIn("LISA-", reply)
        self.assertIn("https://t.me/", reply)

        logs = storage.list_admin_audit_logs()
        self.assertTrue(any(l["action"] == "MANUAL_TIER_GRANT" for l in logs))

    def test_admin_emergency_kill_switch(self):
        from lisa.storage import InMemoryStorage
        admin_id = "super_admin_777"
        storage = InMemoryStorage()
        admin_bot = TelegramBot(mock=True, admin_telegram_ids=[admin_id], storage=storage)

        self.assertFalse(storage.is_system_paused())

        update_pause = TelegramUpdate(30, 401, "chat_admin", admin_id, "boss", text="/sys_pause")
        reply_pause = admin_bot.process_one_update(update_pause)
        self.assertIn("EMERGENCY KILL-SWITCH ENGAGED", reply_pause)
        self.assertTrue(storage.is_system_paused())

        emitted = admin_bot.broadcast_diamond(self.sample_pick)
        self.assertFalse(emitted)

        update_resume = TelegramUpdate(31, 402, "chat_admin", admin_id, "boss", text="/sys_resume")
        reply_resume = admin_bot.process_one_update(update_resume)
        self.assertIn("EMERGENCY KILL-SWITCH DISENGAGED", reply_resume)
        self.assertFalse(storage.is_system_paused())

        emitted_after = admin_bot.broadcast_diamond(self.sample_pick)
        self.assertTrue(emitted_after)

    def test_admin_broadcast(self):
        from lisa.storage import InMemoryStorage
        admin_id = "super_admin_777"
        storage = InMemoryStorage()
        admin_bot = TelegramBot(mock=True, admin_telegram_ids=[admin_id], storage=storage)

        update = TelegramUpdate(40, 501, "chat_admin", admin_id, "boss", text="/broadcast NBA Opening Night slate is active!")
        reply = admin_bot.process_one_update(update)

        self.assertIn("GLOBAL BROADCAST DISPATCHED", reply)
        self.assertTrue(any("NBA Opening Night" in m.get("text", "") for m in admin_bot.outbox))

        logs = storage.list_admin_audit_logs()
        self.assertTrue(any(l["action"] == "GLOBAL_BROADCAST" for l in logs))

    def test_admin_callback_queries(self):
        from lisa.storage import InMemoryStorage
        admin_id = "super_admin_777"
        storage = InMemoryStorage()
        admin_bot = TelegramBot(mock=True, admin_telegram_ids=[admin_id], storage=storage)

        update_pause = TelegramUpdate(50, 601, "chat_admin", admin_id, "boss", callback_query_id="cb_pause", callback_data="admin:pause")
        reply_pause = admin_bot.process_one_update(update_pause)
        self.assertIn("EMERGENCY KILL-SWITCH ENGAGED", reply_pause)
        self.assertTrue(storage.is_system_paused())

        update_resume = TelegramUpdate(51, 602, "chat_admin", admin_id, "boss", callback_query_id="cb_resume", callback_data="admin:resume")
        reply_resume = admin_bot.process_one_update(update_resume)
        self.assertIn("EMERGENCY KILL-SWITCH DISENGAGED", reply_resume)
        self.assertFalse(storage.is_system_paused())

        update_logs = TelegramUpdate(52, 603, "chat_admin", admin_id, "boss", callback_query_id="cb_logs", callback_data="admin:logs")
        reply_logs = admin_bot.process_one_update(update_logs)
        self.assertIn("LISA SECURITY AUDIT TRAIL", reply_logs)


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
