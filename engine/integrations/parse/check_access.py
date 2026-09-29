#!/usr/bin/env python3
"""Report what this account can actually call on Parse, and prove it.

    uv run python check_access.py              # status of both sources
    uv run python check_access.py --betexplorer  # + live sample from the working one
    uv run python check_access.py --n=3        # sample N days back

Subscription and endpoint calls return the same opaque 403, so this probes each
stage separately and names the exact unblocking step.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

BASE = "https://api.parse.bot"
LISTING_ID = "b0c4782f-8591-441b-92ee-6cf4b190317b"          # sportybet-com-ng-api
CANONICAL_ID = "8e652912-d760-4522-85ce-071e539a9c12"         # sportybet-com-ng-api
BETEXPLORER_ID = "5daf70d1-eaad-46ba-afbb-c57c0d5ebc3e"       # betexplorer-com-api
VERIFY_URL = "https://parse.bot/settings?tab=account#verification"


def api_key() -> str | None:
    """Prefer the env var; fall back to the CLI's saved credentials."""
    if key := os.environ.get("PARSE_API_KEY"):
        return key
    path = pathlib.Path.home() / ".config/parse/credentials"
    if not path.exists():
        return None
    raw = path.read_text().strip()
    try:
        data = json.loads(raw)
        return data.get("api_key") or data.get("key") or raw
    except json.JSONDecodeError:
        return raw.split("=", 1)[-1].strip().strip("\"'")


def call(method: str, url: str, key: str, payload: dict | None = None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"X-API-Key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return exc.code, {}
    except Exception as exc:  # network, DNS, timeout
        return "ERR", {"error": str(exc)[:200]}


def code_of(body) -> str:
    """Pull the machine-readable error code out of either error envelope."""
    if not isinstance(body, dict):
        return "non-dict response"
    err = body.get("error")
    if isinstance(err, dict):
        return str(err.get("code", ""))
    if isinstance(err, str):
        return err
    detail = body.get("detail")
    if isinstance(detail, dict):
        return str(detail.get("code", ""))
    return ""


def sample_betexplorer(key: str, days: int) -> None:
    """Prove the working source with a live call and report what came back."""
    day = datetime.date.today() - datetime.timedelta(days=days)
    status, body = call(
        "GET",
        f"{BASE}/scraper/{BETEXPLORER_ID}/search_matches?date={day.isoformat()}",
        key,
    )
    if status != 200:
        print(f"  {day}  HTTP {status}  {code_of(body)}")
        return
    matches = (body.get("data") or {}).get("matches") or []
    scored = [m for m in matches if m.get("score")]
    priced = [m for m in matches if m.get("odds_home")]
    print(f"  {day}  HTTP 200  matches={len(matches)}  "
          f"leagues={len({m.get('league') for m in matches})}  "
          f"countries={len({m.get('country') for m in matches})}  "
          f"scored={len(scored)}  priced={len(priced)}")
    for m in scored[:3]:
        print(f"      {m.get('league', '?')[:26]:26} {m.get('home_team', '?')[:20]:20} "
              f"{m.get('score'):>7} {m.get('away_team', '?')[:20]:20} "
              f"H={m.get('odds_home')} D={m.get('odds_draw')} A={m.get('odds_away')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--betexplorer", action="store_true",
                    help="make a live call to the working source")
    ap.add_argument("--n", type=int, default=1,
                    help="days back for the --betexplorer sample (default 1)")
    args = ap.parse_args()

    key = api_key()
    if not key:
        print("FAIL  no API key")
        print("      export PARSE_API_KEY=pmx_...  or:  uv run parse login --no-web")
        return 1
    print(f"key   {key[:8]}...{key[-4:]}  (len {len(key)})\n")

    # Stage 1 - is the key itself accepted by the API at all?
    status, _ = call("GET", f"{BASE}/dispatch/tasks", key)
    ok_auth = status == 200
    print(f"[1] auth          {'OK  ' if ok_auth else 'FAIL'}  HTTP {status}")
    if not ok_auth:
        print("      key not accepted; re-run: uv run parse login --no-web")
        return 1

    # Stage 2 - the source that needs no verification.
    print("[2] betexplorer   (access_requirements: [])")
    if args.betexplorer:
        for back in range(0, max(1, args.n) + 1):
            sample_betexplorer(key, back)
    else:
        sample_betexplorer(key, 1)
        if args.n <= 1:
            print("      pass --n=N to sample more days back")

    # Stage 3 - the source gated on account verification.
    print("\n[3] sportybet-ng  (access_requirements: [phone, card])")
    status, body = call("POST", f"{BASE}/marketplace/apis/{LISTING_ID}/subscribe", key)
    scraper_id = body.get("scraper_id") if isinstance(body, dict) else None
    if status == 200 and scraper_id:
        print(f"      subscribed  scraper_id={scraper_id}")
        return 0
    print(f"      FAIL  HTTP {status}  {code_of(body)}")
    if code_of(body) == "verification_required":
        missing = (body.get("detail") or {}).get("requirements") or []
        print(f"      outstanding: {', '.join(missing) or 'unknown'}")
        print(f"      unblock: {VERIFY_URL}")
    return 0 if ok_auth else 1


if __name__ == "__main__":
    sys.exit(main())
