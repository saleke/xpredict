"""Run independently against the same SQLite/PostgreSQL database as LISA."""
import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import threading

from ..config import load_settings
from ..cli import _make_storage
from .service import MAX_BYTES, ScalperService
from .sources import ESPN_LEAGUES, JsonSource


def main(argv=None):
    parser = argparse.ArgumentParser(description='Scalper durable sports-data supplier')
    parser.add_argument('command', choices=('once', 'run', 'status', 'ingest', 'browser-once', 'browser-run'))
    parser.add_argument('--database', help='Explicit local SQLite .db file; otherwise use LISA storage settings')
    parser.add_argument('--leagues', help='Comma-separated canonical league keys')
    parser.add_argument('--no-espn', action='store_true')
    parser.add_argument('--no-openfootball', action='store_true', help='Disable CC0 bulk history bootstrap')
    parser.add_argument('--sportybet', action='store_true', help='Try the optional undocumented Nigeria odds adapter')
    parser.add_argument('--feed', nargs=2, action='append', default=[], metavar=('NAME', 'HTTPS_URL'),
                        help='Authorized normalized JSON feed; URLs are never printed')
    parser.add_argument('--interval', type=float, default=60, help='Seconds between bounded collection cycles')
    parser.add_argument('--request-limit', type=int, default=24)
    parser.add_argument('--summary-limit', type=int, default=4)
    parser.add_argument('--history-days', type=int, default=90)
    parser.add_argument('--cycle-seconds', type=float, default=45)
    parser.add_argument('--file', help='Version-1 interchange JSON to ingest')
    parser.add_argument('--source', help='Distinct source name for interchange ingestion')
    parser.add_argument('--browser-bookmaker', choices=('sportybet', 'pinnacle'), action='append',
                        help='Verified public browser adapter; repeat for both (default: both)')
    parser.add_argument('--browser-executable', help='Local Chrome/Chromium path; otherwise discover Chrome or use Playwright Chromium')
    parser.add_argument('--browser-proxy', help='Explicit normal browser egress proxy without embedded credentials')
    parser.add_argument('--headed', action='store_true', help='Show the local browser during collection')
    parser.add_argument('--capture-seconds', type=float, default=30, help='Browser capture budget per bookmaker (5–45 seconds)')
    args = parser.parse_args(argv)
    settings = load_settings()
    if args.database:
        if not args.database.endswith('.db'):
            parser.error('--database must be an explicit SQLite .db file')
        settings = replace(settings, storage_driver='sqlite', database_url=args.database)
    if not 15 <= args.interval <= 86400:
        parser.error('--interval must be 15–86400 seconds')
    leagues = tuple(k.strip() for k in args.leagues.split(',') if k.strip()) if args.leagues else settings.board_leagues or tuple(ESPN_LEAGUES)
    if settings.storage_driver == 'postgres' and os.getenv('LISA_SCALPER_PRIVATE_SCHEMA', '').lower() in ('1', 'true', 'yes'):
        from ..storage import PostgresStorage
        store = PostgresStorage(settings.database_url, serverless=True)
    else:
        store = _make_storage(settings)
    try:
        if args.command.startswith('browser-'):
            from .browser import BrowserCollector, BrowserService
            stop = threading.Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *unused: stop.set())
            async def collect():
                collector = BrowserCollector(executable=args.browser_executable, headed=args.headed,
                                             proxy=args.browser_proxy)
                service = BrowserService(store, bookmakers=tuple(args.browser_bookmaker or ('sportybet', 'pinnacle')),
                    leagues=leagues, collector=collector, capture_seconds=args.capture_seconds,
                    quote_max_age=settings.scalper_quote_max_age_sec)
                try:
                    while not stop.is_set():
                        result = await service.tick()
                        print(json.dumps(result), flush=True)
                        if args.command == 'browser-once':
                            return 0 if any(r['state'] == 'updated' for r in result['outcomes']) else 1
                        await asyncio.to_thread(stop.wait, args.interval)
                    return 0
                finally:
                    await collector.close()
            return asyncio.run(collect())
        service = ScalperService(store, leagues=leagues, espn=not args.no_espn,
            openfootball=not args.no_openfootball, sportybet=args.sportybet,
            feeds=[JsonSource(name, url) for name, url in args.feed], request_limit=args.request_limit,
            summary_limit=args.summary_limit, history_days=args.history_days, cycle_seconds=args.cycle_seconds)
        if args.command == 'status':
            print(json.dumps(service.repository.health(now=datetime.now(timezone.utc)), indent=2))
            return 0
        if args.command == 'ingest':
            if not args.file or not args.source:
                parser.error('ingest requires --file and --source')
            path = Path(args.file)
            with path.open('rb') as stream:
                data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                parser.error('Interchange file exceeds size limit')
            print(json.dumps(service.ingest(json.loads(data), args.source), indent=2))
            return 0
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *unused: stop.set())
        while not stop.is_set():
            status = service.tick()
            print(json.dumps(status), flush=True)
            if args.command == 'once':
                return 1 if status['state'] == 'failed' else 0
            stop.wait(args.interval)
        return 0
    finally:
        store.close()


if __name__ == '__main__':
    raise SystemExit(main())
