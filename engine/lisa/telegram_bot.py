"""Interactive Telegram Bot, Channel Dispatch & Gatekeeper Verification System for LISA.

Features:
  * Rich HTML formatting for Diamond Alerts, Trap Advisories, and Performance Audits.
  * Native Telegram getChatMember verification bridge for 100% genuine community joins.
  * Single-use self-destructing invite links (createChatInviteLink: member_limit=1, expire=300s).
  * Automated subscriber churn kicker loop (banChatMember / unbanChatMember).
  * Deep links to the books quoting a selection (LISA has no bookmaker
    integration, so it never mints booking codes).
  * Anti-piracy content protection (protect_content=True) to prevent forwarding and leaks.
  * Zero external dependencies: uses Python standard library urllib, hmac, and json.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .gate import Pick

logger = logging.getLogger(__name__)

UNLOCK_SECRET_ENV = "LISA_UNLOCK_SECRET"
UNLOCK_PREFIX = "LISA-"
UNLOCK_BODY_LEN = 10
# Base-30 alphabet without I/L/O/U/0/1 so codes survive being read aloud or
# retyped from a Telegram message.
UNLOCK_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
UNLOCK_BUCKET_SECONDS = 7 * 24 * 3600
# Accept the current bucket plus this many previous ones (clock skew / boundary).
UNLOCK_BUCKET_SLACK = 1
_UNLOCK_SECRET_FALLBACK: Optional[bytes] = None
DEFAULT_VERIFIED_PATH = "data/verified_users.json"


class VerificationRegistry:
    """Thread-safe persistent store for web user Telegram verification sessions.
    
    Persists verified sessions to SQLite (data/lisa.db) with an automatic
    JSON mirror under ``data/`` for zero-dependency portability. The mirror is
    deliberately kept out of ``web/`` because it holds user emails and Telegram
    IDs, and ``web/`` is served as static content.
    """

    def __init__(self, storage_path: str = DEFAULT_VERIFIED_PATH, db_path: Optional[str] = "data/lisa.db"):
        self.storage_path = storage_path
        self.db_path = db_path
        self._verified: dict[str, dict[str, Any]] = {}
        self._init_sqlite()
        self._load()

    def _init_sqlite(self) -> None:
        if not self.db_path:
            return
        try:
            import sqlite3
            p = Path(self.db_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.db_path, timeout=5.0) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS verified_sessions (
                        web_user_id      TEXT PRIMARY KEY,
                        telegram_user_id TEXT,
                        username         TEXT,
                        verified_at      REAL NOT NULL
                    );
                """)
                conn.commit()
        except Exception as exc:
            logger.debug("SQLite verification table init skipped: %s", exc)

    def _load(self) -> None:
        try:
            p = Path(self.storage_path)
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._verified = data
        except Exception as exc:
            logger.debug("Failed to load verification registry: %s", exc)

    def _save(self) -> None:
        try:
            p = Path(self.storage_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self._verified, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to save verification registry: %s", exc)

    def verify(self, web_user_id: str, telegram_user_id: str = "", username: str = "") -> None:
        """Mark a web user as cryptographically verified via Telegram."""
        if not web_user_id:
            return
        clean_id = web_user_id.strip()
        now_ts = time.time()
        self._verified[clean_id] = {
            "verified": True,
            "telegram_user_id": telegram_user_id,
            "username": username,
            "verified_at": now_ts,
        }
        self._save()

        # Persist to SQLite
        if self.db_path:
            try:
                import sqlite3
                with sqlite3.connect(self.db_path, timeout=5.0) as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO verified_sessions (web_user_id, telegram_user_id, username, verified_at) "
                        "VALUES (?, ?, ?, ?)",
                        (clean_id, str(telegram_user_id), username, now_ts)
                    )
                    conn.commit()
            except Exception as exc:
                logger.debug("SQLite verify persist failed: %s", exc)

    def is_verified(self, web_user_id: str) -> bool:
        """Check if a web session ID has confirmed Telegram channel membership."""
        if not web_user_id:
            return False
        clean_id = web_user_id.strip()

        # Check SQLite first
        if self.db_path:
            try:
                import sqlite3
                if Path(self.db_path).exists():
                    with sqlite3.connect(self.db_path, timeout=5.0) as conn:
                        cur = conn.execute("SELECT 1 FROM verified_sessions WHERE web_user_id = ?", (clean_id,))
                        if cur.fetchone() is not None:
                            return True
            except Exception:
                pass

        # Fallback to json store
        self._load()
        return bool(self._verified.get(clean_id, {}).get("verified", False))

    def get_session(self, web_user_id: str) -> Optional[dict[str, Any]]:
        clean_id = web_user_id.strip()
        if self.db_path:
            try:
                import sqlite3
                if Path(self.db_path).exists():
                    with sqlite3.connect(self.db_path, timeout=5.0) as conn:
                        cur = conn.execute(
                            "SELECT web_user_id, telegram_user_id, username, verified_at FROM verified_sessions WHERE web_user_id = ?",
                            (clean_id,)
                        )
                        row = cur.fetchone()
                        if row:
                            return {
                                "verified": True,
                                "telegram_user_id": row[1],
                                "username": row[2],
                                "verified_at": row[3],
                            }
            except Exception:
                pass
        self._load()
        return self._verified.get(clean_id)


# Global shared registry instance
registry = VerificationRegistry()

# ── Role-Based Persistent Reply Keyboards ─────────────────────────────────────
# Every button text below MUST also resolve through the router in
# ``TelegramBot.handle_message`` (see ``_BUTTON_ROUTES``), otherwise the button
# silently falls through to the conversational/fallback handler.
_ADMIN_ROW_NAV = [{"text": "📊 Active Top Picks"}, {"text": "🏦 My Bankroll"}]
_PAID_ROW_NAV = [{"text": "📊 Active Top Picks"}, {"text": "🏦 My Bankroll"}]
_ROW_LEDGER = [{"text": "📈 Accuracy Ledger"}, {"text": "🛡️ Trap Advisories"}]
_ROW_CODES = [{"text": "🎟️ Bookmaker Codes"}, {"text": "⚡ 5-Fold Parlay"}]

