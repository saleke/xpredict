#!/usr/bin/env python3
"""Bounded direct OddsPapi check; quota/cache writes only, no publication."""
import argparse
import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lisa.config import load_settings
from lisa.runtime import RuntimeConfig
from lisa.providers.oddspapi import OddsPapiProvider, TOURNAMENTS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--league', choices=sorted(TOURNAMENTS), default='soccer_epl')
    parser.add_argument('--account-only', action='store_true', help='Check account without billable catalogs or odds')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    settings = RuntimeConfig(load_settings()).settings()
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'league': args.league,
              'provider': 'oddspapi', 'connection': 'direct'}
    try:
        if not settings.oddspapi_key:
            report['state'] = 'missing_credential'
        else:
            socket.getaddrinfo('api.oddspapi.io', 443, type=socket.SOCK_STREAM)
            from lisa.cli import _make_storage
            storage = _make_storage(settings)
            provider = OddsPapiProvider(settings.oddspapi_key,
                monthly_limit=settings.oddspapi_monthly_limit, reserve=settings.oddspapi_reserve,
                poll_interval_sec=settings.oddspapi_poll_interval_sec, bookmakers=settings.oddspapi_bookmakers)
            provider.storage = storage
            account = provider.account()
            report['account'] = {k: account[k] for k in ('limit', 'used', 'books')}
            report['state'] = 'account_sample_received'
            if not args.account_only:
                started = datetime.now(timezone.utc)
                sample = provider.fetch(sport_keys=[args.league], window_hours=72, now=started,
                    max_pages=1, deadline=started + timedelta(seconds=45))
                report.update(state='sample_failed' if sample.error else
                    'price_sample_received' if sample.pages else 'no_price_sample',
                    quotes=len(sample.quotes), pages=sample.pages, truncated=sample.truncated,
                    markets=sorted({q.market for q in sample.quotes}),
                    quotes_with_bookmaker_update_time=sum(q.updated_at is not None for q in sample.quotes),
                    dropped=sample.dropped,
                    quota=storage.get_telemetry('provider:oddspapi:status') or {})
                report['detail'] = 'One-league sample; zero quotes does not prove broad lack of coverage.'
    except socket.gaierror:
        report.update(state='blocked_network', detail='Hostname cannot be resolved; key not authenticated.')
    except Exception as exc:
        report.update(state='failed', error_type=type(exc).__name__,
                      detail='Credential, account identity, payload and exception text withheld.')
    encoded = json.dumps(report, indent=2, sort_keys=True) + '\n'
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end='')
    return 0 if report['state'] in ('account_sample_received', 'price_sample_received') else 1


if __name__ == '__main__':
    raise SystemExit(main())
