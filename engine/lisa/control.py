"""Process-wide handle the admin console drives.

The production entry point runs the HTTP server, the Telegram bot and the
adaptive odds poller as three threads in one process. The admin console needs to
act on *those* objects — see when the next poll is due, put a key on cooldown,
apply a settings change the scheduler will pick up on its next tick — and it
must not be handed a second, disconnected instance of any of them.

This module holds that handle and nothing else. It deliberately does not start
threads, touch the database, or import the bot: it is a container plus a few
derived views, so it can be constructed in a test with fakes.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Optional

from .runtime import RuntimeConfig


class Control:
    """Shared handles on the live process.

    ``scheduler`` and ``client`` are assigned after construction, because the
    ingestion thread builds them from settings that the console may later
    change. Both are plain attributes guarded by a lock, and both are read on
    every console request.
    """

    def __init__(self, storage: Any, auth: Any = None, *,
                 runtime: Optional[RuntimeConfig] = None,
                 bot: Any = None, started_at: Optional[datetime] = None):
        self.storage = storage
        self.auth = auth
        self.bot = bot
        self.runtime = runtime
        self.started_at = started_at or datetime.now(timezone.utc)
        self.scheduler: Any = None
        self.client: Any = None
        self.server: Any = None
        self._lock = threading.Lock()

    # -- late binding -------------------------------------------------------

    def attach(self, *, scheduler: Any = None, client: Any = None,
               bot: Any = None) -> None:
        with self._lock:
            if scheduler is not None:
                self.scheduler = scheduler
            if client is not None:
                self.client = client
            if bot is not None:
                self.bot = bot

    def bind_server(self, server: Any) -> None:
        with self._lock:
            self.server = server

    # -- derived views ------------------------------------------------------

    def uptime_seconds(self) -> float:
        return max(0.0, (datetime.now(timezone.utc) - self.started_at).total_seconds())

    def describe(self) -> dict[str, Any]:
        """A small identity summary, useful in logs and in the console footer."""
        scheduler = self.scheduler
        return {
            "pid": __import__("os").getpid(),
            "uptime_seconds": round(self.uptime_seconds(), 1),
            "started_at": self.started_at.isoformat(),
            "has_scheduler": scheduler is not None,
            "has_odds_client": self.client is not None,
            "has_runtime_config": self.runtime is not None,
        }
