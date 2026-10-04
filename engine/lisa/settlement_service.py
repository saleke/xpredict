"""Independent final-result ingestion: never fits a model or fetches prices."""
import logging
from dataclasses import replace
from datetime import datetime, timezone
from . import feed
from .daily_service import DailyService

logger = logging.getLogger(__name__)
STATUS_KEY = 'daily:settlement_status'


class SettlementService(DailyService):
    lease_name = 'settlement'

    def tick(self, *, now=None):
        now = now or datetime.now(timezone.utc)
        if not self._lock.acquire(blocking=False):
            return {'state': 'busy'}
        claimed = False
        try:
            claimed = self._claim(now)
            if not claimed:
                return {'state': 'another_worker'}
            due = self._due_leagues(now)
            errors = []
            results = []
            if due:
                providers = self._provider_set(self.settings)
                api = providers.named('api_football')
                if api is not None:
                    api.purpose = 'settlement'
                    import math
                    count = sum(league in api.leagues() for league in due)
                    reserve = api.daily_limit - max(1, int(api.daily_limit * .8))
                    rounds = reserve // count if count else 0
                    api.settlement_ttl = math.ceil(86400 / rounds) if rounds else 86400
                    self.storage.set_telemetry('coverage:settlement', {
                        'due_leagues': count, 'api_football_reserve': reserve,
                        'api_football_refresh_interval_sec': api.settlement_ttl,
                        'state': 'budgeted' if rounds else 'insufficient_reserve',
                        'detail': 'Other result sources retain independent refresh cadences.'})
                supporting = providers.named('allsports')
                if supporting is not None:
                    supporting.purpose = 'settlement'
                statuses = [replace(s) for s in providers.statuses]
                calendars = feed.fetch_calendar(providers.calendar, due, statuses, purpose='settlement')
                results = [row for rows in calendars.values() for row in rows]
                from .observability import FIXTURES_KEY, fixture_observations
                self.storage.set_telemetry('settlement:'+FIXTURES_KEY,fixture_observations(results,now))
                errors = [s.error for s in statuses if s.error]
                if api is not None:
                    from .providers.calendar import normalise_team
                    pending = self.storage.list_pending_picks()
                    corner_teams = {(p['sport_key'], normalise_team(p['home_team']), normalise_team(p['away_team']))
                                    for p in pending if p['market'] == 'corners'}
                    for index, row in enumerate(results):
                        identity = (row.get('sport_key'), normalise_team(row.get('home_team')), normalise_team(row.get('away_team')))
                        if row.get('provider') == api.name and row.get('completed') and identity in corner_teams:
                            try:
                                results[index] = api.with_corners(row, purpose='settlement')
                            except Exception as exc:
                                errors.append(f'Corner result fetch failed: {exc}')
                from .match_history import HistoryRepository
                HistoryRepository(self.storage).ingest(results, observed_at=now)
            settled = self._settle(results, now)
            status = {'last_attempt': now.isoformat(), 'settled': settled,
                      'pending_leagues': due, 'error': '; '.join(errors) or None,
                      'state': 'failed' if errors else 'ok'}
            self.storage.set_telemetry(STATUS_KEY, status)
            return status
        except Exception as exc:
            logger.exception('automatic settlement failed')
            status = {'state': 'failed', 'last_attempt': now.isoformat(),
                      'error': f'{type(exc).__name__}: {exc}'}
            self.storage.set_telemetry(STATUS_KEY, status)
            return status
        finally:
            if claimed:
                self._release()
            self._lock.release()
