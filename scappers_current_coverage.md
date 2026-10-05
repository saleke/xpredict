Q:
before it was said the system needs all these data to run 
For the full football scope you described, the system needs the following data. Some supports basic predictions; other data is essential for particular markets or provides optional enrichment. This is a requirements list, not a claim that we currently have everything.

Fixture and competition identities: Stable match, league, season, team and player IDs; provider ID mappings; team-name aliases; kickoff times, venues and match status.
Upcoming schedules: Fixtures across covered leagues, including postponements, cancellations, rescheduled kickoffs and competition stages.
Historical match results: Multiple seasons of regulation-time scores, home/away splits, opponents and dates—supporting wins, draws, double chance, draw-no-bet, BTTS and correct scores.
Goal timing and period results: Goal timestamps, halftime scores, second-half scores and stoppage-time events—needed for first-half markets, period totals and time-window predictions.
Team attacking and defensive statistics: Goals scored/conceded, shots, shots on target, chances created, possession, set pieces and opponent strength.
Chance-quality data: Expected goals, expected goals conceded, shot locations, shot types and penalty indicators—potential enrichment beyond raw scorelines.
Corner statistics: Corners won and conceded by each team, historical distributions, home/away splits, period counts and corner timestamps.
Corner-generating activity: Crosses, blocked shots, attacking territory and other consistently defined attacking measures—candidate features for corner models.
Team corner-handicap data: Separate home/away corner counts, quoted handicap lines and settlement rules. Total corners alone cannot support this market adequately.
Disciplinary statistics: Yellow cards, second yellows, direct reds, fouls committed/suffered and their team, player and period attribution.
Referee history: Referee assignments, card and foul distributions, penalty decisions and competition context—particularly relevant to card markets.
Player performance histories: Minutes played, starts, substitutions, goals, assists, shots, shots on target, tackles, fouls, cards, saves and penalties. These must match the statistic being predicted. API-Football data guide
Player availability and roles: Injuries, suspensions, confirmed lineups, substitutes, positions, expected minutes, rotation and penalty/set-piece responsibilities.
Squad and tactical changes: Transfers, managerial changes, formations and changing player roles, with dates showing when the information became available.
Match context: Rest days, fixture congestion, travel, home advantage, neutral venues, standings and aggregate scores in knockout ties. Weather and pitch conditions are optional enrichment.
Current bookmaker odds: Actual prices for each supported selection: result, double chance, BTTS, goals, team totals, handicaps, corners, cards, periods and player props. Market availability must be checked per bookmaker and event. Market catalogue
Complete betting-contract definitions: Market ID, selection, line, team/player, period, regulation versus extra-time scope, and rules for pushes, quarter lines, half wins, half losses and voids.
Quote freshness and availability: Bookmaker update time, provider observation time, retrieval time, active/suspended status, supported region and offer expiry. Stake limits, liquidity and commission matter where applicable.
Historical odds and movement: Timestamped opening, intermediate and closing prices, including inactive offers—needed for realistic evaluation and closing-price analysis. OddsPapi historical data
Live match state: Verified clock and period, current score, red cards, substitutions, players on the pitch and continuously updated match statistics.
Detailed live-event sequences: Timestamped goals, shots, corners, cards, fouls, penalties and VAR reversals, with event IDs, ordering and corrections. Short-window

how much of these does scalper cover

A:

