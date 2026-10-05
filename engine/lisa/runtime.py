"""Live configuration state for a running process.

``Settings`` is frozen and loaded once from the environment, which is the right
shape for startup configuration and the wrong shape for an operations console:
an operator who needs to widen the poll cadence at 3am should not have to edit
``.env`` and restart the service, because a restart drops the scheduler's view
of what it has already polled and interrupts the ingestion thread.

This module keeps three layers, most specific winning:

1. the ``Settings`` built from the environment at startup (the base),
2. overrides persisted in ``system_telemetry`` so they survive a restart,
3. a validated, type-checked edit applied by an operator.

Two rules govern what may be overridden:

* **Never a secret.** Only fields that are safe to change at runtime are
  exposed; tokens, keys and DSNs are excluded by construction rather than by
  remembering to filter them. A redacted view is what the console reads.
* **Validate, then apply, or apply nothing.** Each override is coerced to the
  field's declared type and range-checked before the set is swapped in as one
  unit, so a bad edit can never leave a half-applied configuration.
"""
from __future__ import annotations

import dataclasses
import logging
import threading
from typing import Any, Callable, Iterable, Optional

from . import config as cfg

logger = logging.getLogger(__name__)

#: Telemetry row holding the override map. Namespaced so it cannot collide with
#: the kill-switch row the Telegram bot already writes.
OVERRIDES_KEY = "admin:settings_overrides"

#: Fields an operator may change at runtime. Everything else is either a secret,
#: a credential, or something that only makes sense before first boot.
#:
#: Keys are the dataclass field names. The exclusion is a blocklist on purpose:
#: an allowlist would silently break the day a new secret is added to Settings.
READONLY_FIELDS: frozenset[str] = frozenset({
    "odds_api_key", "odds_api_keys", "api_base_url", "telegram_token",
    "telegram_chat_id", "tier2_telegram_chat_id", "telegram_bot_username",
    "redis_url", "database_url", "storage_driver", "database_url",
    "discord_webhook_url", "metrics_path", "odds_sanity", "sharp_keys",
    "provider_credentials_path",
    "provider_credentials_backend", "credential_encryption_key",
    "paper_mode",
})

#: Never echoed to the console, even masked-by-accident. Substrings cover
#: compound names such as ``unlock_secret`` without needing to enumerate them.
_SECRET_HINTS: tuple[str, ...] = ("token", "secret", "password", "api_key",
                                  "api_keys", "dsn", "url", "webhook")

#: Per-field range limits. A setting outside these is rejected at the edge
#: rather than being allowed to break ingestion or the scheduler loop.
#: (minimum, maximum). Fields absent from this map are only type-checked.
_BOUNDS: dict[str, tuple[float, float]] = {
    'scalper_fixture_max_age_sec': (60, 86400),
    'scalper_quote_max_age_sec': (15, 3600),
    'openfootball_cache_sec': (21600, 604800),
    'the_odds_monthly_limit': (1, 1000000),
    'the_odds_reserve': (0, 1000000),
    'the_odds_daily_limit': (1, 1000000),
    'the_odds_cache_sec': (900, 86400),
    'oddspapi_monthly_limit': (1, 1000000),
    'oddspapi_reserve': (0, 1000000),
    'oddspapi_poll_interval_sec': (900, 86400),
    "daily_interval_sec": (60, 86400),
    "board_min_ev": (0, 10),
    "board_min_model_prob": (0, 1),
    "board_min_fair_odds": (1.01, 1000),
    'board_min_offer_odds': (1.01, 1000),
    "board_kelly_fraction": (0, 1),
    "board_max_stake": (0, 0.1),
    "board_min_accumulator_prob": (0, 1),
    "gate_threshold": (0.0, 1.0),
    "min_books_telemetry": (1, 50),
    "min_books_alert": (1, 50),
    "max_cv": (0.0, 10.0),
    "ev_min": (-1.0, 10.0),
    "sharp_multiplier": (0.0, 100.0),
    "min_margin_floor": (0.0, 1.0),
    "credit_warn": (0, 1_000_000),
    "credit_stop": (0, 1_000_000),
    "credit_budget_daily": (1, 1_000_000),
    "cadence_prematch_sec": (30, 86_400),
    "cadence_spike_sec": (15, 86_400),
    "cadence_live_sec": (15, 86_400),
    "cadence_idle_sec": (30, 604_800),
    "cadence_settle_sec": (60, 604_800),
    "spike_window_sec": (0, 604_800),
    "live_window_hours": (0.0, 48.0),
    "horizon_hours": (1.0, 336.0),
    "forecast_horizon_hours": (1.0, 336.0),
    "forecast_min_matches": (0, 500),
    "forecast_max_matches": (1, 500),
    "inplay_max_leagues": (0, 100),
    "inplay_tail_hours": (0.0, 24.0),
    "settle_after_hours": (0.0, 72.0),
    "settle_grace_hours": (0.0, 336.0),
    "settle_scores_days": (1, 30),
    "alert_cooldown_sec": (0, 604_800),
    "alert_min_delta": (0.0, 1.0),
    "conviction_min": (0.0, 1.0),
    "slippage_max_cv_drift": (0.0, 10.0),
    "live_ttl_sec": (30, 604_800),
    "stale_prematch_sec": (30, 604_800),
    "stale_live_sec": (10, 86_400),
    "max_alerts_per_cycle": (0, 10_000),
    "max_alerts_per_sport_cycle": (0, 10_000),
    "min_margin": (0.0, 1.0),
}


