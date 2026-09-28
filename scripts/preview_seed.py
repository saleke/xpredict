"""Seed a scratch SQLite DB with live odds payloads from the bundled fixtures.

This is a *preview* tool only: it stores fixture payloads under the same
`live:odds:` keys the pipeline uses so `python -m lisa serve` /api/forecast
has something real to render. No API credits are consumed.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lisa.dashboard import live_odds_key
from lisa.fixtures import ODDS_PAYLOADS
from lisa.storage import SqliteStorage

DB = "/tmp/opencode/lisa-preview.db"
if os.path.exists(DB):
    os.remove(DB)

storage = SqliteStorage(DB)
ttl = int(48 * 3600)  # match the 48h horizon TTL
for sport, payload in ODDS_PAYLOADS.items():
    storage.upsert_live(
        live_odds_key(sport),
        {
            "sport_key": sport,
            "observed_at": time.time(),
            "credits_remaining": None,
            "payload": payload,
        },
        ttl_seconds=ttl,
    )

keys = list(storage.scan_live_keys())
print(f"seeded {len(keys)} live keys into {DB}")
for k in sorted(keys):
    print("  ", k)