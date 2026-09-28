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


def _cmd_tune(args: argparse.Namespace) -> int:
    from .tuning import DEFAULT_LEAGUES, format_tuning, tune_subsets

    thresholds: Optional[tuple[float, ...]] = None
    if args.thresholds:
        thresholds = tuple(
            float(t.strip()) for t in args.thresholds.split(",") if t.strip()
        )

    leagues: Optional[tuple[tuple[str, ...], ...]] = None
    if args.leagues:
        keys = tuple(k.strip() for k in args.leagues.split(",") if k.strip())
        leagues = (keys,) if keys else DEFAULT_LEAGUES

    report = tune_subsets(thresholds=thresholds, league_options=leagues)

    if args.export_json:
        out_path = Path(args.export_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _write_report_json_streaming_generic(report, out_path, "grid")
        print(f"[tune] Report exported to {out_path}")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_tuning(report))

    return 0


def _cmd_study(args: argparse.Namespace) -> int:
    from .study import MarketStudyEngine, format_study

    report = MarketStudyEngine().run()

    if args.export_json:
        out_path = Path(args.export_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2))
        print(f"[study] Report exported to {out_path}")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_study(report))

    return 0


def _cmd_forecast(args: argparse.Namespace) -> int:
    from .bulletin import build_bulletin, format_bulletin

    bulletin = build_bulletin(
        mode="archive",
        min_matches=args.min_matches,
        max_matches=args.limit,
    )
    if getattr(args, "top", False):
        matches = [r for r in bulletin["matches"] if r["marquee"] or r["is_top_pick"]]
        bulletin = {**bulletin, "matches": matches, "count": len(matches)}

    if args.json:
        print(json.dumps(bulletin, indent=2))
    else:
        print(format_bulletin(bulletin))
    return 0


def _cmd_tiers(args: argparse.Namespace) -> int:
    from .tiers import format_tiers, upgrade_path

    if args.json:
        print(json.dumps(upgrade_path(), indent=2))
    else:
        print(format_tiers())
    return 0


def _cmd_export_forecast(args: argparse.Namespace) -> int:
    """Print the live forecast board and tier matrix to stdout.

    The dashboard is served from the API, so nothing is written to web/ any
    more: a static file would inevitably go stale and would show fixtures the
    odds feed no longer lists.
    """
    from .bulletin import build_bulletin, build_live_bulletin_from_payloads
    from .dashboard import LIVE_ODDS_PREFIX
    from .odds import utcnow
    from .storage import SqliteStorage

    settings = cfg.load_settings()
    storage = SqliteStorage(args.db or settings.database_url or "data/lisa.db")
    payloads: list[tuple[str, list]] = []
    for key in storage.scan_live_keys():
        if key.startswith(LIVE_ODDS_PREFIX):
            entry = storage.get_live(key)
            if isinstance(entry, dict) and entry.get("payload"):
                payloads.append((str(entry.get("sport_key") or key), entry["payload"]))

    bulletin = (
        build_live_bulletin_from_payloads(payloads, now=utcnow())
        if payloads else build_bulletin()
    )
    print(json.dumps(bulletin, indent=2))
    return 0


