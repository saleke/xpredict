"""Strict observations; absence, period and source time are never inferred."""
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re

from ..providers.base import ParseError
from ..providers.calendar import LEAGUES, normalise_fixture, team_identity

MAX_ROWS = 2000
MAX_QUOTES = 20000
STATISTICS = {
    'corners': 100, 'yellow_cards': 30, 'red_cards': 15, 'fouls': 150,
    'throw_ins': 250, 'shots': 150, 'shots_on_target': 100, 'offsides': 100,
    'passes': 2000, 'tackles': 200, 'saves': 100,
}
BRIDGE_MARKETS = {
    'h2h': {'Home', 'Draw', 'Away'}, 'double_chance': {'1X', 'X2', '12'},
    'draw_no_bet': {'Home', 'Away'}, 'btts': {'Yes', 'No'},
    'totals': {'Over', 'Under'}, 'home_team_totals': {'Over', 'Under'},
    'away_team_totals': {'Over', 'Under'}, 'asian_handicap': {'Home', 'Away'},
    'corners': {'Over', 'Under'},
}
LINE_MARKETS = {'totals', 'home_team_totals', 'away_team_totals', 'asian_handicap', 'corners'}


def fail(message='invalid observation'):
    raise ParseError('scalper: ' + message, provider='scalper')


def label(value, *, maximum=160):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(c) < 32 for c in value)):
        fail('invalid identifier or label')
    return value.strip()


def source_name(value):
    value = label(value, maximum=48)
    if not re.fullmatch(r'[a-z][a-z0-9_\-]*', value):
        fail('invalid source name')
    return value


def timestamp(value, *, optional=False):
    if value is None and optional:
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            fail('timezone required')
        result = result.astimezone(timezone.utc)
        if not 2000 <= result.year <= 2100:
            fail('timestamp outside supported range')
        return result
    except (AttributeError, TypeError, ValueError):
        fail('invalid timestamp')


def count(value, maximum=100):
    # JSON booleans and fractional counts are not valid observations.
    if isinstance(value, str) and re.fullmatch(r'\d{1,4}', value):
        value = int(value)
    if type(value) is not int or not 0 <= value <= maximum:
        fail('invalid count')
    return value


def number(value, *, minimum=-1000, maximum=1000):
    if isinstance(value, bool):
        fail('boolean numeric value')
    try:
        result = float(value)
    except (ValueError, TypeError):
        fail('invalid numeric value')
    if not math.isfinite(result) or not minimum <= result <= maximum:
        fail('numeric value outside supported range')
    return result


@dataclass(frozen=True)
class Batch:
    source: str
    fixtures: tuple = ()
    quotes: tuple = ()
    # Successful full snapshots replace these event/book quote sets, including
    # the empty set. This revokes disappeared or suspended offers immediately.
    replace_books: tuple = ()
    warnings: tuple = ()


