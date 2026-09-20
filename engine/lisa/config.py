"""Environment-driven configuration with conservative defaults.

Every knob is overridable via ``LISA_*`` env vars so the same binary can run
as a $7/month worker or a production deployment without code changes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw is not None else default


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw is not None else default


def _tuple(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    # -- Stage 1: data source ------------------------------------------------
    odds_api_key: str = ""
    regions: str = "eu,uk,us"
    markets: str = "h2h"
    sports: tuple[str, ...] = (
        "basketball_nba",
        "soccer_spain_la_liga",
        "soccer_germany_bundesliga",
    )

    # -- Stage 2: math -------------------------------------------------------
    sharp_keys: tuple[str, ...] = ("pinnacle", "circa")
    sharp_multiplier: float = 2.0
    margin_weighted: bool = True
    min_margin_floor: float = 0.001
    odds_sanity: tuple[float, float] = (1.01, 1001.0)

    # -- Stage 3: quality gate ------------------------------------------------
    gate_threshold: float = 0.75
    min_books_telemetry: int = 3
    min_books_alert: int = 5
    max_cv: float = 0.10
    ev_min: float = 0.0
    require_positive_ev: bool = False

    # -- freshness ------------------------------------------------------------
    stale_prematch_sec: int = 4 * 3600
    stale_live_sec: int = 300

    # -- Stage 4: storage -----------------------------------------------------
    storage_driver: str = "inmemory"  # inmemory | redis | postgres
    redis_url: str = ""
    database_url: str = ""
    live_ttl_sec: int = 5 * 3600

    # -- settlement -----------------------------------------------------------
    settle_after_hours: float = 3.0
    settle_grace_hours: float = 24.0
    settle_scores_days: int = 3

    # -- notifications --------------------------------------------------------
    telegram_token: str = ""
    telegram_chat_id: str = ""

    # -- alerting policy ------------------------------------------------------
    alert_cooldown_sec: int = 600
    alert_min_delta: float = 0.015


def load_settings() -> Settings:
    return Settings(
        odds_api_key=os.environ.get("LISA_ODDS_API_KEY", ""),
        regions=os.environ.get("LISA_REGIONS", "eu,uk,us"),
        markets=os.environ.get("LISA_MARKETS", "h2h"),
        sports=_tuple("LISA_SPORTS", Settings.sports),
        sharp_keys=_tuple("LISA_SHARP_KEYS", Settings.sharp_keys),
        sharp_multiplier=_float("LISA_SHARP_MULTIPLIER", Settings.sharp_multiplier),
        margin_weighted=_bool("LISA_MARGIN_WEIGHTED", Settings.margin_weighted),
        min_margin_floor=_float("LISA_MIN_MARGIN_FLOOR", Settings.min_margin_floor),
        gate_threshold=_float("LISA_GATE_THRESHOLD", Settings.gate_threshold),
        min_books_telemetry=_int("LISA_MIN_BOOKS_TELEMETRY", Settings.min_books_telemetry),
        min_books_alert=_int("LISA_MIN_BOOKS_ALERT", Settings.min_books_alert),
        max_cv=_float("LISA_MAX_CV", Settings.max_cv),
        ev_min=_float("LISA_EV_MIN", Settings.ev_min),
        require_positive_ev=_bool("LISA_REQUIRE_POSITIVE_EV", Settings.require_positive_ev),
        stale_prematch_sec=_int("LISA_STALE_PREMATCH_SEC", Settings.stale_prematch_sec),
        stale_live_sec=_int("LISA_STALE_LIVE_SEC", Settings.stale_live_sec),
        storage_driver=os.environ.get("LISA_STORAGE", "inmemory"),
        redis_url=os.environ.get("LISA_REDIS_URL", ""),
        database_url=os.environ.get("LISA_DATABASE_URL", ""),
        live_ttl_sec=_int("LISA_LIVE_TTL_SEC", Settings.live_ttl_sec),
        settle_after_hours=_float("LISA_SETTLE_AFTER_HOURS", Settings.settle_after_hours),
        settle_grace_hours=_float("LISA_SETTLE_GRACE_HOURS", Settings.settle_grace_hours),
        settle_scores_days=_int("LISA_SETTLE_SCORES_DAYS", Settings.settle_scores_days),
        telegram_token=os.environ.get("LISA_TELEGRAM_TOKEN", ""),
        telegram_chat_id=os.environ.get("LISA_TELEGRAM_CHAT_ID", ""),
        alert_cooldown_sec=_int("LISA_ALERT_COOLDOWN_SEC", Settings.alert_cooldown_sec),
        alert_min_delta=_float("LISA_ALERT_MIN_DELTA", Settings.alert_min_delta),
    )