def _cmd_backtest(args: argparse.Namespace) -> int:
    from .backtest import BacktestEngine, format_backtest_report
    sports = args.sports.split(",") if args.sports else None
    engine = BacktestEngine(sports=sports)
    report = engine.run_simulation()

    if args.export_json:
        out_path = Path(args.export_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Stream the ledger in batches: each records batch is serialized and
        # written, then cleared from memory before the next batch is processed,
        # so the full 7k+ record audit never lives in memory (or as one giant
        # JSON string) at once.
        _write_report_json_streaming(report, out_path)
        print(f"[backtest] Audit report exported to {out_path}")

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(format_backtest_report(report, verbose=args.verbose))

    return 0


def _write_report_json_streaming(report, out_path: Path, batch_size: int = 500) -> None:
    """Write a BacktestReport JSON incrementally, batch-by-batch.

    The report dict is split at ``records``; every other key is written first,
    then the record array is emitted in batches of ``batch_size`` records. Each
    batch is serialized to a string, flushed to disk, and dropped before the
    next batch is built — bounding peak memory on large archives.
    """
    import json as _json

    payload = report.to_dict()
    records = payload.pop("records", [])

    with out_path.open("w", encoding="utf-8") as fh:
        fh.write("{\n")
        keys = list(payload.keys())
        for i, key in enumerate(keys):
            fh.write(_json.dumps({key: payload[key]}, separators=(",", ":"))[1:-1])
            fh.write(",\n" if i < len(keys) - 1 or records else "\n")
        if records:
            fh.write('"records": [')
            wrote_any = False
            while records:
                batch, records = records[:batch_size], records[batch_size:]
                if wrote_any:
                    fh.write(",")
                fh.write(_json.dumps(batch, separators=(",", ":"))[1:-1])
                wrote_any = True
                del batch  # release the batch
            fh.write("]\n")
        fh.write("}\n")


def _cmd_walk_forward(args: argparse.Namespace) -> int:
    from .walkforward import format_walk_forward, walk_forward

    league_filter = None
    if args.sports:
        league_filter = {s.strip() for s in args.sports.split(",") if s.strip()}
    report = walk_forward(
        league_filter=league_filter,
        min_edge=args.min_edge,
        min_prob=args.min_prob,
    )

    if args.export_json:
        out_path = Path(args.export_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _write_report_json_streaming_generic(
            report, out_path, ("model_value_bets", "market_follower_bets"),
        )
        print(f"[walkforward] Report exported to {out_path}")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_walk_forward(report))

    return 0


def _write_report_json_streaming_generic(payload: dict, out_path: Path,
                                         large_keys, batch_size: int = 500) -> None:
    """Variant of ``_write_report_json_streaming`` for plain-dict reports.

    Each key named in ``large_keys`` may grow large (e.g. the walk-forward
    per-bet ledgers) and is streamed in cleared batches like the backtest
    records. Other keys are written verbatim.
    """
    import json as _json

    if isinstance(large_keys, str):
        large_keys = (large_keys,)
    large = {key: payload.pop(key, None) for key in large_keys}
    large_present = {key: value for key, value in large.items() if value is not None}

    with out_path.open("w", encoding="utf-8") as fh:
        fh.write("{\n")
        keys = list(payload.keys())
        n_scalar = len(keys)
        for i, key in enumerate(keys):
            fh.write(_json.dumps({key: payload[key]}, separators=(",", ":"))[1:-1])
            fh.write(",\n")
        for j, (key, records) in enumerate(large_present.items()):
            fh.write(f'"{key}": [')
            wrote_any = False
            while records:
                batch, records = records[:batch_size], records[batch_size:]
                if wrote_any:
                    fh.write(",")
                fh.write(_json.dumps(batch, separators=(",", ":"))[1:-1])
                wrote_any = True
                del batch
            fh.write("]")
            if j < len(large_present) - 1:
                fh.write(",")
            fh.write("\n")
        fh.write("}\n")


def _cmd_serve(args: argparse.Namespace) -> int:
    from .auth import AuthManager
    from .server import make_production_server

    web_dir = Path(args.dir or "web").resolve()
    port = int(args.port or 8080)
    if not web_dir.exists():
        print(f"[serve] Error: Directory {web_dir} does not exist.")
        return 1

    settings = cfg.load_settings()
    storage = _make_storage(settings)

    # `serve` is the dashboard-only entry point: no poller, so the console can
    # read state and edit settings but there is no scheduler to drive.
    from .control import Control
    from .runtime import RuntimeConfig
    runtime = RuntimeConfig(settings, storage=storage)
    control = Control(storage, auth=AuthManager(storage=storage), runtime=runtime)

    bot_inst = None
    if getattr(args, "bot", False) and settings.telegram_token:
        import threading
        import time
        from .telegram_bot import TelegramBot

        bot_inst = TelegramBot(
            token=settings.telegram_token,
            channel_chat_id=settings.telegram_chat_id,
            tier2_channel_chat_id=settings.tier2_telegram_chat_id,
            storage=storage,
        )
        def _bot_loop():
            print(f"[telegram-bot] Background polling daemon started for @{settings.telegram_bot_username or 'bot'}")
            while True:
                had_updates = False
                try:
                    updates = bot_inst.poll_updates()
                    if updates:
                        had_updates = True
                    for u in updates:
                        reply = bot_inst.process_one_update(u)
                        print(f"[telegram-bot] [{u.username} -> {u.text}]: {reply[:60]}...")
                except Exception:
                    pass
                time.sleep(0.05 if had_updates else 0.25)

        t = threading.Thread(target=_bot_loop, daemon=True)
        t.start()

    control.attach(bot=bot_inst)

    server = make_production_server(
        host="0.0.0.0",
        port=port,
        web_dir=str(web_dir),
        storage=storage,
        settings=runtime.settings(),
        bot=bot_inst,
        auth=control.auth,
        control=control,
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
    from .auth import AuthManager
    from .server import make_production_server
    from .storage import SqliteStorage
    from .telegram_bot import TelegramBot

    settings = cfg.load_settings()
    port = int(getattr(args, "port", 8080) or 8080)
    web_dir = Path(getattr(args, "dir", "web") or "web").resolve()

    db_path = settings.database_url if settings.database_url and settings.database_url.endswith(".db") else "data/lisa.db"
    storage = SqliteStorage(db_path)

    # One handle on the live process, shared by the HTTP server, the bot and the
    # poller, so the admin console inspects and controls the objects that are
    # actually running rather than a second set of them.
    from .control import Control
    from .runtime import RuntimeConfig
    runtime = RuntimeConfig(settings, storage=storage)
    if runtime.overrides():
        print(f"[start] Applying {len(runtime.overrides())} persisted setting "
              f"override(s) from a previous console session.")
    settings = runtime.settings()
    control = Control(storage, auth=AuthManager(storage=storage), runtime=runtime)

    # The operational ledger starts empty on purpose.
    #
    # It used to be seeded from the backtest archive when the picks table was
    # empty, which mixed historical simulation rows into the live record and
    # invented the fields the archive does not have (a fixed book count, a
    # fabricated dispersion, and a closing line derived by multiplying the best
    # odds by 0.96). Accuracy and CLV on the dashboard would then have been
    # fiction presented as results. Historical analysis stays in the archive
    # and the backtest report; live picks are only ever rows this service
    # actually emitted and settled.
    counts = storage.count_picks()
    print(f"[start] Operational ledger: {counts['total']} pick(s) on record "
          f"({counts['pending']} pending, {counts['settled']} settled).")
    if counts["total"] == 0:
        print("[start] Ledger is empty, which is correct for a fresh install: "
              "picks appear here only after live ingestion emits and settles them.")

    bot_inst = None
    if not getattr(args, "no_bot", False) and settings.telegram_token:
        bot_inst = TelegramBot(
            token=settings.telegram_token,
            channel_chat_id=settings.telegram_chat_id,
            tier2_channel_chat_id=settings.tier2_telegram_chat_id,
            storage=storage,
        )
        def _bot_loop():
            print(f"[telegram-bot] Production Gatekeeper daemon running for @{settings.telegram_bot_username or 'bot'}")
            while True:
                had_updates = False
                try:
                    updates = bot_inst.poll_updates()
                    if updates:
                        had_updates = True
                    for u in updates:
                        reply = bot_inst.process_one_update(u)
                        print(f"[telegram-bot] [{u.username} -> {u.text}]: {reply[:60]}...")
                except Exception:
                    pass
                time.sleep(0.05 if had_updates else 0.25)
        t_bot = threading.Thread(target=_bot_loop, daemon=True)
        t_bot.start()

    # Start the live odds poller when an API key is configured.
    #
    # This uses the adaptive Scheduler rather than a fixed sleep, and a rotating
    # client that spreads requests across every configured key under a shared
    # quota floor and per-key daily budget. The Ledger (not the process) stays
    # the source of truth, so a restart resumes cleanly.
    api_keys = tuple(getattr(settings, "odds_api_keys", ()) or ())
    if not getattr(args, "no_ingest", False) and api_keys:
        from .key_pool import RotatingOddsClient
        from .scheduler import Scheduler
        client = RotatingOddsClient(
            api_keys,
            base_url=settings.api_base_url,
            budget_daily=settings.credit_budget_daily,
            credit_warn=settings.credit_warn,
            credit_stop=settings.credit_stop,
        )
        # `runtime` is passed as a provider, not a snapshot: the scheduler asks it
        # for the current settings each time it needs one, which is what makes a
        # console change take effect on the next tick with no restart.
        scheduler = Scheduler(
            client=client,
            storage=storage,
            settings=runtime.settings,
            notifier=_make_notifier(settings),
        )
        control.attach(scheduler=scheduler, client=client, bot=bot_inst)
        labels = ", ".join(s.label() for s in client.pool._states)
        print(f"[live-ingest] Rotating client online with {len(api_keys)} key(s): {labels}")
        print(f"[live-ingest] Per-key daily budget: {settings.credit_budget_daily} request(s)")

        def _ingest_loop():
            print("[live-ingest] Adaptive odds poller active.")
            while True:
                try:
                    scheduler.tick()
                except Exception as exc:
                    # Never let one bad cycle kill the thread; the next tick
                    # re-derives cadence from the ledger.
                    print(f"[live-ingest] Cycle error: {exc!r}")
                    time.sleep(min(60, settings.cadence_idle_sec))

        t_ingest = threading.Thread(target=_ingest_loop, daemon=True)
        t_ingest.start()
    else:
        print("[live-ingest] Live odds poller on standby (awaiting API key or offline mode).")

    server = make_production_server(
        host="0.0.0.0",
        port=port,
        web_dir=str(web_dir),
        storage=storage,
        settings=runtime.settings(),
        bot=bot_inst,
        auth=control.auth,
        control=control,
    )

    print(f"[start] Admin console: http://0.0.0.0:{port}/admin.html")
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

    wft = sub.add_parser(
        "walkforward",
        help="honest chronological walk-forward: gated market vs independent Elo/Poisson model vs baselines",
    )
    wft.add_argument("--sports", default=None,
                     help="comma-separated sport keys to evaluate (default: all archived football leagues)")

    tune_sub = sub.add_parser(
        "tune",
        help="strategy tuning on the real archive: threshold x league sweep, per-season splits, Kelly risk sim",
    )
    tune_sub.add_argument("--thresholds", default=None,
                          help="comma-separated gate thresholds (default: 0.75,0.78,0.80,0.82,0.85)")
    tune_sub.add_argument("--leagues", default=None,
                          help="comma-separated sport keys to restrict tuning to (default: all + each league + EPL&Ligue1)")
    tune_sub.add_argument("--export-json", default=None,
                          help="path to save the full tuning report JSON")
    tune_sub.add_argument("--json", action="store_true",
                          help="output raw JSON instead of formatted report")
    wft.add_argument("--min-edge", type=float, default=0.0,
                     help="minimum model EV edge before a value bet fires (default: 0.0)")
    wft.add_argument("--min-prob", type=float, default=0.30,
                     help="minimum model probability before a value bet fires (default: 0.30)")
    wft.add_argument("--export-json", default=None,
                     help="path to save walk-forward report JSON")
    wft.add_argument("--json", action="store_true",
                     help="output raw JSON instead of formatted report")

    st = sub.add_parser(
        "study",
        help="market-efficiency study across all archived markets: 1X2, Asian Handicap, O/U 2.5, BTTS & correct-score analytics",
    )
    st.add_argument("--export-json", default=None,
                    help="path to save the full study report JSON")
    st.add_argument("--json", action="store_true",
                    help="output raw JSON instead of formatted report")

    fo = sub.add_parser(
        "forecast",
        help="daily match forecast board: 10+ probability forecasts per matchday with honest uncertainty flags",
    )
    fo.add_argument("--json", action="store_true",
                    help="output raw JSON instead of formatted board")
    fo.add_argument("--top", action="store_true",
                    help="show only popular fixtures and the pick of the day")
    fo.add_argument("--limit", type=int, default=40,
                    help="max fixtures on the board (default: 40)")
    fo.add_argument("--min-matches", type=int, default=10,
                    help="minimum fixtures before widening to adjacent day(s) (default: 10)")

    tr = sub.add_parser(
        "tiers",
        help="show the subscription tier value ladder, reveal timing and why to upgrade",
    )
    tr.add_argument("--json", action="store_true",
                    help="output raw JSON instead of formatted matrix")

    ef = sub.add_parser(
        "export-forecast",
        help="print the live forecast board as JSON (served from the live cache)",
    )
    ef.add_argument("--db", default="", help="ledger/live-cache database path")

    tg = sub.add_parser("telegram-bot", help="run interactive Telegram bot for pick reveals, stats & unlocks")
    tg.add_argument("--token", default="", help="Telegram Bot API token")
    tg.add_argument("--chat-id", default="", help="Target chat or channel ID")
    tg.add_argument("--mock", action="store_true", help="run in local simulation/mock mode without network calls")
    tg.add_argument("--interactive", "-i", action="store_true", help="run interactive terminal console for testing bot & admin commands")
    tg.add_argument("--user-id", default="", help="simulate updates as this Telegram user ID")
    tg.add_argument("--poll-once", action="store_true", help="poll updates once and exit")

    ing = sub.add_parser("live-ingest", help="run real-time odds ingestion and automated alert dispatch")
    ing.add_argument("--fixtures", action="store_true", help="use fixture payloads instead of live API")
    ing.add_argument("--once", action="store_true", help="run single cycle and exit")
    ing.add_argument("--interval", type=int, default=None,
                     help="polling interval in seconds (default: the prematch cadence; "
                          "each pass spends one credit per league per region)")
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
    if args.cmd == "serve":
        return _cmd_serve(args)
    if args.cmd == "backtest":
        return _cmd_backtest(args)
    if args.cmd == "walkforward":
        return _cmd_walk_forward(args)
    if args.cmd == "tune":
        return _cmd_tune(args)
    if args.cmd == "study":
        return _cmd_study(args)
    if args.cmd == "forecast":
        return _cmd_forecast(args)
    if args.cmd == "tiers":
        return _cmd_tiers(args)
    if args.cmd == "export-forecast":
        return _cmd_export_forecast(args)
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
    from .telegram_bot import TelegramBot, TelegramUpdate
    settings = cfg.load_settings()
    token = args.token or settings.telegram_token
    chat_id = args.chat_id or settings.telegram_chat_id
    mock = args.mock or not bool(token)
    storage = _make_storage(settings)

    bot = TelegramBot(
        token=token,
        channel_chat_id=chat_id,
        tier2_channel_chat_id=settings.tier2_telegram_chat_id,
        mock=mock,
        storage=storage,
    )

    if not mock and token:
        try:
            import urllib.request
            req = urllib.request.Request(f"https://api.telegram.org/bot{token}/getMe")
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                me = json.loads(resp.read().decode("utf-8"))
                if me.get("ok"):
                    bot_user = me.get("result", {}).get("username", "")
                    print(f"[telegram-bot] Authenticated successfully with Telegram API as @{bot_user}.")
                    print(f"[telegram-bot] Admin whitelist: {sorted(list(bot.admin_telegram_ids))}")
        except Exception as exc:
            print(f"[telegram-bot] Warning: Live Telegram connection failed ({exc}).")
            print("[telegram-bot] Running in MOCK/SIMULATION mode so admin & user commands can be tested.")
            bot.mock = True
            mock = True
    else:
        print("[telegram-bot] Running in MOCK/SIMULATION mode (no network token required).")
        print(f"[telegram-bot] Admin whitelist: {sorted(list(bot.admin_telegram_ids))}")

    if args.poll_once:
        updates = bot.poll_updates()
        print(f"[telegram-bot] Polled {len(updates)} update(s).")
        for u in updates:
            reply = bot.process_one_update(u)
            print(f"[{u.username} -> {u.text}]: {reply[:60]}...")
        return 0

    if getattr(args, "interactive", False):
        # No implicit operator identity: an unset ADMIN_TELEGRAM_IDS must fail
        # loudly rather than silently acting as some hard-coded account.
        active_user_id = str(
            args.user_id
            or (sorted(bot.admin_telegram_ids)[0] if bot.admin_telegram_ids else "")
        ).strip()
        if not active_user_id:
            print(
                "Error: no operator identity configured for interactive mode.\n"
                "       Set ADMIN_TELEGRAM_IDS in .env, or pass --user-id <id>."
            )
            return 2
        is_admin_user = bot.is_admin(active_user_id)
        role = "ADMIN" if is_admin_user else "USER"
        print(f"\n[telegram-bot] Interactive Console active. Acting as {role} (ID: {active_user_id}).")
        print("Type any command (e.g. /admin, /picks, /bankroll, /settle, /sys_pause) or 'exit' to quit.\n")
        seq = 100
        while True:
            try:
                line = input(f"lisa-bot ({active_user_id})> ").strip()
                if not line:
                    continue
                if line.lower() in ("exit", "quit", "q"):
                    break
                seq += 1
                upd = TelegramUpdate(
                    update_id=seq,
                    message_id=seq,
                    chat_id=active_user_id,
                    user_id=active_user_id,
                    username="Admin" if is_admin_user else "Trader",
                    text=line,
                )
                resp = bot.process_one_update(upd)
                clean_resp = resp.replace("<b>", "").replace("</b>", "").replace("<code>", "`").replace("</code>", "`").replace("<i>", "").replace("</i>", "")
                print(f"\n{clean_resp}\n")
            except (KeyboardInterrupt, EOFError):
                break
        print("\n[telegram-bot] Console closed.")
        return 0

    print("[telegram-bot] Starting Telegram polling loop (Ctrl+C to stop)...")
    try:
        while True:
            had_updates = False
            updates = bot.poll_updates()
            if updates:
                had_updates = True
            for u in updates:
                reply = bot.process_one_update(u)
                print(f"[{u.username} -> {u.text}]: {reply[:60]}...")
            time.sleep(0.05 if had_updates else 0.25)
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

    daemon.run_daemon(
        max_iterations=args.iterations,
        interval_sec=int(args.interval) if args.interval else None,
    )
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