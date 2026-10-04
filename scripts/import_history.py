#!/usr/bin/env python3
"""Import football-data.co.uk match CSVs; never generate historical recommendations.

Usage: PYTHONPATH=engine python scripts/import_history.py engine/lisa/historical/*.csv
Team names are preserved exactly. Cross-provider aliases need explicit review;
this importer never guesses a club identity from a similar name.
"""
import argparse
import csv
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from lisa.cli import _make_storage
from lisa.config import load_settings
from lisa.match_history import HistoryRepository
from lisa.providers.calendar import normalise_fixture

LEAGUES = {'E0': 'soccer_epl', 'D1': 'soccer_germany_bundesliga',
           'I1': 'soccer_italy_serie_a', 'SP1': 'soccer_spain_la_liga', 'F1': 'soccer_france_ligue_one'}


def read_csv(path):
    rows = []
    with Path(path).open(encoding='utf-8-sig', newline='') as handle:
        for number, row in enumerate(csv.DictReader(handle), 2):
            if not row.get('HomeTeam'):
                continue
            try:
                date = row['Date']
                fmt = '%d/%m/%Y' if len(date.split('/')[-1]) == 4 else '%d/%m/%y'
                kickoff = datetime.strptime(date + ' ' + (row.get('Time') or '12:00'), fmt + ' %H:%M')
                # Archive documentation uses UK local times. Unknown times are
                # explicitly marked so evaluation can avoid same-day leakage.
                kickoff = kickoff.replace(tzinfo=ZoneInfo('Europe/London')).astimezone(timezone.utc)
                league = LEAGUES[row['Div']]
                identity = hashlib.sha256(f'{league}|{date}|{row["HomeTeam"]}|{row["AwayTeam"]}'.encode()).hexdigest()
                result = normalise_fixture(provider='football_data_csv', sport_key=league,
                    match_id=identity, kickoff_epoch=kickoff.timestamp(),
                    home=row['HomeTeam'], away=row['AwayTeam'],
                    home_score=int(row['FTHG']), away_score=int(row['FTAG']), status='FINISHED')
                result['kickoff_time_known'] = bool(row.get('Time'))
                result['archive_file'] = Path(path).name
                result['home_corners'] = int(row['HC']) if row.get('HC', '').strip() else None
                result['away_corners'] = int(row['AC']) if row.get('AC', '').strip() else None
                import math
                result['archive_odds'] = {}
                for market, names in {'h2h': {'Home': 'B365H', 'Draw': 'B365D', 'Away': 'B365A'},
                                      'totals_2_5': {'Over': 'B365>2.5', 'Under': 'B365<2.5'}}.items():
                    prices = {}
                    for side, column in names.items():
                        try:
                            value = float(row.get(column, ''))
                            if math.isfinite(value) and value > 1:
                                prices[side] = value
                        except ValueError:
                            pass
                    result['archive_odds'][market] = prices
                rows.append(result)
            except (ValueError, KeyError) as exc:
                raise ValueError(f'{path}:{number}: malformed historical match') from exc
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='+')
    args = parser.parse_args()
    # Parse everything before opening a write transaction; malformed archives
    # cannot cause a partly parsed file to be imported.
    rows = [row for path in args.files for row in read_csv(path)]
    store = _make_storage(load_settings())
    try:
        n = HistoryRepository(store).ingest(rows)
        print(f'Imported {n} source-attributed match observations; repeated imports are idempotent.')
    finally:
        if hasattr(store, 'close'):
            store.close()


if __name__ == '__main__':
    main()
