"""Unit tests for LISA Interactive Telegram Bot & Live Ingestion Dispatch."""
from __future__ import annotations

import secrets
import time
import unittest
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone

from lisa import config as cfg
from lisa.client import FixtureClient
from lisa.fixtures import FIXTURE_SPORTS, ODDS_PAYLOADS, SCORES_PAYLOADS
from lisa.gate import Execution, Pick
from lisa.live_ingest import LiveIngestionDaemon
from lisa.storage import InMemoryStorage
from lisa.telegram_bot import (
    ADMIN_ONLY_SLUGS,
    ADMIN_REPLY_KEYBOARD,
    BUTTON_SLUGS,
    FREE_REPLY_KEYBOARD,
    MAIN_REPLY_KEYBOARD,
    MIN_SETTLED_FOR_STATS,
    PAID_REPLY_KEYBOARD,
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
    get_main_menu_for_user,
    verify_unlock_token,
)


class TestTelegramBot(unittest.TestCase):
    def setUp(self):
        # Isolated per-test storage: the bot must never read the operator's
        # real ledger, and the tests must not depend on it.
        from lisa.storage import InMemoryStorage
        self.storage = InMemoryStorage()
        self.bot = TelegramBot(
            token="", channel_chat_id="@test_channel", mock=True, storage=self.storage
        )
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
            state="TRIGGER_ALERT",
            created_at=self.now,
            conviction_score=8.5,
            recommended_stake_pct=1.5,
            recommended_units=1.5,
        )

    def test_unlock_token_generation_and_verification(self):
        token1 = generate_unlock_token("user_12345")
        self.assertTrue(token1.startswith("LISA-"))
        self.assertEqual(len(token1), 15)
        self.assertTrue(verify_unlock_token(token1, "user_12345"))

        # Consistent generation with same seed inside the same time bucket
        token2 = generate_unlock_token("user_12345")
        self.assertEqual(token1, token2)

        # Seed-bound: a genuine code is worthless for any other identity
        self.assertFalse(verify_unlock_token(token1, "user_99999"))
        self.assertFalse(verify_unlock_token(token1, ""))

        # Forged / shape-only codes are rejected (regression: any 6-char string used to pass)
        self.assertFalse(verify_unlock_token("AB12CD", "user_12345"))
        self.assertFalse(verify_unlock_token("LISA-000000", "user_12345"))
        self.assertFalse(verify_unlock_token("LISA-23456789AB", "user_12345"))
        self.assertFalse(verify_unlock_token("", "user_12345"))
        self.assertFalse(verify_unlock_token("invalid-too-long-string-code", "user_12345"))
        self.assertFalse(verify_unlock_token(None, "user_12345"))

        # A single flipped character breaks the signature
        tampered = token1[:-1] + ("2" if token1[-1] != "2" else "3")
        self.assertFalse(verify_unlock_token(tampered, "user_12345"))

    def test_unlock_token_expires_outside_bucket_window(self):
        seven_days = 7 * 24 * 3600
        issued = 1_700_000_000.0
        token = generate_unlock_token("user_12345", now=issued)
        self.assertTrue(verify_unlock_token(token, "user_12345", now=issued + 3600))
        self.assertFalse(verify_unlock_token(token, "user_12345", now=issued + 14 * seven_days))

    def test_format_diamond_alert_html(self):
        html = format_diamond_alert_html(self.sample_pick)
        self.assertIn("LISA INSTITUTIONAL DIAMOND ALERT", html)
        self.assertIn("Arsenal vs Wolverhampton", html)
        self.assertIn("81.0%", html)
        self.assertIn("Pinnacle", html)
        # Conviction is ((p_true - 0.75) / cv) * (1 + ev) -- an unbounded ratio
        # that reaches ~27 on a real pick. It was previously labelled "8.5/10.0",
        # which is a scale the formula cannot produce.
        self.assertIn("Conviction Score:", html)
        self.assertNotIn("/10.0", html)
        self.assertIn("not bounded", html)
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
            "settled_picks_count": 50,
            "won_count": 42,
            "lost_count": 8,
        }
        html = format_stats_html(summary)
        self.assertIn("84.0%", html)
        self.assertIn("0.1305", html)
        self.assertIn("3.61%", html)
        self.assertIn("+3.12%", html)
        self.assertIn("50 fully audited real matches", html)

    def test_format_stats_html_never_invents_missing_metrics(self):
        """A missing metric must render as absent, not as a plausible default."""
        html = format_stats_html({"settled_picks_count": 50})
        for fabricated in ("84.0%", "0.1305", "3.61%", "+3.12%", "30 sucker bets"):
            self.assertNotIn(fabricated, html)
        # The metrics that were supplied are still shown.
        self.assertIn("50 fully audited real matches", html)

    def test_format_stats_html_insufficient_sample(self):
        """Below the sample floor we say so instead of publishing a win rate."""
        for summary in ({}, {"settled_picks_count": 0}, {"settled_picks_count": 3}):
            html = format_stats_html(summary)
            self.assertIn("Insufficient settled sample", html)
            self.assertNotIn("Verified Win Rate", html)

    def test_format_stats_html_reports_negative_clv_honestly(self):
        """The real backtest numbers must not be dressed up as positive."""
        summary = {
            "win_rate": 0.7974,
            "brier_score": 0.1584,
            "mean_clv": -0.0111,
            "positive_clv_share": 0.3789,
            "roi_pct": -1.81,
            "wins": 303,
            "settled_picks_count": 380,
        }
        html = format_stats_html(summary)
        self.assertIn("79.7%", html)
        self.assertIn("(303/380 graded)", html)
        self.assertIn("-1.11%", html)
        self.assertIn("37.9%", html)
        self.assertIn("-1.81%", html)
        self.assertIn("did <b>not</b> beat the closing line", html)
        self.assertNotIn("+3.12%", html)

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
        # Empty ledger: the bot must say so rather than print a sample board.
        reply_empty, _ = self.bot.handle_command("/picks", "user_1", "chat_1")
        self.assertIn("No active signals", reply_empty)

        # /stats with nothing settled refuses to publish, and states the bar
        # rather than showing a placeholder rate.
        reply_stats_empty, _ = self.bot.handle_command("/stats", "user_1", "chat_1")
        self.assertIn("Insufficient settled sample to publish a track record", reply_stats_empty)
        self.assertIn("Settled picks audited:</b> <code>0</code>", reply_stats_empty)
        self.assertIn("Required for a meaningful ledger:</b> <code>20</code>", reply_stats_empty)

        reply_traps_empty, _ = self.bot.handle_command("/traps", "user_1", "chat_1")
        self.assertIn("No measured pass-advisory data yet", reply_traps_empty)

        # With a real ledger row the genuine board is rendered.
        storage = self.storage
        self.bot._picks_cache = ({}, 0.0)
        storage.insert_pick(self.sample_pick)
        reply_picks, _ = self.bot.handle_command("/picks", "user_1", "chat_1")
        self.assertIn("LISA TODAY'S TOP SELECTIONS", reply_picks)
        self.assertIn("Arsenal", reply_picks)

        reply_vip, _ = self.bot.handle_command("/vip", "user_1", "chat_1")
        self.assertIn("LISA INSTITUTIONAL MEMBERSHIP TIERS", reply_vip)

    def test_command_stats_from_settled_ledger(self):
        """One graded pick must NOT produce a 100% win rate.

        This test previously asserted that settling a single pick published
        "100.0%" and "1/1". A 1-sample win rate is the single most misleading
        number a betting bot can show: it reads as a perfect record and is
        exactly what a customer screenshots. /stats now requires
        MIN_SETTLED_FOR_STATS (20) graded rows and refuses below that, so the
        assertion is inverted -- the bar is shown, the rate is not.
        """
        storage = self.storage
        self.bot._picks_cache = ({}, 0.0)
        storage.insert_pick(self.sample_pick)
        key = f"{self.sample_pick.match_id}::{self.sample_pick.market}::{self.sample_pick.outcome_name}"
        storage.settle_pick(key, "WIN", datetime.now(timezone.utc))

        reply, _ = self.bot.handle_command("/stats", "user_1", "chat_1")
        self.assertIn("Insufficient settled sample", reply)
        self.assertIn("<code>1</code>", reply)   # the true count is disclosed
        self.assertIn("<code>20</code>", reply)  # and the bar it must clear
        self.assertNotIn("100.0%", reply)        # but no rate is published yet

    def test_command_stats_publishes_once_sample_is_meaningful(self):
        """At the threshold the real rate is published, and it is arithmetic.

        The inverse of the test above: once MIN_SETTLED_FOR_STATS graded rows
        exist, withholding the win rate would be its own kind of dishonesty.
        24 rows with 18 wins must report 75.0%, not a flattering placeholder.
        """
        storage = self.storage
        self.bot._picks_cache = ({}, 0.0)
        n = MIN_SETTLED_FOR_STATS + 4
        for i in range(n):
            pick = replace(self.sample_pick, match_id=f"m{i:03d}")
            storage.insert_pick(pick)
            k = f"{pick.match_id}::{pick.market}::{pick.outcome_name}"
            storage.settle_pick(
                k, "WIN" if i < 18 else "LOSS", datetime.now(timezone.utc)
            )

        reply, _ = self.bot.handle_command("/stats", "user_1", "chat_1")
        self.assertNotIn("Insufficient settled sample", reply)
        self.assertIn("75.0%", reply)

    def test_command_traps_reports_gross_and_net_together(self):
        """/traps must show the honest pair, and must not show a single flattering number.

        The gross "capital preserved" figure is the trap avoided times the stake
        -- roughly $310,500 on the archive. The net counterfactual is what those
        avoided bets would actually have returned: +$5,448, because most of them
        would have won. Showing only the gross is the single most misleading
        thing this command could do, so both appear and the pairing is explained.
        """
        self.bot._summary_cache = ({
            "total_matches_evaluated": 6729,
            "grade_c_traps_avoided": 6699,
            "grade_c_traps_that_won": 3594,
            "grade_c_traps_that_lost": 3105,
            "capital_preserved_dollars": 310500.0,
            "net_counterfactual_value": 5448.0,
        }, time.time() + 3600)

        reply, _ = self.bot.handle_command("/traps", "user_1", "chat_1")
        self.assertIn("LISA CAPITAL PRESERVATION DESK", reply)
        self.assertIn("Matches Evaluated", reply)
        self.assertIn("<code>6699</code>", reply)
        # The split that makes the gross figure interpretable.
        self.assertIn("<code>3594 won</code>", reply)
        self.assertIn("<code>3105 lost</code>", reply)
        self.assertIn("$310,500", reply)
        self.assertIn("$+5,448", reply)
        self.assertIn("Read the two figures together", reply)

    def test_command_traps_with_no_measured_data(self):
        """No summary means no trap counts -- not an illustrative figure."""
        self.bot._summary_cache = ({}, 0.0)
        reply, _ = self.bot.handle_command("/traps", "user_1", "chat_1")
        self.assertIn("LISA CAPITAL PRESERVATION DESK", reply)
        self.assertIn("No measured pass-advisory data yet", reply)
        self.assertNotIn("$", reply)

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
        self.storage.insert_pick(self.sample_pick)
        self.bot._picks_cache = ({}, 0.0)
        self.storage.settle_pick(
            f"{self.sample_pick.match_id}::{self.sample_pick.market}::"
            f"{self.sample_pick.outcome_name}", "WIN", self.now)
        reply = self.bot.process_one_update(update)
        self.assertIn("LISA AUDITED PERFORMANCE AUDIT", reply)
        self.assertEqual(self.bot.outbox[-1]["chat_id"], "chat_42")

    def test_persistent_reply_keyboard_structure(self):
        """The default (free) keyboard is a valid persistent reply keyboard."""
        keyboard = MAIN_REPLY_KEYBOARD
        self.assertTrue(keyboard.get("is_persistent"))
        self.assertTrue(keyboard.get("resize_keyboard"))
        buttons = [btn["text"] for row in keyboard.get("keyboard", []) for btn in row]
        self.assertIn("📊 Active Top Picks", buttons)
        self.assertIn("🏦 My Bankroll", buttons)
        self.assertIn("📈 Accuracy Ledger", buttons)
        self.assertIn("🛡️ Trap Advisories", buttons)
        # Parlay and booking codes are paid-tier features, so they must NOT be
        # advertised on the free keyboard.
        self.assertNotIn("⚡ 5-Fold Parlay", buttons)
        self.assertNotIn("🎟️ Bookmaker Codes", buttons)

    def test_no_keyboard_shows_the_same_menu_item_twice(self):
        """Guards against replicated entries in the persistent reply keyboards.

        The admin keyboard inlined literal copies of _ADMIN_ROW_NAV,
        _ROW_LEDGER and _ROW_CODES and then appended those same constants, so
        an operator saw "📊 Active Top Picks", "🏦 My Bankroll",
        "📈 Accuracy Ledger" and "🛡️ Trap Advisories" twice each: 18 buttons
        for 14 destinations.

        This also catches the subtler form -- two *different* labels resolving
        to the same slug, which is how the unroutable "⚡ Live Accumulator" and
        "🎟️ Execution Guide" aliases survived alongside the canonical
        "⚡ 5-Fold Parlay" and "🎟️ Bookmaker Codes" buttons they duplicate.
        """
        for name, keyboard in (
            ("admin", ADMIN_REPLY_KEYBOARD),
            ("paid", PAID_REPLY_KEYBOARD),
            ("free", FREE_REPLY_KEYBOARD),
        ):
            labels = [b["text"] for row in keyboard["keyboard"] for b in row]

            counts = Counter(labels)
            for label, n in counts.items():
                self.assertEqual(
                    n, 1,
                    f"{name} keyboard repeats {label!r} {n} times",
                )

            by_slug: dict[str, list[str]] = {}
            for label in labels:
                if label in BUTTON_SLUGS:
                    by_slug.setdefault(BUTTON_SLUGS[label], []).append(label)
            for slug, alts in by_slug.items():
                self.assertEqual(
                    len(alts), 1,
                    f"{name} keyboard routes {alts} all to {slug!r}; "
                    f"{alts[0]!r} is an alias that should be dropped",
                )

    def test_every_keyboard_button_resolves_to_a_slug(self):
        """Guards against a button label that the router cannot match.

        An unroutable button silently falls through to the conversational
        fallback, so every label in every keyboard must be a known slug.
        """
        for name, keyboard in (
            ("admin", ADMIN_REPLY_KEYBOARD),
            ("paid", PAID_REPLY_KEYBOARD),
            ("free", FREE_REPLY_KEYBOARD),
        ):
            for row in keyboard["keyboard"]:
                for btn in row:
                    self.assertIn(
                        btn["text"], BUTTON_SLUGS,
                        f"{name} keyboard button {btn['text']!r} has no slug",
                    )

    def test_admin_keyboard_is_the_only_one_with_operator_buttons(self):
        def flat(kb):
            return [b["text"] for row in kb["keyboard"] for b in row]

        admin_only = [t for t, s in BUTTON_SLUGS.items() if s in ADMIN_ONLY_SLUGS]
        self.assertTrue(admin_only)
        for label in admin_only:
            self.assertIn(label, flat(ADMIN_REPLY_KEYBOARD))
            self.assertNotIn(label, flat(PAID_REPLY_KEYBOARD))
            self.assertNotIn(label, flat(FREE_REPLY_KEYBOARD))

    def test_get_main_menu_for_user_selects_by_role(self):
        admin_id = "8720543490"
        bot = TelegramBot(token="", channel_chat_id="@test_channel", mock=True,
                          admin_telegram_ids=[admin_id])

        def flat(kb):
            return [b["text"] for row in kb["keyboard"] for b in row]

        self.assertIs(get_main_menu_for_user(bot, admin_id), ADMIN_REPLY_KEYBOARD)

        paid_id = "paid_user_456"
        bot._tier_cache[paid_id] = ("tier2", time.time() + 300)
        self.assertIs(get_main_menu_for_user(bot, paid_id), PAID_REPLY_KEYBOARD)

        # Unknown user degrades to the least-privilege menu.
        self.assertIs(get_main_menu_for_user(bot, "stranger_789"), FREE_REPLY_KEYBOARD)

        # Admin beats a paid tier on the same account.
        bot._tier_cache[admin_id] = ("tier1", time.time() + 300)
        self.assertIs(get_main_menu_for_user(bot, admin_id), ADMIN_REPLY_KEYBOARD)

    def test_menu_selector_survives_role_resolution_failure(self):
        class ExplodingBot(TelegramBot):
            def is_admin(self, user_id):
                raise RuntimeError("boom")

        bot = ExplodingBot(token="", channel_chat_id="@t", mock=True)
        self.assertIs(get_main_menu_for_user(bot, "anyone"), FREE_REPLY_KEYBOARD)

    def test_start_command_is_role_specific(self):
        admin_id = "8720543490"
        bot = TelegramBot(token="", channel_chat_id="@t", mock=True,
                          admin_telegram_ids=[admin_id])

        text, markup = bot.handle_command("/start", admin_id, "c1")
        self.assertIn("ADMIN CONSOLE ACTIVE", text)
        self.assertIs(markup, ADMIN_REPLY_KEYBOARD)

        paid_id = "paid_user_1"
        bot._tier_cache[paid_id] = ("tier2", time.time() + 300)
        text, markup = bot.handle_command("/start", paid_id, "c2")
        self.assertIn("TIER2 subscriber", text)
        self.assertIs(markup, PAID_REPLY_KEYBOARD)

        text, markup = bot.handle_command("/start", "free_user_1", "c3")
        self.assertIn("Welcome to LISA Gatekeeper", text)
        self.assertIs(markup, FREE_REPLY_KEYBOARD)
        # The fabricated headline claim must be gone.
        self.assertNotIn("84.0%", text)

    def test_help_command_is_role_specific(self):
        admin_id = "8720543490"
        bot = TelegramBot(token="", channel_chat_id="@t", mock=True,
                          admin_telegram_ids=[admin_id])

        admin_text, admin_markup = bot.handle_command("/help", admin_id, "c1")
        self.assertIn("LISA Admin Help Desk", admin_text)
        self.assertIn("/sys_pause", admin_text)
        self.assertIs(admin_markup, ADMIN_REPLY_KEYBOARD)

        paid_id = "paid_user_2"
        bot._tier_cache[paid_id] = ("tier1", time.time() + 300)
        paid_text, paid_markup = bot.handle_command("/help", paid_id, "c2")
        self.assertIn("LISA Subscriber Help Desk", paid_text)
        self.assertNotIn("/sys_pause", paid_text)
        self.assertIs(paid_markup, PAID_REPLY_KEYBOARD)

        free_text, free_markup = bot.handle_command("/help", "free_user_2", "c3")
        self.assertIn("LISA Bot Help Desk", free_text)
        self.assertNotIn("/sys_pause", free_text)
        self.assertNotIn("/grant", free_text)
        self.assertIs(free_markup, FREE_REPLY_KEYBOARD)

    def test_admin_button_press_is_routable(self):
        admin_id = "8720543490"
        bot = TelegramBot(token="", channel_chat_id="@t", mock=True,
                          admin_telegram_ids=[admin_id])
        upd = TelegramUpdate(1, 1, "c_admin", admin_id, "boss", text="🛠️ Admin Console")
        reply = bot.process_one_update(upd)
        self.assertNotIn("Admin access required", reply)
        self.assertTrue(reply.strip())

    def test_admin_buttons_are_rejected_for_non_admins(self):
        """A non-admin must be refused even if they type the button label."""
        bot = TelegramBot(token="", channel_chat_id="@t", mock=True,
                          admin_telegram_ids=["8720543490"])
        for label, slug in BUTTON_SLUGS.items():
            if slug not in ADMIN_ONLY_SLUGS:
                continue
            upd = TelegramUpdate(2, 2, "c_free", "not_an_admin", "someone", text=label)
            reply = bot.process_one_update(upd)
            self.assertIn("Admin access required", reply, f"{label} was not gated")
            # Refusal must not leak operator output.
            self.assertNotIn("EXECUTIVE OVERRIDE", reply)

    def test_subscription_button_reports_tier(self):
        bot = TelegramBot(token="", channel_chat_id="@t", mock=True)
        upd = TelegramUpdate(3, 3, "c_free", "free_sub_user", "someone",
                             text="👑 Upgrade to Unlock Full Slate")
        reply = bot.process_one_update(upd)
        self.assertIn("SUBSCRIPTION STATUS", reply)
        self.assertIn("Free Tier", reply)

    def test_paid_parlay_button_unlocks_parlay(self):
        bot = TelegramBot(token="", channel_chat_id="@t", mock=True)
        paid_id = "paid_parlay_user"
        bot._tier_cache[paid_id] = ("tier2", time.time() + 300)
        upd = TelegramUpdate(4, 4, "c_paid", paid_id, "someone", text="⚡ 5-Fold Parlay")
        reply = bot.process_one_update(upd)
        self.assertNotIn("Admin access required", reply)
        self.assertTrue(reply.strip())

    def test_process_one_update_default_markup_is_role_aware(self):
        """With no explicit markup, the sent keyboard matches the caller's role."""
        admin_id = "8720543490"
        bot = TelegramBot(token="", channel_chat_id="@t", mock=True,
                          admin_telegram_ids=[admin_id])

        # An unknown free-text message resolves to the free menu.
        bot.process_one_update(
            TelegramUpdate(5, 5, "c_free", "free_default_user", "u", text="zzzz qqqq")
        )
        self.assertEqual(bot.outbox[-1]["reply_markup"], FREE_REPLY_KEYBOARD)

        bot.process_one_update(
            TelegramUpdate(6, 6, "c_admin", admin_id, "boss", text="zzzz qqqq")
        )
        self.assertEqual(bot.outbox[-1]["reply_markup"], ADMIN_REPLY_KEYBOARD)

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

        self.storage.insert_pick(self.sample_pick)
        self.bot._picks_cache = ({}, 0.0)
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
        # Real board content only: no invented booking code is ever rendered.
        self.assertNotIn("BC2EEA", reply)
        self.assertIn("Arsenal vs Wolverhampton", reply)

    def test_parlay_and_booking_codes_features(self):
        # No live legs: an honest board instead of a sample 5-fold slip.
        update_parlay = TelegramUpdate(20, 30, "chat_1", "user_1", "user", text="⚡ 5-Fold Parlay")
        reply_parlay = self.bot.process_one_update(update_parlay)
        self.assertIn("LISA ACCUMULATOR", reply_parlay)
        self.assertIn("Not enough live selections", reply_parlay)

        # With two real priced legs the accumulator is computed from them.
        self.storage.insert_pick(self.sample_pick)
        second = Pick(
            match_id="test-match-102", sport_key="soccer_epl", home_team="Chelsea",
            away_team="Brighton", commence_time=self.now, market="h2h",
            outcome_name="Chelsea", p_true=0.72, fair_odds=1.39, n_books=4,
            stdev=0.01, cv=0.02,
            best_execution=Execution(book_key="bet365", book_title="Bet365", odds=1.45, ev=0.045),
            state="TRIGGER_ALERT", created_at=self.now, conviction_score=6.0,
            recommended_stake_pct=1.0, recommended_units=1.0,
        )
        self.storage.insert_pick(second)
        self.bot._picks_cache = ({}, 0.0)
        reply_real = self.bot.process_one_update(update_parlay)
        self.assertIn("LISA LIVE ACCUMULATOR", reply_real)
        self.assertIn("Chelsea", reply_real)
        self.assertIn("Combined Odds", reply_real)
        # No invented booking code is ever printed.
        self.assertNotIn("BC792K", reply_real)
        self.assertNotIn("FC82910", reply_real)

        # The codes page explains there is no integration rather than faking one.
        vip_bot = TelegramBot(token="", channel_chat_id="@test_channel", mock=True,
                              admin_telegram_ids=["vip_boss"], storage=self.storage)
        update_vip = TelegramUpdate(22, 32, "chat_1", "vip_boss", "vip_boss", text="/codes")
        reply_vip = vip_bot.process_one_update(update_vip)
        self.assertIn("does not generate bookmaker booking codes", reply_vip)
        self.assertNotIn("BC792K", reply_vip)

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

    def test_admin_interactive_fsm_control_ui(self):
        from lisa.storage import InMemoryStorage
        admin_id = "8720543490"
        storage = InMemoryStorage()
        storage.insert_pick(self.sample_pick)
        admin_bot = TelegramBot(mock=True, admin_telegram_ids=[admin_id], storage=storage)

        dash_reply, dash_markup = admin_bot.handle_command("/admin", admin_id, "chat_admin")
        self.assertIn("LISA MOBILE ADMIN CONSOLE", dash_reply)
        cb_keys = [btn["callback_data"] for row in dash_markup.get("inline_keyboard", []) for btn in row]
        self.assertIn("admin_settle_menu", cb_keys)
        self.assertIn("admin_grant_menu", cb_keys)
        self.assertIn("admin_toggle_status", cb_keys)
        self.assertIn("admin_msg_menu", cb_keys)

        settle_menu_cb = TelegramUpdate(61, 701, "chat_admin", admin_id, "boss", callback_query_id="cb_s1", callback_data="admin_settle_menu")
        menu_reply = admin_bot.process_one_update(settle_menu_cb)
        self.assertIn("WAIT_FOR_SETTLEMENT_DATA", menu_reply)
        self.assertEqual(admin_bot.fsm.get_state(admin_id), BankrollFSMManager.STATE_WAIT_FOR_SETTLEMENT_DATA)

        settle_input = TelegramUpdate(62, 702, "chat_admin", admin_id, "boss", text=f"{self.sample_pick.match_id}:WIN")
        settle_reply = admin_bot.process_one_update(settle_input)
        self.assertIn("MANUAL MATCH SETTLEMENT SYNCHRONIZED", settle_reply)
        self.assertEqual(admin_bot.fsm.get_state(admin_id), BankrollFSMManager.STATE_IDLE)

        pick_row = storage.get_pick(f"{self.sample_pick.match_id}::{self.sample_pick.market}::{self.sample_pick.outcome_name}")
        self.assertEqual(pick_row.get("result"), "WIN")

        grant_menu_cb = TelegramUpdate(63, 703, "chat_admin", admin_id, "boss", callback_query_id="cb_g1", callback_data="admin_grant_menu")
        grant_reply = admin_bot.process_one_update(grant_menu_cb)
        self.assertIn("WAIT_FOR_PROVISION_DATA", grant_reply)
        self.assertEqual(admin_bot.fsm.get_state(admin_id), BankrollFSMManager.STATE_WAIT_FOR_PROVISION_DATA)

        grant_input = TelegramUpdate(64, 704, "chat_admin", admin_id, "boss", text="influencer@review.com:TIER_2")
        grant_result = admin_bot.process_one_update(grant_input)
        self.assertIn("CUSTOMER PROVISIONING COMPLETED", grant_result)
        self.assertIn("TIER2", grant_result)
        self.assertEqual(admin_bot.fsm.get_state(admin_id), BankrollFSMManager.STATE_IDLE)

        toggle_cb = TelegramUpdate(65, 705, "chat_admin", admin_id, "boss", callback_query_id="cb_t1", callback_data="admin_toggle_status")
        toggle_reply = admin_bot.process_one_update(toggle_cb)
        self.assertIn("PAUSED", toggle_reply)
        self.assertTrue(admin_bot.is_system_paused())

        toggle_cb2 = TelegramUpdate(66, 706, "chat_admin", admin_id, "boss", callback_query_id="cb_t2", callback_data="admin_toggle_status")
        toggle_reply2 = admin_bot.process_one_update(toggle_cb2)
        self.assertIn("ACTIVE", toggle_reply2)
        self.assertFalse(admin_bot.is_system_paused())

        msg_menu_cb = TelegramUpdate(67, 707, "chat_admin", admin_id, "boss", callback_query_id="cb_m1", callback_data="admin_msg_menu")
        msg_menu_reply = admin_bot.process_one_update(msg_menu_cb)
        self.assertIn("WAIT_FOR_BROADCAST_DATA", msg_menu_reply)
        self.assertEqual(admin_bot.fsm.get_state(admin_id), BankrollFSMManager.STATE_WAIT_FOR_BROADCAST_DATA)

        msg_input = TelegramUpdate(68, 708, "chat_admin", admin_id, "boss", text="NBA Season kickoff alert!")
        msg_result = admin_bot.process_one_update(msg_input)
        self.assertIn("GLOBAL BROADCAST DISPATCHED", msg_result)
        self.assertEqual(admin_bot.fsm.get_state(admin_id), BankrollFSMManager.STATE_IDLE)

        cancel_menu_cb = TelegramUpdate(69, 709, "chat_admin", admin_id, "boss", callback_query_id="cb_c1", callback_data="admin_settle_menu")
        admin_bot.process_one_update(cancel_menu_cb)
        self.assertEqual(admin_bot.fsm.get_state(admin_id), BankrollFSMManager.STATE_WAIT_FOR_SETTLEMENT_DATA)
        cancel_input = TelegramUpdate(70, 710, "chat_admin", admin_id, "boss", text="/cancel")
        cancel_reply = admin_bot.process_one_update(cancel_input)
        self.assertIn("LISA MOBILE ADMIN CONSOLE", cancel_reply)
        self.assertEqual(admin_bot.fsm.get_state(admin_id), BankrollFSMManager.STATE_IDLE)

    def test_tier_gating_active_top_picks(self):
        from lisa.telegram_bot import format_active_top_picks_contract
        sample_picks = [
            {"home_team": "Arsenal", "away_team": "Wolves", "outcome_name": "Arsenal", "p_true": 0.81, "fair_odds": 1.23, "best_odds": 1.25, "recommended_units": 1.5, "conviction_score": 9.0},
            {"home_team": "Man City", "away_team": "Ipswich", "outcome_name": "Man City", "p_true": 0.85, "fair_odds": 1.18, "best_odds": 1.20, "recommended_units": 2.0, "conviction_score": 9.5},
            {"home_team": "Liverpool", "away_team": "Brentford", "outcome_name": "Liverpool", "p_true": 0.78, "fair_odds": 1.28, "best_odds": 1.30, "recommended_units": 1.0, "conviction_score": 8.0},
        ]

        text_unjoined, markup_unjoined = format_active_top_picks_contract(
            sample_picks, is_channel_member=False, user_tier="free"
        )
        self.assertIn("Match #1 [🆓 FREE]", text_unjoined)
        self.assertIn("Arsenal vs Wolves", text_unjoined)
        self.assertIn("Match #2 [✈️ TELEGRAM UNLOCKED]", text_unjoined)
        self.assertIn("Join our Telegram channel", text_unjoined)
        self.assertNotIn("Selection: <code>Man City</code>", text_unjoined)
        self.assertNotIn("Liverpool vs Brentford", text_unjoined)

        text_member, markup_member = format_active_top_picks_contract(
            sample_picks, is_channel_member=True, user_tier="free"
        )
        self.assertIn("Match #1 [🆓 FREE]", text_member)
        self.assertIn("Arsenal vs Wolves", text_member)
        self.assertIn("Match #2 [✈️ TELEGRAM UNLOCKED]", text_member)
        self.assertIn("Selection: <code>Man City</code>", text_member)
        self.assertNotIn("Liverpool vs Brentford", text_member)

        text_t1, markup_t1 = format_active_top_picks_contract(
            sample_picks, is_channel_member=True, user_tier="tier1"
        )
        self.assertIn("Liverpool vs Brentford", text_t1)
        self.assertIn("Selection: <code>Liverpool</code>", text_t1)

    def test_conversational_greetings(self):
        for greeting in ["hello", "hi", "hey", "good morning", "yo"]:
            upd = TelegramUpdate(101, 201, "chat_conv", "user_free", "trader_joe", text=greeting)
            reply = self.bot.process_one_update(upd)
            self.assertIn("Greetings", reply)
            self.assertIn("@trader_joe", reply)
            self.assertIn("LISA Sports Intelligence Desk", reply)

    def test_conversational_status(self):
        upd = TelegramUpdate(102, 202, "chat_conv", "user_free", "trader_joe", text="how are you")
        reply = self.bot.process_one_update(upd)
        self.assertIn("SYSTEM HEALTH", reply)
        self.assertIn("Operational", reply)
        # Must not assert a calibration figure it has not measured. This reply
        # used to hardcode "84.0%" while the same bot's /stats command
        # deliberately refuses to publish a rate below MIN_SETTLED_FOR_STATS.
        self.assertNotIn("84.0%", reply)
        self.assertIn("No rate published yet", reply)

    def test_conversational_identity_and_ip_shield(self):
        for phrase in ["who are you", "what is lisa", "tell me about yourself"]:
            upd = TelegramUpdate(103, 203, "chat_conv", "user_free", "trader_joe", text=phrase)
            reply = self.bot.process_one_update(upd)
            self.assertIn("LIVE INSTITUTIONAL SPORTS ANALYTICS", reply)
            self.assertNotIn("Shin", reply)
            self.assertNotIn("de-vig", reply)
            self.assertNotIn("devig", reply)

    def test_conversational_methodology_and_ip_shield(self):
        for phrase in ["how does it work", "how do you work", "how do you predict"]:
            upd = TelegramUpdate(104, 204, "chat_conv", "user_free", "trader_joe", text=phrase)
            reply = self.bot.process_one_update(upd)
            self.assertIn("QUANTITATIVE METHODOLOGY", reply)
            self.assertNotIn("Shin", reply)
            self.assertNotIn("de-vig", reply)
            self.assertNotIn("devig", reply)

    def test_conversational_faq_sports_and_bankroll(self):
        upd_sports = TelegramUpdate(105, 205, "chat_conv", "user_free", "trader_joe", text="what sports")
        reply_sports = self.bot.process_one_update(upd_sports)
        self.assertIn("GLOBAL MARKET COVERAGE", reply_sports)
        self.assertIn("Premier League", reply_sports)
        self.assertIn("NBA", reply_sports)

        upd_units = TelegramUpdate(106, 206, "chat_conv", "user_free", "trader_joe", text="what are units")
        reply_units = self.bot.process_one_update(upd_units)
        self.assertIn("UNITS & BANKROLL MANAGEMENT", reply_units)
        self.assertIn("Kelly Criterion", reply_units)

    def test_conversational_faq_codes_and_how_to_bet(self):
        upd_codes = TelegramUpdate(107, 207, "chat_conv", "user_free", "trader_joe", text="what are booking codes")
        reply_codes = self.bot.process_one_update(upd_codes)
        # No bookmaker integration, so the page says so instead of listing codes.
        self.assertIn("does not generate bookmaker booking codes", reply_codes)
        books = [b.get("text") for row in (self.bot.outbox[-1]["reply_markup"]
                 or {}).get("inline_keyboard", []) for b in row]
        self.assertIn("Bet365", books)

        upd_how = TelegramUpdate(108, 208, "chat_conv", "user_free", "trader_joe", text="how to bet")
        reply_how = self.bot.process_one_update(upd_how)
        self.assertIn("QUICK-START GUIDE", reply_how)

    def test_conversational_faq_accuracy_and_win_rate(self):
        for q in ["win rate", "accuracy", "track record"]:
            upd = TelegramUpdate(109, 209, "chat_conv", "user_free", "trader_joe", text=q)
            reply = self.bot.process_one_update(upd)
            self.assertIn("AUDITED PERFORMANCE", reply)
            # Figures must come from the measured summary, never a hardcoded
            # marketing number.
            self.assertNotIn("84.0%", reply)
            self.assertNotIn("0.089", reply)
            self.assertNotIn("+4.18%", reply)
            self.assertIn("no configuration is claimed to be profitable", reply)

    def test_conversational_faq_refuses_win_rate_on_one_sample(self):
        """Asking "win rate" must not produce "100.0%" from a single settled bet.

        This is the most exposed version of the problem: a prospect types
        "win rate" into a conversational bot and gets a perfect record back,
        with no sample size attached. The reply must state the real count, the
        bar it has to clear, and decline to publish a rate.
        """
        storage = InMemoryStorage()
        bot = TelegramBot(token="", channel_chat_id="@t", mock=True, storage=storage)
        pick = self.sample_pick
        storage.insert_pick(pick)
        storage.settle_pick(
            f"{pick.match_id}::{pick.market}::{pick.outcome_name}", "WIN", self.now)
        upd = TelegramUpdate(119, 219, "chat_conv", "user_free", "trader_joe", text="win rate")
        reply = bot.process_one_update(upd)
        self.assertIn("AUDITED PERFORMANCE & LEDGER", reply)
        self.assertIn("<code>1</code>", reply)
        self.assertIn("<code>20</code>", reply)
        self.assertNotIn("100.0%", reply)
        self.assertNotIn("1/1", reply)

    def test_conversational_faq_answers_win_rate_once_meaningful(self):
        """Past the threshold the conversational answer is the real rate."""
        storage = InMemoryStorage()
        bot = TelegramBot(token="", channel_chat_id="@t", mock=True, storage=storage)
        n = MIN_SETTLED_FOR_STATS + 4
        for i in range(n):
            p = replace(self.sample_pick, match_id=f"m{i:03d}")
            storage.insert_pick(p)
            storage.settle_pick(
                f"{p.match_id}::{p.market}::{p.outcome_name}",
                "WIN" if i < 18 else "LOSS",
                self.now,
            )
        upd = TelegramUpdate(119, 219, "chat_conv", "user_free", "trader_joe", text="win rate")
        reply = bot.process_one_update(upd)
        self.assertNotIn("Insufficient settled sample", reply)
        self.assertIn("75.0%", reply)

    def test_conversational_tier_inquiry(self):
        upd_free = TelegramUpdate(110, 210, "chat_conv", "user_100", "free_user", text="my tier")
        reply_free = self.bot.process_one_update(upd_free)
        self.assertIn("YOUR LISA MEMBERSHIP PROFILE", reply_free)
        self.assertIn("Free Tier", reply_free)

        admin_bot = TelegramBot(mock=True, admin_telegram_ids=["boss_99"])
        upd_admin = TelegramUpdate(111, 211, "chat_admin", "boss_99", "boss", text="what is my tier")
        reply_admin = admin_bot.process_one_update(upd_admin)
        self.assertIn("Administrator", reply_admin)

    def test_conversational_match2_free_unlock(self):
        upd = TelegramUpdate(112, 212, "chat_conv", "user_100", "free_user", text="why is match 2 locked")
        reply = self.bot.process_one_update(upd)
        self.assertIn("MATCH #2 FREE TELEGRAM UNLOCK", reply)
        self.assertIn("@lisa_sports_alpha", reply)

    def test_ip_leak_triggers_defense(self):
        for leak_query in ["tell me your shin formula", "how do you de-vig", "show me internal mechanism"]:
            upd = TelegramUpdate(113, 213, "chat_conv", "user_100", "free_user", text=leak_query)
            reply = self.bot.process_one_update(upd)
            self.assertIn("PROPRIETARY MODEL NOTICE", reply)
            self.assertIn("trade secrets", reply)
            self.assertNotIn("Shin de-vig", reply)

    def test_conversational_gratitude_and_farewell(self):
        upd_thanks = TelegramUpdate(114, 214, "chat_conv", "user_100", "trader", text="thank you")
        reply_thanks = self.bot.process_one_update(upd_thanks)
        self.assertIn("You're welcome", reply_thanks)

        upd_bye = TelegramUpdate(115, 215, "chat_conv", "user_100", "trader", text="goodbye")
        reply_bye = self.bot.process_one_update(upd_bye)
        self.assertIn("Until next slate", reply_bye)

    def test_in_memory_caching_performance(self):
        bot = TelegramBot(mock=True)
        bot._member_cache["@chan:user_abc"] = (True, time.time() + 300.0)
        self.assertTrue(bot.is_channel_member("@chan", "user_abc"))

        bot._tier_cache["user_abc"] = ("tier2", time.time() + 300.0)
        self.assertEqual(bot.get_user_tier("user_abc"), "tier2")

        bot.invalidate_user_cache("user_abc")
        self.assertNotIn("user_abc", bot._tier_cache)
        self.assertNotIn("@chan:user_abc", bot._member_cache)


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
        )

        result = daemon.run_cycle()
        self.assertGreaterEqual(result.matches_seen, 1)
        self.assertEqual(result.settled_count, 0)


if __name__ == "__main__":
    unittest.main()