Here is an exact, item-by-item audit of how much of that comprehensive requirements list **Scalper** actually covers based on its implementation in [`engine/lisa/scalper/`](file:///home/walter/xpredict/engine/lisa/scalper).

---

### Summary Scorecard

| Status | Items Count | Key Capabilities |
| :--- | :---: | :--- |
| **Fully Covered** | **4 / 21** | Upcoming schedules, core historical results, contract rigor (1X2/totals/handicaps), quote freshness/dual clocks. |
| **Partially Covered** | **8 / 21** | Match identities, boxscore stats, corners, cards, period scores, prospective odds history, core market odds. |
| **Completely Uncovered** | **9 / 21** | Advanced chance quality (xG), player props/availability, tactical changes, referees, play-by-play timelines, in-play streams. |

**Verdict**: Scalper covers roughly **35% to 40%** of the full theoretical football data spectrum. It is purpose-built to thoroughly cover **pre-match macro markets** (1X2, Totals, Asian Handicaps, BTTS), but deliberately ignores player props, in-play micro-events, and ball-tracking metrics.

---

### Detailed Breakdown

#### 1. Fully Covered by Scalper

* **Upcoming schedules across covered leagues**:
  * [`EspnSource.parse_scoreboard`](file:///home/walter/xpredict/engine/lisa/scalper/sources.py#L126) covers 14 leagues over a rolling forward window. Fully recognizes `SCHEDULED`, `POSTPONED`, `CANCELED`, and `SUSPENDED` states.
* **Historical match results (Multiple seasons)**:
  * [`OpenFootballSource`](file:///home/walter/xpredict/engine/lisa/scalper/sources.py#L283) ingests multi-season CC0 match results (full-time scores, dates, home/away splits) across 8 major European leagues.
* **Complete betting-contract definitions**:
  * Handled defensively in [`contracts.py`](file:///home/walter/xpredict/engine/lisa/scalper/contracts.py). Strictly isolates regulation scope from extra-time, and standardizes quarter lines, draw-no-bet, and double chance.
* **Quote freshness and availability**:
  * Strict dual-clock tracking in [`browser_sources.py`](file:///home/walter/xpredict/engine/lisa/scalper/browser_sources.py#L18-L53): separates bookmaker `updated_at` from HTTP RFC 9111 publisher `confirmed_at`. Quotes older than `LISA_SCALPER_QUOTE_MAX_AGE_SEC` (default 300s) are automatically expired.

---

#### 2. Partially Covered by Scalper

* **Fixture & competition identities**:
  * **Covered**: Canonical league keys, stable source match IDs, team names, UTC kickoff timestamps, and status normalization.
  * **Missing**: Neutral venue flags, stadium details, and canonical player entity resolution.
* **Goal timing and period results**:
  * **Covered**: Halftime scores (`home_first_half_score`, `away_first_half_score`) and second-half regulation scores are parsed from ESPN's period scores.
  * **Missing**: Exact goal minute timestamps (e.g., 24', 89') and stoppage-time event sequences are not parsed.
* **Team attacking & defensive statistics**:
  * **Covered**: Full-time boxscores from ESPN include shots, shots on target, fouls, tackles, offsides, saves, and passes in [`STAT_MAP`](file:///home/walter/xpredict/engine/lisa/scalper/sources.py#L17-L22).
  * **Missing**: Possession percentage, chances created, and big chances missed are not extracted.
* **Corner statistics**:
  * **Covered**: Full-time regulation corners won/conceded (`home_corners`, `away_corners`) are captured from ESPN.
  * **Missing**: First-half vs. second-half corner breakdowns and corner timestamps.
* **Team corner-handicap data**:
  * **Covered**: Separate home and away corner counts are recorded for finished games.
  * **Missing**: Scalper's bookmaker bridge does **not** ingest corner handicap odds or total corner betting lines from Pinnacle or SportyBet.
* **Disciplinary statistics**:
  * **Covered**: Total yellow cards, red cards, and fouls committed per team per match.
  * **Missing**: Second yellow vs. straight red distinction, card minutes, and player-level attribution.
* **Current bookmaker odds**:
  * **Covered**: Pinnacle (EPL) and SportyBet (select landing matches) supply 1X2 (`h2h`), Double Chance, Draw No Bet, BTTS, Goal Totals, and Asian Handicap.
  * **Missing**: No corner odds, card odds, player prop odds, or multi-book coverage beyond these two books.
* **Historical odds & movement**:
  * **Covered (Prospective only)**: Once running, Scalper archives hourly price checkpoints and line movements in `odds_observations` with a 30-day retention window.
  * **Missing**: No pre-existing multi-season historical odds (Openfootball has scores only, no odds).

---

#### 3. Completely Uncovered / Missing from Scalper

These items from the checklist are **not collected, parsed, or supported** by Scalper:

1. **Chance-quality data (xG / xGA)**: No expected goals, shot location coordinates (x/y), or shot-type classifications.
2. **Corner-generating activity**: No data on crosses, blocked shots, or final-third territory possession.
3. **Referee history**: No tracking of match officials, referee penalty tendencies, or historical card distributions.
4. **Player performance histories**: ESPN summary includes raw unparsed `rosters` JSON, but Scalper does not aggregate individual player minutes, passes, tackles, or shots into player models.
5. **Player availability and roles**: No injury reports, suspension bulletins, confirmed starting lineups, or designated penalty/free-kick takers.
6. **Squad and tactical changes**: No tracking of manager hirings/sackings, tactical formations (e.g., 4-3-3 vs 3-5-2), or transfer windows.
7. **Match context (Environmental/Logistics)**: No travel distance, rest days computation, weather/pitch condition reports, or aggregate score tracking in two-legged cup ties.
8. **Live match state & in-play streams**: Scalper does not provide a sub-second live clock, pitch coordinates, or live substitution alerts (it is a polling worker, not a live streaming websocket client).
9. **Detailed live-event sequences**: No timestamped play-by-play events, shot chronologies, penalty incidents, or VAR reversal timelines.

Q2:
are there other free sources in the providers that conpensate for the other percentage that are not covered

A:

Viewed api_football.py:1-100
Viewed api_football.py:150-250
Viewed allsports.py:1-100
Viewed allsports.py:110-180
Viewed football_data.py:1-100
Viewed football_data.py:140-180
Viewed openligadb.py:1-60
Viewed openligadb.py:90-150

**Yes.** The other provider adapters already implemented in [`engine/lisa/providers/`](file:///home/walter/xpredict/engine/lisa/providers) (and documented in [`docs/LEAN_DATA_STACK.md`](file:///home/walter/xpredict/docs/LEAN_DATA_STACK.md) and [`docs/FREE_SOURCE_RESEARCH.md`](file:///home/walter/xpredict/docs/FREE_SOURCE_RESEARCH.md)) **can compensate for a large portion of the missing ~60%**.

However, unlike Scalper (which has no hard API request ceilings), these providers have **monthly or daily free-tier quotas**. They cannot be polled continuously every few seconds, but they serve as **surgical, metered enrichers** to fill Scalper’s specific blind spots.

---

### How Existing Free Providers Fill Scalper's Gaps

| Uncovered Requirement | Compensating Free Provider | Implementation in Workspace | Free-Tier Allowance & Tradeoffs |
| :--- | :--- | :--- | :--- |
| **1. Referee History & Assignments** | **API-Football** | [`api_football.py:L176`](file:///home/walter/xpredict/engine/lisa/providers/api_football.py#L176): extracts `source_referee` directly from fixture responses. | **100 requests/day free**. Sufficient for daily matchday referee lookups across top leagues. |
| **2. Player Stats, Minutes & Lineups** | **API-Football**<br>**AllSportsAPI**<br>**FPL Public API** | [`api_football.py:L197-205`](file:///home/walter/xpredict/engine/lisa/providers/api_football.py#L197-L205): `/fixtures/players` endpoint.<br>[`allsports.py:L138`](file:///home/walter/xpredict/engine/lisa/providers/allsports.py#L138): extracts `source_lineups` (starting XI + subs). | API-Football player calls must be reserved for targeted matches (1 request per match). FPL is keyless but EPL-only. |
| **3. Corner & Period Betting Odds** | **API-Football**<br>**The Odds API** | [`api_football.py:L221`](file:///home/walter/xpredict/engine/lisa/providers/api_football.py#L221): parses `Corners Over Under`.<br>[`the_odds_api.py`](file:///home/walter/xpredict/engine/lisa/providers/the_odds_api.py): catalog supports 1st-half and corner lines. | The Odds API gives **500 credits/month free**; API-Football odds are included in its 100/day limit. |
| **4. Multi-Bookmaker Depth (Beyond Pinnacle & SportyBet)** | **The Odds API**<br>**OddsPapi** | [`the_odds_api.py`](file:///home/walter/xpredict/engine/lisa/providers/the_odds_api.py)<br>[`oddspapi.py`](file:///home/walter/xpredict/engine/lisa/providers/oddspapi.py) | Ingests Bet365, DraftKings, BetMGM, FanDuel, Bovada. Enables Shin de-vigging across 5+ books instead of just Pinnacle. (OddsPapi has **250 requests/month free**). |
| **5. Goal Timing & Timeline Events** | **OpenLigaDB**<br>**API-Football** | [`openligadb.py:L12`](file:///home/walter/xpredict/engine/lisa/providers/openligadb.py#L12): parses exact goal minutes and goal events.<br>API-Football `/fixtures/events`: cards, goals, substitutions. | **OpenLigaDB is 100% keyless and unmetered** (German leagues). API-Football handles European cups and other leagues. |
| **6. Chance Quality & xG Research** | **StatsBomb Open Data** | Research candidate in [`docs/FREE_SOURCE_RESEARCH.md:L27`](file:///home/walter/xpredict/docs/FREE_SOURCE_RESEARCH.md#L27) (GitHub CC0 open datasets). | Provides xG, shot locations ($x, y$ coordinates), and freeze-frame 360 data for historical model training (not a live feed). |

---

### What Is STILL Impossible on Free Tiers

Even combining Scalper with all available free provider tiers, the following items from the checklist **cannot be obtained without commercial B2B contracts**:

1. **Sub-second in-play WebSocket streams**: Bookmaker push sockets (e.g. Bet365/Sportradar live trackers) are paywalled or restricted to funded betting accounts.
2. **Full-season granular player tracking for 14 leagues**: Fetching starting lineups and player boxscores for every match in 14 leagues exceeds 100 requests/day. Free tiers can only cover marquee matches (e.g., EPL + Champions League).
3. **Tactical formations & manager tracking**: No free API provides structured, historical timestamps of managerial sackings or shifting in-match tactical shapes (e.g., transitioning from a 4-3-3 to a 5-3-2 in the 70th minute).

---

### Recommended Architectural Synergy: The "Hybrid Stack"

The most powerful architecture in this repository is **not** choosing between Scalper and the APIs, but configuring them to work together in `LISA_SCALPER_MODE=supporting`:

```mermaid
flowchart TD
    subgraph Zero Cost / High Volume Backbone (Scalper)
        A[ESPN Web Collector] -->|Calendars, Results, FT Boxscores| DB[(Shared DB)]
        B[Openfootball CC0] -->|Multi-Season Historical Goals| DB
        C[Playwright Browser] -->|Pinnacle & SportyBet Odds| DB
    end

    subgraph Metered Surgical Enrichment (Free API Tiers)
        D[API-Football: 100/day] -->|Referees, Lineups, Corner Odds| DB
        E[The Odds API: 500/mo] -->|Multi-Book Consensus: Bet365, DK| DB
        F[OpenLigaDB: Keyless] -->|Goal Minutes & Halftime Data| DB
    end

    DB --> G[Quantitative Modeling Engine: Shin Devigging, Poisson, Dixon-Coles]
```

1. **Scalper does the heavy lifting (90% of requests)**: Refreshes calendars, updates live scores, scrapes Pinnacle odds, and stores results without touching any API quotas.
2. **Free API keys handle surgical enrichment (10% of requests)**:
   - Use `API-Football` (100 calls/day) **only** to fetch referee names and confirmed lineups 30 minutes before kickoff.
   - Use `The Odds API` (500 calls/month) **only** for weekend marquee matches to pull Bet365 and DraftKings odds to cross-reference against Pinnacle.