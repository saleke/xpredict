#!/usr/bin/env python3
"""One-league The Odds API probe; stores quota/cache only, never publishes."""
import argparse
import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from lisa.config import load_settings
from lisa.runtime import RuntimeConfig
from lisa.providers.the_odds_api import TheOddsApiProvider


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--league', default='soccer_epl')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    settings = RuntimeConfig(load_settings()).settings()
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'league': args.league}
    try:
        if not settings.odds_api_key:
            report['state'] = 'missing_credential'
        else:
            socket.getaddrinfo('api.the-odds-api.com',443,type=socket.SOCK_STREAM)
            from lisa.cli import _make_storage
            storage = _make_storage(settings)
            provider = TheOddsApiProvider(settings.odds_api_key,
                monthly_limit=settings.the_odds_monthly_limit, reserve=settings.the_odds_reserve,
                daily_limit=settings.the_odds_daily_limit, regions=settings.the_odds_regions,
                markets=settings.the_odds_markets, ttl=settings.the_odds_cache_sec)
            provider.storage = storage
            now = datetime.now(timezone.utc)
            snapshot = provider.fetch(sport_keys=[args.league],window_hours=72,now=now,
                                      max_pages=1,deadline=now+timedelta(seconds=30))
            report.update(state='sample_failed' if snapshot.error else
                'sample_received' if snapshot.pages else 'no_price_sample',
                quotes=len(snapshot.quotes), pages=snapshot.pages, truncated=snapshot.truncated,
                markets=sorted({q.market for q in snapshot.quotes}),
                quotes_with_update_time=sum(q.updated_at is not None for q in snapshot.quotes),
                quota=storage.get_telemetry('provider:the_odds_api:status') or {},
                detail='One-league sample; no model approval or guaranteed executable coverage.')
    except socket.gaierror:
        report.update(state='blocked_network',detail='Hostname cannot be resolved; credential not tested.')
    except Exception as exc:
        report.update(state='failed',error_type=type(exc).__name__,detail='Response and exception text withheld.')
    encoded=json.dumps(report,indent=2,sort_keys=True)+'\n'
    if args.output:
        args.output.write_text(encoded)
    print(encoded,end='')
    return 0 if report['state']=='sample_received' else 1


if __name__ == '__main__':
    raise SystemExit(main())
