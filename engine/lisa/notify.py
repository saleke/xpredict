"""Notification fan-out. LogNotifier is the safe default; TelegramNotifier
posts to a chat/channel via the Bot API. Both are idempotent from the caller's
perspective: the pipeline only calls ``send`` once per newly-emitted pick.
"""
from __future__ import annotations

import urllib.parse
import urllib.request

from .gate import Pick


class Notifier:
    def send(self, text: str) -> None:
        raise NotImplementedError


class LogNotifier(Notifier):
    """Prints the alert. Suitable for the $0 tier and local development."""

    def send(self, text: str) -> None:
        print(f"[notify] {text}")


class TelegramNotifier(Notifier):
    def __init__(self, token: str, chat_id: str):
        if not token or not chat_id:
            raise ValueError("telegram token and chat_id are required")
        self.token = token
        self.chat_id = chat_id

    def send(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        body = urllib.parse.urlencode({
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }).encode("utf-8")
        req = urllib.request.Request(url, data=body)
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()


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
    return (
        f"LISA ALERT: {pick.home_team} vs {pick.away_team}\n"
        f"Top Pick: {selection}\n"
        f"True probability: {pick.p_true:.1%}  Fair odds: {pick.fair_odds:.2f}"
        f"  (books: {pick.n_books}, cv: {pick.cv:.2%})"
        f"{exec_part}"
    )