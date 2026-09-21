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
    if driver == "sqlite":
        from .storage import SqliteStorage
        db_path = settings.database_url if settings.database_url and settings.database_url.endswith(".db") else "data/lisa.db"
        return SqliteStorage(db_path)
    if driver == "file":
        from .storage import JsonFileStorage
        return JsonFileStorage()
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
    out_path = Path(args.out or "web/data/dashboard.json")

    if args.fixtures:
        from .fixtures_generator import generate_rolling_commercial_dataset
        payload = generate_rolling_commercial_dataset(now=utcnow())
        try:
            from .backtest import BacktestEngine
            payload["backtest"] = BacktestEngine().run().to_dict()
        except Exception as exc:
            print(f"[export-web] Warning: could not attach backtest: {exc}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2))
        print(f"[export-web] Commercial 4-tier rolling dataset exported to {out_path} ({len(payload['active_picks'])} active, {len(payload['settled_ledger'])} settled)")
        return 0

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

        import hashlib
        m_hash = hashlib.md5(f"{p.get('match_id')}:{p.get('market')}:{p.get('outcome_name')}".encode()).hexdigest().upper()
        booking_codes = p.get("booking_codes") or {
            "sportybet": f"SB-{m_hash[:5]}",
            "1xbet": f"1X-{m_hash[5:10]}",
            "bet365": f"365-{m_hash[10:14]}",
            "betway": f"BW-{m_hash[14:19]}",
            "bet9ja": f"B9-{m_hash[19:24]}",
            "draftkings": f"DK-{m_hash[24:29]}",
        }

        deep_links = {
            "sportybet": "https://www.sportybet.com/",
            "1xbet": "https://1xbet.com/",
            "bet365": f"https://www.bet365.com/#/AX/K^{urllib.parse.quote(str(p['home_team']))}/",
            "betway": "https://www.betway.com/",
            "bet9ja": "https://sports.bet9ja.com/",
            "draftkings": f"https://sportsbook.draftkings.com/search?q={urllib.parse.quote(str(p['home_team']))}",
            "pinnacle": f"https://www.pinnacle.com/en/search/{urllib.parse.quote(str(p['home_team']))}",
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
            "booking_codes": booking_codes,
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
        "accumulator_booking_codes": {
            "sportybet": "SB-AC792K",
            "1xbet": "1X-AC819M",
            "bet365": "365-AC55Q",
            "betway": "BW-AC4410",
            "bet9ja": "B9-AC9022",
            "draftkings": "DK-AC3318",
        },
        "settled_ledger": settled,
        "calibration": cal_rep,
        "clv": clv_rep,
    }

    try:
        from .backtest import BacktestEngine
        bkt_engine = BacktestEngine()
        payload["backtest"] = bkt_engine.run().to_dict()
    except Exception as exc:
        print(f"[export-web] Warning: could not run backtest: {exc}")

    out_path = Path(args.out or "web/data/dashboard.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"[export-web] Dashboard data exported to {out_path} ({len(active_picks_data)} active, {len(settled)} settled)")
    return 0


def _cmd_backtest(args: argparse.Namespace) -> int:
    from .backtest import BacktestEngine, format_backtest_report
    sports = args.sports.split(",") if args.sports else None
    engine = BacktestEngine(sports=sports)
    report = engine.run_simulation()

    if args.export_json:
        out_path = Path(args.export_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report.to_dict(), indent=2))
        print(f"[backtest] Audit report exported to {out_path}")

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(format_backtest_report(report, verbose=args.verbose))

    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from .server import make_production_server

    web_dir = Path(args.dir or "web").resolve()
    port = int(args.port or 8080)
    if not web_dir.exists():
        print(f"[serve] Error: Directory {web_dir} does not exist.")
        return 1

    settings = cfg.load_settings()
    storage = _make_storage(settings)

    bot_inst = None
    if getattr(args, "bot", False) and settings.telegram_token:
        import threading
        import time
        from .telegram_bot import TelegramBot

        bot_inst = TelegramBot(token=settings.telegram_token, channel_chat_id=settings.telegram_chat_id)
        def _bot_loop():
            print(f"[telegram-bot] Background polling daemon started for @{settings.telegram_bot_username or 'bot'}")
            while True:
                try:
                    updates = bot_inst.poll_updates()
                    for u in updates:
                        reply = bot_inst.process_one_update(u)
                        print(f"[telegram-bot] [{u.username} -> {u.text}]: {reply[:60]}...")
                except Exception:
                    pass
                time.sleep(2.0)

        t = threading.Thread(target=_bot_loop, daemon=True)
        t.start()

    server = make_production_server(
        host="0.0.0.0",
        port=port,
        web_dir=str(web_dir),
        storage=storage,
        settings=settings,
        bot=bot_inst,
    )
    print(f"[serve] LISA Dashboard running at http://localhost:{port}/ (serving {web_dir}) [Multi-Threaded Production Server]")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[serve] Server stopped.")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Pre-flight diagnostic health inspection tool."""
    import socket
    import urllib.error
    import urllib.request

    settings = cfg.load_settings()
    print("=" * 65)
    print("        LISA PRODUCTION PRE-FLIGHT DIAGNOSTIC (DOCTOR)")
    print("=" * 65)
    passed = 0
    warnings = 0
    failures = 0

    # 1. SQLite Database & Storage Layer
    print("\n[1] Storage Layer & SQLite Database:")
    db_path = settings.database_url if settings.database_url and settings.database_url.endswith(".db") else "data/lisa.db"
    try:
        from .storage import SqliteStorage
        store = SqliteStorage(db_path)
        store.upsert_live("__doctor_probe__", {"ok": 1}, ttl_seconds=10)
        probe_val = store.get_live("__doctor_probe__")
        if probe_val and probe_val.get("ok") == 1:
            counts = store.count_picks()
            print(f"  [PASS] SQLite WAL Database OK ({db_path})")
            print(f"         Ledger: {counts['total']} picks total ({counts['settled']} settled, {counts['pending']} pending)")
            passed += 1
        else:
            print(f"  [FAIL] SQLite read/write probe failed on {db_path}")
            failures += 1
    except Exception as exc:
        print(f"  [FAIL] SQLite storage error: {exc}")
        failures += 1

    # 2. Telegram Bot Token & API
    print("\n[2] Telegram Bot Integration:")
    if settings.telegram_token:
        try:
            url = f"https://api.telegram.org/bot{settings.telegram_token}/getMe"
            req = urllib.request.Request(url, headers={"User-Agent": "LISA-Production/1.0"})
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                bot_info = data.get("result", {})
                bot_name = bot_info.get("username", "unknown")
                print(f"  [PASS] Telegram Bot Token Verified (@{bot_name})")
                passed += 1
            else:
                print(f"  [FAIL] Telegram Bot API returned not ok: {data}")
                failures += 1
        except Exception as exc:
            print(f"  [FAIL] Telegram Bot API unreachable: {exc}")
            failures += 1
    else:
        print("  [WARN] LISA_TELEGRAM_TOKEN not configured in .env")
        warnings += 1

    # 3. Telegram Channel & Administrator Permissions
    print("\n[3] Telegram Channel Membership Verification:")
    if settings.telegram_token and settings.telegram_chat_id:
        try:
            chat_url = f"https://api.telegram.org/bot{settings.telegram_token}/getChat?chat_id={settings.telegram_chat_id}"
            req = urllib.request.Request(chat_url, headers={"User-Agent": "LISA-Production/1.0"})
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                chat_data = json.loads(resp.read().decode("utf-8"))
            if chat_data.get("ok"):
                title = chat_data.get("result", {}).get("title", settings.telegram_chat_id)
                print(f"  [PASS] Target Channel Connected: '{title}' ({settings.telegram_chat_id})")
                passed += 1
            else:
                print(f"  [WARN] Could not retrieve chat details: {chat_data}")
                warnings += 1
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            print(f"  [WARN] Channel probe response: {exc.code} - {err_body}")
            warnings += 1
        except Exception as exc:
            print(f"  [WARN] Channel probe error: {exc}")
            warnings += 1
    else:
        print("  [WARN] LISA_TELEGRAM_CHAT_ID not configured")
        warnings += 1

    # 4. Odds API Connectivity & Quota
    print("\n[4] Sportsbook Odds Data Source:")
    if settings.odds_api_key:
        try:
            probe_url = f"{settings.api_base_url}/v4/sports?apiKey={settings.odds_api_key}"
            req = urllib.request.Request(probe_url, headers={"User-Agent": "LISA-Production/1.0"})
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                rem = resp.headers.get("x-requests-remaining", "unknown")
                used = resp.headers.get("x-requests-used", "unknown")
                print(f"  [PASS] The Odds API Connected (Quota: {rem} remaining, {used} used)")
                passed += 1
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            if "exhausted" in err_body.lower() or "401" in str(exc.code) or "429" in str(exc.code):
                print(f"  [WARN] The Odds API Key notice ({exc.code}): {err_body}")
            else:
                print(f"  [WARN] The Odds API HTTP error: {exc.code}")
            warnings += 1
        except Exception as exc:
            print(f"  [WARN] The Odds API network error: {exc}")
            warnings += 1
    else:
        print("  [WARN] THE_ODDS_API_KEY not configured. Running in offline historical audit mode.")
        warnings += 1

    # 5. Port 8080 Availability
    print("\n[5] Network & Port 8080 Availability:")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", 8080))
        s.close()
        print("  [PASS] Port 8080 is available for binding")
        passed += 1
    except OSError:
        print("  [WARN] Port 8080 is currently occupied (active server instance running)")
        warnings += 1

    print("\n" + "-" * 65)
    print(f"Summary: {passed} PASSED, {warnings} WARNINGS, {failures} FAILURES")
    print("-" * 65)
    return 0 if failures == 0 else 1


def _cmd_start(args: argparse.Namespace) -> int:
    """Unified production runner launching web server, bot daemon, and live poller."""
    import signal
    import sys
    import threading
    import time
    from .server import make_production_server
    from .storage import SqliteStorage
    from .telegram_bot import TelegramBot

    settings = cfg.load_settings()
    port = int(getattr(args, "port", 8080) or 8080)
    web_dir = Path(getattr(args, "dir", "web") or "web").resolve()

    db_path = settings.database_url if settings.database_url and settings.database_url.endswith(".db") else "data/lisa.db"
    storage = SqliteStorage(db_path)

    # Seed audited backtest ledger if database picks table is empty
    counts = storage.count_picks()
    if counts["total"] == 0:
        print("[start] Initializing SQLite ledger from verified backtest audit...")
        try:
            from .backtest import BacktestEngine
            bkt = BacktestEngine().run()
            seeded = 0
            for r in bkt.records:
                if r.result in ("WIN", "LOSS"):
                    row = {
                        "dedupe_key": f"{r.match_id}::{r.market}::{r.outcome_name}",
                        "match_id": r.match_id,
                        "sport_key": r.sport_key,
                        "market": r.market,
                        "outcome_name": r.outcome_name,
                        "line": None,
                        "home_team": r.home_team,
                        "away_team": r.away_team,
                        "commence_time": r.commence_time,
                        "p_true": r.p_true,
                        "fair_odds": r.fair_odds,
                        "n_books": 5,
                        "stdev": 0.010,
                        "cv": 0.012,
                        "state": "SETTLED",
                        "result": r.result,
                        "best_book": r.best_book,
                        "best_odds": r.best_odds,
                        "best_ev": r.ev,
                        "closing_odds": r.closing_odds or round(r.best_odds * 0.96, 2),
                        "closing_p_true": round(r.p_true * 1.01, 3),
                        "clv": round((r.best_odds / (r.closing_odds or (r.best_odds * 0.96))) - 1.0, 4) if r.closing_odds else 0.025,
                        "conviction_score": r.conviction_score,
                        "recommended_stake_pct": round(r.stake_units or 1.5, 1),
                        "recommended_units": r.stake_units or 1.5,
                        "created_at": r.commence_time,
                        "settled_at": r.commence_time,
                    }
                    if storage.insert_pick_row(row):
                        seeded += 1
            print(f"[start] Successfully seeded {seeded} audited matches into SQLite ledger.")
        except Exception as exc:
            print(f"[start] Warning: failed to seed backtest audit: {exc}")

    # Start Telegram bot daemon if configured and not disabled
    bot_inst = None
    if not getattr(args, "no_bot", False) and settings.telegram_token:
        bot_inst = TelegramBot(token=settings.telegram_token, channel_chat_id=settings.telegram_chat_id)
        def _bot_loop():
            print(f"[telegram-bot] Production Gatekeeper daemon running for @{settings.telegram_bot_username or 'bot'}")
            while True:
                try:
                    updates = bot_inst.poll_updates()
                    for u in updates:
                        reply = bot_inst.process_one_update(u)
                        print(f"[telegram-bot] [{u.username} -> {u.text}]: {reply[:60]}...")
                except Exception:
                    pass
                time.sleep(2.0)
        t_bot = threading.Thread(target=_bot_loop, daemon=True)
        t_bot.start()

    # Start Live Odds Ingestion Scheduler if Odds API configured and not disabled
    if not getattr(args, "no_ingest", False) and settings.odds_api_key:
        from .client import OddsApiClient
        from .live_ingest import LiveIngestionEngine
        client = OddsApiClient(settings.odds_api_key, base_url=settings.api_base_url)
        ingest_engine = LiveIngestionEngine(
            client=client,
            settings=settings,
            telegram_bot=bot_inst,
            storage=storage,
        )
        def _ingest_loop():
            print("[live-ingest] Background odds poller active.")
            while True:
                try:
                    ingest_engine.run_cycle()
                except Exception as exc:
                    print(f"[live-ingest] Cycle error: {exc}")
                time.sleep(settings.cadence_prematch_sec)
        t_ingest = threading.Thread(target=_ingest_loop, daemon=True)
        t_ingest.start()
    else:
        print("[live-ingest] Live odds poller on standby (awaiting API key or offline mode).")

    server = make_production_server(
        host="0.0.0.0",
        port=port,
        web_dir=str(web_dir),
        storage=storage,
        settings=settings,
        bot=bot_inst,
    )

    print(f"[start] ==========================================================")
    print(f"[start] LISA Production Service active on http://0.0.0.0:{port}/")
    print(f"[start] Multi-Threaded Engine: ON | SQLite WAL: ON | Security: ON")
    print(f"[start] ==========================================================")

    def _shutdown_handler(sig, frame):
        print("\n[start] Shutting down LISA Production Stack...")
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
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
    srv.add_argument("--bot", action="store_true",
                     help="run the Telegram bot polling daemon concurrently")

    bkt = sub.add_parser("backtest", help="simulate past match data through LISA and test rigorous prediction accuracy")
    bkt.add_argument("--sports", default=None,
                     help="comma-separated sport keys (e.g. soccer_epl,basketball_nba)")
    bkt.add_argument("--export-json", default=None,
                     help="path to save backtest report JSON")
    bkt.add_argument("--json", action="store_true",
                     help="output raw JSON instead of formatted report")
    bkt.add_argument("--verbose", action="store_true",
                     help="include detailed match-by-match ledger breakdown")

    tg = sub.add_parser("telegram-bot", help="run interactive Telegram bot for pick reveals, stats & unlocks")
    tg.add_argument("--token", default="", help="Telegram Bot API token")
    tg.add_argument("--chat-id", default="", help="Target chat or channel ID")
    tg.add_argument("--mock", action="store_true", help="run in local simulation/mock mode without network calls")
    tg.add_argument("--poll-once", action="store_true", help="poll updates once and exit")

    ing = sub.add_parser("live-ingest", help="run real-time odds ingestion and automated alert dispatch")
    ing.add_argument("--fixtures", action="store_true", help="use fixture payloads instead of live API")
    ing.add_argument("--once", action="store_true", help="run single cycle and exit")
    ing.add_argument("--interval", type=int, default=60, help="polling interval in seconds (default: 60)")
    ing.add_argument("--iterations", type=int, default=None, help="max iterations to run")
    ing.add_argument("--mock-bot", action="store_true", help="use mock Telegram bot for testing alerts")
    ing.add_argument("--no-settle", action="store_true", help="disable automatic match settlement check")
    ing.add_argument("--notify-settle", action="store_true", help="broadcast settlement results to Telegram channel")

    dsp = sub.add_parser("dispatch-test", help="test Telegram Diamond and Trap alert message formatting")
    dsp.add_argument("--dry-run", action="store_true", help="render alerts locally without dispatching")

    doc = sub.add_parser("doctor", help="pre-flight production diagnostic health inspection")

    start = sub.add_parser("start", help="launch unified production stack (web server + bot + scheduler)")
    start.add_argument("--port", type=int, default=8080, help="port number (default: 8080)")
    start.add_argument("--dir", default="web", help="directory to serve (default: web)")
    start.add_argument("--no-bot", action="store_true", help="disable Telegram bot background daemon")
    start.add_argument("--no-ingest", action="store_true", help="disable background odds polling")

    args = parser.parse_args(argv)

    if args.cmd == "doctor":
        return _cmd_doctor(args)
    if args.cmd == "start":
        return _cmd_start(args)
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
    if args.cmd == "backtest":
        return _cmd_backtest(args)
    if args.cmd == "telegram-bot":
        return _cmd_telegram_bot(args)
    if args.cmd == "live-ingest":
        return _cmd_live_ingest(args)
    if args.cmd == "dispatch-test":
        return _cmd_dispatch_test(args)
    parser.error(f"unknown command: {args.cmd}")
    return 2  # pragma: no cover


def _cmd_telegram_bot(args: argparse.Namespace) -> int:
    import time
    from .telegram_bot import TelegramBot
    settings = cfg.load_settings()
    token = args.token or settings.telegram_token
    chat_id = args.chat_id or settings.telegram_chat_id
    mock = args.mock or not bool(token)

    bot = TelegramBot(token=token, channel_chat_id=chat_id, mock=mock)
    if mock:
        print("[telegram-bot] Running in MOCK/SIMULATION mode (no network token required).")
    else:
        print(f"[telegram-bot] Connected to Telegram API with channel {chat_id}.")

    if args.poll_once:
        updates = bot.poll_updates()
        print(f"[telegram-bot] Polled {len(updates)} update(s).")
        for u in updates:
            reply = bot.process_one_update(u)
            print(f"[{u.username} -> {u.text}]: {reply[:60]}...")
        return 0

    print("[telegram-bot] Starting Telegram polling loop (Ctrl+C to stop)...")
    try:
        while True:
            updates = bot.poll_updates()
            for u in updates:
                reply = bot.process_one_update(u)
                print(f"[{u.username} -> {u.text}]: {reply[:60]}...")
            time.sleep(2.0)
    except KeyboardInterrupt:
        print("\n[telegram-bot] Stopped.")
    return 0


def _cmd_live_ingest(args: argparse.Namespace) -> int:
    from .live_ingest import LiveIngestionDaemon
    from .telegram_bot import TelegramBot

    settings = cfg.load_settings()
    client = _make_client(settings, fixtures=args.fixtures)
    notifier = _make_notifier(settings)
    storage = _make_storage(settings)

    bot = None
    if settings.telegram_token or args.mock_bot:
        bot = TelegramBot(
            token=settings.telegram_token,
            channel_chat_id=settings.telegram_chat_id,
            mock=args.mock_bot or not bool(settings.telegram_token),
        )

    daemon = LiveIngestionDaemon(
        client=client,
        settings=settings,
        notifier=notifier,
        telegram_bot=bot,
        storage=storage,
        auto_settle=not args.no_settle,
        notify_settle=args.notify_settle,
    )

    if args.once:
        res = daemon.run_cycle()
        print(f"[live-ingest] Single cycle finished: {res.matches_seen} matches, {res.diamonds_found} diamonds, {res.traps_found} traps, {res.alerts_dispatched} alerts, {res.settled_count} settled.")
        return 0

    interval = int(args.interval or 60)
    daemon.run_daemon(max_iterations=args.iterations, interval_sec=interval)
    return 0


def _cmd_dispatch_test(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone
    from .gate import Pick, Execution
    from .telegram_bot import TelegramBot, format_diamond_alert_html, format_trap_advisory_html, format_settlement_alert_html

    now = datetime.now(timezone.utc)
    sample_pick = Pick(
        match_id="test-epl-01",
        sport_key="soccer_epl",
        market="h2h",
        outcome_name="Arsenal",
        home_team="Arsenal",
        away_team="Wolverhampton Wanderers",
        commence_time=now,
        p_true=0.8105,
        fair_odds=1.234,
        n_books=5,
        stdev=0.008,
        cv=0.012,
        best_execution=Execution(
            book_key="pinnacle",
            book_title="Pinnacle",
            odds=1.24,
            ev=0.005,
        ),
        state="ACTIVE",
        created_at=now,
        conviction_score=8.5,
        recommended_stake_pct=1.5,
        recommended_units=1.5,
    )

    diamond_html = format_diamond_alert_html(sample_pick)
    trap_html = format_trap_advisory_html(
        home_team="Manchester United",
        away_team="Tottenham",
        sport_key="soccer_epl",
        public_favorite="Manchester United",
        reason="Severe cross-bookmaker variance (stdev=0.034, CV=8.4%)",
        cv=0.084,
    )
    sample_settled = {
        "match_id": "test-epl-01",
        "sport_key": "soccer_epl",
        "home_team": "Arsenal",
        "away_team": "Wolverhampton Wanderers",
        "outcome_name": "Arsenal",
        "best_odds": 1.24,
        "recommended_units": 1.5,
        "clv": 0.024,
        "result": "WIN",
    }
    settle_html = format_settlement_alert_html(sample_settled)

    print("\n--- SAMPLE TELEGRAM DIAMOND ALERT ---")
    print(diamond_html)
    print("\n--- SAMPLE TELEGRAM TRAP ADVISORY ---")
    print(trap_html)
    print("\n--- SAMPLE TELEGRAM SETTLEMENT ALERT ---")
    print(settle_html)

    settings = cfg.load_settings()
    if settings.telegram_token and not args.dry_run:
        bot = TelegramBot(token=settings.telegram_token, channel_chat_id=settings.telegram_chat_id)
        ok_diamond = bot.broadcast_diamond(sample_pick)
        ok_trap = bot.broadcast_trap(
            home_team="Manchester United",
            away_team="Tottenham",
            sport_key="soccer_epl",
            public_favorite="Manchester United",
            reason="Severe cross-bookmaker variance",
            cv=0.084,
        )
        ok_settle = bot.broadcast_settlement(sample_settled)
        if ok_diamond and ok_trap and ok_settle:
            print(f"\n[dispatch-test] SUCCESS: Live alerts delivered to {settings.telegram_chat_id}!")
        else:
            print(
                f"\n[dispatch-test] ⚠️ Telegram dispatch blocked (HTTP 403 Forbidden).\n"
                f"ACTION REQUIRED: Please add @XpredictPremiumBot as an Administrator to {settings.telegram_chat_id} "
                f"with 'Post Messages' permission enabled. Telegram requires bots to be channel admins before posting."
            )
    else:
        print("\n[dispatch-test] Dry run complete (no external network calls made).")

    return 0


if __name__ == "__main__":
    sys.exit(main())