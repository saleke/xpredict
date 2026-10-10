#!/usr/bin/env python3
"""Read-only, bounded provider checks. Reports never include keys or error bodies.

PYTHONPATH=engine python scripts/validate_providers.py --output data/reports/provider-validation.json
Add --history to check one previous season; --allsports-odds to test odds entitlement.
No prediction publication, database writes, purchases or notifications occur.
"""
import argparse
import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lisa.config import load_settings
from lisa.providers.base import HttpTransport
from lisa.providers.allsports import AllSportsProvider
from lisa.providers.api_football import ApiFootballProvider
from lisa.providers.football_data import FootballDataProvider
from lisa.providers.sharpapi import SharpApiOddsProvider
from lisa.providers.calendar import LEAGUES
from lisa.providers.oddspapi import OddsPapiProvider

DIAGNOSTIC_CODES = frozenset({'subscription_or_season_restricted', 'quota_or_rate_limited',
    'authentication_rejected', 'provider_error', 'malformed_envelope', 'unsupported_result_shape'})


def safe_check(action):
    try:
        return {'state': 'ok', 'summary': action()}
    except Exception as exc:
        # Even provider exceptions can contain server text or a credential URL.
        result = {'state': 'failed', 'error_type': type(exc).__name__,
                  'detail': 'Request or parsing failed; response and exception text omitted.'}
        if getattr(exc, 'diagnostic_code', None) in DIAGNOSTIC_CODES:
            result['diagnostic_code'] = exc.diagnostic_code
        return result


def fixture_summary(rows, now):
    return {'fixtures': len(rows),
            'finished': sum(bool(r.get('completed')) for r in rows),
            'upcoming': sum(not r.get('completed') and r.get('status') == 'SCHEDULED'
                            and now.timestamp() < r.get('epoch', 0) <= (now + timedelta(days=3)).timestamp()
                            for r in rows),
            'verified_corner_results': sum(bool(r.get('completed')) and
                all(type(r.get(k)) is int for k in ('home_corners', 'away_corners')) for r in rows)}


def price_summary(provider, league, now):
    # Earlier fixture checks may have consumed minutes. Each independent price
    # check gets its own request window, not the overall report's start time.
    started = datetime.now(timezone.utc)
    snapshot = provider.fetch(sport_keys=[league], window_hours=72, now=started, max_pages=1,
                              deadline=started + timedelta(seconds=30))
    if snapshot.error:
        return {'request_state': 'failed', 'detail': 'Provider reported an error; text omitted.'}
    quotes = snapshot.quotes
    if snapshot.pages == 0 and not quotes and snapshot.truncated:
        return {'request_state': 'not_completed', 'quotes': 0, 'pages': 0,
                'truncated': True, 'detail': 'No odds page collected; this is not a successful price check.'}
    return {'request_state': 'ok', 'quotes': len(quotes), 'pages': snapshot.pages,
            'truncated': snapshot.truncated, 'markets': sorted({q.market for q in quotes}),
            'quotes_with_update_time': sum(q.updated_at is not None for q in quotes),
            'detail': 'One-page sample; no offers does not prove missing entitlement.'}


def validate(settings, *, league='soccer_epl', history=False, allsports_odds=False):
    now = datetime.now(timezone.utc)
    entries = [
        ('oddspapi', 'api.oddspapi.io', getattr(settings, 'oddspapi_key', ''),
         lambda: OddsPapiProvider(settings.oddspapi_key, monthly_limit=settings.oddspapi_monthly_limit,
            reserve=settings.oddspapi_reserve, poll_interval_sec=settings.oddspapi_poll_interval_sec,
            bookmakers=settings.oddspapi_bookmakers)),
        ('allsports', 'apiv2.allsportsapi.com', settings.allsports_api_key,
         lambda: AllSportsProvider(settings.allsports_api_key,
             hourly_limit=settings.allsports_hourly_limit, odds_enabled=allsports_odds,
             corner_stat_type=settings.allsports_corner_stat_type)),
        ('api_football', 'v3.football.api-sports.io', settings.api_football_key,
         lambda: ApiFootballProvider(settings.api_football_key, daily_limit=settings.api_football_daily_limit)),
        ('football_data', 'api.football-data.org', settings.football_data_token,
         lambda: FootballDataProvider(settings.football_data_token,
             transport=HttpTransport(timeout=10, max_retries=0))),
        ('sharpapi', 'api.sharpapi.io', settings.sharpapi_key,
         lambda: SharpApiOddsProvider(settings.sharpapi_key,
             transport=HttpTransport(timeout=10, max_retries=0))),
    ]
    report = {'schema_version': 1, 'checked_at': now.isoformat(), 'league': league,
              'live_validation_complete': False, 'providers': {},
              'limitations': ['No model approval follows from successful provider access.',
                  'Configured quotas are operator settings, not verified subscription limits.',
                  'Corner counts and bookmaker availability require independent cross-checks.']}
    for name, host, credential, factory in entries:
        result = {'configured': bool(credential), 'checks': {}}
        report['providers'][name] = result
        if not credential:
            result['state'] = 'missing_credential'
            continue
        try:
            socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError:
            result['state'] = 'blocked_network'
            result['detail'] = 'Provider hostname cannot be resolved; credentials were not tested.'
            continue
        provider = factory()
        checks = result['checks']
        if name not in ('sharpapi', 'oddspapi'):
            checks['fixtures'] = safe_check(lambda: fixture_summary(list(provider.get_fixtures(league).fixtures), now))
        if name == 'api_football' and history:
            checks['history'] = safe_check(lambda: fixture_summary(
                list(provider.get_season(league, provider.competition(league)[1] - 1).fixtures), now))
        elif name == 'football_data' and history:
            checks['history'] = safe_check(lambda: fixture_summary(list(provider.get_season(
                league, int(provider.current_season(LEAGUES[league].fdo).year) - 1).fixtures), now))
        elif name == 'allsports' and history:
            start = (now - timedelta(days=730)).date()
            checks['history'] = safe_check(lambda: fixture_summary(list(provider.get_range(
                league, start.isoformat(), (start + timedelta(days=14)).isoformat()).fixtures), now))
        if name == 'oddspapi':
            checks['account'] = safe_check(lambda: {k: provider.account()[k] for k in ('limit', 'used', 'books')})
        if name in ('api_football', 'sharpapi', 'oddspapi') or name == 'allsports' and allsports_odds:
            checks['prices'] = safe_check(lambda: price_summary(provider, league, now))
        result['state'] = 'sample_passed' if checks and all(c['state'] == 'ok' and
            c.get('summary', {}).get('request_state', 'ok') == 'ok' for c in checks.values()) else 'sample_failed'
    report['live_validation_complete'] = all(r['state'] == 'sample_passed' for r in report['providers'].values())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--league', choices=sorted(LEAGUES), default='soccer_epl')
    parser.add_argument('--history', action='store_true')
    parser.add_argument('--allsports-odds', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    from lisa.runtime import RuntimeConfig
    report = validate(RuntimeConfig(load_settings()).settings(), league=args.league,
                      history=args.history, allsports_odds=args.allsports_odds)
    encoded = json.dumps(report, indent=2, allow_nan=False) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    print(encoded, end='')
    return 0 if report['live_validation_complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
