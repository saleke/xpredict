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
    # The original scope was European football plus two basketball competitions.
    # On any given day that set can be empty inside a 24h window -- the five
    # biggest European leagues share a match calendar, so a day with no fixture
    # in one is usually a day with none in the others. The scope was widened with
    # leagues whose schedules are independent of the European one, so the
    # 24h board has fixtures to show on more days of the year.
    "basketball_nba",
    "basketball_euroleague",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
    "soccer_italy_serie_a",
    "soccer_netherlands_eredivisie",
    "soccer_portugal_primeira_liga",
    "soccer_epl",
    # North American, on a calendar that does not move with Europe.
    "americanfootball_nfl",
    "baseball_mlb",
    "basketball_wnba",
    # Summer/winter season competitions. Both are seasonal: when a league is out
    # of season the API simply returns no events, and an empty league is not an
    # error, but it does contribute nothing to board volume.
    "icehockey_liiga",
    "icehockey_sweden_allsvenskan",
    "tennis_wta_singapore_open",
    # Secondary football leagues, which carry fixtures European football does not.
    "soccer_mexico_ligamx",
    "soccer_brazil_serie_b",
    "soccer_usa_mls",
    "soccer_uefa_nations_league",
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
    odds_api_keys: tuple[str, ...] = ()
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
    storage_driver: str = "sqlite"  # sqlite | inmemory | redis | postgres
    redis_url: str = ""
    database_url: str = ""
    live_ttl_sec: int = 5 * 3600

    # -- settlement -----------------------------------------------------------
    settle_after_hours: float = 3.0
    settle_grace_hours: float = 24.0
    settle_scores_days: int = 3

    # -- BetExplorer backfill (Parse) ----------------------------------------
    # Re-grades pending picks from archived day pages, which carry a final
    # score and a closing 1X2 line. Off by default: it spends Parse credits
    # (one per calendar day fetched) and the settle poll already covers the
    # recent window, so this earns its keep only once history is being
    # backfilled into accuracy_tracker.
    enable_betexplorer_backfill: bool = False
    parse_api_key: str = ""              # read from PARSE_API_KEY when empty
    backfill_lookback_days: int = 7       # calendar days of page history to walk
    backfill_min_interval_sec: float = 1.0
    backfill_dry_run: bool = True         # compute and report, write nothing

    # -- notifications --------------------------------------------------------
    telegram_token: str = ""
    telegram_chat_id: str = ""
    tier2_telegram_chat_id: str = ""
    telegram_bot_username: str = ""

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
    credit_warn: int = 100                 # degrade below this remaining (per key)
    credit_stop: int = 20                  # only settlement below this (per key)
    credit_budget_daily: int = 50          # hard cap on requests per key per UTC day
    metrics_path: str = "data/metrics.jsonl"

    # -- Payment & Tiers -------------------------------------------------------
    tier1_price_ngn: int = 14_999
    tier2_price_ngn: int = 29_999
    tier3_price_ngn: int = 49_888
    paystack_secret_key: str = ""
    paystack_public_key: str = ""
    usdt_trc20_address: str = "TKWFRpQgZRJxhHarM6etoQ2KSeWVK7QXBs"
    sol_address: str = "FKky47wNt2viC1mvKurTqhSAdUe6GZeVcuCJHw1ZKxNn"
    ton_address: str = "UQCJ9UyJzyAwWZhdulzB8K0veAqkjBhaP6v2exhyYWwTmar8"
    # Bank transfer (uncomment when virtual account is ready)
    # bank_name: str = ""
    # bank_account_name: str = ""
    # bank_account_number: str = ""

    # -- forecast board window ------------------------------------------------
    # The board is a short-horizon product. Everything on it must be playable
    # inside this window; fixtures further out are excluded rather than used to
    # pad the board to a volume target.
    forecast_horizon_hours: float = 24.0
    forecast_min_matches: int = 12         # volume promise, reported as a shortfall
    forecast_max_matches: int = 40

    # -- optional product surfaces (built, off by default) -------------------
    # Extra markets cost real credits: 1 per sport for h2h, 3 for
    # h2h+spreads+totals on a single region, 6 across eu+us. They stay off while
    # the pre-match model is being measured for accuracy and precision.
    enable_extra_markets: bool = False
    # Model-only goal markets (BTTS, over/under any line, scorelines) cost no
    # extra credits at all: they are derived from the Poisson matrix on fixtures
    # already being polled. They are still gated so a measurement run can be
    # kept to a single well-understood product surface.
    enable_micro_predictions: bool = False
    # In-play polling costs far more than pre-match: tracking every active
    # league at the live cadence runs 300-500 credits/day, so even when enabled
    # the cap below keeps high-frequency polling to the nearest kickoffs.
    enable_inplay: bool = False
    inplay_max_leagues: int = 2            # concurrent leagues polled live
    inplay_tail_hours: float = 2.0         # keep polling this long after kickoff


_DOTENV_LOADED = False


