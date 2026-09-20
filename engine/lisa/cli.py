"""CLI entry points.

  python -m lisa demo          # full cycle + settlement on bundled fixtures
  python -m lisa run-cycle     # one ingestion pass (needs LISA_ODDS_API_KEY)
  python -m lisa settle        # grade pending ledger rows (needs key or --fixtures)
  python -m lisa run           # scheduler loop (cron-friendly: --once)
  python -m lisa report        # weekly live-validation summary (needs --metrics trail)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta

from . import __version__
from . import config as cfg
from .client import FixtureClient, OddsApiClient
from .fixtures import ODDS_PAYLOADS, SCORES_PAYLOADS
from .notify import LogNotifier
from .odds import utcnow
from .pipeline import CycleReport, Pipeline
from .scheduler import Scheduler
from .settle import SettlementReport, run_settlement
from .storage import InMemoryStorage
from .tracker import Tracker


def _make_storage(settings: cfg.Settings):
    driver = settings.storage_driver
    if driver == "redis":
        from .storage import RedisStorage
        return RedisStorage(settings.redis_url)
    if driver == "postgres":
        from .storage import PostgresStorage
        store = PostgresStorage(settings.database_url)
        store.ensure_schema()
        return store
    return InMemoryStorage()


def _make_client(settings: cfg.Settings, fixtures: bool):
    if fixtures:
        return FixtureClient(ODDS_PAYLOADS, SCORES_PAYLOADS)
    if not settings.odds_api_key:
        raise SystemExit("LISA_ODDS_API_KEY is not set (or pass --fixtures)")
    return OddsApiClient(settings.odds_api_key)


def _report_dict(report: CycleReport) -> dict:
    return {
        "sport": report.sport_key,
        "matches_seen": report.matches_seen,
        "matches_refined": report.matches_refined,
        "picks_emitted": report.picks_emitted,
        "suppressed": report.suppressed,
        "errors": report.errors,
    }


def _settlement_dict(rep: SettlementReport) -> dict:
    return {
        "pending": rep.pending,
        "settled": rep.settled,
        "won": rep.won,
        "lost": rep.lost,
        "void": rep.void,
        "skipped_no_scores": rep.skipped_no_scores,
        "skipped_not_due": rep.skipped_not_due,
        "errors": rep.errors,
    }


def _cmd_demo(args: argparse.Namespace) -> int:
    settings = cfg.Settings()  # deterministic defaults, not env
    client = FixtureClient(ODDS_PAYLOADS, SCORES_PAYLOADS)
    storage = InMemoryStorage()
    pipeline = Pipeline(client, storage, settings, notifier=LogNotifier())

    reports = pipeline.run_cycle()
    summary = {"cycles": [_report_dict(r) for r in reports]}

    settlement = run_settlement(client, storage, settings)
    summary["settlement"] = _settlement_dict(settlement)

    print(json.dumps(summary, indent=2))
    return 0


def _cmd_run_cycle(args: argparse.Namespace) -> int:
    settings = cfg.load_settings()
    client = _make_client(settings, args.fixtures)
    pipeline = Pipeline(client, _make_storage(settings), settings,
                        notifier=LogNotifier())
    sports = args.sports or None
    reports = pipeline.run_cycle(sports)
    print(json.dumps({"cycles": [_report_dict(r) for r in reports]}, indent=2))
    return 0


def _cmd_settle(args: argparse.Namespace) -> int:
    settings = cfg.load_settings()
    client = _make_client(settings, args.fixtures)
    storage = _make_storage(settings)
    settlement = run_settlement(client, storage, settings)
    print(json.dumps({"settlement": _settlement_dict(settlement)}, indent=2))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    settings = cfg.load_settings()
    client = _make_client(settings, args.fixtures)
    storage = _make_storage(settings)
    tracker = Tracker(args.metrics or settings.metrics_path, storage)
    scheduler = Scheduler(client, storage, settings, tracker=tracker)
    if args.once:
        scheduler.run_forever(max_ticks=1)
    else:
        stop_at = None
        if args.duration and args.duration > 0:
            stop_at = utcnow() + timedelta(hours=args.duration)
        scheduler.run_forever(stop_at=stop_at)
    print(json.dumps({
        "stats": {
            "ticks": scheduler.stats.ticks,
            "cycles_run": scheduler.stats.cycles_run,
            "settlements_run": scheduler.stats.settlements_run,
            "cycles_skipped": scheduler.stats.cycles_skipped,
            "errors": scheduler.stats.total_errors,
        },
        "next_cycle_at": scheduler.next_cycle_at.isoformat() if scheduler.next_cycle_at else None,
        "next_settle_at": scheduler.next_settle_at.isoformat() if scheduler.next_settle_at else None,
    }, indent=2))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    settings = cfg.load_settings()
    tracker = Tracker(args.metrics or settings.metrics_path)
    print(json.dumps(tracker.summarize(), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lisa", description=f"LISA data-refinery engine v{__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("demo", help="run a cycle + settlement on bundled fixtures")

    run = sub.add_parser("run-cycle", help="one ingestion/refinement pass")
    run.add_argument("--sports", nargs="*", default=None,
                     help="sport keys (default: all configured sports)")
    run.add_argument("--fixtures", action="store_true",
                     help="use bundled fixture data instead of the live API")

    settle = sub.add_parser("settle", help="grade pending ledger rows")
    settle.add_argument("--fixtures", action="store_true",
                        help="use bundled fixture data instead of the live API")

    run = sub.add_parser("run", help="run the scheduler loop (adapted cadence)")
    run.add_argument("--duration", type=float, default=None,
                     help="stop after N hours (default: run forever)")
    run.add_argument("--once", action="store_true",
                     help="run a single tick and exit (cron-friendly)")
    run.add_argument("--metrics", default=None,
                     help="JSONL trail for the validation summary")
    run.add_argument("--fixtures", action="store_true",
                     help="use bundled fixture data instead of the live API")

    report = sub.add_parser("report", help="weekly live-validation summary")
    report.add_argument("--metrics", default=None,
                        help="JSONL trail to aggregate")

    args = parser.parse_args(argv)

    if args.cmd == "demo":
        return _cmd_demo(args)
    if args.cmd == "run-cycle":
        return _cmd_run_cycle(args)
    if args.cmd == "settle":
        return _cmd_settle(args)
    if args.cmd == "run":
        return _cmd_run(args)
    if args.cmd == "report":
        return _cmd_report(args)
    parser.error(f"unknown command: {args.cmd}")
    return 2  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())