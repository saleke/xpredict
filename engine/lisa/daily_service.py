"""Autonomous publication and settlement, with a shared relational read model.

Network/model work never runs in a database transaction or an HTTP request.
Only the worker publishes; readers use the last committed snapshot. A bounded
lease prevents two processes sharing a ledger from spending provider quota twice.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import feed
from .dashboard import LIVE_SNAPSHOT_KEY
from .odds import Score
from .storage import RelationalStorage, pick_key

logger = logging.getLogger(__name__)
BOARD_KEY = "daily:board"
STATUS_KEY = "daily:status"
LEASE_SECONDS = 300
from .model_policy import MODEL_VERSION, FORECAST_SOURCES


def report_payload(report) -> dict:
    return {"success": report.board is not None, "health": report.health(),
            "generated_at": report.began.isoformat(), "window_hours": report.window_hours,
            "window_selection": report.window_selection,
            "summary": report.summary(), "model": report.model,
            "providers": [p.to_dict() for p in report.providers],
            "prices": report.prices, "price_match": report.price_match,
            "notes": report.notes, "errors": report.errors,
            "timings_ms": getattr(report,'timings_ms',{}),
            "board": report.board.to_dict() if report.board else None,
            "forecast": report.forecast}


class DailyService:
    lease_name = "generation"

    def __init__(self, storage: RelationalStorage, settings, *, runner=None, providers=None, settle_in_cycle=True):
        self.storage = storage
        self.settle_in_cycle = settle_in_cycle
        self._settings = settings
        ZoneInfo(self.settings.product_timezone)
        if not 60 <= float(self.settings.daily_interval_sec) <= 86400:
            raise ValueError("daily interval must be between 60 and 86400 seconds")
        self.runner = runner or feed.run_feed
        self._providers = providers
        self._provider_signature = None
        self._model = None
        self._model_signature = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self.owner = uuid.uuid4().hex
    @property
    def settings(self):
        from .config import paper_settings
        return paper_settings(self._settings() if callable(self._settings) else self._settings)

    def read(self) -> dict | None:
        value = self.storage.get_telemetry(BOARD_KEY)
        if not isinstance(value, dict):
            return None
        # A provider outage must not advertise yesterday's selections as upcoming.
        now = datetime.now(timezone.utc)
        for key in ("winning", "earning", "micro_bets"):
            value["board"][key] = [r for r in value["board"].get(key, [])
                                   if datetime.fromisoformat(r["kickoff"]) > now]
        value["board"]["accumulators"] = [a for a in value["board"].get("accumulators", [])
            if all(datetime.fromisoformat(r["kickoff"]) > now for r in a["legs"])]
        forecast = value.get("forecast", {})
        forecast["matches"] = [r for r in forecast.get("matches", [])
                               if datetime.fromisoformat(r["commence_at"]) > now]
        forecast["count"] = len(forecast["matches"])
        return value

    def status(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        status = self.storage.get_telemetry(STATUS_KEY) or {}
        board = self.read()
        age = None
        if board:
            age = max(0.0, (now - datetime.fromisoformat(board["generated_at"])).total_seconds())
        paused = self.storage.is_system_paused()
        stale_after = max(300.0, float(self.settings.daily_interval_sec) * 3)
        healthy = bool(age is not None and age <= stale_after
                       and not status.get("error") and not paused)
        settlement = self.storage.get_telemetry("daily:settlement_status")
        settlement_ready = None
        if self.storage.get_telemetry("daily:worker_mode") == "separate":
            settlement_ready = bool(settlement and not settlement.get("error")
                and (now - datetime.fromisoformat(settlement["last_attempt"])).total_seconds() <= stale_after)
            healthy = healthy and settlement_ready
        return dict(status, settlement_ready=settlement_ready, state="paused" if paused else (
            "ready" if healthy else "stale" if age is not None else "starting"),
            age_sec=age, stale_after_sec=stale_after, ready=healthy)

    def _claim(self, now: datetime) -> bool:
        with self.storage._tx() as conn:
            self.storage.begin_write(conn)
            conn.execute("INSERT INTO worker_leases VALUES (?, '', 0) ON CONFLICT(name) DO NOTHING", (self.lease_name,))
            updated = conn.execute(
                "UPDATE worker_leases SET owner=?, expires=? WHERE name=? AND expires<=?",
                (self.owner, now.timestamp() + LEASE_SECONDS, self.lease_name, now.timestamp())).rowcount
            return bool(updated)

    def _release(self):
        with self.storage._tx() as conn:
            conn.execute("UPDATE worker_leases SET expires=0 WHERE name=? AND owner=?", (self.lease_name, self.owner))

    def _provider_set(self, settings):
        names = ("football_data_token", "sportsdb_key", "sharpapi_key", "enable_openligadb",
                 "enable_openfootball", "openfootball_cache_sec", "enable_sportsdb", "enable_sharpapi", "provider_timeout_sec", 'api_football_key',
                 'api_football_daily_limit', 'api_football_bookmakers')
        names += ('allsports_api_key', 'allsports_hourly_limit', 'allsports_odds_enabled',
                  'allsports_corner_stat_type', 'allsports_bookmakers')
        names += ('oddspapi_key', 'oddspapi_enabled', 'oddspapi_monthly_limit', 'oddspapi_reserve',
                  'oddspapi_poll_interval_sec', 'oddspapi_bookmakers')
        names += ('odds_api_key', 'the_odds_enabled', 'the_odds_monthly_limit', 'the_odds_reserve',
                  'the_odds_daily_limit', 'the_odds_regions', 'the_odds_markets', 'the_odds_cache_sec')
        names += ('scalper_mode', 'scalper_fixture_max_age_sec', 'scalper_quote_max_age_sec', 'board_leagues')
        signature = hashlib.sha256(repr(tuple(getattr(settings, k) for k in names)).encode()).hexdigest()
        if self._providers is None or (self._provider_signature and signature != self._provider_signature):
            self._providers = feed.build_providers(settings)
            self._model = None
        self._provider_signature = signature
        for source in (*self._providers.calendar, *self._providers.prices):
            source.storage = self.storage
        model_signature = tuple(getattr(settings, k) for k in
            ("model_base_mu", "model_home_adv", "model_shrinkage", "model_xi"))
        if model_signature != self._model_signature:
            self._model = None
        self._model_signature = model_signature
        return self._providers

    def _due_leagues(self, now):
        with self.storage._tx() as conn:
            return [r["sport_key"] for r in conn.execute(
                "SELECT DISTINCT sport_key FROM picks WHERE source IN (" + ','.join('?' for _ in FORECAST_SOURCES) + ") AND result IS NULL AND commence_time<=?",
                (*FORECAST_SOURCES, now.isoformat()))]

    def _settle(self, results: list[dict], now: datetime) -> int:
        # Index outstanding predictions once; never query the ledger for every
        # historical result in the training archive.
        from .providers.calendar import team_identity
        from .data_quality import reconcile_results
        # Discovery files have no settlement authority, including when their
        # stale score would otherwise conflict with a confirmed provider.
        results = [row for row in results if row.get('settlement_eligible') is not False]
        results, conflicts = reconcile_results(results)
        self.storage.set_telemetry('data:result_disagreements', {'observed_at': now.isoformat(),
            'count': len(conflicts), 'fixtures': conflicts[:50]})
        with self.storage._tx() as conn:
            pending = [dict(r) for r in conn.execute(
                "SELECT dedupe_key, match_id, sport_key, market, outcome_name, line, "
                "home_team, away_team, commence_time FROM picks WHERE source IN (" + ','.join('?' for _ in FORECAST_SOURCES) + ") "
                "AND result IS NULL AND commence_time<=?", (*FORECAST_SOURCES, now.isoformat()))]
        if not pending:
            return 0
        by_id = {}
        by_teams = {}
        for row in pending:
            by_id.setdefault(row["match_id"], []).append(row)
            key = (row["sport_key"], team_identity(row["home_team"]), team_identity(row["away_team"]))
            by_teams.setdefault(key, {}).setdefault(row["match_id"], []).append(row)
        updates = {}
        for result in results:
            hs, away_score = result.get("home_score"), result.get("away_score")
            canceled = str(result.get("status", "")).upper() in ("CANCELED", "CANCELLED")
            if not canceled and (not result.get("completed") or type(hs) is not int
                                  or type(away_score) is not int or hs < 0 or away_score < 0):
                continue
            match_id = str(result.get("match_id", ""))
            rows = by_id.get(match_id, [])
            if not rows:
                # Recover a fixture whose original provider is unavailable.
                # Exact normalized teams, league and a bounded kickoff gap only;
                # ambiguous rematches remain pending rather than guessed at.
                key = (result.get("sport_key"), team_identity(result.get("home_team", "")),
                       team_identity(result.get("away_team", "")))
                try:
                    kickoff = datetime.fromisoformat(result["kickoff"].replace("Z", "+00:00"))
                except (KeyError, ValueError, TypeError):
                    continue
                matches = [items for items in by_teams.get(key, {}).values()
                    if abs((datetime.fromisoformat(items[0]["commence_time"]) - kickoff).total_seconds()) <= 6 * 3600]
                if len(matches) != 1:
                    continue
                rows = matches[0]
            score = Score(match_id, result.get("sport_key", ""), now,
                bool(result.get("completed")), hs, away_score, str(result.get("status", "")),
                str(result.get("home_team", "")), str(result.get("away_team", "")),
                result.get('home_corners'), result.get('away_corners'))
            for row in rows:
                grade = "VOID" if canceled else score.grade_pick(row["market"], row["outcome_name"], row["line"])
                if grade:
                    updates.setdefault(row["dedupe_key"], (grade, now.isoformat(),
                        None if canceled else f"{hs}-{away_score}", result.get("provider"), row["dedupe_key"]))
        settled = 0
        with self.storage._tx() as conn:
            self.storage.begin_write(conn)
            for values in updates.values():
                settled += conn.execute(
                    "UPDATE picks SET state='SETTLED', result=?, settled_at=?, actual_score=?, result_source=? "
                    "WHERE dedupe_key=? AND result IS NULL", values).rowcount
        return settled

    def _publish(self, report, now: datetime) -> int:
        if report.board is None:
            return 0
        settings = self.settings
        payload = report_payload(report)
        payload["published_at"] = now.isoformat()
        payload['paper_mode'] = settings.paper_mode
        if settings.paper_mode:
            payload['board']['unproven'] = True
            for collection in ('winning', 'earning', 'micro_bets'):
                for row in payload['board'].get(collection, []):
                    row['stake_fraction'] = 0.0
            for acca in payload['board'].get('accumulators', []):
                acca['stake_fraction'] = 0.0
                for row in acca.get('legs', []):
                    row['stake_fraction'] = 0.0
        # The API carries only upcoming fixtures. A long fetch may have crossed kickoff.
        for name in ("winning", "earning", "micro_bets"):
            payload["board"][name] = [r for r in payload["board"][name]
                if datetime.fromisoformat(r["kickoff"]) > now]
        payload["board"]["accumulators"] = [a for a in payload["board"]["accumulators"]
            if all(datetime.fromisoformat(r["kickoff"]) > now for r in a["legs"])]
        payload["forecast"]["matches"] = [r for r in payload.get("forecast", {}).get("matches", [])
            if datetime.fromisoformat(r["commence_at"]) > now]
        payload["forecast"]["count"] = len(payload["forecast"]["matches"])
        opportunities = {}
        for name in ("winning", "micro_bets", "earning"):
            for o in getattr(report.board, name):
                if o.kickoff > now:
                    opportunities[pick_key(o.match_id, o.market, o.selection, o.line)] = o
        columns = ("dedupe_key", "match_id", "sport_key", "market", "outcome_name", "line",
            "home_team", "away_team", "commence_time", "p_true", "fair_odds", "n_books",
            "stdev", "cv", "state", "best_book", "best_odds", "best_ev", "clv",
            "recommended_stake_pct", "recommended_units", "created_at", "source", "basis",
            "is_recommendation", "model_version")
        inserted = 0
        with self.storage._tx() as conn:
            self.storage.begin_write(conn)
            lease = conn.execute("SELECT owner, expires FROM worker_leases WHERE name=?" + self.storage.lock_suffix, (self.lease_name,)).fetchone()
            if not lease or lease["owner"] != self.owner or lease["expires"] <= now.timestamp():
                raise RuntimeError("publication lease expired; discarding uncommitted cycle")
            for key, o in opportunities.items():
                recommended = bool(not settings.paper_mode and not report.board.unproven and o.priced
                                   and o.ev is not None and o.ev >= settings.board_min_ev
                                   and o.stake_fraction > 0)
                values = (key, o.match_id, o.sport_key, o.market, o.selection, o.line,
                    o.home, o.away, o.kickoff.isoformat(), o.p_model, o.fair_odds,
                    1 if o.priced else 0, None, None, "PENDING_SETTLEMENT", o.best_book,
                    o.best_odds, o.ev, None, o.stake_fraction * 100 if recommended else 0,
                    1 if recommended else 0, now.isoformat(), MODEL_VERSION, o.basis,
                    int(recommended), MODEL_VERSION)
                inserted += conn.execute(
                    f"INSERT INTO picks ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) ON CONFLICT(dedupe_key) DO NOTHING",
                    values).rowcount
            day = now.astimezone(ZoneInfo(settings.product_timezone)).date().isoformat()
            payload["forecast"]["day"] = day
            encoded = json.dumps(payload, allow_nan=False)
            conn.execute("INSERT INTO daily_publications VALUES (?, ?, ?) ON CONFLICT(day) "
                "DO UPDATE SET generated_at=excluded.generated_at, payload=excluded.payload",
                (day, now.isoformat(), encoded))
            conn.execute("INSERT INTO system_telemetry VALUES (?, ?, ?) ON CONFLICT(key) "
                "DO UPDATE SET val_json=excluded.val_json, updated_at=excluded.updated_at",
                (BOARD_KEY, encoded, now.isoformat()))
            snapshot = {"observed_at": now.timestamp(), "matches_observed": report.fixtures_in_window,
                "source": MODEL_VERSION, "sports": {k: {"matches": n} for k, n in report.leagues.items()},
                "last_error": "; ".join(report.errors) or None}
            conn.execute("INSERT INTO live_cache VALUES (?, ?, ?) ON CONFLICT(key) "
                "DO UPDATE SET data=excluded.data, expires_at=excluded.expires_at",
                (LIVE_SNAPSHOT_KEY, json.dumps(snapshot), now.timestamp() + 86400))
        return inserted

    def tick(self, *, now: datetime | None = None) -> dict:
        fixed_clock = now is not None
        now = now or datetime.now(timezone.utc)
        if not self._lock.acquire(blocking=False):
            return {"state": "busy"}
        claimed = False
        try:
            claimed = self._claim(now)
            if not claimed:
                return {"state": "another_worker"}
            settings = self.settings
            due = self._due_leagues(now)
            configured = settings.board_leagues or tuple(feed._default_leagues(settings))
            if self.storage.is_system_paused() and not due:
                return {"state": "paused", "predictions_added": 0, "settled": 0}
            settings = replace(settings, board_leagues=tuple(dict.fromkeys((*configured, *due))))
            previous = self.storage.get_telemetry(STATUS_KEY) or {}
            self.storage.set_telemetry(STATUS_KEY, dict(previous, last_attempt=now.isoformat(), state="running"))
            providers = self._provider_set(settings)
            from .coverage import apply_coverage
            apply_coverage(providers, settings, self.storage, now)
            options = dict(providers=providers, now=now, model=self._model)
            if self.runner is feed.run_feed:
                from .match_history import HistoryRepository
                options['history'] = HistoryRepository(self.storage)
            report = self.runner(settings, **options)
            self._model = report.active_model
            finished = now if fixed_clock else datetime.now(timezone.utc)
            from .observability import FIXTURES_KEY, PERFORMANCE_KEY, fixture_observations, safe_timings
            from .calendar_snapshot import CALENDAR_KEY, calendar_snapshot
            updates = getattr(report, 'fixture_updates', [])
            if updates or not report.errors:
                self.storage.set_telemetry(FIXTURES_KEY, fixture_observations(updates, finished))
                import math
                # Calendar discovery remains useful before training succeeds.
                # Reach verified fixtures even without any forecastable teams.
                calendar_hours = report.window_hours
                next_kickoffs = sorted(f.kickoff for f in feed.to_fixtures(updates)
                                       if f.kickoff > finished)
                if next_kickoffs:
                    target = max(1, int(settings.board_volume_target))
                    nearest_end = next_kickoffs[min(target, len(next_kickoffs))-1]
                    calendar_hours = max(calendar_hours,
                        (nearest_end-finished).total_seconds()/3600)
                self.storage.set_telemetry(CALENDAR_KEY, calendar_snapshot(updates, finished,
                    days_ahead=max(7, math.ceil(calendar_hours / 24) + 1)))
            self.storage.set_telemetry(PERFORMANCE_KEY,{'observed_at':finished.isoformat(),
                'timings_ms':safe_timings(getattr(report,'timings_ms',{})),
                'fixtures':report.fixtures_in_window,'results':report.results_collected})
            settled = self._settle(report.results, finished) if self.settle_in_cycle else 0
            paused = self.storage.is_system_paused()
            from .job_budget import check_budget
            check_budget()
            inserted = 0 if paused else self._publish(report, finished)
            previous = self.storage.get_telemetry(STATUS_KEY) or {}
            status = dict(previous, last_attempt=now.isoformat(), last_finished=finished.isoformat(),
                error="; ".join(report.errors) or ("no board available" if report.board is None else None),
                predictions_added=inserted, settled=settled, state="paused" if paused else report.health())
            if report.board is not None and not paused:
                status["last_success"] = finished.isoformat()
            self.storage.set_telemetry(STATUS_KEY, status)
            return status
        except Exception as exc:
            logger.exception("daily prediction cycle failed")
            previous = self.storage.get_telemetry(STATUS_KEY) or {}
            status = dict(previous, last_attempt=now.isoformat(), error=f"{type(exc).__name__}: {exc}", state="failed")
            self.storage.set_telemetry(STATUS_KEY, status)
            return status
        finally:
            if claimed:
                self._release()
            self._lock.release()

    def request_refresh(self):
        self.storage.set_telemetry("daily:refresh_request", uuid.uuid4().hex)
        self._wake.set()

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            request = self.storage.get_telemetry("daily:refresh_request")
            self.tick()
            deadline = time.monotonic() + max(60.0, float(self.settings.daily_interval_sec))
            while not self._stop.is_set() and time.monotonic() < deadline:
                if self._wake.wait(min(5.0, max(0, deadline - time.monotonic()))):
                    break
                if self.storage.get_telemetry("daily:refresh_request") != request:
                    break

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="lisa-daily-worker", daemon=True)
        self._thread.start()

    def request_stop(self):
        self._stop.set()
        self._wake.set()

    def stop(self):
        self.request_stop()
        if self._thread:
            self._thread.join(timeout=5)
