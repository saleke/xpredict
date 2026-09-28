"""Zero-credit fixture calendar — the economic foundation of the board.

The Odds API bills `/v4/sports/{sport}/odds` at `markets x regions` credits,
but **`/v4/sports` and `/v4/sports/{sport}/events` both cost nothing** (measured,
delta=0 across a live probe). That makes a complete, windowable match calendar
available for every configured league at any cadence, at no cost.

The planner uses this calendar to decide *which* leagues deserve a paid odds
poll in a cycle:

  * a league with no fixture inside the 48h product window is never polled;
  * a league is polled more often the closer its next fixture is to kickoff;
  * every league's region is chosen per sport family (`us` for American
    sports, `eu` elsewhere) so we pay 1 region, never 2.

The calendar is persisted into live storage so a transient fetch failure keeps
the last known fixtures rather than blanking the board, and the board can state
how old its own numbers are.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

from . import config as cfg
from .odds import utcnow
from .storage import Storage

#: Live-storage key that holds the whole calendar.
CALENDAR_KEY = "calendar:fixtures"

#: How old (seconds) a league's event list may be before the planner treats it
#: as "no data" for this cycle rather than polling blindly.
CALENDAR_MAX_AGE_SEC = 6 * 3600

#: How long the calendar itself stays readable before being considered stale.
CALENDAR_TTL_SEC = 7 * 24 * 3600

#: Event lists any older than this on refresh are considered unusable when
#: deciding whether a league may be polled (the /events fetch is free, so this
#: is only about *not* planning paid polls against a stale premise).
EVENTS_MAX_AGE_SEC = 24 * 3600


def default_region(sport_key: str) -> str:
    """One region per sport family.

    `us` returns the American books (DraftKings, FanDuel, BetMGM ...) that the
    US sports are quoted on; `eu` returns the European books (Pinnacle, Bet365,
    1xBet ...) that soccer and ice hockey live on. Always exactly one region:
    the /odds call bills per region, so two regions would double the cost for
    no extra league coverage.
    """
    if sport_key.startswith(("americanfootball_", "basketball_", "baseball_")):
        return "us"
    if sport_key.startswith(("soccer_", "icehockey_", "tennis_")):
        return "eu"
    return "eu"


def _region_for_league(sport_key: str) -> str:
    return default_region(sport_key)


#: T-minus bands. A league with a fixture inside SPIKING_WINDOW is polled at the
#: fast cadence; inside COOL_WINDOW at the moderate one; anything inside the
#: horizon at the quiet one; anything beyond the horizon not at all.
SPIKING_WINDOW = timedelta(hours=6)
COOL_WINDOW = timedelta(hours=24)


@dataclass
class CalendarFixture:
    """One fixture known from the free /events endpoint."""

    match_id: str
    sport_key: str
    commence_time: datetime
    home_team: str
    away_team: str

    @property
    def lead_hours(self, now: Optional[datetime] = None) -> Optional[float]:
        now = now or utcnow()
        if self.commence_time is None:
            return None
        return max(0.0, (self.commence_time - now).total_seconds() / 3600.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "sport_key": self.sport_key,
            "commence_time": self.commence_time.isoformat(),
            "home_team": self.home_team,
            "away_team": self.away_team,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CalendarFixture":
        ct = d.get("commence_time")
        if isinstance(ct, str):
            ct = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        return cls(
            match_id=str(d.get("match_id", "")),
            sport_key=str(d.get("sport_key", "")),
            commence_time=ct or utcnow(),
            home_team=str(d.get("home_team", "")),
            away_team=str(d.get("away_team", "")),
        )


@dataclass
class CalendarReport:
    refreshed: bool = False
    active_leagues: int = 0
    leagues_fetched: int = 0
    leagues_failed: list[str] = field(default_factory=list)
    events_seen: int = 0
    events_in_48h: int = 0
    errors: list[str] = field(default_factory=list)


class FixtureCalendar:
    """Free, persisted, windowed fixture calendar driven by /events."""

    def __init__(self, client, storage: Storage,
                 sports: Iterable[str] | None = None, *,
                 now_fn: Optional[Callable[[], datetime]] = None,
                 calendar_key: str = CALENDAR_KEY,
                 ttl_seconds: int = CALENDAR_TTL_SEC):
        self.client = client
        self.storage = storage
        self.sports = tuple(sports)
        self._now_fn = now_fn or utcnow
        self.calendar_key = calendar_key
        self.ttl_seconds = ttl_seconds

    # -- reading -------------------------------------------------------------

    def _now(self) -> datetime:
        return self._now_fn()

    def stale(self, max_age_sec: float = CALENDAR_MAX_AGE_SEC, *,
              now: Optional[datetime] = None) -> bool:
        """True when the persisted calendar is missing or older than max_age."""
        calendar = self._load()
        observed = calendar.get("observed_at")
        if observed is None:
            return True
        try:
            age = (self._now() if now is None else now).timestamp() - float(observed)
        except (TypeError, ValueError):
            return True
        return age > max_age_sec

    def has_fresh_snapshot(self, max_age_sec: float = CALENDAR_MAX_AGE_SEC, *,
                           now: Optional[datetime] = None) -> bool:
        """True when a *successful* refresh is recent enough to plan against.

        This is what separates "the window genuinely has no fixtures" (spend
        nothing) from "we could not find out" (fall back to the configured
        league list). Without it a single upstream failure would silently stop
        every paid poll and take the whole board dark.
        """
        observed = self._load().get("observed_at")
        if observed is None:
            return False
        try:
            age = (self._now() if now is None else now).timestamp() - float(observed)
        except (TypeError, ValueError):
            return False
        return age <= max_age_sec

    def _load(self) -> dict[str, Any]:
        try:
            data = self.storage.get_live_stale(self.calendar_key) or {}
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        sports = data.get("sports") or {}
        if not isinstance(sports, dict):
            sports = {}
        return {"observed_at": data.get("observed_at"), "sports": sports}

    def _save(self, calendar: dict[str, Any]) -> None:
        try:
            self.storage.upsert_live(self.calendar_key, calendar, self.ttl_seconds)
        except Exception:
            # The calendar backs planning only; losing a write must not crash.
            pass

    def all_fixtures(self) -> list[CalendarFixture]:
        """Every fixture the calendar knows about, newest-fetched first."""
        calendar = self._load()
        out: list[CalendarFixture] = []
        for sport, entries in (calendar.get("sports") or {}).items():
            for ev in (entries.get("events") or []):
                try:
                    out.append(CalendarFixture.from_dict(ev))
                except Exception:
                    continue
        return out

    def fixtures_in_window(self, hours: float = 48.0, *,
                           now: Optional[datetime] = None) -> list[CalendarFixture]:
        """Fixtures kicking off in ``[now, now + hours)``, sorted by kickoff.

        In-play fixtures (kickoff already passed but within the live tail) are
        deliberately excluded here — the planner wants *upcoming* matches; the
        pipeline decides about in-play separately.
        """
        now = now or self._now()
        edge = now + timedelta(hours=hours)
        out = [
            f for f in self.all_fixtures()
            if now <= f.commence_time <= edge
        ]
        out.sort(key=lambda f: (f.commence_time, f.match_id))
        return out

    def leagues_with_fixtures(self, hours: float = 48.0, *,
                              now: Optional[datetime] = None) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.fixtures_in_window(hours, now=now):
            counts[f.sport_key] = counts.get(f.sport_key, 0) + 1
        return counts

    # -- planning ------------------------------------------------------------

    def plan_leagues(self, *, horizon_hours: float = 48.0,
                     spike_window: timedelta = SPIKING_WINDOW,
                     cool_window: timedelta = COOL_WINDOW,
                     max_leagues: int = 8,
                     now: Optional[datetime] = None) -> list[dict[str, Any]]:
        """League poll plan for one cycle, cheapest-first and window-driven.

        Returns rows like::

            {"sport_key": "soccer_epl", "region": "eu",
             "urgency": "cool" | "spike" | "quiet", "n_fixtures": 4,
             "next_kickoff": datetime}

        Only leagues with at least one fixture inside the horizon are included;
        the rest would cost credits for zero board movement. Ties (or a calendar
        that has gone stale) are cut by fixture count then by league order, so
        the plan is deterministic.
        """
        now = now or self._now()
        requested = tuple(self.sports) if self.sports else cfg.SCOPE_LEAGUES
        calendar = self._load()
        sports_raw = calendar.get("sports") or {}
        now_ts = time.time()
        edge = now + timedelta(hours=horizon_hours)

        plan: list[dict[str, Any]] = []
        for sport in requested:
            entries = sports_raw.get(sport)
            if not entries:
                continue
            observed = entries.get("observed_at")
            if isinstance(observed, (int, float)) and now_ts - float(observed) > EVENTS_MAX_AGE_SEC:
                continue
            events = entries.get("events") or []
            upcoming = []
            for ev in events:
                try:
                    fx = CalendarFixture.from_dict(ev)
                except Exception:
                    continue
                if now <= fx.commence_time <= edge:
                    upcoming.append(fx)
            if not upcoming:
                continue
            next_kickoff = min(f.commence_time for f in upcoming)
            lead = next_kickoff - now
            if lead <= spike_window:
                urgency = "spike"
            elif lead <= cool_window:
                urgency = "cool"
            else:
                urgency = "quiet"
            plan.append({
                "sport_key": sport,
                "region": _region_for_league(sport),
                "urgency": urgency,
                "n_fixtures": len(upcoming),
                "next_kickoff": next_kickoff,
            })

        # Paying leagues first: spike > cool > quiet, then more fixtures first,
        # then the configured order (stable, so tests are deterministic).
        order = {s: i for i, s in enumerate(requested)}
        plan.sort(key=lambda p: (
            {"spike": 0, "cool": 1, "quiet": 2}[p["urgency"]],
            -p["n_fixtures"],
            order.get(p["sport_key"], 0),
        ))
        return plan[:max_leagues]

    # -- refresh (all free endpoints) -----------------------------------------

    def refresh(self, *, now: Optional[datetime] = None,
                max_leagues_per_tick: int = 24) -> CalendarReport:
        """Re-sync the calendar from the (free) /sports and /events endpoints.

        Each refresh costs exactly zero credits. Every league is fetched but the
        writes are batched so a single league failing mid-way does not discard
        the rest of the calendar.
        """
        report = CalendarReport()
        now = now or self._now()
        now_ts = time.time()
        requested = tuple(self.sports) if self.sports else cfg.SCOPE_LEAGUES

        active: set[str] = set()
        try:
            sports = self.client.list_sports() or []
            for s in sports:
                if isinstance(s, dict) and s.get("active") and s.get("key"):
                    active.add(str(s["key"]))
        except Exception as exc:
            report.errors.append(f"list_sports: {exc!r}")
            report.refreshed = False
            return report
        report.refreshed = True
        report.active_leagues = len(active)

        calendar = self._load()
        sports_cal = dict(calendar.get("sports") or {})

        # A /events failure for a league keeps its previous event list but
        # marks the entry stale so the planner cools it down.
        to_fetch = [s for s in requested if s in active][:max_leagues_per_tick]
        report.leagues_fetched = len(to_fetch)
        for sport in to_fetch:
            events: list[dict[str, Any]] = []
            try:
                raw = self.client.get_events(sport) or []
                for ev in raw:
                    if not isinstance(ev, dict):
                        continue
                    ct = ev.get("commence_time")
                    if not ct:
                        continue
                    events.append({
                        "match_id": str(ev.get("id", "")),
                        "sport_key": sport,
                        "commence_time": str(ct),
                        "home_team": str(ev.get("home_team", "")),
                        "away_team": str(ev.get("away_team", "")),
                    })
            except Exception as exc:
                report.leagues_failed.append(sport)
                report.errors.append(f"{sport}: {exc!r}")
                events = list((sports_cal.get(sport) or {}).get("events") or [])
            sports_cal[sport] = {
                "observed_at": now_ts,
                "events": events,
            }
            report.events_seen += len(events)

        calendar = {
            "observed_at": now_ts,
            "sports": sports_cal,
        }
        self._save(calendar)

        report.events_in_48h = len(self.fixtures_in_window(48.0, now=now))
        return report