"""Resumable, quota-bounded history backfill independent of predictions/results."""
import logging
from .providers.diagnostics import safe_error_summary
from datetime import datetime, timezone, date, timedelta
from .daily_service import DailyService
from .match_history import HistoryRepository
from . import feed

logger = logging.getLogger(__name__)


class HistoryBackfillService(DailyService):
    lease_name = 'history'

    def _offer_history(self, providers, now):
        source = next((p for p in providers.prices if p.name == 'oddspapi'), None)
        if source is None:
            return {'state': 'unsupported'}
        completed = 'history:oddspapi:completed:'
        try:
            with self.storage._tx() as conn:
                pending = conn.execute('SELECT o.event_id,MIN(o.observed_at) AS first_seen '
                    'FROM odds_observations o LEFT JOIN system_telemetry t ON t.key=(? || o.event_id) '
                    'WHERE o.source=? AND t.key IS NULL GROUP BY o.event_id ORDER BY first_seen LIMIT 20',
                    (completed, 'oddspapi')).fetchall()
            from .odds_history import OddsHistoryRepository
            from .providers.oddspapi import timestamp
            history = OddsHistoryRepository(self.storage)
            imported = []
            attempts = 0
            failures = 0
            for candidate in pending:
                event = candidate['event_id']
                retry = self.storage.get_telemetry('history:oddspapi:retry:' + event) or {}
                if retry.get('retry_after', 0) > now.timestamp():
                    continue
                observations = history.observations('oddspapi', event, limit=1000)
                dated = [r for r in observations if timestamp(r.get('kickoff'))]
                if not dated or timestamp(dated[0]['kickoff']) + timedelta(hours=4) > now:
                    continue
                books = sorted({r['bookmaker'] for r in dated})[:3]
                attempts += 1
                try:
                    result = source.historical(event, books)
                    if not result['observations']:
                        raise ValueError('Historical prices not yet available')
                    self.storage.set_telemetry(completed + event, {'imported_at': now.isoformat(), **result})
                    imported.append(event)
                except Exception as exc:
                    failures += 1
                    self.storage.set_telemetry('history:oddspapi:retry:' + event,
                        {'retry_after': now.timestamp() + 86400, 'error_type': type(exc).__name__})
                if attempts >= 2:
                    break
            status = {'state': 'degraded' if failures else 'ok', 'events_imported': len(imported),
                      'attempts': attempts, 'failures': failures, 'last_attempt': now.isoformat()}
        except Exception as exc:
            status = {'state': 'failed', 'error_type': type(exc).__name__, 'last_attempt': now.isoformat()}
        self.storage.set_telemetry('history:oddspapi:status', status)
        return status

    def _supporting_history(self, providers, history, league, now):
        """One calendar month per tick; failures do not suppress primary history."""
        provider = providers.named('allsports')
        if provider is None or league not in provider.leagues():
            return {'state': 'unsupported'}
        key = f'history:checkpoint:allsports:{league}'
        checkpoint = self.storage.get_telemetry(key) or {}
        if checkpoint.get('retry_after', 0) > now.timestamp():
            return {'state': 'waiting'}
        # Bound collection to the model's recent history, even after a long pause.
        floor = date(now.year - 3, now.month, 1)
        current = date(now.year, now.month, 1)
        try:
            start = max(floor, date.fromisoformat(checkpoint.get('next_month', floor.isoformat())))
            start = min(start, current)
            following = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
            end = min(following - timedelta(days=1), now.date())
            rows = provider.get_range(league, start.isoformat(), end.isoformat(), ttl=86400).fixtures
            count = history.ingest(rows, observed_at=now)
            self.storage.set_telemetry(key, {'next_month': (following if start < current else current).isoformat(),
                'retry_after': now.timestamp() + 86400 if start == current else 0,
                'last_window': [start.isoformat(), end.isoformat()], 'error': None})
            status = {'state': 'ok', 'league': league, 'observations': count,
                      'window': [start.isoformat(), end.isoformat()], 'last_attempt': now.isoformat()}
        except Exception as exc:
            # Provider exception text may contain response data; retain its type only.
            self.storage.set_telemetry(key, dict(checkpoint, retry_after=now.timestamp() + 86400,
                                               error=type(exc).__name__))
            status = {'state': 'failed', 'league': league, 'last_attempt': now.isoformat(),
                      'error_type': type(exc).__name__}
        self.storage.set_telemetry(f'history:allsports:{league}', status)
        return status

    def _collect_seasons(self, provider, history, league, now, *, statistics=False):
        """Reuse one checkpoint path for bulk goals and metered enrichment."""
        prefix = f'history:checkpoint:{provider.name}:{league}:'
        catalog = self.storage.get_telemetry(prefix + 'catalog') or {}
        if catalog.get('retry_after', 0) > now.timestamp():
            return {'state': 'waiting', 'provider': provider.name, 'observations': 0}
        try:
            if provider.name == 'openfootball':
                current_year = provider.season_year()
            elif provider.name == 'football_data':
                from .providers.calendar import LEAGUES
                ref = provider.current_season(LEAGUES[league].fdo)
                if ref is None:
                    raise ValueError('Historical season unavailable')
                current_year = int(ref.year)
            else:
                current_year = provider.competition(league, purpose='history')[1]
        except Exception as exc:
            self.storage.set_telemetry(prefix + 'catalog', {
                'retry_after': now.timestamp() + 86400, 'error_type': type(exc).__name__})
            return {'state': 'failed', 'provider': provider.name,
                    'observations': 0, 'error': safe_error_summary(exc)}
        count = requests = attempts = 0
        failures = []
        for year in range(current_year, current_year - 4, -1):
            key = prefix + str(year)
            checkpoint = self.storage.get_telemetry(key) or {}
            if checkpoint.get('retry_after', 0) > now.timestamp():
                continue
            if checkpoint.get('done') and year != current_year and provider.name != 'openfootball':
                continue
            from .job_budget import check_budget
            check_budget()
            attempts += 1
            attempted = set(checkpoint.get('statistics_attempted', []))
            missing = set(checkpoint.get('statistics_missing', [])) if checkpoint.get('day') == now.date().isoformat() else set()
            try:
                result = (provider.get_season(league, year) if provider.name == 'football_data'
                          else provider.get_season(league, year, purpose='history'))
                rows = list(result.fixtures)
                count += history.ingest(rows, observed_at=now)
                if statistics:
                    for row in sorted(rows, key=lambda r: (r['epoch'], r['match_id'])):
                        if not row.get('completed') or row['match_id'] in attempted or row['match_id'] in missing:
                            continue
                        requests += 1
                        enriched = provider.with_corners(row, purpose='history')
                        count += history.ingest([enriched], observed_at=now)
                        if all(type(enriched.get(k)) is int for k in ('home_corners', 'away_corners')):
                            attempted.add(row['match_id'])
                        else:
                            missing.add(row['match_id'])
                        if requests >= getattr(self, 'statistics_batch_limit', 20):
                            break
                done = not statistics or all(not r.get('completed') or r['match_id'] in attempted for r in rows)
                interval = 30 * 86400 if provider.name == 'openfootball' and year != current_year else 86400
                self.storage.set_telemetry(key, {'statistics_attempted': sorted(attempted), 'done': done,
                    'statistics_missing': sorted(missing), 'day': now.date().isoformat(),
                    'retry_after': now.timestamp() + interval if done else 0})
            except Exception as exc:
                error = safe_error_summary(exc)
                failures.append(error)
                self.storage.set_telemetry(key, dict(checkpoint, statistics_attempted=sorted(attempted),
                    statistics_missing=sorted(missing), day=now.date().isoformat(),
                    retry_after=now.timestamp() + 86400, error=error))
            if (attempts >= getattr(self, 'season_batch_limit', 4)
                    or requests >= getattr(self, 'statistics_batch_limit', 20)):
                break
        with self.storage._tx() as conn:
            usable = conn.execute('SELECT 1 FROM match_observations WHERE source=? AND sport_key=? '
                'AND kickoff<? AND kickoff>=? LIMIT 1', (provider.name, league,
                (now - timedelta(days=2)).isoformat(), (now - timedelta(days=365.25*4)).isoformat())).fetchone()
        return {'state': ('degraded' if usable else 'failed') if failures else 'ok' if usable else 'empty',
            'provider': provider.name, 'observations': count,
            'statistics_requests': requests, 'seasons_attempted': attempts,
            'error': '; '.join(failures) or None}

    def tick(self, *, now=None):
        now = now or datetime.now(timezone.utc)
        if not self._lock.acquire(blocking=False):
            return {'state': 'busy'}
        claimed = False
        try:
            claimed = self._claim(now)
            if not claimed:
                return {'state': 'another_worker'}
            settings = self.settings
            providers = self._provider_set(settings)
            history = HistoryRepository(self.storage)
            offer_history = self._offer_history(providers, now)
            leagues = list(settings.board_leagues or feed._default_leagues(settings))
            if not leagues or self.storage.is_system_paused():
                return {'state': 'paused' if leagues else 'no_leagues'}
            if settings.scalper_mode == 'only':
                # The isolated collector owns historical collection in this
                # mode. Absence of API backfill providers is intentional;
                # expose its actual state rather than report a failed job or
                # start a second collector inside the prediction worker.
                worker = self.storage.get_telemetry('scalper:status') or {}
                state = worker.get('state', 'not_started')
                status = {'state': state if state in ('ok', 'partial', 'degraded', 'failed') else 'partial',
                    'delegated_to': 'scalper', 'observations': 0, 'statistics_requests': 0,
                    'available_results': len(history.results(leagues, as_of=now)),
                    'last_attempt': now.isoformat(), 'error': worker.get('error'),
                    'worker': {'state': state, 'last_attempt': worker.get('last_attempt')}}
                self.storage.set_telemetry('history:status', status)
                return status
            pointer = int(self.storage.get_telemetry('history:next_league') or 0) % len(leagues)
            league = leagues[pointer]
            self.storage.set_telemetry('history:next_league', (pointer + 1) % len(leagues))
            supporting = self._supporting_history(providers, history, league, now)
            # Bulk goal history comes first; a failure cannot suppress the
            # useful official fallback. API-Football is reserved for its
            # distinct contribution: fixture statistics, not duplicate goals.
            api = providers.named('api_football')
            goals = {'state': 'unsupported', 'observations': 0}
            for name in ('openfootball', 'football_data', 'api_football'):
                provider = providers.named(name)
                if provider is None or league not in provider.leagues():
                    continue
                goals = self._collect_seasons(provider, history, league, now,
                                              statistics=provider is api)
                if goals['state'] in ('ok', 'degraded'):
                    break
            enrichment = {'state': 'unsupported', 'observations': 0}
            if api is not None and league in api.leagues() and goals.get('provider') != api.name:
                enrichment = self._collect_seasons(api, history, league, now, statistics=True)
            useful = goals['state'] in ('ok', 'degraded') or supporting.get('state') == 'ok'
            errors = [part.get('error') for part in (goals, enrichment) if part.get('error')]
            status = {'state': 'degraded' if useful and errors else 'ok' if useful else 'failed',
                'league': league, 'observations': goals.get('observations', 0) + enrichment.get('observations', 0),
                'statistics_requests': goals.get('statistics_requests', 0) + enrichment.get('statistics_requests', 0),
                'last_attempt': now.isoformat(), 'error': '; '.join(errors) or None,
                'goals': goals, 'enrichment': enrichment, 'supporting': supporting,
                'offer_history': offer_history}
            self.storage.set_telemetry('history:status', status)
            return status
        except Exception as exc:
            logger.warning('history backfill: %s', safe_error_summary(exc))
            status = {'state': 'failed', 'last_attempt': now.isoformat(), 'error': safe_error_summary(exc)}
            self.storage.set_telemetry('history:status', status)
            return status
        finally:
            if claimed:
                self._release()
            self._lock.release()

    def _run(self):
        while not self._stop.is_set():
            self.tick()
            # Event wait is interrupted immediately by stop; the main worker
            # remains responsive while backfill sleeps between bounded batches.
            self._wake.wait(3600)
            self._wake.clear()
