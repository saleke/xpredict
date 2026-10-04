"""League registry + shared normalisation for the free calendar sources.

One canonical key space, several source vocabularies
---------------------------------------------------
The engine speaks ``soccer_epl`` (The Odds API's vocabulary, already used
throughout ``config.SCOPE_LEAGUES`` and the storage layer). Every free source
names the same league differently:

=================  ===================  ==========  ========
LISA key           football-data.org    OpenLigaDB   TheSportsDB
=================  ===================  ==========  ========
soccer_epl         PL                   --           4328
soccer_germany_..  BL1                  bl1         4331
soccer_italy_..    SA                   --           4332
soccer_france_..   FL1                  --           4334
soccer_spain_..    PD                   --           4335
soccer_nether..    DED                  --           4337
soccer_portugal_.. PPL                  --           --
soccer_uefa_cl     CL                   --           --
soccer_germany_2   BL2                  bl2         --
=================  ===================  ==========  ========

Keeping the LISA key as the canonical form means the model, the ledger, the
tier system and the board never learn that the data came from three different
places. Provenance is carried per fixture instead (see ``source``).

Coverage is deliberately explicit rather than guessed: a league a source does not
carry is ``None`` here, and the registry can be queried for "who can serve this
league?" so the fetcher only ever calls sources that can actually answer.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Iterable, Mapping, Optional

from .base import FixturesResult, SourceTier

__all__ = [
    "LeagueSpec",
    "LEAGUES",
    "league",
    "sources_for",
    "normalise_fixture",
    "normalise_team",
    "team_identity",
    "fixture_groups",
    "dedupe_fixtures",
    "FixturesResult",
    "SourceTier",
]


@dataclass(frozen=True)
class LeagueSpec:
    """How one league is named by each free source."""

    key: str
    title: str
    #: football-data.org competition code (12 free tier-one competitions).
    fdo: Optional[str] = None
    #: OpenLigaDB league shortcut (German football only).
    oldb: Optional[str] = None
    #: TheSportsDB ``idLeague`` (free key exposes 10 leagues).
    tsdb: Optional[str] = None
    #: SharpAPI league slug, for the two US books it prices.
    sharp: Optional[str] = None
    family: str = "soccer"
    #: Rough weight of a result in the model, by competition prestige.
    prestige: float = 1.0
    timezone: str = 'UTC'

    def source_id(self, source: str) -> Optional[str]:
        return {
            "football_data": self.fdo,
            "openligadb": self.oldb,
            "sportsdb": self.tsdb,
            "sharpapi": self.sharp,
        }.get(source)


LEAGUES: dict[str, LeagueSpec] = {
    spec.key: spec
    for spec in (
        # The ``sharp`` slugs are the *canonical* SharpAPI league ids, read off
        # ``GET /api/v1/leagues`` and verified to exist for every row below.
        # SharpAPI is a price source, not a calendar source, so it is
        # deliberately absent from ``_SOURCE_ORDER``: adding it there would make
        # the fetcher ask a prices-only adapter for fixtures, and would move
        # league tiers for the wrong reason.
        LeagueSpec("soccer_epl", "Premier League", "PL", None, "4328",
                   sharp="england_-_premier_league", prestige=1.30, timezone='Europe/London'),
        LeagueSpec("soccer_germany_bundesliga", "Bundesliga", "BL1", "bl1", "4331",
                   sharp="germany_-_bundesliga", prestige=1.25, timezone='Europe/Berlin'),
        LeagueSpec("soccer_italy_serie_a", "Serie A", "SA", None, "4332",
                   sharp="italy_-_serie_a", prestige=1.20, timezone='Europe/Rome'),
        LeagueSpec("soccer_spain_la_liga", "La Liga", "PD", None, "4335",
                   sharp="spain_-_la_liga", prestige=1.20, timezone='Europe/Madrid'),
        LeagueSpec("soccer_france_ligue_one", "Ligue 1", "FL1", None, "4334",
                   sharp="france_-_ligue_1", prestige=1.15, timezone='Europe/Paris'),
        LeagueSpec("soccer_netherlands_eredivisie", "Eredivisie", "DED", None,
                   "4337", sharp="netherlands_-_eredivisie", prestige=1.00, timezone='Europe/Amsterdam'),
        LeagueSpec("soccer_portugal_primeira_liga", "Primeira Liga", "PPL", None,
                   None, sharp="portugal_-_primeira_liga", prestige=1.00, timezone='Europe/Lisbon'),
        LeagueSpec("soccer_uefa_champions_league", "UEFA Champions League", "CL",
                   None, None, sharp="uefa_-_champions_league", prestige=1.45),
        # football-data.org is the only free source for these two.
        LeagueSpec("soccer_england_championship", "Championship", "ELC", None,
                   "4329", sharp="england_-_championship", prestige=0.75, timezone='Europe/London'),
        LeagueSpec("soccer_brazil_serie_a", "Brasileirao Serie A", "BSA", None,
                   None, sharp="brazil_-_serie_a", prestige=1.00),
        # OpenLigaDB is the only free source for this one. football-data.org
        # publishes a "BL2" competition, but the free tier answers 403 for it
        # ("restricted... not within your permissions"), so claiming coverage
        # there spends a request every cycle to collect a permanent error.
        LeagueSpec("soccer_germany_2_bundesliga", "2. Bundesliga", None, "bl2",
                   None, sharp="germany_-_bundesliga_2", prestige=0.70),
        # TheSportsDB-only coverage.
        LeagueSpec("soccer_scotland_premiership", "Scottish Premiership", None,
                   None, "4330", sharp="scotland_-_premiership", prestige=0.70),
        LeagueSpec("soccer_greece_superleague", "Greek Super League 1", None, None,
                   "4336", sharp="greece_-_super_league", prestige=0.65),
        LeagueSpec("soccer_belgium_first_division", "Belgian Pro League", None,
                   None, "4338", sharp="belgium_-_jupiler_pro_league",
                   prestige=0.65),
        # US sports: priced (not modelled) by the free SharpAPI tier.
        LeagueSpec("basketball_nba", "NBA", None, None, None, sharp="nba",
                   family="basketball", prestige=1.20),
        LeagueSpec("americanfootball_nfl", "NFL", None, None, None, sharp="nfl",
                   family="american-football", prestige=1.20),
        LeagueSpec("baseball_mlb", "MLB", None, None, None, sharp="mlb",
                   family="baseball", prestige=1.15),
        LeagueSpec("icehockey_nhl", "NHL", None, None, None, sharp="nhl",
                   family="ice-hockey", prestige=1.10),
    )
}

#: Which sources can serve a given league, cheapest-first.
_SOURCE_ORDER: tuple[str, ...] = ("openligadb", "sportsdb", "football_data")


def league(key: str) -> Optional[LeagueSpec]:
    return LEAGUES.get(key)


def sources_for(key: str) -> tuple[str, ...]:
    """Calendar sources that can serve ``key``, cheapest-first.

    OpenLigaDB leads because it is unmetered, so it is always safe to ask; the
    metered sources follow. An empty tuple means no free source covers the
    league, and the caller must not spend a request finding that out.
    """
    spec = LEAGUES.get(key)
    if spec is None:
        return ()
    return tuple(s for s in _SOURCE_ORDER if spec.source_id(s))


def normalise_fixture(*, provider: str, sport_key: str, match_id: str,
                      kickoff_epoch: float, home: str, away: str,
                      home_score: Optional[int] = None,
                      away_score: Optional[int] = None,
                      status: str = "scheduled",
                      season: Optional[str] = None) -> dict[str, Any]:
    """Build the single fixture shape every source is normalised into.

    ``epoch`` is stored alongside the ISO time deliberately: sorting, horizon
    filtering and in-window arithmetic all happen on every cycle, and integer
    comparison is both cheaper and free of timezone surprises.
    """
    from datetime import datetime, timezone
    return {
        "match_id": f"{provider}:{match_id}",
        "provider": provider,
        "sport_key": sport_key,
        "home_team": home.strip(),
        "away_team": away.strip(),
        "kickoff": datetime.fromtimestamp(kickoff_epoch, tz=timezone.utc).isoformat(),
        "epoch": float(kickoff_epoch),
        "home_score": home_score,
        "away_score": away_score,
        # A row with a status but no scoreline must never be treated as a
        # result: the model trains on goals, and a missing goal is missing data.
        "completed": status in ("FINISHED", "FT", "AET", "PEN") and
                     home_score is not None and away_score is not None,
        "status": status,
        "season": season,
    }


#: Legal-form tokens that carry no identifying information. Every one of them is
#: attached to a team by at least one source and omitted by another: "Arsenal FC"
#: (football-data.org) against "Arsenal" (SharpAPI), "1. FC Union Berlin" against
#: "Union Berlin".
#:
#: The two- and three-letter entries are the South American club forms.
#: football-data.org abbreviates the name plus its form ("CA Paranaense") while
#: the US books publish the form's expansion ("Atl Paranaense"). They identify
#: a legal form, never a club -- and "SC" is ambiguous in exactly the way that
#: matters: "SC Internacional" is Sport Club Internacional while "SC Corinthians
#: Paulista" is Sociedade Esportiva Corinthians Paulista. Stripping it is what
#: lets both reach the name the books actually publish.
#:
#: Four-digit tokens are founding years ("1899 Hoffenheim", "TSV 1860 München").
#: Two-digit ones are deliberately *not* stripped: "Hannover 96" and "Holstein
#: 04" are the clubs' names, and "Team 96" collapsing to "team" would collide
#: with every other side in the league called Team.
_LEGAL_TOKENS = frozenset({
    "fc", "cf", "afc", "sc", "ac", "as", "ss", "ssc", "sv", "vfl", "vfb", "fk",
    "sk", "if", "ik", "cd", "ud", "rc", "sd", "rcd", "ca", "cr", "ec", "se",
    "fr", "af", "fbc", "fbpa", "usp", "ltd", "plc", "gmbh", "sa", "srl", "spa",
    "club", "clube", "calcio", "de", "do", "da", "dos", "das", "the", "e",
    "1899", "1900", "1904", "1846", "1860",
})

#: Trailing tokens that are dropped because they qualify rather than identify.
#: Deliberately tiny. An earlier draft also stripped "united", on the theory
#: that a book might say "Newcastle" for "Newcastle United" -- but it does not
#: (DraftKings and FanDuel both publish the full name, as does
#: football-data.org), and stripping it merged two different Premier League
#: clubs: "Manchester United" and "Manchester City" both collapsed to
#: "manchester". Two teams in one fixture is a wrong price on a wrong match, so
#: the token stays.
_TRAILING_TOKENS: frozenset[str] = frozenset()


def normalise_team(name: Any) -> str:
    """Reduce a team name to a comparable key across source vocabularies.

    Case, accents, punctuation, bracket suffixes ("Inter Milan [W]"), founding
    years and legal-form tokens are all noise for identity purposes. This is
    deliberately lossy: it is used to decide whether two records describe the
    same *team*, never to display a name.
    """
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    # Bracket suffixes carry the competition or the squad, never the club.
    text = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    tokens = [t for t in text.split() if t and t not in _LEGAL_TOKENS]
    # A leading ordinal is part of the legal form, not the name: "1. FC Koln" and
    # "FC Koln" are one club. Only the leading token is treated this way, because a
    # trailing number is the club's name ("Team 96", "Holstein 04").
    if len(tokens) > 1 and tokens[0].isdigit() and len(tokens[0]) <= 2:
        tokens.pop(0)
    while len(tokens) > 1 and tokens[-1] in _TRAILING_TOKENS:
        tokens.pop()
    return " ".join(tokens)


def team_tokens(name: Any) -> frozenset[str]:
    """The normalised name as a token set, for containment comparisons."""
    return frozenset(normalise_team(name).split())


def name_affinity(a: Any, b: Any) -> Optional[float]:
    """How strongly two team names refer to the same club, or ``None``.

    Two tiers, in descending order of evidence:

    ``1.0``
        Identical after normalisation. The strongest evidence available.
    ``0.85``
        One name's tokens are a strict subset of the other's -- the shape taken
        when one source appends a city or region the other omits. Measured
        against live responses, this is what carries every Brazilian and
        Portuguese pairing: "Internacional" / "Internacional RS", "Remo" /
        "Remo Belem", "Vitoria" / "Vitoria Salvador", "Corinthians" /
        "Corinthians Paulista".

    There is deliberately no abbreviation tier. One was tried and rejected: it
    scored ``Internazionale`` against ``Inter Miami`` as a match, because
    "internazionale" begins with "inter" as a matter of spelling rather than of
    abbreviation. A looser rule bought recall and paid for it with wrong matches,
    which is the wrong trade for a source whose entire job is attaching a price
    to the right fixture.

    The ``None`` result is load-bearing. It is what stops the matcher from
    pricing one club's fixture with another club's book -- which would fabricate
    an edge on a match nobody is playing.
    """
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb:
        return None
    if ta == tb:
        return 1.0
    aliased = _ALIAS_INDEX.get(ta)
    if aliased is not None and aliased is _ALIAS_INDEX.get(tb):
        return 0.95
    shorter, longer = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if not shorter < longer:
        return None
    # A purely numeric token identifies a club, it does not qualify one. "FC
    # Koln" is a strict subset of "Koln 04" by token alone, and those are two
    # different clubs -- so an added number disqualifies the subset tier rather
    # than qualifying it. "Hannover 96" must not collapse into "Hannager".
    if all(t.isdigit() for t in longer - shorter):
        return None
    return 0.85


#: Explicit, hand-checked equivalences. Nothing here is inferred -- each entry is
#: a pair of *forms of the same club*, in token-set form after normalisation, and
#: each exists because two sources publish different names for one team. They are
#: kept apart from the general rules because each one is an assertion about the
#: world that has to be checked by a person, not something the matcher discovers.
_TEAM_ALIASES: tuple[tuple[frozenset[str], frozenset[str]], ...] = (
    # German city exonyms: the sources do not agree on the English or the
    # German form, and no amount of token arithmetic recovers "Munich" from
    # "Munchen".
    (frozenset({"bayern", "munich"}), frozenset({"bayern", "munchen"})),
    (frozenset({"koln"}), frozenset({"cologne"})),
    (frozenset({"borussia", "monchengladbach"}), frozenset({"gladbach"})),
    (frozenset({"eintracht", "frankfurt"}), frozenset({"frankfurt", "main"})),
    # Inter's two registered names.
    (frozenset({"internazionale"}), frozenset({"inter", "milan"})),
)

#: Both sides of every alias pair are keyed to the *same* object, so a lookup
#: decides equivalence by identity. The obvious alternative -- returning ``None``
#: for a miss and comparing the two results -- is wrong, and was wrong here:
#: two teams absent from the table both returned ``None``, compared equal, and
#: every unrelated pair scored as an alias match.
_ALIAS_INDEX: dict[frozenset[str], frozenset[str]] = {
    form: left
    for left, right in _TEAM_ALIASES
    for form in (left, right)
}


def team_identity(name: Any) -> str:
    """Model and result identity, using only existing explicit club aliases."""
    name = normalise_team(name)
    alias = _ALIAS_INDEX.get(frozenset(name.split()))
    return ' '.join(sorted(alias)) if alias else name


def fixture_groups(fixtures: Iterable[Mapping[str, Any]]) -> list[list[dict[str, Any]]]:
    """Join date-only archives to one unambiguous timed match, never a rematch.

    Date-only timestamps are storage anchors, not invented kickoffs. Match them
    using the competition's local date; multiple timed pairings on that date
    leave the archive ambiguous and excluded. Timed rows retain minute identity.
    """
    groups = {}
    timed_dates = {}
    unknown = []
    for row in fixtures:
        if not isinstance(row, Mapping):
            continue
        home, away = team_identity(row.get('home_team')), team_identity(row.get('away_team'))
        key = row.get('sport_key', '')
        if not home or not away or home == away:
            continue
        if row.get('kickoff_time_known') is False:
            try:
                day = datetime.strptime(row['match_date'], '%Y-%m-%d').date().isoformat()
            except (KeyError, TypeError, ValueError):
                continue
            unknown.append(((key, home, away, day), dict(row)))
            continue
        epoch = row.get('epoch')
        if epoch is None:
            try:
                kickoff = datetime.fromisoformat(row['kickoff'].replace('Z', '+00:00'))
                epoch = kickoff.timestamp() if kickoff.tzinfo else None
            except (KeyError, TypeError, ValueError):
                continue
        if type(epoch) not in (int, float):
            continue
        try:
            spec = LEAGUES.get(key)
            day = datetime.fromtimestamp(epoch, ZoneInfo(spec.timezone if spec else 'UTC')).date().isoformat()
            identity = (key, round(epoch / 60), home, away)
        except (ValueError, OverflowError, OSError):
            continue
        groups.setdefault(identity, []).append(dict(row))
        timed_dates.setdefault((key, home, away, day), set()).add(identity)
    for dated, row in unknown:
        identities = timed_dates.get(dated, ())
        if len(identities) > 1:
            continue
        identity = next(iter(identities)) if identities else ('date', *dated)
        groups.setdefault(identity, []).append(row)
    return list(groups.values())


def dedupe_fixtures(fixtures: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Collapse the same match arriving from two sources.

    Two free sources covering one league (football-data.org *and* TheSportsDB,
    say) double the result count and would double-weight that team in the model
    unless the overlap is removed. Keyed on kickoff plus a normalised pairing so
    a fixture present in both feeds is counted exactly once; the richer record
    (one that actually carries a score) wins.

    The pairing goes through :func:`team_identity`, not a bare ``lower()``.
    Stripping only case meant "Arsenal FC" and "Arsenal" were treated as two
    different teams and the same fixture survived as two records -- which
    double-counted the result in training and could put one club's rating on
    two different strength scales.
    """
    def priority(row):
        # A provisional file must never displace a verified kickoff or a
        # cancellation. Rich statistics survive equal-quality duplicates.
        verified = not row.get('discovery_only', False)
        cancelled = str(row.get('status', '')).upper() in ('CANCELED', 'CANCELLED', 'POSTPONED')
        return (verified, bool(row.get('completed') or cancelled),
                row.get('kickoff_time_known') is not False,
                sum(row.get(k) is not None for k in ('home_corners', 'away_corners',
                    'home_first_half_score', 'away_first_half_score')))
    return [max(rows, key=priority) for rows in fixture_groups(fixtures)]
