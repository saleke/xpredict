"""Read-only bridge. Prediction requests never launch network collectors."""
from datetime import datetime, timedelta, timezone

from ..scalper.contracts import bridge_contract, timestamp
from ..scalper.repository import ScalperRepository
from ..scalper.sources import ESPN_LEAGUES
from .base import FixturesResult, ProviderNotAvailableError, ProviderQuota, SourceTier
from .sharpapi import SharpQuote, SharpSnapshot, MatchOutcome


class ScalperSnapshot(SharpSnapshot):
    def to_dict(self):
        value = super().to_dict()
        value['source'] = 'scalper'
        return value


class ScalperProvider:
    name = 'scalper'
    tier = SourceTier.UNOFFICIAL

    def __init__(self, *, leagues=(), fixture_max_age=900, quote_max_age=300, clock=None):
        from .calendar import LEAGUES
        if leagues and any(key not in LEAGUES for key in leagues):
            raise ValueError('Scalper requires known league keys')
        if not 60 <= fixture_max_age <= 86400 or not 15 <= quote_max_age <= 3600:
            raise ValueError('Invalid Scalper freshness allowance')
        self._leagues = tuple(leagues) or tuple(ESPN_LEAGUES)
        self.fixture_max_age, self.quote_max_age = fixture_max_age, quote_max_age
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.storage = None
        self._repository = None

    @property
    def repository(self):
        if self.storage is None:
            raise ProviderNotAvailableError('Scalper requires the worker\'s shared relational database', provider=self.name)
        if self._repository is None or self._repository.storage is not self.storage:
            self._repository = ScalperRepository(self.storage)
        return self._repository

    def leagues(self):
        return self._leagues

    def is_available(self):
        return self.storage is not None

    def quota_status(self):
        return ProviderQuota(0, 0, tier=self.tier)

    remaining_quota = quota_status

    def get_fixtures(self, sport_key):
        if sport_key not in self._leagues:
            raise ProviderNotAvailableError('Scalper league is outside configured coverage', provider=self.name)
        now = self.clock()
        rows = self.repository.fixtures((sport_key,), now=now, max_age=self.fixture_max_age)
        from ..data_quality import reconcile_results
        from .calendar import dedupe_fixtures
        accepted, conflicts = reconcile_results(rows)
        warnings = []
        if conflicts:
            warnings.append(f'{len(conflicts)} conflicting result/statistic observations quarantined')
        if not any(not r.get('completed') and not r.get('discovery_only') for r in accepted):
            warnings.append('No fresh verified upcoming/live observations; history alone does not establish calendar coverage')
        return FixturesResult(self.name, sport_key, tuple(dedupe_fixtures(accepted)), tuple(warnings))

    def fetch(self, *, sport_keys, now=None, **kwargs):
        now = now or self.clock()
        snapshot = ScalperSnapshot(quota=self.quota_status())
        quotes = []
        circuits = {}
        for row in self.repository.quotes(sport_keys, now=now, max_age=self.quote_max_age):
            snapshot.rows += 1
            if not row['active']:
                snapshot.count('suspended')
                continue
            if not bridge_contract(row):
                snapshot.count('unsupported_period_or_contract')
                continue
            updated = timestamp(row['updated_at'], optional=True)
            # Only built-in browser adapters can attest to a public publisher
            # snapshot. Generic imports cannot relabel retrieval as freshness.
            confirmed = (timestamp(row.get('confirmed_at'), optional=True)
                         if row['source'] in ('pinnacle_browser', 'sportybet_browser')
                         and row.get('freshness_basis') == 'publisher_snapshot' else None)
            browser_snapshot = row['source'] in ('pinnacle_browser', 'sportybet_browser')
            fresh = confirmed if browser_snapshot else updated
            if fresh is None:
                snapshot.count('missing_bookmaker_timestamp')
                continue
            if not -60 <= (now - fresh).total_seconds() <= self.quote_max_age:
                snapshot.count('stale_bookmaker_timestamp')
                continue
            if browser_snapshot and row['source'] not in circuits:
                circuits[row['source']] = self.repository.source_state(row['source'])['next_attempt']
            if (browser_snapshot and circuits[row['source']] > now.timestamp()
                    and (now - fresh).total_seconds() > 30):
                snapshot.count('disconnected_publisher')
                continue
            # The source-scoped identity avoids collisions across two collectors.
            quotes.append(SharpQuote(event_id=row['source'] + ':' + row['event_id'],
                market=row['market'], selection=row['selection'], odds=row['odds'],
                book_key=row['book_key'], book_title=row['book_title'], source=row['source'],
                line=row['line'], kickoff=timestamp(row['kickoff']), home=row['home'], away=row['away'],
                updated_at=updated, sport_key=row['sport_key'], confirmed_at=confirmed,
                freshness_basis='publisher_snapshot' if confirmed else 'provider_update'))
        snapshot.quotes = tuple(quotes)
        snapshot.stopped_because = 'stored_observations_only'
        return snapshot

    def match(self, quotes, fixtures, *, max_kickoff_gap_h=.25):
        # Separate books/sources can share one fixture. The existing matcher
        # intentionally regards two events as contested, so run it per source
        # rather than falsely treating real multi-book coverage as ambiguity.
        from collections import Counter
        from .calendar import team_identity
        prices, matched, unmatched, ambiguous, contested = {}, 0, 0, 0, 0
        for source in sorted({q.source for q in quotes}):
            subset = [q for q in quotes if q.source == source]
            events = {}
            for q in subset:
                events.setdefault(q.event_id, []).append(q)
            candidates = []
            for eid, offers in events.items():
                q = offers[0]
                for fixture in fixtures:
                    if (fixture.sport_key == q.sport_key and q.kickoff is not None
                            and abs((fixture.kickoff - q.kickoff).total_seconds()) <= min(.25, max_kickoff_gap_h) * 3600
                            and team_identity(fixture.home) == team_identity(q.home)
                            and team_identity(fixture.away) == team_identity(q.away)):
                        candidates.append((eid, fixture.match_id))
            event_counts = Counter(eid for eid, fid in candidates)
            fixture_counts = Counter(fid for eid, fid in candidates)
            ambiguous_events, contested_fixtures, accepted = set(), set(), set()
            from ..board import MarketPrice
            from ..scalper.repository import quote_identity
            for eid, fid in candidates:
                if event_counts[eid] != 1 or fixture_counts[fid] != 1:
                    ambiguous_events.add(eid)
                    contested_fixtures.add(fid)
                    continue
                accepted.add(eid)
                prices[fid] = prices.get(fid, ()) + tuple(MarketPrice(
                    match_id=fid, selection=q.selection, odds=q.odds, book_key=q.book_key,
                    book_title=q.book_title, source=q.source, market=q.market, line=q.line,
                    updated_at=q.updated_at, confirmed_at=q.confirmed_at,
                    freshness_basis=q.freshness_basis,
                    valid_until=min(q.kickoff, (q.confirmed_at or q.updated_at)
                                    + timedelta(seconds=self.quote_max_age))
                        if q.confirmed_at or q.updated_at else None,
                    source_event_id=q.event_id.removeprefix(q.source + ':'),
                    source_quote_identity=quote_identity(dict(market=q.market, selection=q.selection,
                        line=q.line, period='regulation', settlement_contract='regulation')))
                    for q in events[eid])
            matched += len(accepted)
            unmatched += len(set(events) - accepted - ambiguous_events)
            ambiguous += len(ambiguous_events)
            contested += len(contested_fixtures)
        return MatchOutcome(prices, matched, len(prices), unmatched, ambiguous, contested)
