"""Bounded, source-attributed fixture observations without extra provider calls."""
import math
from datetime import datetime, timedelta, timezone

FIXTURES_KEY = 'daily:fixture_observations'
PERFORMANCE_KEY = 'daily:performance'


def fixture_observations(rows, now, *, limit=500, days_ahead=2):
    observations = {}
    provisional = 0
    for row in rows:
        if row.get('discovery_only') or row.get('kickoff_time_known') is False:
            provisional += 1
            continue
        try:
            kickoff = datetime.fromisoformat(row['kickoff'].replace('Z','+00:00'))
            if kickoff.tzinfo is None or not now-timedelta(days=1) <= kickoff <= now+timedelta(days=days_ahead):
                continue
            source = str(row.get('provider') or 'unknown')[:80]
            match_id = str(row['match_id'])[:160]
            score = [row.get('home_score'),row.get('away_score')]
            score = score if all(type(n) is int and 0 <= n <= 99 for n in score) else None
            observations[(source,match_id)] = {
                'source':source,'match_id':match_id,'league':str(row['sport_key'])[:100],
                'home':str(row['home_team'])[:150],'away':str(row['away_team'])[:150],
                'kickoff':kickoff.astimezone(timezone.utc).isoformat(),
                'status':str(row.get('status') or ('FINISHED' if row.get('completed') else
                    'SCHEDULED' if kickoff > now else 'UNCONFIRMED'))[:64],
                'completed':bool(row.get('completed')),'score':score}
        except (KeyError,ValueError,TypeError,AttributeError):
            continue
    ordered = sorted(observations.values(),key=lambda r: (r['kickoff'],r['source'],r['match_id']))
    return {'worker_observed_at':now.isoformat(),'rows':ordered[:limit],
            'provisional_rows_withheld':provisional,
            'truncated':len(ordered)>limit,
            'detail':'Worker observations can come from provider caches. Observation time is not a verified provider update time.'}


def safe_timings(values):
    return {key:round(float(value),3) for key,value in values.items()
            if key in ('calendar','history_and_model','prices','board','total')
            and type(value) in (int,float) and math.isfinite(value) and value>=0}