def normalized(payload, source, *, now):
    """Versioned interchange for authorized collectors and JSON feeds.

    Input fixtures use event_id, sport_key, kickoff, home_team, away_team,
    status and optional scores/statistics. Quote identities include the exact
    period and settlement_contract. No downloaded URL is ever followed.
    """
    source = source_name(source)
    if not isinstance(payload, dict) or type(payload.get('schema_version')) is not int or payload.get('schema_version') != 1:
        fail('unsupported interchange version')
    collected = timestamp(payload.get('collected_at'))
    if (collected - now).total_seconds() > 60:
        fail('capture time is in the future')
    fixtures, quotes = payload.get('fixtures', []), payload.get('quotes', [])
    if (not isinstance(fixtures, list) or not isinstance(quotes, list)
            or len(fixtures) > MAX_ROWS or len(quotes) > MAX_QUOTES):
        fail('invalid or oversized interchange')
    out, events = [], {}
    for raw in fixtures:
        if not isinstance(raw, dict):
            fail()
        eid = label(raw.get('event_id'))
        key = raw.get('sport_key')
        if key not in LEAGUES:
            fail('unknown competition')
        home, away = label(raw.get('home_team')), label(raw.get('away_team'))
        if team_identity(home) == team_identity(away):
            fail('identical opponents')
        kickoff = timestamp(raw.get('kickoff'))
        status = raw.get('status')
        if status not in {'SCHEDULED', 'LIVE', 'HT', 'FINISHED', 'POSTPONED',
                          'CANCELED', 'SUSPENDED', 'ABANDONED', 'UNKNOWN'}:
            fail('unknown match status')
        full = []
        for side in ('home', 'away'):
            value = raw.get(side + '_score')
            full.append(count(value, 30) if value is not None else None)
        if status == 'FINISHED' and (None in full or kickoff > now):
            fail('missing or future final score')
        if status == 'FINISHED' and 'score_period' not in raw:
            fail('final observations require an explicit score period')
        regulation = raw.get('score_period', 'regulation') == 'regulation'
        row = normalise_fixture(provider=source, sport_key=key, match_id=eid,
            kickoff_epoch=kickoff.timestamp(), home=home, away=away,
            home_score=full[0], away_score=full[1], status=status)
        row.update(source_event_id=eid, kickoff_time_known=True,
            settlement_eligible=regulation and status in ('FINISHED', 'CANCELED'),
            completed=status == 'FINISHED' and regulation,
            score_period=raw.get('score_period', 'regulation'),
            observed_at=collected.isoformat(), source_tier='unofficial')
        if status == 'UNKNOWN':
            row['discovery_only'] = True
        for side, final in zip(('home', 'away'), full):
            half = raw.get(side + '_first_half_score')
            if half is not None:
                half = count(half, 30)
                if final is not None and half > final:
                    fail('first-half score exceeds full score')
                row[side + '_first_half_score'] = half
            for stat, maximum in STATISTICS.items():
                value = raw.get(side + '_' + stat)
                if value is not None:
                    row[side + '_' + stat] = count(value, maximum)
        if raw.get('statistics_period', 'regulation') != 'regulation':
            row['statistics_period'] = raw['statistics_period']
            row['ended_after_extra_time'] = True
            row.pop('home_corners', None)
            row.pop('away_corners', None)
        if eid in events and events[eid] != row:
            fail('contradictory event identity')
        events[eid] = row
    out.extend(events.values())
    offers, replacements = {}, set()
    for raw in quotes:
        if not isinstance(raw, dict):
            fail()
        eid = label(raw.get('event_id'))
        if eid not in events:
            fail('quote lacks an event in this snapshot')
        book = source_name(raw.get('book_key'))
        market, selection = label(raw.get('market')), label(raw.get('selection'))
        period = label(raw.get('period'))
        contract = label(raw.get('settlement_contract'))
        line = number(raw['line'], minimum=-100, maximum=250) if raw.get('line') is not None else None
        active = raw.get('active')
        if type(active) is not bool:
            fail('quote active state must be explicit')
        odds = number(raw.get('odds'), minimum=1.000001, maximum=100000) if active else None
        updated = timestamp(raw.get('updated_at'), optional=True)
        if updated and (updated - now).total_seconds() > 60:
            fail('quote timestamp is in the future')
        event = events[eid]
        offer = dict(source=source, event_id=eid, sport_key=event['sport_key'],
            home=event['home_team'], away=event['away_team'], kickoff=event['kickoff'],
            book_key=book, book_title=label(raw.get('book_title', book)),
            market=market, selection=selection, line=line, period=period,
            settlement_contract=contract, active=active, odds=odds,
            updated_at=updated.isoformat() if updated else None, observed_at=collected.isoformat())
        identity = (eid, book, market, selection, line, period, contract)
        if identity in offers and offers[identity] != offer:
            fail('conflicting duplicate quote')
        offers[identity] = offer
        replacements.add((eid, book))
    # Explicit replacements let a feed revoke an event with zero offers.
    explicit = payload.get('replace_books', [])
    if not isinstance(explicit, list) or len(explicit) > MAX_ROWS:
        fail('invalid quote replacement list')
    for item in explicit:
        if not isinstance(item, dict) or item.get('event_id') not in events:
            fail('replacement lacks event')
        replacements.add((item['event_id'], source_name(item.get('book_key'))))
    return Batch(source, tuple(out), tuple(offers.values()), tuple(sorted(replacements)))


def bridge_contract(q):
    """Whether the current LISA models understand precisely this contract."""
    if q['period'] != 'regulation' or q['settlement_contract'] != 'regulation':
        return False
    market, selection, line = q['market'], q['selection'], q['line']
    if market == 'correct_score':
        return line is None and bool(re.fullmatch(r'\d{1,2}-\d{1,2}', selection))
    if selection not in BRIDGE_MARKETS.get(market, set()):
        return False
    if market in LINE_MARKETS:
        if line is None or abs(line * 4 - round(line * 4)) > 1e-8:
            return False
        # Corners currently support half-ball binary totals only.
        return market != 'corners' or abs(line % 1 - .5) < 1e-8
    return line is None
