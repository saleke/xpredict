"""Environment-driven configuration with conservative defaults.

Credentials come from ``THE_ODDS_API_KEY`` / ``THE_ODDS_API_BASE_URL``
(``LISA_*`` aliases accepted for continuity); every other knob is overridable
via ``LISA_*`` env vars so the same binary can run as a small worker or a
production deployment without code changes.
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


DEFAULT_API_BASE_URL = "https://api.the-odds-api.com"

SCOPE_LEAGUES: tuple[str, ...] = (
    "basketball_nba",
    "basketball_euroleague",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
    "soccer_italy_serie_a",
    "soccer_netherlands_eredivisie",
    "soccer_portugal_primeira_liga",
    "soccer_epl",
)
SCOPE_SET: frozenset[str] = frozenset(SCOPE_LEAGUES)


def validate_sports(sports: tuple[str, ...]) -> tuple[str, ...]:
    unknown = [s for s in sports if s not in SCOPE_SET]
    if unknown:
        raise ValueError(
            "unsupported league keys: "
            + ", ".join(sorted(unknown))
            + f"; allowed scope: {list(SCOPE_LEAGUES)}"
        )
    return sports


@dataclass(frozen=True)
class Settings:
    # -- Stage 1: data source ------------------------------------------------
    odds_api_key: str = ""
    api_base_url: str = DEFAULT_API_BASE_URL
    regions: str = "eu,us"
    markets: str = "h2h"
    sports: tuple[str, ...] = SCOPE_LEAGUES

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

    # -- volume controller & waiting room -------------------------------------
    max_alerts_per_cycle: int = 0          # 0 = unlimited
    max_alerts_per_sport_cycle: int = 0    # 0 = unlimited
    conviction_min: float = 0.0            # minimum conviction score required to alert
    slippage_max_cv_drift: float = 0.03    # max allowed CV increase before flagging volatility spike
    discord_webhook_url: str = ""          # Discord incoming webhook URL

    # -- scheduler cadence ----------------------------------------------------
    cadence_prematch_sec: int = 60 * 60     # quiet pre-match poll
    cadence_spike_sec: int = 15 * 60       # kickoff within spike window
    cadence_live_sec: int = 15 * 60        # match(es) in progress
    cadence_idle_sec: int = 6 * 60 * 60    # nothing upcoming in horizon
    cadence_settle_sec: int = 60 * 60      # grading poll
    spike_window_sec: int = 90 * 60        # before kickoff
    live_window_hours: float = 4.0         # post-kickoff "in progress" buffer
    horizon_hours: float = 36.0            # how far ahead cadence cares
    credit_warn: int = 100                 # degrade below this remaining
    credit_stop: int = 20                  # only settlement below this
    metrics_path: str = "data/metrics.jsonl"


def load_settings() -> Settings:
    sports = validate_sports(_tuple("LISA_SPORTS", Settings.sports))
    return Settings(
        odds_api_key=os.getenv("THE_ODDS_API_KEY") or os.getenv("LISA_ODDS_API_KEY", ""),
        api_base_url=os.getenv("THE_ODDS_API_BASE_URL")
        or os.getenv("LISA_API_BASE_URL", DEFAULT_API_BASE_URL),
        regions=os.getenv("LISA_REGIONS", "eu,us"),
        markets=os.getenv("LISA_MARKETS", "h2h"),
        sports=sports,
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
        max_alerts_per_cycle=_int("LISA_MAX_ALERTS_PER_CYCLE", Settings.max_alerts_per_cycle),
        max_alerts_per_sport_cycle=_int("LISA_MAX_ALERTS_PER_SPORT_CYCLE", Settings.max_alerts_per_sport_cycle),
        conviction_min=_float("LISA_CONVICTION_MIN", Settings.conviction_min),
        slippage_max_cv_drift=_float("LISA_SLIPPAGE_MAX_CV_DRIFT", Settings.slippage_max_cv_drift),
        discord_webhook_url=os.environ.get("LISA_DISCORD_WEBHOOK_URL", Settings.discord_webhook_url),
        cadence_prematch_sec=_int("LISA_CADENCE_PREMATCH_SEC", Settings.cadence_prematch_sec),
        cadence_spike_sec=_int("LISA_CADENCE_SPIKE_SEC", Settings.cadence_spike_sec),
        cadence_live_sec=_int("LISA_CADENCE_LIVE_SEC", Settings.cadence_live_sec),
        cadence_idle_sec=_int("LISA_CADENCE_IDLE_SEC", Settings.cadence_idle_sec),
        cadence_settle_sec=_int("LISA_CADENCE_SETTLE_SEC", Settings.cadence_settle_sec),
        spike_window_sec=_int("LISA_SPIKE_WINDOW_SEC", Settings.spike_window_sec),
        live_window_hours=_float("LISA_LIVE_WINDOW_HOURS", Settings.live_window_hours),
        horizon_hours=_float("LISA_HORIZON_HOURS", Settings.horizon_hours),
        credit_warn=_int("LISA_CREDIT_WARN", Settings.credit_warn),
        credit_stop=_int("LISA_CREDIT_STOP", Settings.credit_stop),
        metrics_path=os.environ.get("LISA_METRICS_PATH", Settings.metrics_path),
    )