def _load_dotenv(filepath: str = ".env", force: bool = False) -> None:
    """Zero-dependency .env loader that populates os.environ if key not already set."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED and not force:
        return
    _DOTENV_LOADED = True
    try:
        from pathlib import Path
        for candidate in (
            Path(filepath),
            Path.cwd() / filepath,
            Path(__file__).resolve().parents[2] / filepath,
        ):
            if candidate.exists() and candidate.is_file():
                for line in candidate.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("\"'")
                    if k and v and k not in os.environ:
                        os.environ[k] = v
                break
    except Exception:
        pass


def _collect_api_keys() -> tuple[str, ...]:
    """Gather every configured odds key, in preference order.

    Supported forms, so an operator can add a key without touching code:
      THE_ODDS_API_KEYS=k1,k2      explicit pool (preferred)
      THE_ODDS_API_KEY=k1          primary, kept for backwards compatibility
      THE_ODDS_API_KEY_2=k2        numbered extras (and 3, 4, ...)
      THE_ODD_API_KEY=k1           tolerated typo seen in older templates
    """
    found: list[str] = []

    def _add(value: str) -> None:
        for part in (value or "").split(","):
            key = part.strip()
            if key and key not in found:
                found.append(key)

    _add(os.getenv("THE_ODDS_API_KEYS", ""))
    _add(os.getenv("THE_ODDS_API_KEY", ""))
    _add(os.getenv("LISA_ODDS_API_KEY", ""))
    _add(os.getenv("THE_ODD_API_KEY", ""))
    # Numbered extras, tolerating gaps so setting only _KEY_3 still works.
    for index in range(2, 12):
        _add(os.getenv(f"THE_ODDS_API_KEY_{index}", ""))
    return tuple(found)


def load_settings() -> Settings:
    _load_dotenv()
    sports = validate_sports(_tuple("LISA_SPORTS", Settings.sports))
    api_keys = _collect_api_keys()
    return Settings(
        odds_api_key=api_keys[0] if api_keys else "",
        odds_api_keys=api_keys,
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
        storage_driver=os.environ.get("LISA_STORAGE", "sqlite"),
        redis_url=os.environ.get("LISA_REDIS_URL", ""),
        database_url=os.environ.get("LISA_DATABASE_URL", ""),
        live_ttl_sec=_int("LISA_LIVE_TTL_SEC", Settings.live_ttl_sec),
        settle_after_hours=_float("LISA_SETTLE_AFTER_HOURS", Settings.settle_after_hours),
        settle_grace_hours=_float("LISA_SETTLE_GRACE_HOURS", Settings.settle_grace_hours),
        settle_scores_days=_int("LISA_SETTLE_SCORES_DAYS", Settings.settle_scores_days),
        enable_betexplorer_backfill=_bool("LISA_ENABLE_BETEXPLORER_BACKFILL",
                                         Settings.enable_betexplorer_backfill),
        parse_api_key=os.environ.get("PARSE_API_KEY", Settings.parse_api_key),
        backfill_lookback_days=_int("LISA_BACKFILL_LOOKBACK_DAYS",
                                   Settings.backfill_lookback_days),
        backfill_min_interval_sec=_float("LISA_BACKFILL_MIN_INTERVAL_SEC",
                                         Settings.backfill_min_interval_sec),
        backfill_dry_run=_bool("LISA_BACKFILL_DRY_RUN", Settings.backfill_dry_run),
        telegram_token=os.environ.get("LISA_TELEGRAM_TOKEN", ""),
        telegram_chat_id=os.environ.get("LISA_TELEGRAM_CHAT_ID", ""),
        tier2_telegram_chat_id=os.environ.get("LISA_TIER2_TELEGRAM_CHAT_ID", ""),
        telegram_bot_username=os.environ.get("LISA_TELEGRAM_BOT_USERNAME", ""),
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
        credit_budget_daily=_int("LISA_CREDIT_BUDGET_DAILY", Settings.credit_budget_daily),
        forecast_horizon_hours=_float(
            "LISA_FORECAST_HORIZON_HOURS", Settings.forecast_horizon_hours),
        forecast_min_matches=_int(
            "LISA_FORECAST_MIN_MATCHES", Settings.forecast_min_matches),
        forecast_max_matches=_int(
            "LISA_FORECAST_MAX_MATCHES", Settings.forecast_max_matches),
        enable_extra_markets=_bool(
            "LISA_ENABLE_EXTRA_MARKETS", Settings.enable_extra_markets),
        enable_micro_predictions=_bool(
            "LISA_ENABLE_MICRO_PREDICTIONS", Settings.enable_micro_predictions),
        enable_inplay=_bool("LISA_ENABLE_INPLAY", Settings.enable_inplay),
        inplay_max_leagues=_int(
            "LISA_INPLAY_MAX_LEAGUES", Settings.inplay_max_leagues),
        inplay_tail_hours=_float(
            "LISA_INPLAY_TAIL_HOURS", Settings.inplay_tail_hours),
        metrics_path=os.environ.get("LISA_METRICS_PATH", Settings.metrics_path),
        tier1_price_ngn=_int("LISA_TIER1_PRICE_NGN", Settings.tier1_price_ngn),
        tier2_price_ngn=_int("LISA_TIER2_PRICE_NGN", Settings.tier2_price_ngn),
        tier3_price_ngn=_int("LISA_TIER3_PRICE_NGN", Settings.tier3_price_ngn),
        paystack_secret_key=os.environ.get("PAYSTACK_SECRET_KEY", ""),
        paystack_public_key=os.environ.get("PAYSTACK_PUBLIC_KEY", ""),
        usdt_trc20_address=os.environ.get("LISA_USDT_TRC20_ADDRESS", Settings.usdt_trc20_address),
        sol_address=os.environ.get("LISA_SOL_ADDRESS", Settings.sol_address),
        ton_address=os.environ.get("LISA_TON_ADDRESS", Settings.ton_address),
    )