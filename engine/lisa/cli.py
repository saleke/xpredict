"""CLI entry points.

  python -m lisa demo          # full cycle + settlement on bundled fixtures
  python -m lisa run-cycle     # one ingestion pass (needs THE_ODDS_API_KEY)
  python -m lisa settle        # grade pending ledger rows (needs key or --fixtures)
  python -m lisa run           # scheduler loop (cron-friendly: --once)
  python -m lisa report        # weekly live-validation summary (needs --metrics trail)
  python -m lisa calibrate     # calibration metrics (Brier, ECE) on settled picks
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

from . import __version__
from . import config as cfg
from .client import FixtureClient, OddsApiClient
from .fixtures import FIXTURE_SPORTS, ODDS_PAYLOADS, SCORES_PAYLOADS
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
        raise SystemExit("THE_ODDS_API_KEY is not set (or pass --fixtures)")
    return OddsApiClient(settings.odds_api_key, base_url=settings.api_base_url)


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
    settings = cfg.Settings(sports=FIXTURE_SPORTS)
    client = FixtureClient(ODDS_PAYLOADS, SCORES_PAYLOADS)
    storage = InMemoryStorage()
    pipeline = Pipeline(client, storage, settings, notifier=LogNotifier())

    reports = pipeline.run_cycle()
    summary = {"cycles": [_report_dict(r) for r in reports]}

    settlement = run_settlement(client, storage, settings)
    summary["settlement"] = _settlement_dict(settlement)

    print(json.dumps(summary, indent=2))
    return 0


def _make_notifier(settings: cfg.Settings):
    notifiers = [LogNotifier()]
    if settings.telegram_token and settings.telegram_chat_id:
        from .notify import TelegramNotifier
        notifiers.append(TelegramNotifier(settings.telegram_token, settings.telegram_chat_id))
    if settings.discord_webhook_url:
        from .notify import DiscordNotifier
        notifiers.append(DiscordNotifier(settings.discord_webhook_url))
    if len(notifiers) == 1:
        return notifiers[0]
    from .notify import CompositeNotifier
    return CompositeNotifier(notifiers)


def _cmd_run_cycle(args: argparse.Namespace) -> int:
    settings = cfg.load_settings()
    client = _make_client(settings, args.fixtures)
    pipeline = Pipeline(client, _make_storage(settings), settings,
                        notifier=_make_notifier(settings))
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
    notifier = _make_notifier(settings)
    tracker = Tracker(args.metrics or settings.metrics_path, storage)
    scheduler = Scheduler(client, storage, settings, notifier=notifier, tracker=tracker)
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


def _cmd_calibrate(args: argparse.Namespace) -> int:
    from .calibration import (
        evaluate_calibration, evaluate_by_market, format_calibration_report,
        compute_clv_metrics, evaluate_clv_by_market
    )

    records: list[dict] = []
    settings = cfg.load_settings()

    if args.fixtures:
        settings = cfg.Settings(sports=FIXTURE_SPORTS)
        client = FixtureClient(ODDS_PAYLOADS, SCORES_PAYLOADS)
        storage = InMemoryStorage()
        pipeline = Pipeline(client, storage, settings, notifier=LogNotifier())
        pipeline.run_cycle()
        run_settlement(client, storage, settings)
        records = storage.list_settled_picks()
    else:
        metrics_file = args.metrics or settings.metrics_path
        if metrics_file and Path(metrics_file).exists():
            with Path(metrics_file).open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if rec.get("kind") == "settled_pick":
                        records.append(rec)
        if not records:
            storage = _make_storage(settings)
            records = storage.list_settled_picks()

    if args.market:
        records = [r for r in records if r.get("market") == args.market]

    if args.json:
        if args.by_market:
            cal_reports = evaluate_by_market(records)
            clv_reports = evaluate_clv_by_market(records)
            payload = {}
            for m in cal_reports:
                payload[m] = {
                    "calibration": cal_reports[m].to_dict(),
                    "clv": clv_reports.get(m, compute_clv_metrics([])).to_dict(),
                }
            print(json.dumps(payload, indent=2))
        else:
            rep = evaluate_calibration(records)
            clv_rep = compute_clv_metrics(records)
            print(json.dumps({
                "calibration": rep.to_dict(),
                "clv": clv_rep.to_dict(),
            }, indent=2))
        return 0

    if args.by_market:
        cal_reports = evaluate_by_market(records)
        clv_reports = evaluate_clv_by_market(records)
        for m, rep in cal_reports.items():
            print(format_calibration_report(
                rep, clv_report=clv_reports.get(m),
                title=f"LISA Calibration & CLV — Market: {m}"))
            print()
    else:
        title = f"LISA Calibration & CLV — Market: {args.market}" if args.market else "LISA Global Calibration & CLV Report"
        rep = evaluate_calibration(records)
        clv_rep = compute_clv_metrics(records)
        print(format_calibration_report(rep, clv_report=clv_rep, title=title))

    return 0


def _cmd_export_web(args: argparse.Namespace) -> int:
    import urllib.parse
    settings = cfg.load_settings()
    client = _make_client(settings, args.fixtures)
    storage = _make_storage(settings)
    pipeline = Pipeline(client, storage, settings, notifier=LogNotifier())

    # Populate live picks and settlement
    pipeline.run_cycle(now=utcnow())
    run_settlement(client, storage, settings)

    pending = storage.list_pending_picks()
    settled = storage.list_settled_picks()

    from .calibration import evaluate_calibration, compute_clv_metrics

    cal_rep = evaluate_calibration(settled).to_dict() if settled else None
    clv_rep = compute_clv_metrics(settled).to_dict() if settled else None

    active_picks_data = []
    for p in pending:
        exec_book = p.get("best_book") or "pinnacle"
        odds_val = float(p.get("best_odds") or p.get("fair_odds", 1.0))
        fair_val = float(p.get("fair_odds", 1.0))
        ev_val = float(p.get("best_ev") or 0.0)

        if odds_val > fair_val:
            freshness = "FRESH"
            badge_color = "emerald"
            gauge_text = f"Optimal Entry (+{ev_val*100:.1f}% EV)"
        elif abs(odds_val - fair_val) < 0.01:
            freshness = "FAIR"
            badge_color = "amber"
            gauge_text = "Fair Value Entry"
        else:
            freshness = "SLIPPED"
            badge_color = "rose"
            gauge_text = "Decayed / Slippage"

        deep_links = {
            "pinnacle": f"https://www.pinnacle.com/en/search/{urllib.parse.quote(str(p['home_team']))}",
            "bet365": f"https://www.bet365.com/#/AX/K^{urllib.parse.quote(str(p['home_team']))}/",
            "draftkings": f"https://sportsbook.draftkings.com/search?q={urllib.parse.quote(str(p['home_team']))}",
        }

        active_picks_data.append({
            "dedupe_key": p.get("dedupe_key"),
            "match_id": p.get("match_id"),
            "sport_key": p.get("sport_key"),
            "home_team": p.get("home_team"),
            "away_team": p.get("away_team"),
            "commence_time": p.get("commence_time"),
            "market": p.get("market"),
            "outcome_name": p.get("outcome_name"),
            "line": p.get("line"),
            "p_true": p.get("p_true"),
            "fair_odds": fair_val,
            "best_book": exec_book,
            "best_odds": odds_val,
            "best_ev": ev_val,
            "conviction_score": p.get("conviction_score", 0.0),
            "recommended_stake_pct": p.get("recommended_stake_pct", 0.0),
            "recommended_units": p.get("recommended_units", 0.0),
            "freshness": freshness,
            "badge_color": badge_color,
            "gauge_text": gauge_text,
            "deep_links": deep_links,
        })

    payload = {
        "meta": {
            "generated_at": utcnow().isoformat(),
            "version": __version__,
            "total_sports": len(settings.sports),
            "scope_leagues": list(settings.sports),
        },
        "summary": {
            "active_picks_count": len(active_picks_data),
            "settled_picks_count": len(settled),
            "win_rate": cal_rep.get("win_rate") if cal_rep else None,
            "brier_score": cal_rep.get("brier_score") if cal_rep else None,
            "ece": cal_rep.get("ece") if cal_rep else None,
            "mean_clv": clv_rep.get("mean_clv") if clv_rep else None,
            "positive_clv_share": clv_rep.get("positive_clv_share") if clv_rep else None,
        },
        "active_picks": active_picks_data,
        "settled_ledger": settled,
        "calibration": cal_rep,
        "clv": clv_rep,
    }

    out_path = Path(args.out or "web/data/dashboard.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"[export-web] Dashboard data exported to {out_path} ({len(active_picks_data)} active, {len(settled)} settled)")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import http.server
    import socketserver
    import os

    web_dir = Path(args.dir or "web").resolve()
    port = int(args.port or 8080)
    if not web_dir.exists():
        print(f"[serve] Error: Directory {web_dir} does not exist.")
        return 1

    os.chdir(web_dir)
    class ReusableTCPServer(socketserver.TCPServer):
        allow_reuse_address = True

    handler = http.server.SimpleHTTPRequestHandler
    with ReusableTCPServer(("", port), handler) as httpd:
        print(f"[serve] LISA Dashboard running at http://localhost:{port}/ (serving {web_dir})")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[serve] Server stopped.")
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

    cal = sub.add_parser("calibrate", help="measure calibration (Brier score, ECE) on settled picks")
    cal.add_argument("--metrics", default=None,
                     help="JSONL trail to aggregate")
    cal.add_argument("--market", default=None,
                     help="filter to specific market (e.g. h2h, totals, spreads)")
    cal.add_argument("--by-market", action="store_true",
                     help="break down calibration per market")
    cal.add_argument("--fixtures", action="store_true",
                     help="evaluate on bundled fixture matches")
    cal.add_argument("--json", action="store_true",
                     help="output JSON instead of formatted text table")

    exp = sub.add_parser("export-web", help="export live consensus and settled metrics to web dashboard JSON")
    exp.add_argument("--out", default="web/data/dashboard.json",
                     help="output JSON path (default: web/data/dashboard.json)")
    exp.add_argument("--fixtures", action="store_true",
                     help="use bundled fixture data instead of live API")

    srv = sub.add_parser("serve", help="launch local HTTP server for the web dashboard")
    srv.add_argument("--port", type=int, default=8080,
                     help="port number (default: 8080)")
    srv.add_argument("--dir", default="web",
                     help="directory to serve (default: web)")

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
    if args.cmd == "calibrate":
        return _cmd_calibrate(args)
    if args.cmd == "export-web":
        return _cmd_export_web(args)
    if args.cmd == "serve":
        return _cmd_serve(args)
    parser.error(f"unknown command: {args.cmd}")
    return 2  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())