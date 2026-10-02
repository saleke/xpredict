"""A rejected Telegram token must not be retried forever.

The production symptom: a revoked bot token made the poll loop log
``Failed to poll Telegram updates: HTTP Error 401`` four times a second, for
the life of the process. That is not merely noisy. It buries every other log
line underneath it, so an unrelated real fault becomes invisible, and it burns
a CPU spinning on a request whose outcome is already decided.

A 401 is a statement about the token, not about the network. Nothing about
waiting changes the answer, so the only correct response is to stop.
"""
from __future__ import annotations

import io
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lisa.telegram_bot import TelegramBot  # noqa: E402


def _bot(token: str = "123:ABC") -> TelegramBot:
    return TelegramBot(token=token, mock=False)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.telegram.org/bot123:ABC/getUpdates",
        code=code,
        msg="err",
        hdrs=None,
        fp=io.BytesIO(b'{"ok": false, "description": "Unauthorized"}'),
    )


def test_a_401_stops_polling(monkeypatch) -> None:
    bot = _bot()

    def _raise(*_a, **_k):
        raise _http_error(401)

    monkeypatch.setattr("lisa.telegram_bot.urllib.request.urlopen", _raise)

    assert bot.poll_updates() == []
    assert bot.auth_failed, "a rejected token must be recorded as permanent"


def test_polling_does_not_retry_after_a_401(monkeypatch) -> None:
    """The second poll must not touch the network at all."""
    bot = _bot()
    calls = []

    def _counting(*a, **k):
        calls.append(1)
        raise _http_error(401)

    monkeypatch.setattr("lisa.telegram_bot.urllib.request.urlopen", _counting)

    bot.poll_updates()
    bot.poll_updates()
    bot.poll_updates()

    assert len(calls) == 1, "a 401 was re-requested after it was known permanent"


def test_the_auth_failure_is_reported_once_not_once_per_poll(monkeypatch, caplog) -> None:
    bot = _bot()

    def _raise(*_a, **_k):
        raise _http_error(401)

    monkeypatch.setattr("lisa.telegram_bot.urllib.request.urlopen", _raise)

    with caplog.at_level("ERROR"):
        for _ in range(25):
            bot.poll_updates()

    errors = [r for r in caplog.records if r.levelname == "ERROR"
              and "token" in r.getMessage()]
    assert len(errors) == 1, f"the same failure was logged {len(errors)} times"


def test_a_transient_failure_does_not_disable_polling(monkeypatch) -> None:
    """429 and 5xx are about timing, not about the token. They must retry."""
    bot = _bot()

    def _raise(*_a, **_k):
        raise _http_error(429)

    monkeypatch.setattr("lisa.telegram_bot.urllib.request.urlopen", _raise)

    bot.poll_updates()
    assert not bot.auth_failed, "a rate limit must not be mistaken for a bad token"

    def _ok(*_a, **_k):
        raise urllib.error.URLError("connection reset")

    monkeypatch.setattr("lisa.telegram_bot.urllib.request.urlopen", _ok)
    bot.poll_updates()
    assert not bot.auth_failed, "a network error must not be mistaken for a bad token"


def test_no_token_is_not_an_auth_failure() -> None:
    """An unconfigured bot is a deliberate off state, not a fault to report."""
    bot = TelegramBot(token="", mock=False)

    assert bot.poll_updates() == []
    assert not bot.auth_failed


def test_a_mock_bot_is_not_an_auth_failure() -> None:
    bot = TelegramBot(token="123:ABC", mock=True)

    assert bot.poll_updates() == []
    assert not bot.auth_failed