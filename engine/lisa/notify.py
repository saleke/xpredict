"""Notification fan-out. LogNotifier is the safe default; TelegramNotifier
posts to a chat/channel via the Bot API. Both are idempotent from the caller's
perspective: the pipeline only calls ``send`` once per newly-emitted pick.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Sequence

from .gate import Pick

logger = logging.getLogger(__name__)


class Notifier:
    def send(self, text: str) -> None:
        raise NotImplementedError


class LogNotifier(Notifier):
    """Prints the alert. Suitable for the $0 tier and local development."""

    def send(self, text: str) -> None:
        print(f"[notify] {text}")


class TelegramNotifier(Notifier):
    def __init__(self, token: str, chat_id: str, timeout: float = 10.0):
        if not token or not chat_id:
            raise ValueError("telegram token and chat_id are required")
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout

    def send(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        body = urllib.parse.urlencode({
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }).encode("utf-8")
        req = urllib.request.Request(url, data=body)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
        except Exception as exc:
            logger.warning("Telegram notification failed: %s", exc)


class DiscordNotifier(Notifier):
    """Posts structured alerts to Discord via incoming webhooks."""

    def __init__(self, webhook_url: str, timeout: float = 10.0):
        if not webhook_url:
            raise ValueError("Discord webhook_url is required")
        self.webhook_url = webhook_url
        self.timeout = timeout

    def send(self, text: str) -> None:
        payload = json.dumps({"content": f"```\n{text}\n```"}).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "LISA-Sports-Refinery/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
        except Exception as exc:
            logger.warning("Discord webhook notification failed: %s", exc)


class CompositeNotifier(Notifier):
    """Safely fans out alerts to multiple notifiers with error isolation."""

    def __init__(self, notifiers: Sequence[Notifier]):
        self.notifiers = tuple(notifiers)

    def send(self, text: str) -> None:
        for n in self.notifiers:
            try:
                n.send(text)
            except Exception as exc:
                logger.warning("Composite notifier sub-dispatch failed: %s", exc)


def pick_alert_text(pick: Pick) -> str:
    exec_part = ""
    if pick.best_execution is not None:
        exec_part = (f" | execute @ {pick.best_execution.book_title} "
                     f"({pick.best_execution.odds:.2f}, EV {pick.best_execution.ev:+.1%})")
    if pick.line is not None:
        if pick.market == "spreads":
            selection = f"{pick.outcome_name} {pick.line:+g}"
        else:
            selection = f"{pick.outcome_name} {pick.line}"
    else:
        selection = pick.outcome_name

    conviction_str = ""
    if getattr(pick, "conviction_score", 0.0) > 0:
        conviction_str = f" [Conviction: {pick.conviction_score:.1f}]"

    stake_part = ""
    if getattr(pick, "recommended_units", 0.0) > 0:
        stake_part = f" | Stake: {pick.recommended_units:.1f}u ({pick.recommended_stake_pct:.1f}%)"

    return (
        f"LISA ALERT{conviction_str}: {pick.home_team} vs {pick.away_team}\n"
        f"Top Pick: {selection}\n"
        f"True probability: {pick.p_true:.1%}  Fair odds: {pick.fair_odds:.2f}"
        f"  (books: {pick.n_books}, cv: {pick.cv:.2%})"
        f"{exec_part}{stake_part}"
    )