def is_secret_field(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith(('_key', '_keys')) or any(hint in lowered for hint in _SECRET_HINTS)


def editable_fields() -> tuple[str, ...]:
    """Field names an operator may change, in a stable order."""
    return tuple(sorted(f.name for f in dataclasses.fields(cfg.Settings)
                        if f.name not in READONLY_FIELDS and not is_secret_field(f.name)))


class OverrideError(ValueError):
    """A rejected edit. Carries per-field detail so the UI can show why."""


def _coerce(name: str, raw: Any) -> Any:
    """Coerce one override to the field's declared type and range."""
    fields = {f.name: f for f in dataclasses.fields(cfg.Settings)}
    field = fields.get(name)
    if field is None:
        raise OverrideError(f"unknown setting: {name}")
    if name in READONLY_FIELDS or is_secret_field(name):
        raise OverrideError(f"{name} cannot be changed at runtime")

    # The declared type is matched against the field's *default value* rather
    # than its annotation. With `from __future__ import annotations` the
    # annotation is the string "tuple[str, ...]", and string-matching it is
    # both fragile and easy to get subtly wrong. The default is a concrete
    # object, so its type is unambiguous.
    default = field.default
    if isinstance(default, bool):
        if isinstance(raw, bool):
            value = raw
        else:
            text = str(raw).strip().lower()
            if text in ("1", "true", "yes", "on"):
                value = True
            elif text in ("0", "false", "no", "off"):
                value = False
            else:
                raise OverrideError(f"{name} must be true or false")
    elif isinstance(default, tuple):
        # A tuple-typed field arrives as a JSON list, a comma-separated string,
        # or a genuine tuple. A single scalar is treated as a one-element list
        # so a form field sending "soccer_epl" does not explode into letters.
        if isinstance(raw, str):
            items = [p.strip() for p in raw.split(",") if p.strip()]
        elif isinstance(raw, (list, tuple)):
            items = [str(p) for p in raw]
        else:
            items = [str(raw)]
        value = tuple(items)
    elif isinstance(default, int):
        # bool is a subclass of int, but the bool branch above already claimed
        # it, so this only sees genuine integers.
        if isinstance(raw, bool):
            raise OverrideError(f"{name} must be a whole number")
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            raise OverrideError(f"{name} must be a whole number")
    elif isinstance(default, float):
        if isinstance(raw, bool):
            raise OverrideError(f"{name} must be a number")
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            raise OverrideError(f"{name} must be a number")
    elif isinstance(default, str):
        # A list sent to a string field means "join these", not "repr these".
        if isinstance(raw, (list, tuple)):
            value = ",".join(str(p).strip() for p in raw if str(p).strip())
        else:
            value = str(raw).strip()
    else:
        value = raw

    if name == "product_timezone":
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise OverrideError("product_timezone must be a valid IANA timezone") from exc
    bounds = _BOUNDS.get(name)
    if name == 'scalper_mode' and value not in ('off', 'supporting', 'only'):
        raise OverrideError('scalper_mode must be off, supporting or only')
    if bounds is not None:
        low, high = bounds
        # bool fields are ints in Python; comparing them is harmless but the
        # guard keeps a stray 1/0 out of a flag setting.
        if not isinstance(value, bool) and not (low <= value <= high):
            raise OverrideError(
                f"{name} must be between {low:g} and {high:g} (got {value:g})")
    return value


class RuntimeConfig:
    """Effective settings plus the operator's overrides, applied live.

    Thread-safe: the scheduler and the HTTP worker threads read ``settings()``
    on every access while an admin request may be swapping the set.
    """

    def __init__(self, base: cfg.Settings, *,
                 storage: Any = None,
                 on_change: Optional[Callable[[cfg.Settings], None]] = None):
        self._base = base
        self._storage = storage
        self._on_change = on_change
        self._overrides: dict[str, Any] = {}
        self._effective: cfg.Settings = cfg.paper_settings(base)
        self._lock = threading.RLock()
        self._revision = 0
        self._credential_values = {}
        self._credential_checked_at = None
        self.load()

    # -- persistence --------------------------------------------------------

    def load(self) -> dict[str, Any]:
        """Read persisted overrides and apply them over the base settings.

        Every row is put back through the same validation an operator edit goes
        through. Without that, this is a trust hole: the row lives in
        ``system_telemetry``, so anything able to write that table could set
        ``odds_api_keys`` or ``telegram_token`` (both are read-only by policy)
        or hand a flag a value like the string ``"no"``, which is truthy and
        would silently switch on a feature the operator believes is off. Boot
        must not be a way around the console's own rules.
        """
        raw: dict[str, Any] = {}
        if self._storage is not None:
            try:
                read_one = getattr(self._storage, 'get_telemetry', None)
                if callable(read_one):
                    stored = read_one(OVERRIDES_KEY)
                else:
                    # Preserve support for older non-relational read models.
                    stored = next((r['value'] for r in self._storage.admin_get_telemetry()
                                   if r.get('key') == OVERRIDES_KEY), None)
                if isinstance(stored, dict):
                    raw = stored
            except Exception:
                # A malformed or unreadable override row must not stop the
                # service from booting: fall back to base settings.
                raw = {}

        accepted: dict[str, Any] = {}
        rejected: dict[str, str] = {}
        for name, value in raw.items():
            try:
                accepted[name] = _coerce(name, value)
            except OverrideError as exc:
                rejected[name] = str(exc)
        candidate = dataclasses.replace(self._base, **accepted)
        if candidate.oddspapi_reserve >= candidate.oddspapi_monthly_limit:
            for name in ('oddspapi_reserve', 'oddspapi_monthly_limit'):
                if name in accepted:
                    accepted.pop(name)
                    rejected[name] = 'Reserve must be smaller than the monthly limit'
        if len(candidate.oddspapi_bookmakers) > 3:
            accepted.pop('oddspapi_bookmakers', None)
            rejected['oddspapi_bookmakers'] = 'At most three bookmakers are supported'
        from .providers.the_odds_api import TheOddsApiProvider
        try:
            TheOddsApiProvider('', monthly_limit=candidate.the_odds_monthly_limit,
                reserve=candidate.the_odds_reserve, daily_limit=candidate.the_odds_daily_limit,
                regions=candidate.the_odds_regions, markets=candidate.the_odds_markets,
                ttl=candidate.the_odds_cache_sec)
        except ValueError as exc:
            for name in tuple(accepted):
                if name.startswith('the_odds_'):
                    accepted.pop(name)
                    rejected[name] = str(exc)
        if rejected:
            logger.warning(
                "Discarding %d persisted setting override(s) that no longer "
                "validate: %s", len(rejected),
                ", ".join(f"{k} ({v})" for k, v in sorted(rejected.items())))
        # Rewrite the row so the rejected entries are not re-logged on every
        # restart, and so the stored state matches what is actually in effect.
        with self._lock:
            self._overrides = accepted
            self._recompute_locked()
            if raw != accepted:
                self._persist()
            return dict(self._overrides)

    def _persist(self) -> None:
        if self._storage is None:
            return
        try:
            self._storage.admin_set_telemetry(OVERRIDES_KEY, self._overrides)
        except Exception:
            # Persistence is best effort. The in-memory change still applies;
            # losing it across a restart is far better than failing the edit.
            pass

    # -- effective settings -------------------------------------------------

    def _recompute_locked(self) -> None:
        settings = self._base
        for name, value in self._overrides.items():
            try:
                settings = dataclasses.replace(settings, **{name: value})
            except (TypeError, ValueError):
                # A field that vanished from Settings between releases: drop it
                # rather than failing every future settings read.
                self._overrides.pop(name, None)
        self._effective = cfg.paper_settings(settings)
        self._revision += 1

    def settings(self) -> cfg.Settings:
        with self._lock:
            from .provider_credentials import credential_store
            import time
            if (self._base.provider_credentials_backend != 'postgres'
                    or self._credential_checked_at is None
                    or time.monotonic()-self._credential_checked_at >= 5):
                values = credential_store(self._base, self._storage).read()
                self._credential_checked_at = time.monotonic()
            else:
                values = self._credential_values
            if values != self._credential_values:
                self._credential_values = values
                self._recompute_locked()
            if values:
                overrides = dict(values)
                if 'odds_api_key' in overrides:
                    overrides['odds_api_keys'] = (overrides['odds_api_key'],) if overrides['odds_api_key'] else ()
                return dataclasses.replace(self._effective, **overrides)
            return self._effective

    def invalidate_credentials(self):
        with self._lock:
            self._credential_checked_at = None

    @property
    def base(self) -> cfg.Settings:
        return self._base

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def overrides(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._overrides)

    def reload_base(self, base: cfg.Settings) -> None:
        """Re-read the environment layer, keeping overrides on top."""
        with self._lock:
            self._base = base
            self._recompute_locked()

    # -- mutation -----------------------------------------------------------

    def apply(self, updates: dict[str, Any], *,
              replace: bool = False) -> dict[str, Any]:
        """Validate and apply overrides as one unit.

        Every field is coerced first and the new set is only swapped in once all
        of them have passed, so a rejected edit leaves the running configuration
        exactly as it was.
        """
        if not isinstance(updates, dict):
            raise OverrideError("expected an object of setting overrides")
        if not updates and not replace:
            raise OverrideError("no settings supplied")

        coerced: dict[str, Any] = {}
        errors: list[str] = []
        for name, raw in updates.items():
            try:
                coerced[str(name)] = _coerce(str(name), raw)
            except OverrideError as exc:
                errors.append(str(exc))
        if errors:
            raise OverrideError("; ".join(errors))

        # sports is validated against the league allowlist before anything else,
        # because an unknown key would make the next cycle raise instead of
        # merely polling the wrong league.
        if "sports" in coerced:
            try:
                coerced["sports"] = cfg.validate_sports(tuple(coerced["sports"]))
            except ValueError as exc:
                raise OverrideError(str(exc))
        if "markets" in coerced:
            # Validated at the edge: an unknown market key would otherwise be
            # accepted, sent upstream, and fail the whole cycle on a 422.
            allowed = {"h2h", "spreads", "totals", "btts", "double_chance"}
            chosen = [m.strip() for m in str(coerced["markets"]).split(",")
                      if m.strip()]
            if not chosen:
                raise OverrideError("at least one market is required")
            bad = sorted({m for m in chosen if m not in allowed})
            if bad:
                raise OverrideError(
                    f"unsupported markets: {', '.join(bad)} "
                    f"(allowed: {', '.join(sorted(allowed))})")
            coerced["markets"] = ",".join(chosen)

        with self._lock:
            new_overrides = {} if replace else dict(self._overrides)
            new_overrides.update(coerced)
            candidate = dataclasses.replace(self._base, **new_overrides)
            from .providers.the_odds_api import TheOddsApiProvider
            try:
                TheOddsApiProvider('', monthly_limit=candidate.the_odds_monthly_limit,
                    reserve=candidate.the_odds_reserve, daily_limit=candidate.the_odds_daily_limit,
                    regions=candidate.the_odds_regions, markets=candidate.the_odds_markets,
                    ttl=candidate.the_odds_cache_sec)
            except ValueError as exc:
                raise OverrideError(str(exc)) from None
            if not 0 <= candidate.oddspapi_reserve < candidate.oddspapi_monthly_limit:
                raise OverrideError('oddspapi_reserve must be smaller than oddspapi_monthly_limit')
            if len(candidate.oddspapi_bookmakers) > 3:
                raise OverrideError('At most three OddsPapi bookmakers may be configured')
            previous = self._effective
            self._overrides = new_overrides
            self._recompute_locked()
            # Roll back if the recomputed object failed to build, so a bad edit
            # can never leave the process without settings.
            try:
                self._effective
            except Exception:
                self._overrides = dict(self._overrides) if not replace else {}
                self._recompute_locked()
                raise OverrideError("settings could not be applied")
            changed = self._effective != previous
            self._persist()

        if changed and self._on_change:
            try:
                self._on_change(self._effective)
            except Exception:
                # A listener failure must not undo an already-applied change.
                pass
        return self.overrides()

    def reset(self, names: Optional[Iterable[str]] = None) -> dict[str, Any]:
        """Clear all overrides, or only the named ones, back to the env value."""
        with self._lock:
            if names is None:
                self._overrides = {}
            else:
                for name in names:
                    self._overrides.pop(str(name), None)
            self._recompute_locked()
            self._persist()
        if self._on_change:
            try:
                self._on_change(self._effective)
            except Exception:
                pass
        return self.overrides()

    # -- console view -------------------------------------------------------

    def describe(self) -> list[dict[str, Any]]:
        """Editable settings with value, default, and whether it is overridden.

        Secrets are absent from this list entirely: they are not editable, and
        their presence in a console payload is a leak waiting to happen.
        """
        base = self._base
        effective = self.settings()
        out: list[dict[str, Any]] = []
        for name in editable_fields():
            field = next(f for f in dataclasses.fields(cfg.Settings)
                         if f.name == name)
            override = self.overrides().get(name)
            low, high = _BOUNDS.get(name, (None, None))
            out.append({
                "name": name,
                "type": _json_type(field.type),
                "value": _jsonable(getattr(effective, name, None)),
                "default": _jsonable(getattr(base, name, None)),
                "overridden": override is not None and name in self.overrides(),
                "min": low,
                "max": high,
                "choices": _choices(name),
            })
        return out

    def redacted_overview(self) -> dict[str, Any]:
        """Non-editable configuration, reported as presence flags only."""
        effective = self.settings()
        return {
            name: bool(getattr(effective, name, None))
            for name in ("odds_api_key", "telegram_token", "redis_url",
                         "database_url", "discord_webhook_url")
        }


def _json_type(annotation: Any) -> str:
    text = str(annotation)
    if text.startswith("tuple") or text == "tuple":
        return "list"
    if text.startswith("bool") or text == "bool":
        return "bool"
    if text.startswith("int") or text == "int":
        return "int"
    if text.startswith("float") or text == "float":
        return "float"
    return "string"


def _jsonable(value: Any) -> Any:
    if isinstance(value, tuple):
        return list(value)
    return value


def _choices(name: str) -> list[str]:
    if name == "storage_driver":
        return ["sqlite", "inmemory", "redis", "postgres"]
    if name == "regions":
        return ["eu", "us", "eu,us", "uk", "au"]
    if name == "markets":
        return ["h2h", "h2h,spreads,totals"]
    return []


def sports_catalogue() -> list[dict[str, Any]]:
    """Leagues the console can offer, flagged by whether they are configured.

    The allowlist is the source of truth: the API only accepts these keys, so
    offering anything else would produce a configuration that fails on the next
    cycle rather than at save time.
    """
    return [{"key": key, "configured": False} for key in cfg.SCOPE_LEAGUES]
