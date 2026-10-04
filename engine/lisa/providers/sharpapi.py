"""SharpAPI: the free-tier *price* source, replacing The Odds API.

Why this exists
---------------
The model can price a match on its own, but it cannot know whether that price
is worth taking without someone quoting it. Without a price source the earning
ladder, the accumulators, Kelly staking, slippage and CLV are all structurally
empty -- every pick is unpriced, every edge is ``None``, and the board is
honest about being unable to answer the question it exists to answer.

SharpAPI's free tier is the only multi-book price feed available at zero cost:
DraftKings and FanDuel, 12 requests per minute, prices delayed 60 seconds. That
is enough to price a 24-48 hour board; it is not enough to poll aggressively,
which shapes every decision below.

Three things this module is careful about
-----------------------------------------
**Rate limit.** 12 requests/minute is the binding constraint on the whole
product. The fetch is one narrow request per page, paginates with the opaque
cursor (never ``offset``, which drifts as live rows move), and stops as soon as
the page it just read sits past the window's end -- results are documented as
chronological, so nothing later can be inside a window that has already passed.

**Matching.** SharpAPI and the calendar sources name the same match with
completely different identifiers, so every quote has to be joined to a fixture
before it means anything. A wrong join does not merely lose a price, it attaches
one book's price to another team's match and invents an edge. The matcher is
therefore built to refuse: it resolves exact name matches first, falls back to
subset matches only for what is still unclaimed, and drops anything ambiguous.

**Absence.** No price, an unknown market, a suspended market, a two-way
"draw no bet" quoted as if it were 1X2 -- each yields *no row at all*, counted in
:class:`SharpSnapshot`, never a guess.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from ..board import MarketPrice
from .base import HttpTransport, ProviderQuota, SourceTier
from .calendar import LEAGUES, name_affinity

logger = logging.getLogger(__name__)

__all__ = [
    "NAME",
    "SharpQuote",
    "SharpSnapshot",
    "MatchOutcome",
    "SharpApiOddsProvider",
    "US_TO_DECIMAL_ADD",
]

NAME = "sharpapi"
BASE_URL = "https://api.sharpapi.io/api/v1"

#: The free tier's published budget. Twelve requests a minute is the constraint
#: the whole fetch strategy is built around.
FREE_REQUESTS_PER_MINUTE = 12
FREE_DATA_DELAY_SEC = 60
FREE_BOOKS: tuple[str, ...] = ("draftkings", "fanduel")

#: Rows per page. The API caps this at 200 and the cap is what makes a bounded
#: fetch possible at all.
PAGE_LIMIT = 200

#: Markets requested from the source. Exactly the two the Dixon-Coles model can
#: price: 1X2 and goal totals.
#:
#: The ``main`` and ``total`` aliases were rejected here. ``total`` matches *any*
#: market type containing "total", which in soccer pulls in team totals, corner
#: totals, shots, fouls and odd/even -- measured at 4.7x the rows for the two
#: markets actually wanted, against a 12 req/min budget. ``market_type`` also
#: differs from the ``market=`` query parameter; the exact strings are correct.
DEFAULT_MARKETS: tuple[str, ...] = ("moneyline", "total_goals")

#: Kickoff tolerance when joining a book event to a calendar fixture. Generous
#: enough to survive a kickoff being moved by an hour, far too tight to span two
#: meetings of the same two teams, which are months apart in every league here.
DEFAULT_MAX_KICKOFF_GAP_H = 6.0

#: American-odds conversion, so a row carrying only ``odds_american`` still
#: yields a price instead of being dropped.
US_TO_DECIMAL_ADD = 100.0

#: Market types mapped to a match-result price, keyed by the selection side.
#: ``moneyline_3-way`` is the same 1X2 market as ``moneyline`` under the other
#: name; the API documents that ``market=moneyline`` matches both, because
#: different books publish different names for the same three-way contract.
_H2H_MARKETS = frozenset({"moneyline", "moneyline_3-way"})

#: Goal-total markets. ``asian_total_goals`` is a distinct type from
#: ``total_goals`` but the same market; both are read.
_TOTALS_MARKETS = frozenset({"total_goals", "asian_total_goals"})

#: Both-teams-to-score. Published under different names by different books; an
#: unrecognised name is counted and dropped, never guessed at.
_BTTS_MARKETS = frozenset({"both_teams_to_score", "btts", "both_teams_score"})

_SIDE_TO_SELECTION = {"home": "Home", "draw": "Draw", "away": "Away"}


@dataclass(frozen=True)
class SharpQuote:
    """One book's price on one selection, still keyed by SharpAPI's event id.

    ``event_id`` is retained because the quote is meaningless until it has been
    joined to a calendar fixture -- that join happens once, in :meth:`match`,
    and is reported separately so a failure to match is visible rather than
    silent.
    """

    event_id: str
    market: str
    selection: str
    odds: float
    book_key: str
    book_title: str = ""
    source: str = NAME
    line: Optional[float] = None
    kickoff: Optional[datetime] = None
    home: str = ""
    away: str = ""
    updated_at: Optional[datetime] = None
    sport_key: str = ""

    @property
    def identity(self) -> tuple[str, str, Optional[float]]:
        """``(market, selection, line)`` -- what makes a price the same price."""
        return (self.market, self.selection, self.line)


@dataclass
class SharpSnapshot:
    """Everything one fetch learned, including what it could not use."""

    quotes: tuple[SharpQuote, ...] = ()
    #: Counter of why a delivered row produced no price. The operator needs this
    #: to tell "the books are not offering this" from "the adapter is not
    #: reading the feed correctly" -- they look identical from the board.
    dropped: dict[str, int] = field(default_factory=dict)
    rows: int = 0
    pages: int = 0
    truncated: bool = False
    stopped_because: str = ""
    quota: Optional[ProviderQuota] = None
    error: str = ""

    def count(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1

    @property
    def dropped_total(self) -> int:
        return sum(self.dropped.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": NAME,
            "quotes": len(self.quotes),
            "rows": self.rows,
            "pages": self.pages,
            "truncated": self.truncated,
            "stopped_because": self.stopped_because,
            "dropped": dict(sorted(self.dropped.items())),
            "error": self.error,
            "quota": None if self.quota is None else {
                "remaining": self.quota.remaining,
                "limit": self.quota.limit,
            },
        }


@dataclass(frozen=True)
class MatchOutcome:
    """The result of joining quotes to fixtures."""

    #: ``match_id`` -> prices, keyed the way :meth:`board.Board.build` expects.
    prices: dict[str, tuple[MarketPrice, ...]]
    matched_events: int
    matched_fixtures: int
    unmatched_events: int
    ambiguous_events: int
    #: Fixtures that were left unpriced because two book events fitted them
    #: equally well. Reported separately from ``unmatched_events`` because the
    #: two call for different responses: a name this matcher does not know is a
    #: coverage limit, while a contested fixture is a data problem upstream.
    contested_fixtures: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched_events": self.matched_events,
            "matched_fixtures": self.matched_fixtures,
            "unmatched_events": self.unmatched_events,
            "ambiguous_events": self.ambiguous_events,
            "contested_fixtures": self.contested_fixtures,
        }


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _decimal_odds(row: Mapping[str, Any]) -> Optional[float]:
    """The decimal price on a row, or ``None`` if there is not a valid one.

    A price at or below 1.0 is not a price -- it is either a malformed payload
    or a void, and treating it as one would put a stake on a guaranteed loss.
    """
    raw = row.get("odds_decimal")
    value: Optional[float]
    try:
        value = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        value = None
    if value is None:
        american = row.get("odds_american")
        try:
            american = float(american) if american is not None else None
        except (TypeError, ValueError):
            return None
        if american is None:
            return None
        # -150 means the stake wins 150 for every 100 at risk, so the decimal
        # price is 1 + 100/150. Writing it as 100/(100 + |american|) instead --
        # which is the formula that reads most naturally -- inverts the whole
        # scale and turns -200 into 0.333, a "price" below 1.0 that would then
        # be rejected as malformed while every genuinely long shot passed.
        value = (1.0 + american / US_TO_DECIMAL_ADD) if american > 0 else \
            (1.0 + US_TO_DECIMAL_ADD / abs(american))
    if value != value or value in (float("inf"), float("-inf")) or value <= 1.0:
        return None
    return value


class SharpApiOddsProvider:
    """Prices for the leagues the calendar sources cover."""

    name = NAME
    tier = SourceTier.OFFICIAL

    def __init__(self, api_key: str, *, transport: Optional[HttpTransport] = None,
                 ttl_sec: float = 90.0) -> None:
        self.api_key = (api_key or "").strip()
        self.ttl_sec = ttl_sec
        self._transport = transport or HttpTransport(timeout=15.0)
        self._transport.set_rate_limit(
            "api.sharpapi.io", FREE_REQUESTS_PER_MINUTE / 60.0, burst=3)
        self._last_quota: Optional[ProviderQuota] = None
        self._requests_this_minute = 0

    # -- contract -----------------------------------------------------------

    def is_available(self) -> bool:
        return bool(self.api_key)

    def quota_status(self) -> ProviderQuota:
        """A locally tracked view of the free-tier budget.

        The rate-limit headers are not reachable through the shared transport,
        which returns a decoded body rather than headers. Reporting the
        configured ceiling as though it were a server reading would be a small
        fiction; what is returned instead is the provider's own request count
        against the published limit, which is true and is the useful direction.
        """
        return ProviderQuota(
            remaining=max(0, FREE_REQUESTS_PER_MINUTE - self._requests_this_minute),
            limit=FREE_REQUESTS_PER_MINUTE, tier=self.tier)

    def leagues(self) -> list[str]:
        return [k for k, spec in LEAGUES.items() if spec.sharp]

    # -- fetch --------------------------------------------------------------

    def fetch(self, *, sport_keys: Sequence[str], window_hours: float,
              now: Optional[datetime] = None, max_pages: int = 6,
              deadline: Optional[datetime] = None) -> SharpSnapshot:
        """Read every price the books offer for the window.

        Stops on whichever comes first: the first page that sits entirely past
        the window, an exhausted cursor, the page cap, or the deadline.
        """
        now = now or datetime.now(timezone.utc)
        horizon = now + timedelta(hours=window_hours)
        snapshot = SharpSnapshot()
        if not self.is_available():
            snapshot.error = "no SHARPAPI_KEY set"
            return snapshot

        slugs = self._slugs_for(sport_keys)
        if not slugs:
            snapshot.error = ("no configured league has a SharpAPI slug; nothing "
                              "to request")
            snapshot.stopped_because = "no_mapped_leagues"
            return snapshot

        params: dict[str, Any] = {
            "league": ",".join(slugs),
            "market": ",".join(DEFAULT_MARKETS),
            # A live price is a price on a match already under way, which is not
            # something the model can pre-match evaluate. Live rows are dropped
            # at the source rather than filtered out downstream.
            "is_live": "false",
            "limit": PAGE_LIMIT,
        }

        cursor: Optional[str] = None
        for page in range(max(1, max_pages)):
            if deadline is not None and datetime.now(timezone.utc) >= deadline:
                snapshot.stopped_because = "time budget exhausted"
                snapshot.truncated = True
                break
            call = dict(params)
            if cursor:
                call["cursor"] = cursor
            try:
                body = self._transport.get_json(
                    f"{BASE_URL}/odds", params=call, headers=self._headers(),
                    ttl=self.ttl_sec, provider=NAME)
            except Exception as exc:
                from .diagnostics import safe_error_summary
                snapshot.error = f"odds fetch failed: {safe_error_summary(exc)}"
                break
            self._requests_this_minute += 1
            snapshot.pages += 1

            if not isinstance(body, Mapping):
                snapshot.error = (f"expected an object, got "
                                  f"{type(body).__name__}")
                break

            rows = body.get("data")
            if not isinstance(rows, list):
                rows = []
                snapshot.error = "response carried no data array"

            snapshot.rows += len(rows)
            parsed = self.parse_odds(rows, now=now, window_hours=window_hours,
                                     snapshot=snapshot)
            snapshot.quotes = snapshot.quotes + tuple(parsed)

            # Read the page's time span off the *delivered rows*, not off the
            # parsed quotes. The parser has already discarded everything outside
            # the window, so a page lying wholly beyond the horizon yields no
            # quotes at all -- and testing the parsed quotes for that condition
            # would never fire, leaving the fetch to run to the page cap on every
            # cycle instead of stopping as soon as it is clear there is nothing
            # left to read.
            page_times = [_parse_ts(r.get("event_start_time"))
                          for r in rows if isinstance(r, Mapping)]
            page_times = [t for t in page_times if t is not None]
            pagination = body.get("pagination")
            pagination = pagination if isinstance(pagination, Mapping) else {}
            has_more = bool(pagination.get("has_more"))
            next_cursor = pagination.get("next_cursor")

            # Results are documented as ordered by start time, so once the whole
            # page is beyond the horizon no later page can hold a fixture inside
            # it. This is what makes the fetch bounded by the window rather than
            # by the size of the provider's store (810k rows, 12 req/min).
            if page_times and min(page_times) > horizon:
                snapshot.stopped_because = "page is past the window horizon"
                break
            if not has_more:
                snapshot.stopped_because = "no further pages"
                break
            if not next_cursor:
                snapshot.stopped_because = "page reported more rows but no cursor"
                snapshot.truncated = True
                break
            if page + 1 >= max_pages:
                snapshot.stopped_because = (f"page cap of {max_pages} reached; the "
                                            "window may hold matches not yet read")
                snapshot.truncated = True
                break
            cursor = str(next_cursor)

        if not snapshot.stopped_because:
            snapshot.stopped_because = "no pages requested"
        return snapshot

    def _headers(self) -> dict[str, str]:
        # The header is the documented server-side method; the query-string
        # alternative puts the key in URLs, which end up in logs.
        return {"X-API-Key": self.api_key}

    @staticmethod
    def _slugs_for(sport_keys: Sequence[str]) -> list[str]:
        """SharpAPI slugs for the requested leagues, in the registry's order."""
        slugs: list[str] = []
        for key in sport_keys:
            spec = LEAGUES.get(key)
            if spec is not None and spec.sharp and spec.sharp not in slugs:
                slugs.append(spec.sharp)
        return slugs

    # -- parse --------------------------------------------------------------

    def parse_odds(self, rows: Iterable[Mapping[str, Any]], *,
                   now: Optional[datetime] = None,
                   window_hours: float = 48.0,
                   snapshot: Optional[SharpSnapshot] = None) -> list[SharpQuote]:
        """Translate delivered rows into quotes the board can consume.

        Untranslatable rows are counted and dropped. Nothing is inferred: a
        market whose type is not recognised contributes no price, because a
        guessed market would be indistinguishable from a real one on the board.
        """
        now = now or datetime.now(timezone.utc)
        window_start = now - timedelta(hours=6)
        window_end = now + timedelta(hours=window_hours + 6)
        counts = snapshot if snapshot is not None else SharpSnapshot()

        # A 1X2 cohort is only usable if it actually has three sides. Books sell
        # draw-no-bet as a two-way market carrying selection_type home/away, and
        # pricing a fixture's "Draw" leg from that cohort would invent a draw
        # price that nobody will accept.
        three_way: set[tuple[str, str]] = set()
        for row in rows:
            if row.get("market_type") in _H2H_MARKETS \
                    and row.get("selection_type") == "draw":
                three_way.add((str(row.get("event_id", "")),
                               str(row.get("sportsbook", ""))))

        out: list[SharpQuote] = []
        for row in rows:
            if not isinstance(row, Mapping):
                counts.count("row was not an object")
                continue

            event_id = str(row.get("event_id") or "")
            book = str(row.get("sportsbook") or "").strip()
            if not event_id:
                counts.count("row carried no event id")
                continue
            if not book:
                counts.count("row carried no sportsbook")
                continue

            # Suspended markets hold their last price frozen. That is a real
            # number but not an offer anyone can take right now, and publishing
            # it as one is exactly the "absent rendered as present" failure.
            if row.get("is_active") is False:
                counts.count("market suspended or closed")
                continue
            if row.get("is_live"):
                counts.count("in-play price")
                continue
            if row.get("is_player_prop"):
                counts.count("player prop")
                continue

            market_type = str(row.get("market_type") or "")
            side = str(row.get("selection_type") or "").strip().lower()
            line_raw = row.get("line")
            try:
                line = float(line_raw) if line_raw is not None else None
            except (TypeError, ValueError):
                line = None

            market: Optional[str] = None
            selection: Optional[str] = None

            if market_type in _H2H_MARKETS:
                if side not in _SIDE_TO_SELECTION:
                    counts.count(f"{market_type}: unsupported side {side!r}")
                    continue
                if (event_id, book) not in three_way:
                    counts.count("two-way moneyline (draw no bet), not 1X2")
                    continue
                market, selection = "h2h", _SIDE_TO_SELECTION[side]
            elif market_type in _TOTALS_MARKETS:
                if side not in ("over", "under"):
                    counts.count(f"{market_type}: unsupported side {side!r}")
                    continue
                if line is None:
                    counts.count(f"{market_type}: no line")
                    continue
                market, selection = "totals", f"{side.capitalize()} {line:g}"
            elif market_type in _BTTS_MARKETS:
                if side not in ("yes", "no"):
                    counts.count(f"{market_type}: unsupported side {side!r}")
                    continue
                market, selection = "btts", f"BTTS {'Yes' if side == 'yes' else 'No'}"
            else:
                counts.count(f"unmapped market type {market_type!r}")
                continue

            odds = _decimal_odds(row)
            if odds is None:
                counts.count(f"{market_type}: no usable decimal or American price")
                continue

            kickoff = _parse_ts(row.get("event_start_time"))
            if kickoff is None:
                counts.count("row carried no parsable start time")
                continue
            if not (window_start <= kickoff <= window_end):
                counts.count("outside the requested window")
                continue

            out.append(SharpQuote(
                event_id=event_id, market=market, selection=selection, odds=odds,
                book_key=book, book_title=book.replace("_", " ").title(),
                line=line if market == "totals" else None,
                kickoff=kickoff,
                home=str(row.get("home_team") or ""),
                away=str(row.get("away_team") or ""),
                updated_at=_parse_ts(row.get("timestamp")),
            ))
        return out

    # -- match --------------------------------------------------------------

    def match(self, quotes: Sequence[SharpQuote],
              fixtures: Sequence[Any], *,
              max_kickoff_gap_h: float = DEFAULT_MAX_KICKOFF_GAP_H
              ) -> MatchOutcome:
        """Join book prices to calendar fixtures.

        Runs in two passes. The first accepts only exact normalised name
        matches, so the strongest evidence claims its fixtures first. The second
        offers the weaker subset matches to whatever is still unclaimed, which
        keeps a bare "Internacional" from outranking a book that spelled out
        "Internacional RS" for a different fixture.

        Anything still ambiguous is dropped rather than resolved arbitrarily. A
        guess here is the worst available outcome: it produces a confident edge
        on a match the bettor did not intend to bet.
        """
        events = self._group_events(quotes)
        by_id = {f.match_id: f for f in fixtures}

        accepted: dict[str, list[SharpQuote]] = {}
        claimed_fixtures: set[str] = set()
        claimed_events: set[str] = set()
        contested_events: set[str] = set()
        contested_fixtures: set[str] = set()

        for minimum in (1.0, 0.0):
            candidates = self._candidates(events, by_id, minimum,
                                          max_kickoff_gap_h)
            # A fixture or event reachable from more than one pairing in this
            # pass is ambiguous. Counting is by distinct entity, not by pairing:
            # one event sitting between two fixtures is one ambiguity, and
            # reporting it as two overstated the problem -- and the same
            # per-edge mistake drove the unmatched count negative.
            this_pass_fixtures = Counter(fid for fid, _ in candidates)
            this_pass_events = Counter(eid for _, eid in candidates)
            ambiguous_f = {f for f, n in this_pass_fixtures.items() if n > 1}
            ambiguous_e = {e for e, n in this_pass_events.items() if n > 1}
            for fid, eid in candidates:
                if fid in ambiguous_f or eid in ambiguous_e:
                    # No evidence decides between the pairings, so no price is
                    # published for either side rather than one being guessed.
                    contested_fixtures.add(fid)
                    contested_events.add(eid)
                    continue
                if fid in claimed_fixtures or eid in claimed_events:
                    continue
                claimed_fixtures.add(fid)
                claimed_events.add(eid)
                accepted.setdefault(fid, []).extend(events[eid])

        prices: dict[str, tuple[MarketPrice, ...]] = {}
        for fid, matched in accepted.items():
            fixture = by_id[fid]
            prices[fid] = tuple(
                MarketPrice(
                    match_id=fixture.match_id, selection=q.selection, odds=q.odds,
                    book_key=q.book_key, book_title=q.book_title, source=q.source,
                    market=q.market, line=q.line, updated_at=q.updated_at)
                for q in matched)

        # An event whose pairings were contested stays contested even if a later
        # pass found it unambiguous elsewhere, because nothing published it.
        ambiguous_events = contested_events - claimed_events
        return MatchOutcome(
            prices=prices, matched_events=len(claimed_events),
            matched_fixtures=len(claimed_fixtures),
            unmatched_events=len(set(events) - claimed_events - ambiguous_events),
            ambiguous_events=len(ambiguous_events),
            contested_fixtures=len(contested_fixtures))

    @staticmethod
    def _group_events(quotes: Sequence[SharpQuote]) -> dict[str, list[SharpQuote]]:
        events: dict[str, list[SharpQuote]] = {}
        for quote in quotes:
            events.setdefault(quote.event_id, []).append(quote)
        return events

    @staticmethod
    def _candidates(events: Mapping[str, Sequence[SharpQuote]],
                    by_id: Mapping[str, Any], minimum: float,
                    max_gap_h: float
                    ) -> list[tuple[str, str]]:
        """(fixture_id, event_id) pairs scoring at or above ``minimum``.

        Both sides must clear the bar and neither may be cross-matched: an event
        where the home side resembles the fixture's *away* team is the same club
        meeting itself, not the fixture.
        """
        out: list[tuple[str, str]] = []
        gap = timedelta(hours=max_gap_h)
        for event_id, quotes in events.items():
            first = quotes[0]
            if first.kickoff is None:
                continue
            for fixture_id, fixture in by_id.items():
                kickoff = getattr(fixture, "kickoff", None)
                if kickoff is None or abs(kickoff - first.kickoff) > gap:
                    continue
                home = name_affinity(fixture.home, first.home)
                away = name_affinity(fixture.away, first.away)
                if home is None or away is None:
                    continue
                if home < minimum or away < minimum:
                    continue
                crossed = name_affinity(fixture.home, first.away)
                reversed_side = name_affinity(fixture.away, first.home)
                if ((crossed is not None and crossed >= minimum)
                        and (reversed_side is not None and reversed_side >= minimum)):
                    continue
                out.append((fixture_id, event_id))
        return out


def event_summary(quotes: Sequence[SharpQuote]) -> dict[str, dict[str, Any]]:
    """Per-event view of a snapshot, for the diagnostics the board reports."""
    out: dict[str, dict[str, Any]] = {}
    for quote in quotes:
        entry = out.setdefault(quote.event_id, {
            "home": quote.home, "away": quote.away,
            "kickoff": None if quote.kickoff is None else quote.kickoff.isoformat(),
            "quotes": 0, "markets": set(), "books": set(),
        })
        entry["quotes"] += 1
        entry["markets"].add(quote.market)
        entry["books"].add(quote.book_key)
    return out