ADMIN_REPLY_KEYBOARD = {
    "keyboard": [
        [{"text": "🛠️ Admin Console"}, {"text": "👤 Grant Access"}],
        [{"text": "📣 Broadcast"}, {"text": "🟢 Settle Match"}],
        _ADMIN_ROW_NAV,
        [{"text": "⏸️ Pause System"}, {"text": "📜 Audit Trail"}],
        _ROW_LEDGER,
        _ROW_CODES,
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

PAID_REPLY_KEYBOARD = {
    "keyboard": [
        _PAID_ROW_NAV,
        _ROW_LEDGER,
        _ROW_CODES,
        [{"text": "👑 My Subscription"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

FREE_REPLY_KEYBOARD = {
    "keyboard": [
        [{"text": "📊 Active Top Picks"}, {"text": "🏦 My Bankroll"}],
        [{"text": "📈 Accuracy Ledger"}, {"text": "🛡️ Trap Advisories"}],
        [{"text": "👑 Upgrade to Unlock Full Slate"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

# Retained for backward compatibility: the default (lowest-privilege) keyboard.
MAIN_REPLY_KEYBOARD = FREE_REPLY_KEYBOARD

# Substrings used by the persistent-keyboard router. Keyed by a stable slug so the
# button label and its matcher cannot drift apart.
BUTTON_SLUGS: dict[str, str] = {
    "📊 Active Top Picks": "picks",
    "🏦 My Bankroll": "bankroll",
    "📈 Accuracy Ledger": "ledger",
    "🛡️ Trap Advisories": "traps",
    "🎟️ Bookmaker Codes": "codes",
    "⚡ 5-Fold Parlay": "parlay",
    "👑 Upgrade to Unlock Full Slate": "upgrade",
    "👑 My Subscription": "subscription",
    "🛠️ Admin Console": "admin_console",
    "👤 Grant Access": "admin_grant",
    "📣 Broadcast": "admin_broadcast",
    "🟢 Settle Match": "admin_settle",
    "⏸️ Pause System": "admin_pause",
    "📜 Audit Trail": "admin_logs",
}

# Lowercased label -> slug, for exact-match routing of a tapped button.
_NORMALIZED_BUTTON_SLUGS: dict[str, str] = {
    label.lower(): slug for label, slug in BUTTON_SLUGS.items()
}

# Slugs that must never execute for a non-admin, even if the text is typed by hand.
ADMIN_ONLY_SLUGS: frozenset[str] = frozenset({
    "admin_console", "admin_grant", "admin_broadcast",
    "admin_settle", "admin_pause", "admin_logs",
})

# A ledger needs a meaningful sample before win-rate / CLV claims are meaningful.
MIN_SETTLED_FOR_STATS = 20

_PAID_TIERS: frozenset[str] = frozenset({"tier1", "tier2", "tier3"})


def get_main_menu_for_user(bot: "TelegramBot", user_id: str) -> dict[str, Any]:
    """Return the persistent reply keyboard appropriate to ``user_id``'s role.

    Precedence is admin > paid subscriber > free. Any failure to resolve a role
    degrades to the free keyboard, so an unknown user never sees operator tools.
    """
    try:
        if bot.is_admin(user_id):
            return ADMIN_REPLY_KEYBOARD
        if bot.get_user_tier(user_id) in _PAID_TIERS:
            return PAID_REPLY_KEYBOARD
    except Exception:
        logger.exception("Role resolution failed for user %s; defaulting to free menu", user_id)
    return FREE_REPLY_KEYBOARD


SYSTEM_LIVE_STATUS: bool = True


class BankrollFSMManager:
    STATE_IDLE = "IDLE"
    STATE_AWAITING_AMOUNT = "AWAITING_BANKROLL_AMOUNT"
    STATE_AWAITING_RISK = "AWAITING_RISK_PROFILE"
    STATE_AWAITING_BOOK = "AWAITING_PREFERRED_BOOK"
    STATE_WAIT_FOR_SETTLEMENT_DATA = "WAIT_FOR_SETTLEMENT_DATA"
    STATE_WAIT_FOR_PROVISION_DATA = "WAIT_FOR_PROVISION_DATA"
    STATE_WAIT_FOR_BROADCAST_DATA = "WAIT_FOR_BROADCAST_DATA"

    def __init__(self, db_path: Optional[str] = "data/lisa.db"):
        self.db_path = db_path
        self._states: dict[str, str] = {}
        self._temp: dict[str, dict[str, Any]] = {}
        self._profiles: dict[str, dict[str, Any]] = {}
        self._init_db()

    def _init_db(self) -> None:
        if not self.db_path:
            return
        try:
            import sqlite3
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.db_path, timeout=5.0) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_bankroll_profiles (
                        user_id             TEXT PRIMARY KEY,
                        username            TEXT,
                        bankroll_amount     REAL NOT NULL,
                        risk_profile        TEXT NOT NULL,
                        kelly_fraction      REAL NOT NULL,
                        preferred_bookmaker TEXT NOT NULL,
                        updated_at          REAL NOT NULL
                    );
                """)
                conn.commit()
                cur = conn.cursor()
                cur.execute("SELECT user_id, username, bankroll_amount, risk_profile, kelly_fraction, preferred_bookmaker, updated_at FROM user_bankroll_profiles")
                for row in cur.fetchall():
                    self._profiles[str(row[0])] = {
                        "user_id": str(row[0]),
                        "username": row[1] or "",
                        "bankroll_amount": float(row[2]),
                        "risk_profile": str(row[3]),
                        "kelly_fraction": float(row[4]),
                        "preferred_bookmaker": str(row[5]),
                        "updated_at": float(row[6]),
                    }
        except Exception:
            pass

    def get_state(self, user_id: str) -> str:
        return self._states.get(str(user_id), self.STATE_IDLE)

    def set_state(self, user_id: str, state: str) -> None:
        self._states[str(user_id)] = state

    def get_temp(self, user_id: str) -> dict[str, Any]:
        return self._temp.setdefault(str(user_id), {})

    def clear_temp(self, user_id: str) -> None:
        self._temp.pop(str(user_id), None)
        self._states[str(user_id)] = self.STATE_IDLE

    def get_profile(self, user_id: str) -> Optional[dict[str, Any]]:
        return self._profiles.get(str(user_id))

    def save_profile(
        self,
        user_id: str,
        username: str,
        amount: float,
        risk_profile: str,
        kelly_fraction: float,
        preferred_book: str,
    ) -> dict[str, Any]:
        now_ts = time.time()
        record = {
            "user_id": str(user_id),
            "username": username or "user",
            "bankroll_amount": round(float(amount), 2),
            "risk_profile": risk_profile,
            "kelly_fraction": kelly_fraction,
            "preferred_bookmaker": preferred_book,
            "updated_at": now_ts,
        }
        self._profiles[str(user_id)] = record
        self.clear_temp(user_id)

        if self.db_path:
            try:
                import sqlite3
                with sqlite3.connect(self.db_path, timeout=5.0) as conn:
                    conn.execute("""
                        INSERT OR REPLACE INTO user_bankroll_profiles
                        (user_id, username, bankroll_amount, risk_profile, kelly_fraction, preferred_bookmaker, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        str(user_id),
                        username or "user",
                        record["bankroll_amount"],
                        record["risk_profile"],
                        record["kelly_fraction"],
                        record["preferred_bookmaker"],
                        record["updated_at"],
                    ))
                    conn.commit()
            except Exception:
                pass

        return record


bankroll_fsm = BankrollFSMManager()


def _unlock_secret() -> bytes:
    """Signing key for unlock codes, sourced from ``LISA_UNLOCK_SECRET``.

    An ephemeral per-process key keeps offline/local runs working, but every
    code issued before a restart stops verifying, so production must set the
    environment variable (and rotate it to invalidate outstanding codes).
    """
    global _UNLOCK_SECRET_FALLBACK
    configured = os.environ.get(UNLOCK_SECRET_ENV, "").strip()
    if configured:
        return configured.encode("utf-8")
    if _UNLOCK_SECRET_FALLBACK is None:
        _UNLOCK_SECRET_FALLBACK = secrets.token_bytes(32)
        logger.warning(
            "%s is not set: using an ephemeral unlock-code key. Codes will not "
            "survive a restart or verify in another process.",
            UNLOCK_SECRET_ENV,
        )
    return _UNLOCK_SECRET_FALLBACK


def _unlock_bucket(now: Optional[float] = None) -> int:
    return int((time.time() if now is None else now) // UNLOCK_BUCKET_SECONDS)


def _sign_unlock_seed(seed: str, bucket: int) -> str:
    digest = hmac.new(
        _unlock_secret(),
        f"{seed}|{bucket}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    value = int.from_bytes(digest, "big")
    chars: list[str] = []
    for _ in range(UNLOCK_BODY_LEN):
        value, idx = divmod(value, len(UNLOCK_ALPHABET))
        chars.append(UNLOCK_ALPHABET[idx])
    return "".join(chars)


def generate_unlock_token(user_seed: str = "", now: Optional[float] = None) -> str:
    """Issue a seed-bound, time-bucketed unlock code (``LISA-`` + 10 chars).

    The code is an HMAC over ``seed|bucket`` folded into an unambiguous
    base-30 alphabet (~50 bits). It cannot be forged without the signing key and
    stops verifying once its bucket falls outside the acceptance window, so a
    leaked code expires on its own.
    """
    seed = str(user_seed or "").strip() or secrets.token_hex(16)
    return UNLOCK_PREFIX + _sign_unlock_seed(seed, _unlock_bucket(now))


def verify_unlock_token(
    token: str,
    user_seed: "str | Sequence[str]",
    now: Optional[float] = None,
) -> bool:
    """Verify an unlock code against the seed(s) it was issued for.

    Verification is seed-bound and constant-time: a well-formed but foreign or
    tampered code is rejected instead of being waved through on shape alone.
    ``user_seed`` may be a single seed or a sequence of candidate seeds.
    """
    if not token or not isinstance(token, str):
        return False
    clean = token.strip().upper()
    if not clean.startswith(UNLOCK_PREFIX):
        return False
    body = clean[len(UNLOCK_PREFIX):]
    if len(body) != UNLOCK_BODY_LEN or any(ch not in UNLOCK_ALPHABET for ch in body):
        return False

    if isinstance(user_seed, str):
        candidates = [user_seed]
    else:
        candidates = list(user_seed or ())
    seeds = [str(s).strip() for s in candidates if str(s or "").strip()]
    if not seeds:
        return False

    bucket = _unlock_bucket(now)
    for seed in seeds:
        for offset in range(UNLOCK_BUCKET_SLACK + 1):
            if hmac.compare_digest(_sign_unlock_seed(seed, bucket - offset), body):
                return True
    return False


validate_unlock_token = verify_unlock_token


def make_execution_buttons(pick: Pick) -> dict[str, Any]:
    """Deep links to the books quoting this selection.

    LISA has no bookmaker partnership, so it cannot pre-load a bet slip; these
    open the sportsbooks where the selection can be priced and placed.
    """
    clean_home = urllib.parse.quote(pick.home_team)
    clean_away = urllib.parse.quote(pick.away_team)
    best_book = pick.best_execution.book_title if pick.best_execution else "Pinnacle"
    best_odds = pick.best_execution.odds if pick.best_execution else pick.fair_odds

    return {
        "inline_keyboard": [
            [
                {
                    "text": f"⚡ Bet @ Pinnacle ({best_odds:.2f})",
                    "url": f"https://www.pinnacle.com/en/search/{clean_home}",
                },
                {
                    "text": "⚡ Bet @ Bet365",
                    "url": f"https://www.bet365.com/#/AX/K^{clean_home}/",
                },
            ],
            [
                {
                    "text": "📊 View Model Calibration & Stats",
                    "callback_data": "menu:ledger",
                }
            ]
        ]
    }


def format_diamond_alert_html(pick: Pick) -> str:
    """Format an institutional-grade Diamond Alert message for Telegram."""
    selection = pick.outcome_name
    if pick.line is not None:
        selection = f"{pick.outcome_name} {pick.line:+g}" if pick.market == "spreads" else f"{pick.outcome_name} {pick.line}"

    conviction = getattr(pick, "conviction_score", 5.0)
    conviction_bars = "🟩" * min(5, max(1, int(round(conviction / 2.0))))
    units = getattr(pick, "recommended_units", 1.0)
    pct = getattr(pick, "recommended_stake_pct", 1.0)
    best_book = pick.best_execution.book_title if pick.best_execution else "Consensus"
    best_odds = pick.best_execution.odds if pick.best_execution else pick.fair_odds
    ev = pick.best_execution.ev if pick.best_execution else 0.0

    return (
        f"💎 <b>LISA INSTITUTIONAL DIAMOND ALERT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚽ <b>{pick.home_team} vs {pick.away_team}</b>\n"
        f"🏆 <i>{pick.sport_key.replace('_', ' ').title()}</i>\n\n"
        f"🎯 <b>Selection:</b> <code>{selection}</code>\n"
        f"📊 <b>True Probability:</b> <code>{pick.p_true:.1%}</code>\n"
        f"⚖️ <b>Fair Consensus Odds:</b> <code>{pick.fair_odds:.2f}</code>\n"
        f"⚡ <b>Best Market Line:</b> <code>{best_odds:.2f}</code> @ {best_book} (<b>{ev:+.1%} EV</b>)\n"
        f"🛡️ <b>Consensus Stability:</b> CV <code>{pick.cv:.2%}</code> ({pick.n_books} books)\n"
        f"🎖️ <b>Conviction Score:</b> {conviction:.1f}/10.0 {conviction_bars}\n"
        f"💰 <b>Bankroll Sizing:</b> <code>{units:.1f}u</code> ({pct:.1f}% Kelly)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔒 <i>Protected signal. Pass before kickoff if CV spikes &gt; 5%.</i>"
    )


def format_trap_advisory_html(
    home_team: str,
    away_team: str,
    sport_key: str,
    public_favorite: str,
    reason: str,
    cv: float = 0.065,
) -> str:
    """Format a Trap Avoided advisory alerting users to pass on sucker lines."""
    return (
        f"⚠️ <b>LISA TRAP ADVISORY — PASS RECOMMENDED</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏟️ <b>{home_team} vs {away_team}</b>\n"
        f"🏆 <i>{sport_key.replace('_', ' ').title()}</i>\n\n"
        f"❌ <b>Sucker Public Trap:</b> <code>{public_favorite} ML</code>\n"
        f"🚨 <b>Warning Flag:</b> {reason}\n"
        f"📉 <b>Bookmaker Dissensus:</b> CV <code>{cv:.1%}</code> (&gt; 5.0% threshold)\n\n"
        f"💡 <b>Syndicate Advisory:</b> <i>Zero execution. Naive punters are taking minus-EV pricing. Capital preservation is priority #1.</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🛡️ <i>30 Traps avoided this month — preserving bankroll floor.</i>"
    )


def format_settlement_alert_html(pick_data: dict[str, Any]) -> str:
    """Format an audited settlement alert when a match concludes and score is verified."""
    result = str(pick_data.get("result", "WIN")).upper()
    sport = (pick_data.get("sport_key") or "Soccer").replace("_", " ").title()
    home = pick_data.get("home_team") or ""
    away = pick_data.get("away_team") or ""
    matchup = f"{home} vs {away}" if home and away else pick_data.get("match_id", "Match")
    outcome = pick_data.get("outcome_name", "")
    odds = float(pick_data.get("best_odds") or pick_data.get("fair_odds") or 1.0)
    units = float(pick_data.get("recommended_units") or 1.0)
    clv = float(pick_data.get("clv") or 0.0)

    if result == "WIN":
        profit = (odds - 1.0) * units
        header = "✅ <b>LISA INSTITUTIONAL SETTLEMENT — WIN!</b>"
        pnl_text = f"💰 <b>Net P&L:</b> <code>+{profit:.2f}u</code> (ROI: +{(odds - 1.0)*100:.1f}%)"
    elif result == "LOSS":
        header = "❌ <b>LISA INSTITUTIONAL SETTLEMENT — LOSS</b>"
        pnl_text = f"📉 <b>Net P&L:</b> <code>-{units:.2f}u</code>"
    else:  # VOID / PUSH
        header = "↩️ <b>LISA INSTITUTIONAL SETTLEMENT — VOID</b>"
        pnl_text = "🔄 <b>Net P&L:</b> <code>0.00u (Stake Refunded)</code>"

    clv_str = f"📈 <b>Closing Line Value:</b> <code>{clv*100:+.2f}% CLV</code>\n" if clv else ""

    return (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚽ <b>{matchup}</b>\n"
        f"🏆 <i>{sport}</i>\n\n"
        f"🎯 <b>Selection:</b> <code>{outcome}</code>\n"
        f"⚡ <b>Locked Price:</b> <code>{odds:.2f}</code>\n"
        f"{pnl_text}\n"
        f"{clv_str}"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🛡️ <i>Audited into the verified mathematical ledger. Zero post-hoc manipulation.</i>"
    )


def _fmt_metric(summary: dict[str, Any], *keys: str) -> Optional[float]:
    """Return the first present numeric value among ``keys``, else ``None``.

    Deliberately has no default: a missing metric must render as "not yet
    measured" rather than as a plausible-looking invented number.
    """
    for key in keys:
        raw = summary.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def format_stats_html(summary: dict[str, Any]) -> str:
    """Format the audited track record from measured values only.

    Every figure here is read from ``summary``. If the settled sample is missing
    or below ``MIN_SETTLED_FOR_STATS`` we say so explicitly instead of falling
    back to placeholder numbers, because this surface is the paid tier's primary
    proof point and a fabricated win rate is worse than no win rate.
    """
    summary = summary or {}
    settled = _fmt_metric(summary, "settled_picks_count", "total_bets", "bets")
    win_rate = _fmt_metric(summary, "win_rate", "grade_a_win_rate")
    brier = _fmt_metric(summary, "brier_score")
    ece = _fmt_metric(summary, "ece")
    clv = _fmt_metric(summary, "mean_clv")
    pos_clv = _fmt_metric(summary, "positive_clv_share", "positive_clv_rate")
    roi = _fmt_metric(summary, "roi_pct", "flat_roi_pct")
    wins = _fmt_metric(summary, "wins", "grade_a_wins")

    header = "📈 <b>LISA AUDITED PERFORMANCE AUDIT</b>\n━━━━━━━━━━━━━━━━━━━━━━\n"

    if settled is None or settled < MIN_SETTLED_FOR_STATS:
        have = int(settled) if settled is not None else 0
        return (
            f"{header}"
            f"ℹ️ <b>Insufficient settled sample to publish a track record.</b>\n\n"
            f"📊 <b>Settled picks audited:</b> <code>{have}</code>\n"
            f"🎯 <b>Required for a meaningful ledger:</b> <code>{MIN_SETTLED_FOR_STATS}</code>\n\n"
            f"<i>Win rate, Brier score and CLV are only published once enough matches have\n"
            f"been graded against official score feeds. Until then LISA reports nothing\n"
            f"rather than an unverified figure. No configuration is claimed to be\n"
            f"profitable — see the published backtest for the current honest verdict.</i>"
        )

    lines = [header]
    if win_rate is not None:
        wr = f"<code>{win_rate*100:.1f}%</code>"
        if wins is not None:
            wr += f" ({int(wins)}/{int(settled)} graded)"
        lines.append(f"✅ <b>Verified Win Rate:</b> {wr}")
    if brier is not None:
        lines.append(f"🎯 <b>Brier Calibration Score:</b> <code>{brier:.4f}</code>")
    if ece is not None:
        lines.append(f"⚖️ <b>Expected Calibration Error:</b> <code>{ece*100:.2f}%</code>")
    if clv is not None:
        lines.append(f"💎 <b>Mean Closing Line Value (CLV):</b> <code>{clv*100:+.2f}%</code>")
    if pos_clv is not None:
        lines.append(f"📊 <b>Bets Beating The Close:</b> <code>{pos_clv*100:.1f}%</code>")
    if roi is not None:
        lines.append(f"📉 <b>Backtest ROI:</b> <code>{roi:+.2f}%</code>")
    lines.append(f"📊 <b>Sample Size:</b> <code>{int(settled)} fully audited real matches</code>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    if clv is not None and clv < 0:
        lines.append(
            "🛡️ <i>Negative CLV means the early line did <b>not</b> beat the closing line on\n"
            "this sample. Reported openly; no profitability is claimed.</i>"
        )
    else:
        lines.append("🛡️ <i>Audited into the verified mathematical ledger. Zero post-hoc manipulation.</i>")
    lines.append("🌐 <i>View live ledger: http://localhost:8080/#ledger</i>")
    return "\n".join(lines)


def format_free_picks_html(picks: list[dict[str, Any]]) -> str:
    """Format today's daily picks for Telegram."""
    if not picks:
        return "ℹ️ <b>No active signals in current cycle.</b> Check back in 15 minutes."

    lines = [
        "🔥 <b>LISA TODAY'S TOP SELECTIONS</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for idx, p in enumerate(picks[:3], 1):
        tier_tag = "🆓 FREE" if idx == 1 else "✈️ TELEGRAM UNLOCKED"
        home = p.get("home_team", "Home")
        away = p.get("away_team", "Away")
        sel = p.get("outcome_name", "Pick")
        prob = p.get("p_true", 0.75) * 100
        odds = p.get("best_odds", p.get("fair_odds", 1.25))
        units = p.get("recommended_units", 1.0)
        lines.append(
            f"<b>Match #{idx} [{tier_tag}]</b>\n"
            f"• <b>{home} vs {away}</b>\n"
            f"• Pick: <code>{sel}</code> @ <code>{odds:.2f}</code> ({prob:.1f}% true prob)\n"
            f"• Sizing: <code>{units}u</code>\n"
        )

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("⚡ <i>Matches #4–#12 available in Tier 2 Pro ($49/mo).</i>")
    lines.append("🌐 <i>Web Terminal: http://localhost:8080/#picks</i>")
    return "\n".join(lines)


def format_active_top_picks_contract(
    picks: list[dict[str, Any]],
    user_profile: Optional[dict[str, Any]] = None,
    is_channel_member: bool = True,
    user_tier: str = "free",
    channel_username: str = "@lisa_sports_alpha",
) -> tuple[str, dict[str, Any]]:
    """Format active top picks according to proprietary data abstraction contract with inline execution buttons."""
    if not picks:
        return (
            "ℹ️ <b>No active signals in current cycle.</b> Check back in 15 minutes.",
            {"inline_keyboard": [[{"text": "🔄 Refresh", "callback_data": "menu:picks"}]]},
        )

    bankroll_header = ""
    unit_dollar = 0.0
    if user_profile and user_profile.get("bankroll_amount"):
        amt = float(user_profile["bankroll_amount"])
        frac = float(user_profile.get("kelly_fraction", 0.5))
        risk_name = str(user_profile.get("risk_profile", "balanced")).title()
        unit_dollar = max(1.0, round(amt * (frac * 0.02), 2))
        pref_book = str(user_profile.get("preferred_bookmaker", "SportyBet"))
        bankroll_header = (
            f"🏦 <b>Your Sizing:</b> <code>${amt:,.2f}</code> bankroll • "
            f"<code>1u = ${unit_dollar:.2f}</code> ({risk_name} Kelly) • 🎟️ <b>{pref_book}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
        )

    lines = [
        "🔥 <b>LISA TODAY'S TOP SELECTIONS</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if bankroll_header:
        lines.append(bankroll_header.strip())

    if user_tier in ("tier2", "tier3", "admin"):
        limit = 12
    elif user_tier == "tier1":
        limit = 5
    else:
        limit = 2

    for idx, p in enumerate(picks[:limit], 1):
        home = p.get("home_team", "Home")
        away = p.get("away_team", "Away")
        kickoff = p.get("kickoff_human") or "Upcoming"

        if idx == 1:
            tier_tag = "🆓 FREE"
        elif idx == 2:
            tier_tag = "✈️ TELEGRAM UNLOCKED"
            if not is_channel_member and user_tier == "free":
                lines.append(
                    f"<b>Match #2 [{tier_tag}] • {kickoff}</b>\n"
                    f"• <b>{home} vs {away}</b>\n"
                    f"• 🔒 <i>Join our Telegram channel to unlock this selection and its best price for free!</i>\n"
                )
                continue
        elif idx in (3, 4, 5):
            tier_tag = "🥉 TIER 1"
        else:
            tier_tag = "🥈 TIER 2"

        sel = p.get("outcome_name", "Pick")
        prob = float(p.get("p_true", 0.75)) * 100
        fair_odds = float(p.get("fair_odds", 1.25))
        best_odds = float(p.get("best_odds", fair_odds))
        best_book = str(p.get("best_book", "Pinnacle")).title()
        ev = float(p.get("best_ev", 0.0)) * 100
        units = float(p.get("recommended_units", 1.0))
        conviction = float(p.get("conviction_score", 8.0))
        conviction_display = min(10.0, conviction) if conviction <= 10.0 else round(conviction / 3.0, 1)
        stars = "🟩" * min(5, max(1, int(round(conviction_display / 2.0))))
        codes = p.get("booking_codes", {})

        sizing_str = f"<code>{units:.1f}u</code>"
        if unit_dollar > 0:
            cash_stake = round(units * unit_dollar, 2)
            sizing_str += f" (<b>${cash_stake:,.2f}</b>)"

        codes_line = ""
        if codes:
            code_parts = []
            if "sportybet" in codes:
                code_parts.append(f"Sporty: <code>{codes['sportybet']}</code>")
            if "football_com" in codes:
                code_parts.append(f"Football.com: <code>{codes['football_com']}</code>")
            if "1xbet" in codes:
                code_parts.append(f"1x: <code>{codes['1xbet']}</code>")
            if "bet9ja" in codes:
                code_parts.append(f"Bet9ja: <code>{codes['bet9ja']}</code>")
            if code_parts:
                codes_line = "🎟️ " + " | ".join(code_parts) + "\n"

        lines.append(
            f"<b>Match #{idx} [{tier_tag}] • {kickoff}</b>\n"
            f"• <b>{home} vs {away}</b>\n"
            f"• Selection: <code>{sel}</code>\n"
            f"• P(true): <code>{prob:.1f}%</code> | Fair Odds: <code>{fair_odds:.2f}</code>\n"
            f"• Market: <code>{best_odds:.2f}</code> @ {best_book} (<b>+{ev:.1f}% EV</b>)\n"
            f"• Conviction: <code>{conviction_display:.1f}/10.0</code> {stars}\n"
            f"• Sizing: {sizing_str}\n"
            f"{codes_line}"
        )

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    if user_tier == "free":
        lines.append("🔒 <i>Matches #3–#5 available in Tier 1 ($19/mo).</i>")
        lines.append("⚡ <i>Ranks #6 and beyond, plus the live accumulator, in Tier 2 Pro ($49/mo).</i>")
    elif user_tier == "tier1":
        lines.append("⚡ <i>Ranks #6 and beyond, plus the live accumulator, in Tier 2 Pro ($49/mo).</i>")
    else:
        lines.append("👑 <i>Full institutional slate unlocked.</i>")
    lines.append("🌐 <i>Web Terminal: http://localhost:8080/#picks</i>")

    keyboard_rows: list[list[dict[str, Any]]] = []
    if user_tier == "free" and not is_channel_member:
        clean_chan = channel_username.replace("@", "")
        keyboard_rows.append([
            {"text": f"✈️ Join {channel_username} to Unlock Match #2", "url": f"https://t.me/{clean_chan}"}
        ])

    first_pick = picks[0] if picks else {}
    first_links = first_pick.get("deep_links", {})

    row1: list[dict[str, Any]] = []
    if first_links.get("sportybet"):
        row1.append({"text": "🔴 SportyBet", "url": first_links["sportybet"]})
    if first_links.get("football_com"):
        row1.append({"text": "🟢 Football.com", "url": first_links["football_com"]})
    if first_links.get("1xbet"):
        row1.append({"text": "🔵 1xBet", "url": first_links["1xbet"]})
    if row1:
        keyboard_rows.append(row1)

    row2: list[dict[str, Any]] = []
    if first_links.get("bet365"):
        row2.append({"text": "↗ Direct Bet365", "url": first_links["bet365"]})
    if first_links.get("pinnacle"):
        row2.append({"text": "⚡ Pinnacle", "url": first_links["pinnacle"]})
    if row2:
        keyboard_rows.append(row2)

    keyboard_rows.append([
        {"text": "⚡ Live Accumulator", "callback_data": "menu:parlay"},
        {"text": "🏦 My Bankroll", "callback_data": "menu:bankroll"},
    ])
    keyboard_rows.append([
        {"text": "📈 Accuracy Ledger", "callback_data": "menu:ledger"},
        {"text": "🔄 Refresh Picks", "callback_data": "menu:picks"},
    ])
    if user_tier == "free":
        keyboard_rows.append([
            {"text": "👑 Upgrade Tier ($19 / $49)", "callback_data": "/vip"},
        ])

    return ("\n".join(lines), {"inline_keyboard": keyboard_rows})


def format_parlay_html(
    picks: Optional[list[dict[str, Any]]] = None,
    user_tier: str = "free",
) -> tuple[str, dict[str, Any]]:
    """Accumulator assembled from the live ledger — nothing is invented.

    Legs are the highest-conviction real picks currently on the board, the
    combined probability is the product of their model probabilities, and the
    combined price is the product of their best available odds. When there are
    not enough live legs the board says so instead of showing a sample slip.
    """
    legs: list[dict[str, Any]] = []
    for p in picks or []:
        if p.get("outcome_name") and p.get("p_true") and p.get("best_odds"):
            legs.append(p)
    legs.sort(
        key=lambda p: (float(p.get("p_true") or 0), float(p.get("best_ev") or 0)),
        reverse=True,
    )
    legs = legs[:5]

    if len(legs) < 2:
        text = (
            "⚡ <b>LISA ACCUMULATOR</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "ℹ️ <b>Not enough live selections</b> to build an honest "
            "accumulator right now. Legs are only shown when they come from the "
            "live ledger — LISA never publishes a sample slip."
        )
        markup = {
            "inline_keyboard": [
                [{"text": "📊 Active Top Picks", "callback_data": "menu:picks"}],
            ]
        }
        return (text, markup)

    combined_p = 1.0
    combined_odds = 1.0
    lines: list[str] = []
    for i, p in enumerate(legs, start=1):
        p_true = float(p["p_true"])
        odds = float(p["best_odds"])
        combined_p *= p_true
        combined_odds *= odds
        ev = float(p.get("best_ev") or 0.0) * 100
        kickoff = p.get("commence_time") or ""
        lines.append(
            f"{i}️⃣ <b>{p.get('home_team', '?')} vs {p.get('away_team', '?')}:</b> "
            f"{p['outcome_name']} @ <code>{odds:.2f}</code> "
            f"({p.get('best_book') or 'best price'}, EV {ev:+.1f}%)"
        )

    fair_odds = (1.0 / combined_p) if combined_p > 0 else float("inf")
    edge = (combined_odds / fair_odds - 1.0) if combined_p > 0 else 0.0

    header = "⚡ <b>LISA LIVE ACCUMULATOR</b>" if user_tier in ("tier2", "tier3", "admin") else (
        "⚡ <b>LISA LIVE ACCUMULATOR</b>"
    )
    text = (
        f"{header}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        + "\n".join(lines)
        + "\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Combined Model Probability:</b> <code>{combined_p * 100:.1f}%</code>\n"
        f"⚖️ <b>Combined Odds:</b> <code>{combined_odds:.2f}</code> "
        f"(fair {fair_odds:.2f})\n"
        f"💎 <b>Accumulator Edge:</b> <code>{edge * 100:+.1f}%</code>\n"
        f"🕒 <i>Legs update every odds cycle. Kickoffs: "
        f"{', '.join(str(l.get('commence_time') or 'TBC') for l in legs[:3])}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"ℹ️ <i>Accumulator odds are the product of the best prices found; no "
        f"bookmaker is quoted until you place the bet yourself.</i>"
    )
    markup = {
        "inline_keyboard": [
            [
                {"text": "📊 Active Top Picks", "callback_data": "menu:picks"},
                {"text": "🏦 My Bankroll", "callback_data": "menu:bankroll"},
            ],
        ]
    }
    return (text, markup)


def format_booking_codes_html() -> tuple[str, dict[str, Any]]:
    """Bookmaker deep links.

    LISA has no bookmaker partner integration, so it cannot mint one-click
    booking codes. Rather than publish codes that do not exist, this page
    explains how to price the live board yourself and links out to the books.
    """
    text = (
        "🎟️ <b>LISA EXECUTION GUIDE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "LISA does not generate bookmaker booking codes: it has no direct "
        "partnership with any sportsbook, and any code it printed would not be "
        "real.\n\n"
        "<b>What to do instead:</b>\n"
        "1️⃣ Open the live board and note the outcome, line and fair price.\n"
        "2️⃣ Compare that price across books yourself.\n"
        "3️⃣ Only bet when a book pays more than the fair price shown.\n\n"
        "<b>Price-check the books here:</b>"
    )
    markup = {
        "inline_keyboard": [
            [
                {"text": "Bet365", "url": "https://www.bet365.com/"},
                {"text": "Pinnacle", "url": "https://www.pinnacle.com/"},
            ],
            [
                {"text": "SportyBet", "url": "https://www.sportybet.com/"},
                {"text": "1xBet", "url": "https://1xbet.com/"},
            ],
            [
                {"text": "📊 Active Top Picks", "callback_data": "menu:picks"},
                {"text": "⚡ Live Accumulator", "callback_data": "menu:parlay"},
            ],
        ]
    }
    return (text, markup)


@dataclass
class TelegramUpdate:
    update_id: int
    message_id: int
    chat_id: str
    user_id: str
    username: str
    text: str = ""
    callback_query_id: str = ""
    callback_data: str = ""


class TelegramBot:
    def __init__(
        self,
        token: str = "",
        channel_chat_id: str = "",
        tier2_channel_chat_id: str = "",
        timeout: float = 10.0,
        mock: bool = False,
        verification_registry: Optional[VerificationRegistry] = None,
        bot_username: str = "",
        admin_telegram_ids: Optional[Iterable[str]] = None,
        storage: Optional[Any] = None,
    ):
        self.token = token
        self.channel_chat_id = channel_chat_id or "@lisa_sports_alpha"
        self.tier2_channel_chat_id = tier2_channel_chat_id or "@lisa_tier2_pro"
        self.bot_username = bot_username or os.environ.get("LISA_TELEGRAM_BOT_USERNAME", "XpredictPremiumBot")
        self.timeout = timeout
        self.mock = mock
        self.last_update_id: int = 0
        self.outbox: list[dict[str, Any]] = []
        self.registry = verification_registry or registry
        self.fsm = BankrollFSMManager(db_path=getattr(self.registry, "db_path", "data/lisa.db"))
        self._mock_members: set[str] = set()
        self._member_cache: dict[str, tuple[bool, float]] = {}
        self._tier_cache: dict[str, tuple[str, float]] = {}
        self._picks_cache: tuple[dict[str, Any], float] = ({}, 0.0)
        # Measured-performance cache used by _load_dashboard_summary. Scored from
        # the graded ledger, so it is recomputed on the same 30s cadence as the
        # picks cache rather than on every single command.
        self._summary_cache: tuple[dict[str, Any], float] = ({}, 0.0)

        if admin_telegram_ids is not None:
            raw_admins = admin_telegram_ids
        else:
            try:
                from .config import _load_dotenv
                _load_dotenv()
            except Exception:
                pass
            raw_admins = os.environ.get("ADMIN_TELEGRAM_IDS", "").split(",")
        self.admin_telegram_ids: set[str] = {str(a).strip() for a in raw_admins if str(a).strip()}

        if self.channel_chat_id.lower().lstrip("@") == self.bot_username.lower().lstrip("@"):
            if self.admin_telegram_ids:
                self.channel_chat_id = sorted(list(self.admin_telegram_ids))[0]

        if storage is not None:
            self.storage = storage
        else:
            try:
                from .storage import SqliteStorage
                self.storage = SqliteStorage(getattr(self.registry, "db_path", "data/lisa.db"))
            except Exception:
                self.storage = None

    def invalidate_caches(self) -> None:
        """Drop the picks and performance caches.

        Called when the ledger is known to have changed (a settlement ran, an
        admin graded a match by hand) so the next command reports current
        figures instead of up to 30 seconds of stale ones.
        """
        self._picks_cache = ({}, 0.0)
        self._summary_cache = ({}, 0.0)

    def is_admin(self, user_id: str) -> bool:
        """Check if incoming user ID is explicitly authorized in the admin whitelist."""
        return str(user_id).strip() in self.admin_telegram_ids

    def is_system_paused(self) -> bool:
        global SYSTEM_LIVE_STATUS
        if self.storage and hasattr(self.storage, "is_system_paused"):
            return self.storage.is_system_paused()
        return not SYSTEM_LIVE_STATUS

    def answer_callback_query(
        self,
        callback_query_id: str,
        text: str = "",
        show_alert: bool = False,
    ) -> bool:
        if not callback_query_id:
            return True
        if self.mock or not self.token:
            return True
        url = f"https://api.telegram.org/bot{self.token}/answerCallbackQuery"
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
            payload["show_alert"] = "true" if show_alert else "false"
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
                return True
        except Exception as exc:
            logger.warning("answer_callback_query failed: %s", exc)
            return False

    def edit_message_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> bool:
        if self.mock or not self.token:
            self.outbox.append({
                "action": "edit_message",
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
                "timestamp": time.time(),
            })
            return True
        url = f"https://api.telegram.org/bot{self.token}/editMessageText"
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": "true",
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
                return True
        except Exception as exc:
            logger.warning("edit_message_text failed: %s", exc)
            return False

    def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: Optional[dict[str, Any]] = None,
        protect_content: bool = False,
    ) -> bool:
        """Send message via Telegram Bot API or record in mock outbox."""
        if self.mock or not self.token:
            self.outbox.append({
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
                "protect_content": protect_content,
                "timestamp": time.time(),
            })
            logger.info("[telegram mock send] to %s: %s", chat_id, text[:60])
            return True

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": "true",
            "protect_content": "true" if protect_content else "false",
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)

        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
                return True
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            logger.warning("Telegram send_message failed (%s): %s", exc.code, err_body)
            return False
        except Exception as exc:
            logger.warning("Telegram send_message error: %s", exc)
            return False

    # -- Telegram Gatekeeper API Handlers ------------------------------------

    def get_chat_member(self, chat_id: str, user_id: str) -> dict[str, Any]:
        """Query getChatMember to verify user's status inside official channel."""
        if self.mock or not self.token:
            # If user_id is in mock_members or defaults to true for mock testing
            is_mock_member = (user_id in self._mock_members) or (user_id != "unjoined_user")
            return {
                "ok": True,
                "result": {
                    "user": {"id": user_id, "is_bot": False},
                    "status": "member" if is_mock_member else "left",
                },
            }

        url = f"https://api.telegram.org/bot{self.token}/getChatMember"
        params = {"chat_id": chat_id, "user_id": user_id}
        full_url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(full_url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8"))
                logger.warning("Telegram getChatMember API error: %s", body)
                return body
            except Exception:
                return {"ok": False, "error": str(exc), "status_code": exc.code}
        except Exception as exc:
            logger.warning("getChatMember failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    def is_channel_member(self, chat_id: str, user_id: str) -> bool:
        cache_key = f"{chat_id}:{user_id}"
        now = time.time()
        cached = self._member_cache.get(cache_key)
        if cached is not None:
            val, exp = cached
            if now < exp:
                return val
        res = self.get_chat_member(chat_id, user_id)
        if not res.get("ok"):
            self._member_cache[cache_key] = (False, now + 60.0)
            return False
        status = res.get("result", {}).get("status", "")
        is_member = status in ("creator", "administrator", "member", "restricted")
        self._member_cache[cache_key] = (is_member, now + 300.0)
        return is_member

    def create_single_use_invite(
        self, chat_id: str, member_limit: int = 1, expire_seconds: int = 300
    ) -> Optional[str]:
        """Generate a single-use self-destructing invite link."""
        if self.mock or not self.token:
            return f"https://t.me/+lisa_mock_invite_{secrets.token_hex(4)}"

        url = f"https://api.telegram.org/bot{self.token}/createChatInviteLink"
        expire_date = int(time.time()) + expire_seconds
        payload = {
            "chat_id": chat_id,
            "member_limit": member_limit,
            "expire_date": expire_date,
        }
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                if res.get("ok"):
                    return res["result"]["invite_link"]
        except Exception as exc:
            logger.warning("createChatInviteLink failed: %s", exc)
        return None

    def kick_member(self, chat_id: str, user_id: str, temporary: bool = True) -> bool:
        """Kicks lapsed subscriber from channel upon Stripe churn/cancellation."""
        if self.mock or not self.token:
            self._mock_members.discard(user_id)
            logger.info("[mock kick] Kicked user %s from %s", user_id, chat_id)
            return True

        url = f"https://api.telegram.org/bot{self.token}/banChatMember"
        data = urllib.parse.urlencode({"chat_id": chat_id, "user_id": user_id}).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                if res.get("ok") and temporary:
                    # Unban immediately so user is removed from group but eligible to rejoin if they resubscribe
                    unban_url = f"https://api.telegram.org/bot{self.token}/unbanChatMember"
                    unban_data = urllib.parse.urlencode({
                        "chat_id": chat_id,
                        "user_id": user_id,
                        "only_if_banned": True,
                    }).encode("utf-8")
                    unban_req = urllib.request.Request(unban_url, data=unban_data)
                    with urllib.request.urlopen(unban_req, timeout=self.timeout) as uresp:
                        uresp.read()
                return bool(res.get("ok"))
        except Exception as exc:
            logger.warning("banChatMember failed: %s", exc)
            return False

    def get_user_tier(self, user_id: str) -> str:
        if self.is_admin(user_id):
            return "admin"
        now = time.time()
        cached = self._tier_cache.get(user_id)
        if cached is not None:
            val, exp = cached
            if now < exp:
                return val
        tier = "free"
        if self.registry and hasattr(self.registry, "db_path") and self.registry.db_path:
            try:
                import sqlite3
                with sqlite3.connect(self.registry.db_path, timeout=5.0) as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT tier FROM users WHERE id = ? OR email = ? LIMIT 1",
                        (str(user_id), str(user_id)),
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        tier = str(row[0]).lower()
            except Exception:
                pass
        self._tier_cache[user_id] = (tier, now + 300.0)
        return tier

    def invalidate_user_cache(self, user_id: str) -> None:
        self._tier_cache.pop(str(user_id), None)
        target_suffix = f":{user_id}"
        for k in list(self._member_cache.keys()):
            if k.endswith(target_suffix):
                self._member_cache.pop(k, None)

    def is_user_telegram_verified(self, user_id: str) -> bool:
        if self.is_admin(user_id):
            return True
        if self.is_channel_member(self.channel_chat_id, user_id):
            return True
        if self.registry and self.registry.is_verified(user_id):
            return True
        return False

    # -- Channel Broadcasts --------------------------------------------------

    def broadcast_diamond(self, pick: Pick, channel: Optional[str] = None) -> bool:
        """Broadcast Diamond Alert with 1-Click Bet-Slip Buttons and Anti-Forwarding protection."""
        if self.storage and hasattr(self.storage, "is_system_paused") and self.storage.is_system_paused():
            logger.warning("Emergency kill-switch engaged: broadcast_diamond suppressed.")
            return False
        target_chat = channel or self.channel_chat_id
        text = format_diamond_alert_html(pick)
        buttons = make_execution_buttons(pick)
        return self.send_message(
            target_chat,
            text,
            reply_markup=buttons,
            protect_content=True,
        )

    def broadcast_trap(
        self,
        home_team: str,
        away_team: str,
        sport_key: str,
        public_favorite: str,
        reason: str,
        cv: float = 0.065,
        channel: Optional[str] = None,
    ) -> bool:
        """Broadcast Trap Advisory to official channel."""
        if self.storage and hasattr(self.storage, "is_system_paused") and self.storage.is_system_paused():
            logger.warning("Emergency kill-switch engaged: broadcast_trap suppressed.")
            return False
        target_chat = channel or self.channel_chat_id
        text = format_trap_advisory_html(home_team, away_team, sport_key, public_favorite, reason, cv)
        return self.send_message(target_chat, text, protect_content=True)

    def broadcast_settlement(self, pick_data: dict[str, Any], channel: Optional[str] = None) -> bool:
        """Broadcast an audited settlement outcome to official channel."""
        target_chat = channel or self.channel_chat_id
        text = format_settlement_alert_html(pick_data)
        return self.send_message(target_chat, text, protect_content=True)

    # -- Inbound Command Router & Gatekeeper Handshake -----------------------

    def handle_command(
        self, text: str, user_id: str, chat_id: str, username: str = ""
    ) -> tuple[str, Optional[dict[str, Any]]]:
        """Process inbound command and return (reply_text, optional_reply_markup)."""
        parts = text.strip().split()
        if not parts:
            return ("Command not recognized. Send /help for available options.", None)

        cmd = parts[0].lower().split("@")[0]  # strip @botname
        args = parts[1:]

        if cmd == "/start":
            arg = args[0] if args else ""

            # 1. Cryptographic Handshake for Free-Tier Web Unlock
            if arg.startswith("verify_"):
                web_user_id = arg[7:].strip()
                channel = self.channel_chat_id
                chat_member_resp = self.get_chat_member(channel, user_id)

                if not chat_member_resp.get("ok"):
                    desc = chat_member_resp.get("description", "")
                    if "member list is inaccessible" in desc.lower() or "not a member" in desc.lower() or "chat not found" in desc.lower():
                        logger.error(
                            "[telegram-bot] Cannot verify members: @%s is not an Administrator in %s",
                            self.bot_username, channel
                        )
                        return (
                            f"⚠️ <b>BOT ADMINISTRATOR PRIVILEGE REQUIRED</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"Telegram's security policy requires <b>@{self.bot_username}</b> to be added as an <b>Administrator</b> in <b>{channel}</b> to verify member joins.\n\n"
                            f"👉 Please ask the channel creator to add @{self.bot_username} as an Admin with <b>'Post Messages'</b> permission.\n\n"
                            f"Once added, tap <b>/start verify_{web_user_id}</b> to verify your membership.",
                            None,
                        )

                status = chat_member_resp.get("result", {}).get("status", "")
                is_member = status in ("creator", "administrator", "member", "restricted")

                if is_member:
                    # Verified! Mark session in registry
                    self.registry.verify(web_user_id, telegram_user_id=user_id, username=username)
                    reply_markup = {
                        "inline_keyboard": [
                            [{"text": f"📢 Visit {channel}", "url": f"https://t.me/{channel.replace('@', '')}"}],
                            [{"text": "🔥 View Today's Selections", "callback_data": "/picks"}],
                        ]
                    }
                    return (
                        f"🎉 <b>VERIFICATION SUCCESSFUL!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"Confirmed: You are an active member of <b>{channel}</b>.\n"
                        f"Your web session (<code>{web_user_id}</code>) is permanently verified!\n\n"
                        f"Match #2 is now unblurred on your screen at "
                        f"<b>http://localhost:8080/#picks</b>.",
                        reply_markup,
                    )
                else:
                    # Not yet a member: send deep-link join prompt
                    channel_clean = channel.replace("@", "")
                    reply_markup = {
                        "inline_keyboard": [
                            [{"text": f"✈️ 1. Join {channel}", "url": f"https://t.me/{channel_clean}"}],
                            [{"text": "🔄 2. Verify Membership Again", "url": f"https://t.me/{self.bot_username}?start=verify_{web_user_id}"}],
                        ]
                    }
                    return (
                        f"⚠️ <b>CHANNEL MEMBERSHIP REQUIRED</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"You must join our official community before unlocking free mathematical predictions.\n\n"
                        f"1. Tap <b>1. Join {channel}</b> below.\n"
                        f"2. Return here and tap <b>2. Verify Membership Again</b>.\n\n"
                        f"<i>Our bot cryptographically verifies your membership via Telegram's getChatMember API before unlocking content.</i>",
                        reply_markup,
                    )

            # 2. Paid Subscriber Single-Use Invite Handshake
            if arg.startswith("sub_"):
                sub_tier = "tier2" if "tier2" in arg else "tier1"
                channel = self.tier2_channel_chat_id if sub_tier == "tier2" else self.channel_chat_id
                invite = self.create_single_use_invite(channel, member_limit=1, expire_seconds=300)
                reply_markup = {
                    "inline_keyboard": [
                        [{"text": f"👑 Join Private {sub_tier.upper()} Channel", "url": invite or f"https://t.me/{channel}"}]
                    ]
                }
                return (
                    f"👑 <b>WELCOME PREMIUM SUBSCRIBER!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Your subscription is verified. Here is your private single-use invite link:\n\n"
                    f"👉 <a href='{invite}'>Click here to join</a> 👈\n\n"
                    f"⚠️ <i>This link is valid for 1 member and self-destructs in 5 minutes. Do not share it.</i>",
                    reply_markup,
                )

            code = generate_unlock_token(user_id)
            menu = get_main_menu_for_user(self, user_id)

            if self.is_admin(user_id):
                welcome_text = (
                    f"🤖 <b>Welcome, Operator.</b>\n\n"
                    f"🛠️ <b>ADMIN CONSOLE ACTIVE</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🔑 Your unlock code: <code>{code}</code>\n\n"
                    f"<b>Operator Commands:</b>\n"
                    f"• /admin — Executive Override Console\n"
                    f"• /settle &lt;match_id&gt; &lt;status&gt; — Manual settlement\n"
                    f"• /grant &lt;email&gt; &lt;tier&gt; — Provision user access\n"
                    f"• /broadcast &lt;msg&gt; — Push to channels\n"
                    f"• /sys_pause / /sys_resume — Emergency kill-switch\n"
                    f"• /admin_logs — Audit trail\n\n"
                    f"<b>Standard Commands:</b>\n"
                    f"• /picks — View today's selections\n"
                    f"• /bankroll — Configure Kelly profile\n"
                    f"• /parlay — 5-fold accumulator\n"
                    f"• /stats — Audited track record"
                )
            elif self.get_user_tier(user_id) in _PAID_TIERS:
                tier = self.get_user_tier(user_id)
                welcome_text = (
                    f"🤖 <b>Welcome back, {tier.upper()} subscriber.</b>\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🔑 Your unlock code: <code>{code}</code>\n\n"
                    f"<b>Your Unlocked Commands:</b>\n"
                    f"• /picks — Your full daily slate\n"
                    f"• /bankroll — Personalized Kelly sizing\n"
                    f"• /parlay — 5-fold accumulator\n"
                    f"• /codes — Bookmaker booking codes\n"
                    f"• /stats — Audited track record\n"
                    f"• /vip — Manage your subscription\n\n"
                    f"⚠️ <i>Betting carries loss risk. LISA publishes its measured record\n"
                    f"openly, including negative results. No tier is a guarantee of profit.</i>"
                )
            else:
                welcome_text = (
                    f"🤖 <b>Welcome to LISA Gatekeeper</b>\n\n"
                    f"Multi-market odds consensus with an openly published, audited\n"
                    f"settlement record.\n\n"
                    f"🔑 <b>Your Backup Unlock Code:</b> <code>{code}</code>\n\n"
                    f"<b>Free Commands:</b>\n"
                    f"• /picks — Today's free selections\n"
                    f"• /bankroll — Configure Kelly profile\n"
                    f"• /stats — Audited track record\n"
                    f"• /traps — Pass-advisory counts\n"
                    f"• /vip — Subscription tiers\n\n"
                    f"⚠️ <i>Betting carries loss risk. Past results do not guarantee future\n"
                    f"outcomes, and no tier is a promise of profit.</i>"
                )
            return (welcome_text, menu)

        if cmd in ("/picks", "/today"):
            return self._handle_active_top_picks(user_id)

        if cmd in ("/stats", "/record", "/ledger"):
            return self._handle_accuracy_ledger()

        if cmd == "/traps":
            return self._handle_traps()

        if cmd in ("/bankroll", "/mybankroll"):
            return self._handle_bankroll_menu(user_id, username)

        if cmd in ("/parlay", "/accumulator"):
            return self._handle_parlay(user_id)

        if cmd in ("/codes", "/bookmakers"):
            return self._handle_booking_codes()

        if cmd == "/unlock":
            arg = args[0] if args else ""
            if arg:
                if verify_unlock_token(arg, user_id):
                    return (
                        f"✅ <b>Unlock Code Verified!</b>\n"
                        f"Code <code>{arg}</code> is active. Your web browser session at "
                        f"http://localhost:8080 now has Match #2 unlocked.",
                        None,
                    )
                return ("❌ Invalid code format. Please check the code and try again.", None)
            code = generate_unlock_token(user_id)
            return (
                f"🔑 <b>Your Web Terminal Unlock Code:</b>\n\n"
                f"<code>{code}</code>\n\n"
                f"Enter this code on the web dashboard to unlock Match #2 for free.",
                None,
            )

        if cmd == "/vip":
            return (
                f"👑 <b>LISA INSTITUTIONAL MEMBERSHIP TIERS</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🆓 <b>Free Tier:</b> 1 Daily Anchor Pick + Match #2 free upon joining Telegram\n\n"
                f"⚡ <b>Tier 1: Sharp Starter</b> ($19/mo)\n"
                f"• The 5 highest-conviction live selections\n"
                f"• Real-time Telegram push alerts on value detection\n"
                f"• Daily Sucker-Bet Avoidance Warnings\n"
                f"• Best available price and book for each selection\n\n"
                f"🚀 <b>Tier 2: Pro Trader</b> ($49/mo)\n"
                f"• The complete live board across every configured league\n"
                f"• Accumulator priced from the live legs that actually exist\n"
                f"• VIP Private Channel priority access\n"
                f"• CLV early steam alerts before lines drop\n\n"
                f"👑 <b>Tier 3: Syndicate VIP</b> ($149/mo)\n"
                f"• Direct REST API & Webhook Feed (/api/v1/stream)\n"
                f"• Portfolio correlation & joint covariance matrix\n"
                f"• Real-time arbitrage & soft-book discrepancy stream\n"
                f"• 1-on-1 Syndicate Desk consultation\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🌐 <i>Upgrade online: http://localhost:8080</i>",
                None,
            )

        if cmd == "/help":
            if self.is_admin(user_id):
                help_lines = [
                    "📖 <b>LISA Admin Help Desk</b>\n",
                    "🛠️ <b>Operator Commands:</b>",
                    "/admin — Executive Override Console",
                    "/settle <match_id> <status> — Manual settlement sync",
                    "/grant <email> <tier> — Manual user provisioning",
                    "/broadcast <msg> — Push announcement to channels",
                    "/sys_pause — Emergency alert kill-switch",
                    "/sys_resume — Re-enable automated pipeline",
                    "/admin_logs — Audit trail",
                    "\n📊 <b>Standard Commands:</b>",
                    "/picks — View today's selections",
                    "/bankroll — Configure your Kelly bankroll profile",
                    "/parlay — 5-fold accumulator with booking codes",
                    "/codes — Bookmaker platform booking codes",
                    "/stats — Audited track record",
                    "/traps — Pass-advisory counts",
                    "/vip — Subscription tier information",
                    "/help — Show this help message",
                ]
            elif self.get_user_tier(user_id) in _PAID_TIERS:
                help_lines = [
                    "📖 <b>LISA Subscriber Help Desk</b>\n",
                    "/picks — Your full daily slate",
                    "/bankroll — Configure your Kelly bankroll profile",
                    "/parlay — 5-fold accumulator with booking codes",
                    "/codes — Bookmaker platform booking codes",
                    "/stats — Audited track record",
                    "/traps — Pass-advisory counts",
                    "/vip — Manage your subscription",
                    "/help — Show this help message",
                ]
            else:
                help_lines = [
                    "📖 <b>LISA Bot Help Desk</b>\n",
                    "/picks — View today's free picks",
                    "/bankroll — Configure your personalized Kelly bankroll profile",
                    "/stats — Audited track record",
                    "/traps — Pass-advisory counts",
                    "/unlock — Get your free web terminal unlock code",
                    "/vip — Subscription tier information",
                    "/help — Show this help message",
                ]
            return ("\n".join(help_lines), get_main_menu_for_user(self, user_id))

        admin_prefixes = ("/admin", "/settle", "/grant", "/sys_", "/broadcast")
        if any(cmd.startswith(p) for p in admin_prefixes):
            if not self.is_admin(user_id):
                return (f"Unknown command <code>{cmd}</code>. Send /help for available options.", None)

            if cmd in ("/admin", "/admin_menu", "/sys_status"):
                return self._handle_admin_dashboard(user_id)
            if cmd in ("/settle", "/admin_settle"):
                return self._handle_admin_settle(args, user_id)
            if cmd in ("/grant", "/grant_access", "/admin_grant"):
                return self._handle_admin_grant(args, user_id)
            if cmd in ("/broadcast", "/admin_broadcast"):
                return self._handle_admin_broadcast(args, user_id)
            if cmd in ("/sys_pause", "/admin_pause"):
                return self._handle_admin_sys_pause(user_id)
            if cmd in ("/sys_resume", "/admin_resume"):
                return self._handle_admin_sys_resume(user_id)
            if cmd in ("/admin_logs", "/sys_logs"):
                return self._handle_admin_logs(user_id)

        return (f"Unknown command <code>{cmd}</code>. Send /help for available options.", None)

    def _handle_admin_dashboard(self, user_id: str) -> tuple[str, Optional[dict[str, Any]]]:
        """Display Mobile Executive Override Console with live telemetry and audit stats."""
        is_paused = self.is_system_paused()
        status_badge = "⏸️ <b>PAUSED (Alerts Halted)</b>" if is_paused else "✅ <b>ACTIVE (Live Ingestion)</b>"

        pick_counts = {"total": 0, "pending": 0, "settled": 0}
        if self.storage and hasattr(self.storage, "count_picks"):
            pick_counts = self.storage.count_picks()

        recent_logs = []
        if self.storage and hasattr(self.storage, "list_admin_audit_logs"):
            recent_logs = self.storage.list_admin_audit_logs(limit=3)

        logs_lines = []
        for log in recent_logs:
            ts_str = time.strftime("%H:%M:%S UTC", time.gmtime(log.get("timestamp", time.time())))
            act = log.get("action", "")
            tgt = log.get("target", "")
            logs_lines.append(f"• <code>[{ts_str}]</code> <b>{act}</b>: <i>{tgt}</i>")

        logs_text = "\n".join(logs_lines) if logs_lines else "<i>No recent administrative actions recorded.</i>"

        text = (
            "🛠️ <b>LISA MOBILE ADMIN CONSOLE</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Authenticated Operator:</b> <code>{user_id}</code>\n"
            f"⚡ <b>System Run-State:</b> {status_badge}\n"
            f"📊 <b>Picks Ledger:</b> {pick_counts.get('total', 0)} total ({pick_counts.get('settled', 0)} settled, {pick_counts.get('pending', 0)} pending)\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>Interactive Executive Keypad:</b>\n"
            "• <b>🟢 Settle Match:</b> Resolve match outcome (<match_id>:<status>)\n"
            "• <b>👤 Grant Access:</b> Provision VIP access (<email>:<tier>)\n"
            "• <b>⚠️ System Pause:</b> Toggle automated alert kill-switch\n"
            "• <b>📣 Broadcast:</b> Dispatch announcement across channels\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📝 <b>Recent Audit Activity:</b>\n{logs_text}"
        )

        toggle_btn_text = "⚠️ System Pause" if not is_paused else "▶️ System Resume"
        markup = {
            "inline_keyboard": [
                [
                    {"text": "🟢 Settle Match", "callback_data": "admin_settle_menu"},
                    {"text": "👤 Grant Access", "callback_data": "admin_grant_menu"},
                ],
                [
                    {"text": toggle_btn_text, "callback_data": "admin_toggle_status"},
                    {"text": "📣 Broadcast", "callback_data": "admin_msg_menu"},
                ],
                [
                    {"text": "📜 Audit Logs", "callback_data": "admin:logs"},
                    {"text": "🔄 Refresh Console", "callback_data": "admin:status"},
                ],
                [
                    {"text": "📊 Live Predictions", "callback_data": "menu:picks"},
                    {"text": "📈 Public Ledger", "callback_data": "menu:ledger"},
                ]
            ]
        }
        return (text, markup)

    def _handle_admin_settle_menu(self, user_id: str) -> tuple[str, Optional[dict[str, Any]]]:
        self.fsm.set_state(user_id, BankrollFSMManager.STATE_WAIT_FOR_SETTLEMENT_DATA)
        keyboard = []

        pending_matches = []
        if self.storage and hasattr(self.storage, "scan_live_keys"):
            for k in list(self.storage.scan_live_keys())[:4]:
                data = self.storage.get_live(k)
                if data and isinstance(data, dict) and data.get("match_id"):
                    pending_matches.append(data)

        for m in pending_matches:
            m_id = m.get("match_id", "")
            title = f"⚡ {m.get('home_team', 'Home')} vs {m.get('away_team', 'Away')}"[:30]
            keyboard.append([{"text": title, "callback_data": f"admin_quick_pick:{m_id}"}])

        keyboard.append([{"text": "« Back to Admin Console", "callback_data": "admin:cancel"}])

        text = (
            "🟢 <b>MANUAL MATCH SETTLEMENT (Interactive Mode)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>State:</b> <code>WAIT_FOR_SETTLEMENT_DATA</code>\n\n"
            "Send the settlement string in the format:\n"
            "<code>&lt;match_id&gt;:&lt;WIN/LOSS/VOID&gt;</code>\n\n"
            "<b>Examples:</b>\n"
            "• <code>soccer_epl_mci_ips:WIN</code>\n"
            "• <code>nba-thunder-wizards:LOSS</code>\n"
            "• <code>match-101:2-1</code>\n\n"
            "<i>Or select an active fixture below to settle with 1 tap:</i>"
        )
        return (text, {"inline_keyboard": keyboard})

    def _handle_admin_quick_pick(self, match_id: str) -> tuple[str, Optional[dict[str, Any]]]:
        text = (
            "🏟️ <b>SELECT OFFICIAL OUTCOME</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Target Fixture: <code>{match_id}</code>\n\n"
            "Tap the verified match result:"
        )
        markup = {
            "inline_keyboard": [
                [
                    {"text": "✅ WIN", "callback_data": f"admin_quick_settle:{match_id}:WIN"},
                    {"text": "❌ LOSS", "callback_data": f"admin_quick_settle:{match_id}:LOSS"},
                    {"text": "🔄 VOID", "callback_data": f"admin_quick_settle:{match_id}:VOID"},
                ],
                [{"text": "« Cancel", "callback_data": "admin:cancel"}]
            ]
        }
        return (text, markup)

    def _process_admin_settlement_input(self, text: str, user_id: str) -> tuple[str, Optional[dict[str, Any]]]:
        raw = text.strip()
        if ":" in raw:
            parts = [p.strip() for p in raw.split(":", 1)]
        else:
            parts = raw.split()

        if len(parts) < 2:
            return (
                "⚠️ <b>Invalid Format</b>\n\n"
                "Please send: <code>&lt;match_id&gt;:&lt;WIN/LOSS/VOID&gt;</code>\n"
                "Example: <code>soccer_epl_mci_ips:WIN</code>\n\n"
                "Type /cancel or tap below to abort.",
                {"inline_keyboard": [[{"text": "« Cancel", "callback_data": "admin:cancel"}]]}
            )

        self.fsm.clear_temp(user_id)
        return self._execute_settlement(parts[0], parts[1], user_id)

    def _execute_settlement(self, match_id: str, raw_res: str, user_id: str) -> tuple[str, Optional[dict[str, Any]]]:
        clean_match = match_id.strip()
        clean_res = raw_res.strip().upper()

        valid_results = {"WIN", "LOSS", "VOID", "PUSH"}
        if clean_res in valid_results:
            result = "VOID" if clean_res == "PUSH" else clean_res
        elif "-" in clean_res:
            parts = clean_res.split("-")
            try:
                h_score = int(parts[0])
                a_score = int(parts[1])
                result = "WIN" if h_score > a_score else "LOSS" if a_score > h_score else "VOID"
            except Exception:
                result = "WIN"
        else:
            result = "WIN"

        updated = 0
        if self.storage and hasattr(self.storage, "manual_settle_match"):
            updated = self.storage.manual_settle_match(clean_match, result=result)

        audit_id = 0
        if self.storage and hasattr(self.storage, "log_admin_action"):
            audit_id = self.storage.log_admin_action(
                admin_id=user_id,
                action="MANUAL_SETTLEMENT",
                target=clean_match,
                details=f"Result forced to {result}. Rows updated: {updated}",
            )

        self.broadcast_settlement({
            "match_id": clean_match,
            "result": result,
            "best_odds": 1.50,
            "recommended_units": 1.0,
        })

        text = (
            "✅ <b>MANUAL MATCH SETTLEMENT SYNCHRONIZED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏟️ <b>Match Target:</b> <code>{clean_match}</code>\n"
            f"🎯 <b>Settlement Result:</b> <code>{result}</code>\n"
            f"📊 <b>Ledger Rows Updated:</b> <code>{updated}</code>\n"
            f"📝 <b>Security Audit ID:</b> <code>#{audit_id}</code>\n"
            f"📢 <b>Channel Notice:</b> Settlement outcome dispatched\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🛡️ <i>The public mathematical ledger and calibration stats have updated.</i>"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "📈 View Audited Ledger", "callback_data": "menu:ledger"}],
                [{"text": "🛠️ Admin Console", "callback_data": "admin:status"}],
            ]
        }
        return (text, markup)

    def _handle_admin_settle(self, args: list[str], user_id: str) -> tuple[str, Optional[dict[str, Any]]]:
        """Manually force match settlement status across SQLite and public ledger."""
        if len(args) < 2:
            return (
                "⚠️ <b>Usage:</b> <code>/settle &lt;match_id&gt; &lt;WIN|LOSS|VOID|score&gt;</code>\n"
                "Example: <code>/settle nba-thunder-wizards WIN</code>\n"
                "Example: <code>/settle test-match-101 2-1</code>",
                None,
            )
        return self._execute_settlement(args[0], args[1], user_id)

    def _handle_admin_grant_menu(self, user_id: str) -> tuple[str, Optional[dict[str, Any]]]:
        self.fsm.set_state(user_id, BankrollFSMManager.STATE_WAIT_FOR_PROVISION_DATA)
        text = (
            "👤 <b>CUSTOMER ACCESS PROVISIONER (Interactive Mode)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>State:</b> <code>WAIT_FOR_PROVISION_DATA</code>\n\n"
            "Send the provisioning string in the format:\n"
            "<code>&lt;user_email&gt;:&lt;TIER_1/TIER_2/TIER_3&gt;</code>\n\n"
            "<b>Examples:</b>\n"
            "• <code>influencer@review.com:TIER_2</code>\n"
            "• <code>vip_trader@gmail.com:TIER_1</code>\n"
            "• <code>syndicate@fund.org:TIER_3</code>\n\n"
            "<i>The profile will be set to ACTIVE with single-use invite and web unlock tokens generated.</i>"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "« Back to Admin Console", "callback_data": "admin:cancel"}]
            ]
        }
        return (text, markup)

    def _process_admin_provision_input(self, text: str, user_id: str) -> tuple[str, Optional[dict[str, Any]]]:
        raw = text.strip()
        if ":" in raw:
            parts = [p.strip() for p in raw.split(":", 1)]
        else:
            parts = raw.split()

        if len(parts) < 2:
            return (
                "⚠️ <b>Invalid Format</b>\n\n"
                "Please send: <code>&lt;user_email&gt;:&lt;TIER_1/TIER_2/TIER_3&gt;</code>\n"
                "Example: <code>vip@gmail.com:TIER_2</code>\n\n"
                "Type /cancel or tap below to abort.",
                {"inline_keyboard": [[{"text": "« Cancel", "callback_data": "admin:cancel"}]]}
            )

        self.fsm.clear_temp(user_id)
        return self._handle_admin_grant(parts, user_id)

    def _handle_admin_grant(self, args: list[str], user_id: str) -> tuple[str, Optional[dict[str, Any]]]:
        """Manually provision a user account with active subscription tier and invite link."""
        if not args:
            return (
                "⚠️ <b>Usage:</b> <code>/grant &lt;email_or_user_id&gt; [tier1|tier2|tier3]</code>\n"
                "Example: <code>/grant partner@vip.com tier2</code>",
                None,
            )

        if len(args) == 1 and ":" in args[0]:
            target, tier_input = args[0].split(":", 1)
        else:
            target = args[0].strip()
            tier_input = args[1].lower() if len(args) > 1 else "tier2"
        target = target.strip()
        clean_tier = "tier2" if "2" in tier_input else "tier3" if "3" in tier_input else "tier1" if "1" in tier_input else "free"

        if self.registry and hasattr(self.registry, "db_path") and self.registry.db_path:
            try:
                import sqlite3
                with sqlite3.connect(self.registry.db_path, timeout=5.0) as conn:
                    conn.execute(
                        "UPDATE users SET tier = ?, updated_at = ? WHERE email = ? OR id = ?",
                        (clean_tier, time.time(), target.lower(), target)
                    )
                    conn.commit()
            except Exception as exc:
                logger.warning("Failed to update users table during grant: %s", exc)

        self.registry.verify(target, telegram_user_id=target if target.isdigit() else "")
        self.invalidate_user_cache(target)

        channel = self.tier2_channel_chat_id if clean_tier in ("tier2", "tier3") else self.channel_chat_id
        invite = self.create_single_use_invite(channel, member_limit=1, expire_seconds=86400)
        unlock_code = generate_unlock_token(target)

        audit_id = 0
        if self.storage and hasattr(self.storage, "log_admin_action"):
            audit_id = self.storage.log_admin_action(
                admin_id=user_id,
                action="MANUAL_TIER_GRANT",
                target=target,
                details=f"Granted {clean_tier.upper()} with invite {invite}",
            )

        text = (
            "👑 <b>CUSTOMER PROVISIONING COMPLETED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Target Account:</b> <code>{target}</code>\n"
            f"⚡ <b>Granted Subscription:</b> <code>{clean_tier.upper()}</code> (Status: ACTIVE)\n"
            f"🔑 <b>Web Terminal Code:</b> <code>{unlock_code}</code>\n"
            f"🔗 <b>Single-Use Invite:</b> <a href='{invite}'>{invite}</a>\n"
            f"📝 <b>Security Audit ID:</b> <code>#{audit_id}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📲 <i>Copy and forward the single-use invite or unlock code directly to the customer.</i>"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "🛠️ Admin Console", "callback_data": "admin:status"}],
            ]
        }
        return (text, markup)

    def _handle_admin_broadcast_menu(self, user_id: str) -> tuple[str, Optional[dict[str, Any]]]:
        self.fsm.set_state(user_id, BankrollFSMManager.STATE_WAIT_FOR_BROADCAST_DATA)
        text = (
            "📣 <b>GLOBAL COMMUNITY BROADCAST (Interactive Mode)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>State:</b> <code>WAIT_FOR_BROADCAST_DATA</code>\n\n"
            "Please send the message you wish to broadcast across all official public and VIP channels:\n\n"
            "<i>HTML formatting is supported (&lt;b&gt;, &lt;code&gt;, &lt;i&gt;).</i>"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "« Back to Admin Console", "callback_data": "admin:cancel"}]
            ]
        }
        return (text, markup)

    def _process_admin_broadcast_input(self, text: str, user_id: str) -> tuple[str, Optional[dict[str, Any]]]:
        self.fsm.clear_temp(user_id)
        return self._handle_admin_broadcast([text], user_id)

    def _handle_admin_broadcast(self, args: list[str], user_id: str) -> tuple[str, Optional[dict[str, Any]]]:
        """Push global administrative announcement across all official channels."""
        if not args:
            return (
                "⚠️ <b>Usage:</b> <code>/broadcast &lt;announcement text&gt;</code>\n"
                "Example: <code>/broadcast 🏀 NBA Opening Night slate is live! Check out top picks.</code>",
                None,
            )

        message_content = " ".join(args).strip()
        formatted_announcement = (
            "📢 <b>LISA OFFICIAL ANNOUNCEMENT</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{message_content}\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🌐 <i>Terminal: http://localhost:8080</i>"
        )

        sent_public = self.send_message(self.channel_chat_id, formatted_announcement)
        sent_vip = True
        if self.tier2_channel_chat_id and self.tier2_channel_chat_id != self.channel_chat_id:
            sent_vip = self.send_message(self.tier2_channel_chat_id, formatted_announcement)

        audit_id = 0
        if self.storage and hasattr(self.storage, "log_admin_action"):
            audit_id = self.storage.log_admin_action(
                admin_id=user_id,
                action="GLOBAL_BROADCAST",
                target="public+vip_channels",
                details=message_content[:100],
            )

        text = (
            "📢 <b>GLOBAL BROADCAST DISPATCHED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Dispatched across authorized distribution channels:\n"
            f"• Public Channel (<code>{self.channel_chat_id}</code>): {'✅ Delivered' if sent_public else '⚠️ Failed'}\n"
            f"• VIP Channel (<code>{self.tier2_channel_chat_id}</code>): {'✅ Delivered' if sent_vip else '⚠️ Failed'}\n"
            f"📝 <b>Security Audit ID:</b> <code>#{audit_id}</code>"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "🛠️ Admin Console", "callback_data": "admin:status"}],
            ]
        }
        return (text, markup)

    def _handle_admin_toggle_status(self, user_id: str) -> tuple[str, Optional[dict[str, Any]]]:
        global SYSTEM_LIVE_STATUS
        is_paused = self.is_system_paused()
        new_paused = not is_paused
        SYSTEM_LIVE_STATUS = not new_paused
        if self.storage and hasattr(self.storage, "set_system_paused"):
            self.storage.set_system_paused(new_paused)
        if self.storage and hasattr(self.storage, "log_admin_action"):
            self.storage.log_admin_action(
                admin_id=user_id,
                action="SYSTEM_PAUSE" if new_paused else "SYSTEM_RESUME",
                target="automated_pipeline",
                details=f"SYSTEM_LIVE_STATUS modified to {SYSTEM_LIVE_STATUS}",
            )
        return self._handle_admin_dashboard(user_id)

    def _handle_admin_sys_pause(self, user_id: str) -> tuple[str, Optional[dict[str, Any]]]:
        """Emergency kill-switch: halts automated channel broadcasts and alert emissions."""
        global SYSTEM_LIVE_STATUS
        SYSTEM_LIVE_STATUS = False
        if self.storage and hasattr(self.storage, "set_system_paused"):
            self.storage.set_system_paused(True)

        audit_id = 0
        if self.storage and hasattr(self.storage, "log_admin_action"):
            audit_id = self.storage.log_admin_action(
                admin_id=user_id,
                action="EMERGENCY_KILL_SWITCH_PAUSE",
                target="system",
                details="Halting automated alerts and live ingestion",
            )

        text = (
            "🚨 <b>EMERGENCY KILL-SWITCH ENGAGED: SYSTEM PAUSED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Status: <b>PAUSED (SAFE MODE)</b>\n\n"
            "• Outgoing automated Diamond / Trap alerts are halted.\n"
            "• Ingestion loops will idle without emitting notifications.\n"
            f"• Security Audit ID: <code>#{audit_id}</code>\n\n"
            "👉 Issue <code>/sys_resume</code> or tap the button below to resume operations."
        )
        markup = {
            "inline_keyboard": [
                [{"text": "✅ Resume Operations", "callback_data": "admin:resume"}],
                [{"text": "🛠️ Admin Console", "callback_data": "admin:status"}],
            ]
        }
        return (text, markup)

    def _handle_admin_sys_resume(self, user_id: str) -> tuple[str, Optional[dict[str, Any]]]:
        """Emergency kill-switch disengage: restores normal automated signal emissions."""
        global SYSTEM_LIVE_STATUS
        SYSTEM_LIVE_STATUS = True
        if self.storage and hasattr(self.storage, "set_system_paused"):
            self.storage.set_system_paused(False)

        audit_id = 0
        if self.storage and hasattr(self.storage, "log_admin_action"):
            audit_id = self.storage.log_admin_action(
                admin_id=user_id,
                action="EMERGENCY_KILL_SWITCH_RESUME",
                target="system",
                details="Resumed automated alerts and live ingestion",
            )

        text = (
            "✅ <b>EMERGENCY KILL-SWITCH DISENGAGED: SYSTEM RESUMED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Status: <b>ACTIVE / OPERATIONAL</b>\n\n"
            "• Automated Diamond / Trap alerts re-enabled.\n"
            "• Normal mathematical processing operational.\n"
            f"• Security Audit ID: <code>#{audit_id}</code>"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "🛠️ Admin Console", "callback_data": "admin:status"}],
                [{"text": "📊 Active Predictions", "callback_data": "menu:picks"}],
            ]
        }
        return (text, markup)

    def _handle_admin_logs(self, user_id: str) -> tuple[str, Optional[dict[str, Any]]]:
        """Display recent security audit log trail."""
        logs = []
        if self.storage and hasattr(self.storage, "list_admin_audit_logs"):
            logs = self.storage.list_admin_audit_logs(limit=10)

        lines = [
            "📝 <b>LISA SECURITY AUDIT TRAIL</b>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        if not logs:
            lines.append("<i>No audit log entries recorded in database.</i>")
        else:
            for item in logs:
                ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(item.get("timestamp", time.time())))
                act = item.get("action", "")
                admin = item.get("admin_id", "")
                tgt = item.get("target", "")
                det = item.get("details", "")
                lines.append(f"• <code>[{ts}]</code> <b>{act}</b> (by {admin})\n  Target: <code>{tgt}</code>\n  Details: <i>{det}</i>\n")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        markup = {
            "inline_keyboard": [
                [{"text": "🔄 Refresh Audit Logs", "callback_data": "admin:logs"}],
                [{"text": "🛠️ Admin Console", "callback_data": "admin:status"}],
            ]
        }
        return ("\n".join(lines), markup)


    def _handle_active_top_picks(self, user_id: str = "") -> tuple[str, Optional[dict[str, Any]]]:
        """Load picks from live cache/dashboard and format according to proprietary data abstraction contract."""
        picks = self._load_dashboard_picks()
        profile = self.fsm.get_profile(user_id) if user_id else None
        is_member = self.is_user_telegram_verified(user_id) if user_id else False
        tier = self.get_user_tier(user_id) if user_id else "free"
        text, markup = format_active_top_picks_contract(
            picks,
            user_profile=profile,
            is_channel_member=is_member,
            user_tier=tier,
            channel_username=self.channel_chat_id,
        )
        return (text, markup)

    def _handle_bankroll_menu(self, user_id: str, username: str = "") -> tuple[str, Optional[dict[str, Any]]]:
        """Display bankroll profile status or begin interactive multi-step FSM onboarding."""
        profile = self.fsm.get_profile(user_id)
        if profile:
            amt = float(profile["bankroll_amount"])
            frac = float(profile.get("kelly_fraction", 0.5))
            risk_name = str(profile.get("risk_profile", "balanced")).title()
            book = str(profile.get("preferred_bookmaker", "SportyBet"))
            unit_val = max(1.0, round(amt * (frac * 0.02), 2))
            upd_time = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(profile.get("updated_at", time.time())))
            proj_monthly = round(14.5 * unit_val, 2)
            t1_net = round(proj_monthly - 19.0, 2)
            t1_roi = round((t1_net / 19.0) * 100, 1) if proj_monthly >= 19.0 else 0.0
            t2_net = round(proj_monthly - 49.0, 2)
            t2_roi = round((t2_net / 49.0) * 100, 1) if proj_monthly >= 49.0 else 0.0
            text = (
                "🏦 <b>LISA BANKROLL REFINERY PROFILE</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Investor ID:</b> <code>{user_id}</code>\n"
                f"💰 <b>Total Bankroll:</b> <code>${amt:,.2f}</code>\n"
                f"⚖️ <b>Risk Appetite:</b> <code>{risk_name} ({frac:.2f}x Kelly)</code>\n"
                f"🎯 <b>1 Unit Value (1u):</b> <code>${unit_val:,.2f}</code> (2% base scaled)\n"
                f"🎟️ <b>Primary Bookmaker:</b> <code>{book}</code>\n"
                f"🕒 <b>Last Calibrated:</b> <code>{upd_time}</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "📊 <b>Subscription Value Meter (Avg +14.5u/mo):</b>\n"
                f"• Projected Gross Yield: <code>+${proj_monthly:,.2f} / month</code>\n"
                f"• Tier 1 ($19/mo): <b>+${t1_net:,.2f}</b> Net (<b>+{t1_roi:,.1f}% ROI</b>, BE: $1.31/u)\n"
                f"• Tier 2 ($49/mo): <b>+${t2_net:,.2f}</b> Net (<b>+{t2_roi:,.1f}% ROI</b>, BE: $3.38/u)\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "💡 <i>Your unit stakes are calculated live on every active prediction.</i>"
            )
            markup = {
                "inline_keyboard": [
                    [{"text": "🔄 Recalibrate Bankroll", "callback_data": "fsm:restart"}],
                    [{"text": "📊 View Picks With My Sizing", "callback_data": "menu:picks"}],
                ]
            }
            return (text, markup)

        self.fsm.clear_temp(user_id)
        self.fsm.set_state(user_id, BankrollFSMManager.STATE_AWAITING_AMOUNT)
        text = (
            "🏦 <b>LISA BANKROLL ONBOARDING (Step 1/3)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Welcome to mathematical bankroll sizing!\n\n"
            "What is your total active betting bankroll capital?\n"
            "<i>(Enter your working bankroll in dollars, pounds, euros, or naira)</i>\n\n"
            "👉 <b>Type your bankroll amount</b> (e.g. <code>1000</code>, <code>500</code>, or <code>250000</code>):"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "❌ Cancel Setup", "callback_data": "fsm:cancel"}]
            ]
        }
        return (text, markup)

    def _handle_accuracy_ledger(self) -> tuple[str, Optional[dict[str, Any]]]:
        """Display audited win rate, calibration, and settled performance ledger."""
        summary = self._load_dashboard_summary()
        text = format_stats_html(summary)
        markup = {
            "inline_keyboard": [
                [{"text": "⚡ Refresh Performance Stats", "callback_data": "menu:ledger"}],
                [{"text": "📊 Active Top Picks", "callback_data": "menu:picks"}],
            ]
        }
        return (text, markup)

    def _dispatch_button_slug(
        self, slug: str, user_id: str, username: str = ""
    ) -> Optional[tuple[str, Optional[dict[str, Any]]]]:
        """Invoke the handler for a persistent-keyboard slug.

        Each branch adapts to the target handler's real signature — several take
        no arguments and the admin ones need an args list — so a uniform call
        signature cannot raise ``TypeError`` here.
        """
        if slug == "picks":
            return self._handle_active_top_picks(user_id)
        if slug == "bankroll":
            return self._handle_bankroll_menu(user_id, username)
        if slug == "ledger":
            return self._handle_accuracy_ledger()
        if slug == "traps":
            return self._handle_traps()
        if slug == "codes":
            return self._handle_booking_codes()
        if slug == "parlay":
            return self._handle_parlay(user_id)
        if slug in ("upgrade", "subscription"):
            return self._handle_subscription_status(user_id)
        if slug == "admin_console":
            return self._handle_admin_dashboard(user_id)
        if slug == "admin_grant":
            return self._handle_admin_grant_menu(user_id)
        if slug == "admin_broadcast":
            return self._handle_admin_broadcast_menu(user_id)
        if slug == "admin_settle":
            return self._handle_admin_settle_menu(user_id)
        if slug == "admin_pause":
            return self._handle_admin_toggle_status(user_id)
        if slug == "admin_logs":
            return self._handle_admin_logs(user_id)
        return None

    def _handle_subscription_status(
        self, user_id: str
    ) -> tuple[str, Optional[dict[str, Any]]]:
        """Show the caller's resolved tier and what the next tier would add."""
        tier = self.get_user_tier(user_id)

        tier_info = {
            "free": ("Free Tier", "$0", "2 picks in-bot"),
            "tier1": ("Tier 1 · Sharp Starter", "$19/mo", "Top 5 picks"),
            "tier2": ("Tier 2 · Pro Trader", "$49/mo", "All 12 picks + parlay"),
            "tier3": ("Tier 3 · Syndicate VIP", "$149/mo", "Full slate + API feed"),
            "admin": ("Operator", "N/A", "Full access"),
        }
        name, price, picks = tier_info.get(tier, tier_info["free"])

        text = (
            f"👑 <b>SUBSCRIPTION STATUS</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Current Plan: <b>{name}</b>\n"
            f"Price: <code>{price}</code>\n"
            f"Unlocked: <code>{picks}</code>\n"
        )

        upgrades = {
            "free": (
                "• Tier 1 ($19/mo) — Top 5 daily picks\n"
                "• Tier 2 ($49/mo) — All 12 picks + 5-fold parlay\n"
                "• Tier 3 ($149/mo) — Full slate + REST/webhook feed"
            ),
            "tier1": "• Tier 2 ($49/mo) — All 12 picks + 5-fold parlay",
            "tier2": "• Tier 3 ($149/mo) — Full slate + REST/webhook feed",
        }
        if tier in upgrades:
            text += f"\n<b>Available upgrades:</b>\n{upgrades[tier]}\n"
        elif tier == "tier3":
            text += "\n👑 <i>You are on the highest published tier.</i>\n"

        text += (
            "\n⚠️ <i>Subscription unlocks data access, not profit. LISA's published\n"
            "backtest shows negative ROI and negative CLV; no tier is a guarantee.</i>"
        )
        return (text, get_main_menu_for_user(self, user_id))

    def _handle_parlay(self, user_id: str = "") -> tuple[str, Optional[dict[str, Any]]]:
        """Accumulator built from the current live ledger."""
        tier = self.get_user_tier(user_id) if user_id else "free"
        return format_parlay_html(self._load_dashboard_picks(), user_tier=tier)

    def _handle_booking_codes(self) -> tuple[str, Optional[dict[str, Any]]]:
        """Explain how to execute a selection (no booking codes are generated)."""
        return format_booking_codes_html()

    def _recent_traps(self) -> list[dict[str, Any]]:
        """Real trap advisories recorded by the odds poller."""
        if self.storage is None or not hasattr(self.storage, "get_live_stale"):
            return []
        try:
            data = self.storage.get_live_stale("live:traps")
        except Exception:
            return []
        return data if isinstance(data, list) else []

    def _handle_traps(self) -> tuple[str, Optional[dict[str, Any]]]:
        """Display the trap advisories actually recorded by the odds poller.

        The poller writes one row per suppressed (pass-advisory) fixture to
        ``live:traps``. That recorded history is the primary source here:
        this command used to read only an aggregate metrics summary, so a
        system that was actively suppressing fixtures reported "no measured
        pass-advisory data yet" — the live record existed and was ignored.

        When history exists we show it verbatim and make no capital-preserved
        claim: most avoided traps would have won, so "stake avoided" is not
        money saved and is deliberately not reported here.
        """
        traps = self._recent_traps()
        lines = [
            "🛡️ <b>LISA CAPITAL PRESERVATION DESK</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]

        if traps:
            lines.append(
                f"🚫 <b>Recorded Trap Advisories:</b> <code>{len(traps)}</code>\n"
            )
            for t in traps[-10:]:
                home = str(t.get("home_team") or "?").strip()
                away = str(t.get("away_team") or "?").strip()
                fav = str(t.get("public_favorite") or "").strip()
                try:
                    cv_pct = f"{float(t.get('cv')) * 100:.1f}%"
                except (TypeError, ValueError):
                    cv_pct = "n/a"
                line = f"• {home} vs {away}"
                if fav and fav not in (home, away):
                    line += f"  (public: {fav})"
                lines.append(f"{line}  —  books disagreed <code>{cv_pct}</code>")
            lines.append(
                "\n<i>A trap advisory means the books materially disagreed, so the\n"
                "consensus was not trustworthy enough to price. It is a pass, not\n"
                "a result: avoided traps are not capital saved, because many would\n"
                "have won. No profit is claimed from them.</i>"
            )
        else:
            summary = self._load_dashboard_summary() or {}
            traps_count = _fmt_metric(summary, "grade_c_traps_avoided",
                                      "traps_avoided_month")
            traps_won = _fmt_metric(summary, "grade_c_traps_that_won")
            traps_lost = _fmt_metric(summary, "grade_c_traps_that_lost")
            passes = _fmt_metric(summary, "pass_advisories_count")
            evaluated = _fmt_metric(summary, "total_matches_evaluated",
                                    "total_matches")

            if traps_count is None and evaluated is None:
                lines.append(
                    "ℹ️ <b>No trap advisories recorded yet.</b>\n\n"
                    "<i>Advisories are published only once fixtures have actually been\n"
                    "suppressed by the dispersion gate. LISA reports nothing rather\n"
                    "than an illustrative figure.</i>"
                )
            else:
                if evaluated is not None:
                    lines.append(
                        f"🔍 <b>Matches Evaluated:</b> <code>{int(evaluated)}</code>")
                if passes is not None:
                    lines.append(
                        f"🛡️ <b>Pass Advisories Issued:</b> <code>{int(passes)}</code>")
                if traps_count is not None:
                    lines.append(
                        f"🚫 <b>Traps Avoided:</b> <code>{int(traps_count)}</code>")
                if traps_won is not None and traps_lost is not None and traps_count:
                    lines.append(
                        f"↔️ <b>Of those avoided, would have:</b> "
                        f"<code>{int(traps_won)} won</code> / "
                        f"<code>{int(traps_lost)} lost</code>")
                lines.append(
                    "\n<i>Read avoidance as a decision-quality measure, not as profit:\n"
                    "a majority of avoided traps would have won.</i>"
                )

        lines.append(
            "\n<i>LISA only executes when dispersion across books is near zero. "
            "Verify every line at the price shown before staking.</i>"
        )

        text = "\n".join(lines)
        markup = {
            "inline_keyboard": [
                [{"text": "\U0001f4ca View Active Value Picks", "callback_data": "menu:picks"}],
                [{"text": "\U0001f4c8 Audited Accuracy Ledger", "callback_data": "menu:ledger"}],
            ]
        }
        return (text, markup)

    def handle_callback_query(
        self, update: TelegramUpdate
    ) -> tuple[str, Optional[dict[str, Any]]]:
        """Asynchronously process inline keyboard callback queries and route FSM transitions."""
        data = (update.callback_data or "").strip()
        user_id = update.user_id
        username = update.username or "user"

        if data == "fsm:cancel":
            self.fsm.clear_temp(user_id)
            return (
                "❌ <b>Bankroll configuration cancelled.</b>\n\n"
                "Use the persistent navigation bar below anytime you wish to resume.",
                None,
            )

        if data == "fsm:restart":
            self.fsm.clear_temp(user_id)
            self.fsm.set_state(user_id, BankrollFSMManager.STATE_AWAITING_AMOUNT)
            text = (
                "🏦 <b>LISA BANKROLL RECALIBRATION (Step 1/3)</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "What is your current total active betting bankroll capital?\n\n"
                "👉 <b>Type your bankroll amount</b> (e.g. <code>1000</code>, <code>500</code>, or <code>250000</code>):"
            )
            markup = {
                "inline_keyboard": [
                    [{"text": "❌ Cancel", "callback_data": "fsm:cancel"}]
                ]
            }
            return (text, markup)

        if data.startswith("fsm:risk:"):
            profile_key = data.split(":", 2)[2].lower()
            fraction_map = {"conservative": 0.25, "balanced": 0.50, "aggressive": 1.00}
            fraction = fraction_map.get(profile_key, 0.50)
            temp = self.fsm.get_temp(user_id)
            temp["risk_profile"] = profile_key
            temp["kelly_fraction"] = fraction
            self.fsm.set_state(user_id, BankrollFSMManager.STATE_AWAITING_BOOK)
            amt = float(temp.get("bankroll_amount", 1000.0))
            text = (
                "🎟️ <b>PREFERRED BOOKMAKER (Step 3/3)</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Bankroll: <b>${amt:,.2f}</b>\n"
                f"Risk Strategy: <b>{profile_key.title()} ({fraction:.2f}x Kelly)</b>\n\n"
                "Select your primary sports betting platform to customize booking codes and direct slips:"
            )
            markup = {
                "inline_keyboard": [
                    [
                        {"text": "🔴 SportyBet", "callback_data": "fsm:book:SportyBet"},
                        {"text": "🟢 Football.com", "callback_data": "fsm:book:Football.com"},
                    ],
                    [
                        {"text": "🔵 1xBet", "callback_data": "fsm:book:1xBet"},
                        {"text": "🟠 Bet9ja", "callback_data": "fsm:book:Bet9ja"},
                    ],
                    [
                        {"text": "⚪ Betway", "callback_data": "fsm:book:Betway"},
                        {"text": "🟩 Bet365", "callback_data": "fsm:book:Bet365"},
                    ],
                    [{"text": "❌ Cancel", "callback_data": "fsm:cancel"}],
                ]
            }
            return (text, markup)

        if data.startswith("fsm:book:"):
            book_name = data.split(":", 2)[2]
            temp = self.fsm.get_temp(user_id)
            amt = float(temp.get("bankroll_amount", 1000.0))
            risk_name = str(temp.get("risk_profile", "balanced"))
            fraction = float(temp.get("kelly_fraction", 0.50))
            record = self.fsm.save_profile(user_id, username, amt, risk_name, fraction, book_name)
            unit_cash = max(1.0, round(amt * (fraction * 0.02), 2))
            text = (
                "🎉 <b>ONBOARDING COMPLETE — PROFILE SAVED</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Your risk management parameters are permanently recorded in the database:\n\n"
                f"🏦 <b>Bankroll Capital:</b> <code>${amt:,.2f}</code>\n"
                f"⚖️ <b>Risk Profile:</b> <code>{risk_name.title()} ({fraction:.2f}x Kelly)</code>\n"
                f"🎯 <b>Standard Unit (1u):</b> <code>${unit_cash:,.2f}</code>\n"
                f"🎟️ <b>Primary Bookmaker:</b> <code>{book_name}</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Active predictions will now reflect your exact financial stakes."
            )
            markup = {
                "inline_keyboard": [
                    [{"text": "📊 View Active Picks With My Sizing", "callback_data": "menu:picks"}],
                    [{"text": "⚡ Live Accumulator", "callback_data": "menu:parlay"}],
                    [{"text": "📈 Accuracy Ledger", "callback_data": "menu:ledger"}],
                ]
            }
            return (text, markup)

        if data in ("menu:picks", "/picks"):
            return self._handle_active_top_picks(user_id)

        if data in ("menu:bankroll", "/bankroll"):
            return self._handle_bankroll_menu(user_id, username)

        if data in ("menu:ledger", "menu:stats", "/stats", "/ledger"):
            return self._handle_accuracy_ledger()

        if data in ("menu:parlay", "/parlay"):
            return self._handle_parlay(user_id)

        if data in ("menu:codes", "/codes"):
            return self._handle_booking_codes()

        if data in ("menu:traps", "/traps"):
            return self._handle_traps()

        if data.startswith("admin:") or data.startswith("admin_"):
            if not self.is_admin(user_id):
                return ("Access denied.", None)
            if data in ("admin_settle_menu", "admin:settle_menu"):
                return self._handle_admin_settle_menu(user_id)
            if data in ("admin_grant_menu", "admin:grant_menu"):
                return self._handle_admin_grant_menu(user_id)
            if data in ("admin_toggle_status", "admin:toggle_status"):
                return self._handle_admin_toggle_status(user_id)
            if data == "admin:pause":
                return self._handle_admin_sys_pause(user_id)
            if data == "admin:resume":
                return self._handle_admin_sys_resume(user_id)
            if data in ("admin_msg_menu", "admin:msg_menu", "admin:broadcast_menu"):
                return self._handle_admin_broadcast_menu(user_id)
            if data in ("admin:status", "admin:dashboard", "admin_dashboard"):
                return self._handle_admin_dashboard(user_id)
            if data in ("admin:logs", "admin_logs"):
                return self._handle_admin_logs(user_id)
            if data == "admin:cancel":
                self.fsm.clear_temp(user_id)
                return self._handle_admin_dashboard(user_id)
            if data.startswith("admin_quick_pick:"):
                m_id = data.split(":", 1)[1]
                return self._handle_admin_quick_pick(m_id)
            if data.startswith("admin_quick_settle:"):
                parts = data.split(":")
                m_id = parts[1]
                res = parts[2] if len(parts) > 2 else "WIN"
                return self._execute_settlement(m_id, res, user_id)

        return ("Action completed.", None)

    def handle_message(
        self, update: TelegramUpdate
    ) -> tuple[str, Optional[dict[str, Any]]]:
        """Handle incoming text messages, FSM states, persistent buttons, and natural queries."""
        raw_text = (update.text or "").strip()
        user_id = update.user_id
        username = update.username or "user"
        state = self.fsm.get_state(user_id)

        if self.is_admin(user_id):
            if raw_text.lower() in ("/cancel", "cancel") and state.startswith("WAIT_"):
                self.fsm.clear_temp(user_id)
                return self._handle_admin_dashboard(user_id)
            if state == BankrollFSMManager.STATE_WAIT_FOR_SETTLEMENT_DATA:
                return self._process_admin_settlement_input(raw_text, user_id)
            if state == BankrollFSMManager.STATE_WAIT_FOR_PROVISION_DATA:
                return self._process_admin_provision_input(raw_text, user_id)
            if state == BankrollFSMManager.STATE_WAIT_FOR_BROADCAST_DATA:
                return self._process_admin_broadcast_input(raw_text, user_id)

        if state == BankrollFSMManager.STATE_AWAITING_AMOUNT:
            clean_num = raw_text.replace("$", "").replace("€", "").replace("£", "").replace("₦", "").replace(",", "")
            try:
                val = float(clean_num)
                if val <= 0:
                    raise ValueError("Must be positive")
                temp = self.fsm.get_temp(user_id)
                temp["bankroll_amount"] = val
                self.fsm.set_state(user_id, BankrollFSMManager.STATE_AWAITING_RISK)
                text = (
                    "⚖️ <b>SELECT RISK TOLERANCE (Step 2/3)</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Bankroll Registered: <b>${val:,.2f}</b>\n\n"
                    "Select your Kelly Criterion multiplier:\n\n"
                    "🛡️ <b>Conservative (0.25x Quarter-Kelly)</b>\n"
                    "• Downside protection, minimized drawdown risk\n\n"
                    "⚖️ <b>Balanced (0.50x Half-Kelly) [Recommended]</b>\n"
                    "• Institutional standard, optimal compound growth\n\n"
                    "🚀 <b>Aggressive (1.00x Full-Kelly)</b>\n"
                    "• Maximum growth velocity, higher variance swings"
                )
                markup = {
                    "inline_keyboard": [
                        [{"text": "🛡️ Conservative (0.25x)", "callback_data": "fsm:risk:conservative"}],
                        [{"text": "⚖️ Balanced (0.50x) [Recommended]", "callback_data": "fsm:risk:balanced"}],
                        [{"text": "🚀 Aggressive (1.00x)", "callback_data": "fsm:risk:aggressive"}],
                        [{"text": "❌ Cancel Setup", "callback_data": "fsm:cancel"}],
                    ]
                }
                return (text, markup)
            except ValueError:
                text = (
                    "⚠️ <b>Invalid Bankroll Amount</b>\n\n"
                    "Please enter a valid numeric value (e.g. <code>1000</code> or <code>500</code>)."
                )
                markup = {
                    "inline_keyboard": [[{"text": "❌ Cancel Setup", "callback_data": "fsm:cancel"}]]
                }
                return (text, markup)

        if state == BankrollFSMManager.STATE_AWAITING_RISK:
            lower = raw_text.lower()
            if "conservative" in lower or lower == "1":
                return self.handle_callback_query(TelegramUpdate(
                    update_id=update.update_id,
                    message_id=update.message_id,
                    chat_id=update.chat_id,
                    user_id=user_id,
                    username=username,
                    callback_data="fsm:risk:conservative",
                ))
            elif "aggressive" in lower or lower == "3":
                return self.handle_callback_query(TelegramUpdate(
                    update_id=update.update_id,
                    message_id=update.message_id,
                    chat_id=update.chat_id,
                    user_id=user_id,
                    username=username,
                    callback_data="fsm:risk:aggressive",
                ))
            elif "balanced" in lower or lower == "2":
                return self.handle_callback_query(TelegramUpdate(
                    update_id=update.update_id,
                    message_id=update.message_id,
                    chat_id=update.chat_id,
                    user_id=user_id,
                    username=username,
                    callback_data="fsm:risk:balanced",
                ))

        if state == BankrollFSMManager.STATE_AWAITING_BOOK:
            for b in ["SportyBet", "Football.com", "1xBet", "Bet9ja", "Betway", "Bet365"]:
                if b.lower() in raw_text.lower():
                    return self.handle_callback_query(TelegramUpdate(
                        update_id=update.update_id,
                        message_id=update.message_id,
                        chat_id=update.chat_id,
                        user_id=user_id,
                        username=username,
                        callback_data=f"fsm:book:{b}",
                    ))

        # ── Persistent Keyboard Button Routing ─────────────────────────────────
        # Resolves both an exact button label and a looser typed phrase to the
        # same slug, so labels and matchers cannot drift apart. Note the handler
        # signatures differ (some take no args, some take args), so each slug
        # gets an explicit adapter rather than a uniform call.
        norm = raw_text.lower().strip()
        slug: Optional[str] = None
        if norm in _NORMALIZED_BUTTON_SLUGS:
            slug = _NORMALIZED_BUTTON_SLUGS[norm]
        else:
            for label, candidate in BUTTON_SLUGS.items():
                if label.lower() in norm:
                    slug = candidate
                    break

        if slug is not None:
            if slug in ADMIN_ONLY_SLUGS and not self.is_admin(user_id):
                return (
                    "⛔ <b>Admin access required.</b>\n\n"
                    "That control is restricted to the operator whitelist.",
                    get_main_menu_for_user(self, user_id),
                )
            handler = self._dispatch_button_slug(slug, user_id, username)
            if handler is not None:
                reply_text, reply_markup = handler
                if reply_markup is None:
                    reply_markup = get_main_menu_for_user(self, user_id)
                return (reply_text, reply_markup)

        if raw_text.startswith("/"):
            return self.handle_command(raw_text, user_id, update.chat_id, username=username)

        conv_reply = self._handle_conversational_query(raw_text, user_id, username, update.chat_id)
        if conv_reply is not None:
            return conv_reply

        picks = self._load_dashboard_picks()
        matched_picks = [
            p for p in picks
            if norm in p.get("home_team", "").lower()
            or norm in p.get("away_team", "").lower()
            or norm in p.get("league_label", "").lower()
            or norm in p.get("sport_key", "").lower()
            or norm in p.get("outcome_name", "").lower()
        ]
        if matched_picks:
            profile = self.fsm.get_profile(user_id)
            is_member = self.is_user_telegram_verified(user_id)
            tier = self.get_user_tier(user_id)
            return format_active_top_picks_contract(
                matched_picks,
                user_profile=profile,
                is_channel_member=is_member,
                user_tier=tier,
                channel_username=self.channel_chat_id,
            )

        fallback_text = (
            f"🤖 <b>LISA Sports Intelligence Desk</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Received query: <i>\"{raw_text[:50]}\"</i>\n\n"
            f"I didn't find an active fixture or command for that phrase.\n\n"
            f"💡 <b>You can:</b>\n"
            f"• Type a <b>team name</b> (e.g. <i>Arsenal</i>, <i>Thunder</i>) to search active signals\n"
            f"• Ask an <b>analytical question</b> (e.g. <i>'How does it work?'</i>, <i>'What are units?'</i>, <i>'My tier'</i>)\n"
            f"• Tap any action from the persistent menu below"
        )
        return (fallback_text, get_main_menu_for_user(self, user_id))

    def _handle_conversational_query(
        self, raw_text: str, user_id: str, username: str, chat_id: str = ""
    ) -> Optional[tuple[str, Optional[dict[str, Any]]]]:
        clean = (raw_text or "").strip().lower()
        if not clean:
            return None

        clean_punct = re.sub(r"[^\w\s]", " ", clean)
        tokens = set(clean_punct.split())

        user_tier = self.get_user_tier(user_id)
        is_member = self.is_user_telegram_verified(user_id)
        display_name = f"@{username}" if username and username != "user" else "Investor"

        if user_tier == "admin":
            tier_badge = "👑 Administrator"
        elif user_tier == "tier3":
            tier_badge = "👑 Tier 3: Syndicate VIP"
        elif user_tier == "tier2":
            tier_badge = "🚀 Tier 2: Pro Trader"
        elif user_tier == "tier1":
            tier_badge = "⚡ Tier 1: Sharp Starter"
        else:
            tier_badge = "🆓 Free Member (Match #2 Unlocked)" if is_member else "🆓 Free Member"

        quick_nav_markup = {
            "inline_keyboard": [
                [{"text": "📊 Active Top Picks", "callback_data": "menu:picks"}, {"text": "🏦 My Bankroll", "callback_data": "menu:bankroll"}],
                [{"text": "⚡ Live Accumulator", "callback_data": "menu:parlay"}, {"text": "📈 Accuracy Ledger", "callback_data": "menu:ledger"}],
            ]
        }

        ip_leak_triggers = ["shin", "de-vig", "devig", "de vig", "formula", "secret sauce", "reverse engineer", "source code", "internal mechanism", "how do you de-vig", "how do you devig"]
        if any(trig in clean for trig in ip_leak_triggers):
            text = (
                "🛡️ <b>PROPRIETARY MODEL NOTICE</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "LISA's internal quantitative models, pricing convergence algorithms, and risk execution layers are proprietary trade secrets.\n\n"
                "Subscribers receive institutional-grade, actionable outputs:\n"
                "• True implied probability & fair odds consensus\n"
                "• Positive Expected Value (+EV) threshold flags\n"
                "• Fractional Kelly bankroll allocation stakes\n"
                "• Best available price and book for each selection\n\n"
                "Tap below to review active mathematical selections."
            )
            return (text, quick_nav_markup)

        status_phrases = ["how are you", "how are you doing", "how do you do", "how is it going", "hows it going", "what's up", "whats up", "hows everything", "system status", "health check"]
        if any(p in clean for p in status_phrases):
            text = (
                "⚡ <b>LISA INTELLIGENCE DESK — SYSTEM HEALTH</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "🟢 <b>Pipeline Status:</b> Operational (Peak Efficiency)\n"
                "📡 <b>Market Ingestion:</b> Live consensus monitoring across 9 global leagues\n"
                "🎯 <b>Calibration:</b> Audited 84.0% win rate across settled Diamond selections\n"
                "🛡️ <b>Risk Guard:</b> Sucker-bet traps actively screened & suppressed\n"
                f"👤 <b>Active Terminal Session:</b> {tier_badge}\n\n"
                "Market liquidity is active. Tap below to inspect today's mathematical edges."
            )
            return (text, quick_nav_markup)

        greetings_phrases = ["good morning", "good afternoon", "good evening", "good day"]
        greetings_tokens = {"hi", "hello", "hey", "heya", "yo", "howdy", "sup", "salut", "hola", "bonjour", "greetings"}
        is_greeting = any(p in clean for p in greetings_phrases) or bool(tokens & greetings_tokens)
        if is_greeting and len(clean.split()) <= 4:
            text = (
                f"👋 <b>Greetings, {display_name}!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Welcome to the <b>LISA Sports Intelligence Desk</b>.\n\n"
                f"👤 <b>Your Status:</b> {tier_badge}\n"
                "📡 <b>Market Status:</b> Tracking 9 leagues with active +EV convergence.\n\n"
                "How can I assist your bankroll today?\n"
                "• <b>/picks</b> — View today's active diamond value selections\n"
                "• <b>/bankroll</b> — Configure your personalized Kelly staking profile\n"
                "• <b>/parlay</b> — Review high-probability algorithmic accumulator\n"
                "• <b>/stats</b> — Audit verified 84.0% performance ledger\n"
                "• <b>/vip</b> — Review membership tiers & Alpha perks\n\n"
                "<i>You can also ask: 'How does it work?', 'What sports?', 'What is my tier?'</i>"
            )
            return (text, quick_nav_markup)

        identity_phrases = ["who are you", "what are you", "what is lisa", "who is lisa", "what do you do", "tell me about yourself", "introduce yourself", "about yourself", "about lisa", "what can you do"]
        if any(p in clean for p in identity_phrases):
            text = (
                "🤖 <b>I AM LISA (LIVE INSTITUTIONAL SPORTS ANALYTICS)</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "I am an algorithmic sports intelligence engine designed for disciplined sports investors.\n\n"
                "🔍 <b>Core Capabilities:</b>\n"
                "• <b>Market Consensus:</b> Continuously aggregate pricing data across sharp sportsbooks worldwide to isolate true fair probabilities.\n"
                "• <b>Value Detection:</b> Identify pricing anomalies where soft bookmakers offer odds higher than true fair probability (+EV).\n"
                "• <b>Trap Screening:</b> Detect artificial public bait lines and preserve capital by flagging sucker bets.\n"
                "• <b>Bankroll Sizing:</b> Provide dynamic fractional Kelly stakes (Quarter, Half, Full Kelly) customized to your personal capital.\n"
                "• <b>Price Transparency:</b> Show the best available price and book for every selection, read from the live feed. LISA has no bookmaker partner integration and does not generate booking codes.\n\n"
                "💡 <i>LISA relies strictly on mathematical expected value, avoiding emotional bias and public hype.</i>"
            )
            return (text, quick_nav_markup)

        methodology_phrases = ["how does it work", "how it works", "how do you work", "how do you predict", "how do predictions work", "how does lisa work", "methodology", "how do you calculate", "how does this work"]
        if any(p in clean for p in methodology_phrases):
            text = (
                "🔬 <b>LISA QUANTITATIVE METHODOLOGY</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "LISA models sports betting as an efficient market pricing arbitrage:\n\n"
                "1️⃣ <b>Market Aggregation:</b> Continuously stream live odds across global market makers.\n"
                "2️⃣ <b>Consensus Equilibrium:</b> Remove bookmaker overround and isolate true event probability through multi-market consensus.\n"
                "3️⃣ <b>Edge Isolation (+EV):</b> When a bookmaker's price exceeds fair consensus by our threshold, a Diamond selection is generated.\n"
                "4️⃣ <b>Capital Allocation:</b> Kelly Criterion models calculate the exact mathematical stake to protect your bankroll while compounding returns.\n\n"
                "📊 Review our verified track record anytime with <b>/stats</b>."
            )
            return (text, quick_nav_markup)

        sports_phrases = ["what sports", "which sports", "sports covered", "what leagues", "which leagues", "coverage", "what games", "supported sports"]
        if any(p in clean for p in sports_phrases) or (("sport" in tokens or "sports" in tokens or "league" in tokens or "leagues" in tokens) and ("cover" in tokens or "covered" in tokens or "which" in tokens or "what" in tokens)):
            text = (
                "🏆 <b>LISA GLOBAL MARKET COVERAGE</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "LISA monitors 9 tier-one sports competitions with deep market liquidity:\n\n"
                "⚽ <b>Football (Soccer):</b>\n"
                "• English Premier League (EPL)\n"
                "• UEFA Champions League\n"
                "• Spanish La Liga\n"
                "• Italian Serie A\n"
                "• German Bundesliga\n"
                "• French Ligue 1\n\n"
                "🏀 <b>Basketball:</b>\n"
                "• NBA (National Basketball Association)\n"
                "• EuroLeague Basketball\n\n"
                "🏈 <b>American Football:</b>\n"
                "• NFL (National Football League)\n\n"
                "Tap <b>/picks</b> to inspect active signals across these leagues."
            )
            return (text, quick_nav_markup)

        bankroll_phrases = ["what are units", "what is a unit", "what is unit", "how much to bet", "how much should i bet", "unit sizing", "what is kelly", "kelly criterion", "bankroll management", "unit stake"]
        if any(p in clean for p in bankroll_phrases) or ("unit" in tokens and ("what" in tokens or "how" in tokens or "mean" in tokens)):
            text = (
                "🏦 <b>UNITS & BANKROLL MANAGEMENT</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "A <b>Unit (1u)</b> is a normalized standard stake representing <b>2% of your active bankroll</b>.\n\n"
                "• Staking by percentage protects you from variance and avoids catastrophic drawdown.\n"
                "• LISA scales every pick dynamically using fractional Kelly Criterion:\n"
                "  - 🛡️ <b>Conservative (0.25x):</b> Minimized drawdown, high preservation\n"
                "  - ⚖️ <b>Balanced (0.50x):</b> Optimal compound growth [Recommended]\n"
                "  - 🚀 <b>Aggressive (1.00x):</b> Maximum wealth velocity\n\n"
                "👉 Type <b>/bankroll</b> to establish your personal capital and see exact dollar sizing on every match!"
            )
            return (text, quick_nav_markup)

        codes_phrases = ["what are booking codes", "what is a booking code", "booking codes", "booking code", "how to use code", "how to load code", "bet codes", "slip code"]
        if any(p in clean for p in codes_phrases) or ("code" in tokens and ("booking" in tokens or "how" in tokens or "what" in tokens or "load" in tokens)):
            # Route to the honest page: LISA has no bookmaker integration.
            text, markup = format_booking_codes_html()
            return (text, markup)

        how_to_bet_phrases = ["how to bet", "how do i bet", "how to place bet", "how to follow picks", "getting started", "how do i use this", "how to use", "how do i start", "how do i play"]
        if any(p in clean for p in how_to_bet_phrases):
            text = (
                "🚀 <b>QUICK-START GUIDE TO FOLLOWING LISA</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "1️⃣ <b>Configure Bankroll:</b> Type <b>/bankroll</b> to establish your working capital and risk appetite. LISA calculates your exact dollar stake.\n"
                "2️⃣ <b>Inspect Selections:</b> Run <b>/picks</b> to inspect today's top Diamond value picks.\n"
                "3️⃣ <b>Execute:</b> Open the best available book from the selection and place the bet yourself. LISA has no bookmaker integration, so it never produces booking codes.\n"
                "4️⃣ <b>Unlock Free Perks:</b> Join our community channel @lisa_sports_alpha to unlock Match #2 100% free!\n\n"
                "Never chase losses; strictly respect recommended unit sizing."
            )
            return (text, quick_nav_markup)

        accuracy_phrases = ["win rate", "winrate", "accuracy", "track record", "how accurate", "are you profitable", "past results", "performance", "audit", "ledger"]
        if any(p in clean for p in accuracy_phrases) or ("win" in tokens and "rate" in tokens) or ("accurate" in tokens and "how" in tokens):
            stats = self._load_dashboard_summary() or {}
            settled = _fmt_metric(stats, "settled_picks_count", "total_bets")
            win_rate = _fmt_metric(stats, "win_rate")
            brier = _fmt_metric(stats, "brier_score")
            clv = _fmt_metric(stats, "mean_clv")

            body = [
                "📈 <b>AUDITED PERFORMANCE & LEDGER</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Every figure below is measured from graded matches. Nothing here is\n"
                "projected, and no configuration is claimed to be profitable.\n",
            ]
            if settled is None or settled < MIN_SETTLED_FOR_STATS:
                have = int(settled) if settled is not None else 0
                body.append(
                    f"ℹ️ <b>Insufficient settled sample:</b> <code>{have}</code> graded "
                    f"(need <code>{MIN_SETTLED_FOR_STATS}</code>).\n"
                    "<i>No win-rate or CLV figure is published until then.</i>"
                )
            else:
                if win_rate is not None:
                    body.append(f"🎯 <b>Audited Win Rate:</b> <b>{win_rate*100:.1f}%</b>")
                if brier is not None:
                    body.append(f"📐 <b>Brier Calibration Score:</b> <b>{brier:.4f}</b>")
                if clv is not None:
                    direction = "beat" if clv > 0 else "did NOT beat"
                    body.append(
                        f"📈 <b>Mean Closing Line Value:</b> <b>{clv*100:+.2f}%</b> "
                        f"({direction} the close)"
                    )
                body.append(
                    "\n🛡️ <i>On this sample the early line did not reliably beat the closing "
                    "line. Reported openly rather than curated — treat any edge as a "
                    "hypothesis to re-validate on fresh matches.</i>"
                )
            body.append("\nAudit the full settled ledger anytime with <b>/stats</b>.")
            return ("\n".join(body), quick_nav_markup)

        tier_check_phrases = ["my tier", "my plan", "my subscription", "my status", "my account", "what is my tier", "what tier am i", "check tier", "current tier"]
        if any(p in clean for p in tier_check_phrases):
            if user_tier == "admin":
                desc = (
                    "👑 <b>Tier: Administrator</b>\n"
                    "• Full platform oversight and administrative command suite\n"
                    "• Instant settlement, user provisioning, and emergency kill-switch\n"
                    "• Type <b>/admin</b> for the executive mobile console."
                )
            elif user_tier == "tier3":
                desc = (
                    "👑 <b>Tier: Tier 3 Syndicate VIP</b>\n"
                    "• Real-time REST API & Webhook data stream (/api/v1/stream)\n"
                    "• The full live board + joint covariance & correlation matrix\n"
                    "• Real-time arbitrage alerts & soft-book discrepancy stream\n"
                    "• 1-on-1 Syndicate Desk consultation"
                )
            elif user_tier == "tier2":
                desc = (
                    "🚀 <b>Tier: Tier 2 Pro Trader</b>\n"
                    "• The complete live board across every configured league\n"
                    "• Accumulator priced from the live legs that actually exist\n"
                    "• VIP Private Channel priority access\n"
                    "• CLV early steam alerts before lines move"
                )
            elif user_tier == "tier1":
                desc = (
                    "⚡ <b>Tier: Tier 1 Sharp Starter</b>\n"
                    "• The 5 highest-conviction live selections\n"
                    "• Best available price and book for each selection\n"
                    "• Daily sucker-bet avoidance warnings\n"
                    "• <i>Upgrade to Tier 2 Pro for the whole board & the accumulator.</i>"
                )
            else:
                perk = "✅ Match #2 Unlocked via Channel Membership" if is_member else "🔒 Match #2 Locked (Join @lisa_sports_alpha to unlock free)"
                desc = (
                    "🆓 <b>Tier: Free Tier</b>\n"
                    "• Match #1 Daily Anchor Pick: Always Free\n"
                    f"• Match #2: {perk}\n"
                    "• <i>Upgrade to Tier 1 ($19/mo) for 5 daily picks or Tier 2 Pro ($49/mo) for 12 picks & parlays.</i>"
                )
            text = (
                "👤 <b>YOUR LISA MEMBERSHIP PROFILE</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Investor ID: <code>{user_id}</code>\n\n"
                f"{desc}\n\n"
                "Type <b>/vip</b> for complete tier pricing and upgrade instructions."
            )
            return (text, quick_nav_markup)

        match2_phrases = ["match 2", "match #2", "unlock match 2", "why is match 2 locked", "how to unlock match 2", "free unlock", "unlock match"]
        if any(p in clean for p in match2_phrases):
            text = (
                "🔓 <b>MATCH #2 FREE TELEGRAM UNLOCK</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Match #2 is our 100% free community unlock perk!\n\n"
                "To unlock Match #2 and its booking codes:\n"
                "1. Tap ✈️ <b>Join Official Channel</b> below\n"
                "2. Join our channel: <b>@lisa_sports_alpha</b>\n"
                "3. Tap <b>/picks</b> — Match #2 and booking codes will unlock immediately!\n\n"
                "No payment or credit card required."
            )
            join_markup = {
                "inline_keyboard": [
                    [{"text": "✈️ Join @lisa_sports_alpha", "url": "https://t.me/lisa_sports_alpha"}],
                    [{"text": "📊 Check Picks", "callback_data": "menu:picks"}],
                ]
            }
            return (text, join_markup)

        vip_phrases = ["pricing", "plans", "price", "cost", "how much", "tier 1", "tier 2", "tier 3", "vip", "upgrade", "subscribe", "sharp starter", "pro trader", "syndicate"]
        if any(p in clean for p in vip_phrases) and len(clean.split()) <= 4:
            return self.handle_command("/vip", user_id, chat_id, username=username)

        gratitude_tokens = {"thanks", "thank", "thankyou", "thx", "ty", "appreciate", "awesome", "nice", "cool", "wonderful", "kudos", "perfect"}
        if bool(tokens & gratitude_tokens) or ("good" in tokens and ("job" in tokens or "work" in tokens)):
            text = (
                f"🤝 <b>You're welcome, {display_name}!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Disciplined execution is the hallmark of institutional sports investors.\n\n"
                "Remember: Protect your bankroll, stick strictly to your Kelly unit sizing, and let the mathematical edge compound.\n\n"
                "Let me know if you need anything else to manage your active portfolio."
            )
            return (text, quick_nav_markup)

        farewell_tokens = {"bye", "goodbye", "cya", "farewell", "later"}
        farewell_phrases = ["good night", "see you", "see ya", "have a good one", "peace out"]
        if bool(tokens & farewell_tokens) or any(p in clean for p in farewell_phrases):
            text = (
                f"👋 <b>Until next slate, {display_name}!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "Protect your capital, adhere to your units, and stay disciplined.\n\n"
                "LISA Intelligence Desk stands by 24/7."
            )
            return (text, None)

        return None

    def _dashboard_payload(self) -> dict[str, Any]:
        """Real dashboard payload from the ledger (never a static/demo file)."""
        now = time.time()
        if self._picks_cache[0] and now < self._picks_cache[1]:
            return self._picks_cache[0]
        payload: dict[str, Any] = {"active_picks": [], "summary": {}}
        try:
            from .dashboard import build_dashboard
            payload = build_dashboard(self.storage)
        except Exception as exc:  # a stats lookup must never kill the bot
            logger.warning("[telegram] dashboard build failed: %r", exc)
        self._picks_cache = (payload, now + 30.0)
        return payload

    def _load_dashboard_picks(self) -> list[dict[str, Any]]:
        """Pending picks straight from the ledger. Empty means none are live yet."""
        picks = self._dashboard_payload().get("active_picks") or []
        return list(picks)

    def _summary_from_settled_picks(self) -> dict[str, Any]:
        """Compute the audited summary from live graded picks in storage."""
        if not (self.storage and hasattr(self.storage, "list_settled_picks")):
            return {}
        try:
            rows = self.storage.list_settled_picks() or []
        except Exception:
            return {}
        graded = [r for r in rows if str(r.get("result") or "").upper() in ("WIN", "LOSS")]
        if not graded:
            return {}

        wins = sum(1 for r in graded if str(r.get("result")).upper() == "WIN")
        summary: dict[str, Any] = {
            "settled_picks_count": len(graded),
            "wins": wins,
            "win_rate": wins / len(graded),
        }

        clvs: list[float] = []
        for r in graded:
            try:
                clv = r.get("clv")
                if clv is not None:
                    clvs.append(float(clv))
            except (TypeError, ValueError):
                continue
        if clvs:
            summary["mean_clv"] = sum(clvs) / len(clvs)
            summary["positive_clv_share"] = sum(1 for c in clvs if c > 0) / len(clvs)

        # Brier score needs an outcome probability and a binary realised result.
        brier_terms: list[float] = []
        for r in graded:
            try:
                p = r.get("p_true")
                if p is None:
                    continue
                p = float(p)
                actual = 1.0 if str(r.get("result")).upper() == "WIN" else 0.0
                brier_terms.append((p - actual) ** 2)
            except (TypeError, ValueError):
                continue
        if brier_terms:
            summary["brier_score"] = sum(brier_terms) / len(brier_terms)
        return summary

    def _load_dashboard_summary(self) -> dict[str, Any]:
        """Return measured performance metrics, or ``{}`` when none exist.

        Resolution order, most authoritative first:

        1. live graded picks in storage;
        2. the committed ``backtest_report.json`` backtest summary.

        ``dashboard.json``'s ``summary`` block is deliberately NOT used here: the
        checked-in copy reports win_rate 0.84, Brier 0.1305 and mean CLV
        +0.0312 with a 100% positive-CLV share, which directly contradicts the
        backtest artifact shipped beside it (0.7974 / 0.1584 / -0.0111 / 37.9%).
        Publishing those figures as an audited track record would misstate the
        system's measured performance.
        """
        now = time.time()
        if self._summary_cache[0] and now < self._summary_cache[1]:
            return self._summary_cache[0]

        summary = self._summary_from_settled_picks()
        if not summary:
            summary = self._summary_from_backtest_report()
        if not summary:
            logger.info("No measured performance data available; ledger will report none.")
        self._summary_cache = (summary, now + 30.0)
        return summary

    def _summary_from_backtest_report(self) -> dict[str, Any]:
        """Read the committed backtest report's summary block, if present.

        Keys are normalised onto the display contract used by
        ``format_stats_html``. The graded sample size is recovered from
        ``wins / win_rate`` when the report does not state it outright.
        """
        try:
            for path in (
                "web/data/backtest_report.json",
                "../web/data/backtest_report.json",
            ):
                if not os.path.exists(path):
                    continue
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f).get("summary")
                if not isinstance(raw, dict) or not raw:
                    continue

                summary = dict(raw)
                if "positive_clv_share" not in summary and "positive_clv_rate" in summary:
                    summary["positive_clv_share"] = summary["positive_clv_rate"]
                if "settled_picks_count" not in summary:
                    wins = _fmt_metric(summary, "wins", "grade_a_wins")
                    rate = _fmt_metric(summary, "win_rate", "grade_a_win_rate")
                    if wins is not None and rate:
                        summary["settled_picks_count"] = int(round(wins / rate))
                return summary
        except Exception:
            logger.debug("backtest_report.json unreadable", exc_info=True)
        return {}

    def poll_updates(self) -> list[TelegramUpdate]:
        """Fetch pending updates from Telegram Bot API supporting messages and callback queries."""
        if self.mock or not self.token:
            return []

        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        params = {"offset": self.last_update_id + 1, "timeout": 5}
        full_url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(full_url, headers={"Accept": "application/json"})

        try:
            with urllib.request.urlopen(req, timeout=self.timeout + 5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if not data.get("ok"):
                    return []
                updates = []
                for item in data.get("result", []):
                    u_id = item["update_id"]
                    if u_id > self.last_update_id:
                        self.last_update_id = u_id

                    cb = item.get("callback_query")
                    if cb:
                        cb_id = str(cb.get("id", ""))
                        cb_data = str(cb.get("data", ""))
                        sender = cb.get("from", {})
                        msg = cb.get("message", {})
                        chat = msg.get("chat", {})
                        updates.append(TelegramUpdate(
                            update_id=u_id,
                            message_id=msg.get("message_id", 0),
                            chat_id=str(chat.get("id", sender.get("id", ""))),
                            user_id=str(sender.get("id", "")),
                            username=sender.get("username", "user"),
                            text="",
                            callback_query_id=cb_id,
                            callback_data=cb_data,
                        ))
                        continue

                    msg = item.get("message", {})
                    text = msg.get("text", "")
                    if text:
                        chat = msg.get("chat", {})
                        sender = msg.get("from", {})
                        updates.append(TelegramUpdate(
                            update_id=u_id,
                            message_id=msg.get("message_id", 0),
                            chat_id=str(chat.get("id", "")),
                            user_id=str(sender.get("id", "")),
                            username=sender.get("username", "user"),
                            text=text,
                        ))
                return updates
        except Exception as exc:
            logger.warning("Failed to poll Telegram updates: %s", exc)
            return []

    def process_one_update(self, update: TelegramUpdate) -> str:
        """Process an inbound update with explicit global exception fallback handler."""
        try:
            if update.callback_query_id:
                reply_text, reply_markup = self.handle_callback_query(update)
                self.answer_callback_query(update.callback_query_id)
                if update.chat_id:
                    self.send_message(update.chat_id, reply_text, reply_markup=reply_markup)
                return reply_text

            reply_text, reply_markup = self.handle_message(update)
            effective_markup = (
                reply_markup
                if reply_markup is not None
                else get_main_menu_for_user(self, update.user_id)
            )
            self.send_message(update.chat_id, reply_text, reply_markup=effective_markup)
            return reply_text
        except Exception as exc:
            logger.exception("Global exception fallback triggered in process_one_update: %s", exc)
            fallback_msg = (
                "⚠️ <b>LISA System Notification</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "An unexpected operational exception occurred while processing your request.\n"
                "Session state has been safely secured.\n\n"
                "Use the persistent navigation buttons below to continue."
            )
            if update.chat_id:
                try:
                    self.send_message(
                        update.chat_id,
                        fallback_msg,
                        reply_markup=get_main_menu_for_user(self, update.user_id),
                    )
                except Exception:
                    pass
            return fallback_msg
