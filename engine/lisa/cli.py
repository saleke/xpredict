"""CLI entry points.

  python -m lisa demo          # full cycle + settlement on bundled fixtures
  python -m lisa run-cycle     # one ingestion pass (needs LISA_ODDS_API_KEY)
  python -m lisa settle        # grade pending ledger rows (needs key or --fixtures)
"""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from . import config as cfg
from .client import FixtureClient, OddsApiClient
from .fixtures import ODDS_PAYLOADS, SCORES_PAYLOADS
from .notify import LogNotifier
from .pipeline import CycleReport, Pipeline
from .settle import SettlementReport, run_settlement
from .storage import InMemoryStorage


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

    args = parser.parse_args(argv)

    if args.cmd == "demo":
        return _cmd_demo(args)
    if args.cmd == "run-cycle":
        return _cmd_run_cycle(args)
    if args.cmd == "settle":
        return _cmd_settle(args)
    parser.error(f"unknown command: {args.cmd}")
    return 2  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())