"""CC0 season files for background history and provisional fixture discovery.

The files do not specify UTC offsets or promise live updates. Their date is a
storage anchor, never a fabricated kickoff; no file row can settle a wager or
become an upcoming selection without a timed provider observation.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from .base import (CacheEntry, FixturesResult, HttpTransport, ParseError,
                   ProviderError, ProviderNotAvailableError, ProviderQuota, SourceTier)
from .calendar import normalise_fixture, team_identity

BASE_URL = 'https://raw.githubusercontent.com/openfootball/football.json/master'
# Deliberately restricted to inspected league-season filenames; no URL supplied
# by an admin, fixture, or downloaded payload is ever followed by this adapter.
FILES = {
    'soccer_epl': ('en.1.json', 'Premier League'),
    'soccer_england_championship': ('en.2.json', 'Championship'),
    'soccer_germany_bundesliga': ('de.1.json', 'Bundesliga'),
    'soccer_spain_la_liga': ('es.1.json', 'Primera'),
    'soccer_italy_serie_a': ('it.1.json', 'Serie A'),
    'soccer_france_ligue_one': ('fr.1.json', 'Ligue 1'),
    'soccer_netherlands_eredivisie': ('nl.1.json', 'Eredivisie'),
    'soccer_portugal_primeira_liga': ('pt.1.json', 'Primeira'),
}
MAX_FILE_BYTES = 2_000_000
PARSER_VERSION = 1


def score_pair(value):
    if not isinstance(value, list) or len(value) != 2 or any(
            type(v) is not int or not 0 <= v <= 30 for v in value):
        raise ParseError('openfootball: invalid score pair', provider='openfootball')
    return value


class OpenFootballProvider:
    name = 'openfootball'
    tier = SourceTier.COMMUNITY

    def __init__(self, *, transport=None, cache_sec=86400, clock=None):
        if type(cache_sec) is not int or not 21600 <= cache_sec <= 604800:
            raise ValueError('Openfootball cache must be between six hours and seven days')
        self.cache_sec = cache_sec
        self._transport = transport or HttpTransport(timeout=10, max_retries=1,
            time_budget=20, max_response_bytes=MAX_FILE_BYTES)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.storage = None
        self.purpose = 'ordinary'

    def is_available(self):
        return True

    def leagues(self):
        return tuple(FILES)

    def remaining_quota(self):
        return ProviderQuota(0, 0, tier=self.tier)

    def season_year(self):
        now = self.clock()
        return now.year if now.month >= 7 else now.year - 1

    def _file(self, sport_key, year):
        if sport_key not in FILES or type(year) is not int or not 2010 <= year <= self.clock().year:
            raise ProviderNotAvailableError('openfootball: unsupported league or season', provider=self.name)
        return f'{year}-{str(year + 1)[-2:]}/{FILES[sport_key][0]}'

    def parse(self, payload, sport_key, year):
        season = f'{year}/{str(year + 1)[-2:]}'
        if (not isinstance(payload, dict) or not isinstance(payload.get('matches'), list)
                or not 1 <= len(payload['matches']) <= 1000
                or not isinstance(payload.get('name'), str)
                or season not in payload['name']
                or FILES[sport_key][1].casefold() not in payload['name'].casefold()):
            raise ParseError('openfootball: invalid league-season document', provider=self.name)
        rows = {}
        for raw in payload['matches']:
            try:
                if not isinstance(raw, dict):
                    raise ValueError()
                day = datetime.strptime(raw['date'], '%Y-%m-%d').replace(tzinfo=timezone.utc)
                if not datetime(year, 7, 1, tzinfo=timezone.utc) <= day < datetime(year + 1, 9, 1, tzinfo=timezone.utc):
                    raise ValueError()
                home, away, round_name = raw['team1'], raw['team2'], raw['round']
                if any(not isinstance(v, str) or not v.strip() or len(v) > 160
                       or any(ord(c) < 32 for c in v) for v in (home, away, round_name)):
                    raise ValueError()
                if team_identity(home) == team_identity(away):
                    raise ValueError()
                source_time = raw.get('time')
                if source_time is not None:
                    if not isinstance(source_time, str):
                        raise ValueError()
                    datetime.strptime(source_time, '%H:%M')
                full = half = None
                if 'score' in raw:
                    value = raw['score']
                    if isinstance(value, dict):
                        full = score_pair(value['ft'])
                        half = score_pair(value['ht']) if 'ht' in value else None
                    else:
                        full = score_pair(value)
                    if half and any(h > f for h, f in zip(half, full)):
                        raise ValueError()
                    # A future score is a corrupt observation, not a result to
                    # accept now and accidentally leak into tomorrow's model.
                    if day.date() > self.clock().astimezone(timezone.utc).date():
                        raise ValueError()
                identity = hashlib.sha256(json.dumps([sport_key, year, round_name,
                    team_identity(home), team_identity(away)], separators=(',', ':')).encode()).hexdigest()
                row = normalise_fixture(provider=self.name, sport_key=sport_key,
                    match_id=identity, kickoff_epoch=day.timestamp(), home=home, away=away,
                    home_score=full[0] if full else None, away_score=full[1] if full else None,
                    status='FINISHED' if full else 'SCHEDULED', season=str(year))
                row.update(match_date=day.date().isoformat(), kickoff_time_known=False,
                    source_local_time=source_time, discovery_only=True, settlement_eligible=False,
                    result_available_after=(day + timedelta(days=1, hours=12)).isoformat(),
                    source_license='CC0-1.0', source_file=self._file(sport_key, year))
                if half:
                    row.update(home_first_half_score=half[0], away_first_half_score=half[1])
                if identity in rows and rows[identity] != row:
                    raise ValueError()
                rows[identity] = row
            except (KeyError, TypeError, ValueError) as exc:
                raise ParseError('openfootball: malformed or future-dated result', provider=self.name) from exc
        return tuple(rows.values())

    def get_season(self, sport_key, year=None, *, purpose='history'):
        year = self.season_year() if year is None else year
        path = self._file(sport_key, year)
        url = f'{BASE_URL}/{path}'
        key = f'openfootball:file:{path}'
        now = self.clock().astimezone(timezone.utc)
        ttl = self.cache_sec if year == self.season_year() else 30 * 86400
        saved = self.storage.get_telemetry(key) if self.storage else None
        if isinstance(saved, dict) and saved.get('parser_version') == PARSER_VERSION:
            checked = saved.get('checked_at', 0)
            try:
                rows = self.parse(saved['payload'], sport_key, year)
                encoded = json.dumps(saved['payload'], separators=(',', ':'), allow_nan=False).encode()
                if len(encoded) > MAX_FILE_BYTES:
                    raise ValueError('Oversized cache')
                if type(checked) in (int, float) and 0 <= now.timestamp() - checked < ttl:
                    return FixturesResult(self.name, sport_key, rows)
                if type(checked) in (int, float):
                    self._transport.cache.put(url, CacheEntry(encoded, saved.get('etag'),
                        saved.get('last_modified'), checked))
            except (ProviderError, KeyError, TypeError, ValueError):
                pass  # Invalid saved documents are repaired by collection.
        retry_key = key + ':retry'
        retry = self.storage.get_telemetry(retry_key) if self.storage else None
        if isinstance(retry, dict) and retry.get('retry_after', 0) > now.timestamp():
            raise ProviderNotAvailableError('openfootball: file collection is in retry cooldown', provider=self.name)
        try:
            payload = self._transport.get_json(url, ttl=ttl, provider=self.name)
            if len(json.dumps(payload, separators=(',', ':')).encode()) > MAX_FILE_BYTES:
                raise ParseError('openfootball: season file exceeds size limit', provider=self.name)
            rows = self.parse(payload, sport_key, year)
            if self.storage:
                entry = self._transport.cache.get(url)
                encoded = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
                self.storage.set_telemetry(key, {'parser_version': PARSER_VERSION, 'payload': payload,
                    'checked_at': now.timestamp(), 'content_hash': hashlib.sha256(encoded).hexdigest(),
                    'etag': entry.etag if entry else None,
                    'last_modified': entry.last_modified if entry else None,
                    'rows': len(rows), 'license': 'CC0-1.0',
                    'freshness': 'File retrieval is not proof of upstream match updates.'})
                self.storage.set_telemetry(retry_key, {})
            return FixturesResult(self.name, sport_key, rows)
        except ProviderError as exc:
            if self.storage:
                self.storage.set_telemetry(retry_key, {'retry_after': now.timestamp() + 86400,
                    'error_type': type(exc).__name__})
            raise

    def get_fixtures(self, sport_key):
        # Bootstrap a useful sample even early in a season. The completed file
        # is durable and refreshed only monthly; older history belongs to the
        # independent resumable job. One missing file never erases another.
        rows, warnings = [], []
        for year in (self.season_year(), self.season_year() - 1):
            try:
                rows.extend(self.get_season(sport_key, year).fixtures)
            except ProviderError as exc:
                warnings.append(f'{self.name}: season {year} unavailable ({type(exc).__name__})')
        if not rows:
            raise ProviderNotAvailableError('openfootball: no usable season files', provider=self.name)
        return FixturesResult(self.name, sport_key, tuple(rows), tuple(warnings))
