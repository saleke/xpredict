"""Public calendar projection of observations already collected by workers."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .observability import FIXTURES_KEY, fixture_observations
from .providers.calendar import normalise_team

CALENDAR_KEY = 'daily:calendar'


def calendar_snapshot(rows, now, *, days_ahead=7):
    return fixture_observations(rows, now, limit=1500, days_ahead=days_ahead)


def saved_daily_board(storage, settings, *, now=None):
    """No provider I/O. Merge result observations and group in product time."""
    now = now or datetime.now(timezone.utc)
    zone = ZoneInfo(settings.product_timezone)
    today = now.astimezone(zone).date()
    tomorrow = today + timedelta(days=1)
    week_end = today + timedelta(days=7)
    board = {key: [] for key in ('today', 'tomorrow', 'this_week', 'all_upcoming')}
    snapshots = [storage.get_telemetry(key) or {} for key in
                 (CALENDAR_KEY, FIXTURES_KEY, 'settlement:'+FIXTURES_KEY)]
    observed = [s.get('worker_observed_at') for s in snapshots if s.get('worker_observed_at')]
    entries = {}
    for snapshot in snapshots:
        seen_at = snapshot.get('worker_observed_at') or ''
        for row in snapshot.get('rows', []):
            try:
                kickoff = datetime.fromisoformat(row['kickoff'])
                if kickoff.tzinfo is None:
                    continue
                day = kickoff.astimezone(zone).date()
                if day < today:
                    continue
                identity = (row['league'], round(kickoff.timestamp()/60),
                            normalise_team(row['home']), normalise_team(row['away']))
                old = entries.get(identity)
                # Newer result observations update the calendar without a new
                # generation run. A final result wins if timestamps tie.
                priority = (seen_at, bool(row.get('completed')))
                if old and old[0] >= priority:
                    continue
                score = row.get('score')
                if not isinstance(score, list) or len(score) != 2 or not all(
                        type(n) is int and 0 <= n <= 99 for n in score):
                    score = None
                entries[identity] = (priority, {
                    'match_id': row['match_id'], 'sport_key': row['league'],
                    'home_team': row['home'], 'away_team': row['away'],
                    'commence_time': row['kickoff'], 'status': row['status'],
                    'completed': bool(row.get('completed')), 'score': score,
                    'source': row['source'], 'worker_observed_at': seen_at})
            except (KeyError, ValueError, TypeError, AttributeError):
                continue
    for _, row in sorted(entries.values(), key=lambda e: (datetime.fromisoformat(e[1]['commence_time']), e[1]['match_id'])):
        day = datetime.fromisoformat(row['commence_time']).astimezone(zone).date()
        if day == today:
            board['today'].append(row)
        elif day == tomorrow:
            board['tomorrow'].append(row)
        if day < week_end:
            board['this_week'].append(row)
        if not row['completed'] and row['status'] not in ('CANCELED', 'CANCELLED', 'POSTPONED', 'SUSPENDED', 'ABANDONED', 'FINISHED', 'FT', 'AET', 'PEN', 'LIVE', 'IN_PLAY', '1H', '2H', 'HT', 'ET') and datetime.fromisoformat(row['commence_time']) > now:
            board['all_upcoming'].append(row)
    return {'success': True, 'state': 'observed' if observed else 'starting',
            'board': board, 'timezone': settings.product_timezone,
            'worker_observed_at': max(observed) if observed else None,
            'provenance': {'sources': sorted({row['source'] for _, row in entries.values()}),
                'synthetic': False, 'provider_requests': 0,
                'truncated': any(s.get('truncated', False) for s in snapshots),
                'detail': 'Saved worker observations; source caches determine score freshness.'}}
