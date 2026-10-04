#!/usr/bin/env python3
"""Read-only AllSportsAPI trial coverage probe; never prints credentials/payloads.

ALLSPORTS_API_KEY=... PYTHONPATH=engine python scripts/probe_allsports.py --league-id 207
Use a league ID returned by the subscription's Leagues catalog. No purchases,
notifications, bets, prediction publication or production data writes occur.
"""
import argparse
import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from lisa.config import _load_dotenv

URL = 'https://apiv2.allsportsapi.com/football/'


def request(method, key, **parameters):
    # POST is documented. Credential in the body avoids URLs/access logs and
    # HttpTransport's GET cache keys. Exceptions are sanitized deliberately.
    encoded = urlencode(dict(met=method, APIkey=key, **parameters)).encode()
    request = Request(URL, data=encoded, headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read(10_000_001)
        if len(raw) > 10_000_000:
            raise ValueError('AllSportsAPI response exceeds 10 MB limit')
        payload = json.loads(raw)
    except HTTPError as exc:
        raise ValueError(f'AllSportsAPI HTTP {exc.code}; credential and response omitted') from None
    except (URLError, TimeoutError, json.JSONDecodeError):
        raise ValueError('AllSportsAPI transport or JSON failure; credential and response omitted') from None
    if not isinstance(payload, dict) or payload.get('success') != 1:
        raise ValueError('AllSportsAPI rejected this request; check account entitlement')
    return payload.get('result')


def inspect_fixtures(rows):
    if not isinstance(rows, list):
        raise ValueError('Fixtures result is not a list')
    finished = [r for r in rows if str(r.get('event_status')).lower() == 'finished']
    stats = Counter(str(s.get('type')) for r in finished for s in r.get('statistics', []) if isinstance(s, dict))
    corners = []
    for r in finished:
        for s in r.get('statistics', []):
            if isinstance(s, dict) and 'corner' in str(s.get('type', '')).lower():
                corners.append(s)
    return {'fixtures': len(rows), 'finished': len(finished), 'statistic_names': dict(sorted(stats.items())),
            'corner_statistic_rows': len(corners),
            'corner_rows_with_both_integer_counts': sum(bool(re.fullmatch(r'\d+', str(s.get('home', ''))))
                and bool(re.fullmatch(r'\d+', str(s.get('away', '')))) for s in corners),
            'regulation_score_fields_present': sum(bool(r.get('event_ft_result')) for r in finished)}


def inspect_odds(payload):
    if not isinstance(payload, dict):
        return {'events': 0, 'market_names': [], 'bookmakers': []}
    markets, books = set(), set()
    for event in payload.values():
        if not isinstance(event, dict):
            continue
        for market, selections in event.items():
            markets.add(market)
            if isinstance(selections, dict):
                for offers in selections.values():
                    if isinstance(offers, dict):
                        books.update(offers)
    return {'events': len(payload), 'market_names': sorted(markets), 'bookmakers': sorted(books),
            'quote_timestamp_contract': 'not established by public FullOdds example; verify actual payload'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--league-id')
    args = parser.parse_args()
    _load_dotenv()
    key = os.getenv('ALLSPORTS_API_KEY', '').strip()
    if not key:
        parser.error('Set ALLSPORTS_API_KEY in the local environment or private .env')
    leagues = request('Leagues', key)
    summary = {'provider': 'AllSportsAPI', 'leagues': [
        {k: r.get(k) for k in ('league_key', 'league_name', 'country_name')}
        for r in leagues] if isinstance(leagues, list) else []}
    if args.league_id:
        today = datetime.now(timezone.utc).date()
        parameters = {'leagueId': args.league_id, 'timezone': 'UTC'}
        summary['recent_results'] = inspect_fixtures(request('Fixtures', key, **parameters,
            **{'from': (today - timedelta(days=14)).isoformat(), 'to': today.isoformat()}))
        summary['upcoming'] = inspect_fixtures(request('Fixtures', key, **parameters,
            **{'from': today.isoformat(), 'to': (today + timedelta(days=3)).isoformat()}))
        summary['historical_sample'] = inspect_fixtures(request('Fixtures', key, **parameters,
            **{'from': (today - timedelta(days=730)).isoformat(), 'to': (today - timedelta(days=716)).isoformat()}))
        summary['offers'] = inspect_odds(request('FullOdds', key, leagueId=args.league_id,
            **{'from': today.isoformat(), 'to': (today + timedelta(days=3)).isoformat()}))
    print(json.dumps(summary, indent=2, allow_nan=False))


if __name__ == '__main__':
    try:
        main()
    except ValueError as exc:
        # request() constructs sanitized errors; avoid printing a traceback.
        raise SystemExit(str(exc)) from None
