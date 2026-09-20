## 🌐 Phase 1 Architectural Map: The Data-Refinery Foundation
To achieve absolute mathematical precision over high-volume noise, the initial architecture must focus entirely on building The Data-Refinery Engine. This engine acts as LISA’s core intelligence. It is responsible for ingestion, mathematical filtration, consensus alignment, and state caching.
Every other part of your business—the Stripe tiers, the premium Telegram notifications, and the Next.js web application—relies completely on this foundational layer being stable, cheap to run, and sub-second accurate.

[ Stage 1: Data Ingestion Worker ] 
       │ (Polls Raw Multi-Book Arrays via REST API)
       ▼
[ Stage 2: Quantitative Math Node ] 
       │ (Powers Shin's Method De-Vig / Extracts True Market Consensus)
       ▼
[ Stage 3: The Precision Quality Gate ] 
       │ (Enforces Strict Confidence Boundary: True Probability >= 85%)
       ▼
[ Stage 4: Transient Telemetry Cache ] 
         (Saves Active Live Hashes to Redis / Streams Confirmed Picks to Postgres)

------------------------------
## 📥 Stage 1: The Raw Data Ingestion Protocol
The system avoids heavy, brittle web scraping by deploying lightweight, event-driven cron workers that poll structured aggregators.

* 
* Primary Source: [The Odds API](https://the-odds-api.com/) (or [Sports Game Odds](https://sportsgameodds.com/)). We query the /v4/sports/{sport}/odds live endpoint.
* Target Inventory: NBA Basketball (lowest variance, high scoring volume) and Spanish La Liga / German Bundesliga Soccer (highly imbalanced, top-heavy leagues).
* Ingestion Cadence: To protect your budget and optimize API credit usage, the ingestion worker uses a dynamic scheduling loop:
* Pre-Match Aggregation: Runs once every 60 minutes throughout the day. Spikes to a 15-minute frequency exactly 90 minutes before a game starts (capturing the massive "Smart Money" line movements when official starting lineups are released).
   * Live In-Play Aggregation: Runs exclusively during defined macro-windows: Halftimes, end-of-quarter intervals, or after the 70th minute of soccer matches. This cuts operational api costs by roughly 70% compared to tracking live games second-by-second.
* 

------------------------------
## 🧠 Stage 2: The Quantitative Math Refinery (The De-Vig Engine)
Raw odds are a lie. They represent public emotion combined with the bookmaker's built-in transaction fee (the Vig). To find consistent winners, LISA must strip out the house edge.
Instead of using a naive linear calculation, the engine uses Shin's Probability Distribution Method. Shin’s method mathematically accounts for the Favorite-Longshot Bias (the proven market anomaly where bookmakers stack a higher percentage of their hidden fees onto the underdog because the general public loves backing longshots).
## The Process:

   1. The Multi-Book Loop: For every single match ingested, LISA extracts an array of odds from at least 5 different sportsbooks simultaneously, making sure to include a Sharp Anchor (like Pinnacle) to act as the baseline source of truth.
   2. Shin's Transformation: The math node runs an optimization equation that isolates the net transaction fee. It calculates the exact amount of "noise" injected by the house, removes it, and calculates the Pure Fair Probability for each individual bookmaker.
   3. Consensus Synthesis: The engine blends these un-juiced probabilities into a single Global Consensus True Probability ($P_{\text{true}}$) and calculates the standard deviation across books to verify that the market is in complete algorithmic agreement.

------------------------------
## 🛡️ Stage 3: The Precision Quality Gate & Top-Pick Isolation
This layer implements your core business rule: Precision over Quantity.

   1. Top-Pick Selection: For every game processed, the engine evaluates all potential outcomes (Home Win, Away Win, Draw). It automatically isolates the outcome with the highest percentage chance of occurring and flags it as the definitive Top Pick for that match.
   2. The $\ge 85\%$ Boundary Filter:
   * If $P_{\text{true}} \ge 0.85$, the match passes the gate. It is officially classified as a High-Certainty Prediction and assigned a state of TRIGGER_ALERT.
      * If $P_{\text{true}} < 0.85$, the match is immediately suppressed. Even if a team has a 78% chance of winning, LISA keeps it hidden. This strict boundary removes high-variance, unpredictable games, ensuring that your subscribers are only exposed to the absolute highest-grade mathematical data.
   
------------------------------
## 💾 Stage 4: Dual-Layer Database Architecture
To scale your platform to thousands of subscribers without triggering massive cloud compute fees, the storage layer is divided into two distinct responsibilities:
## 1. Hot Layer: The Transient Cache (Redis Stack)
Live match probabilities fluctuate constantly. Writing these shifting live matrices into a relational SQL database will freeze your connections and balloon your server costs.

* 
* The Plan: Active match states are stored inside an in-memory Redis Cache.
* Cost Control: Every match hash inside Redis is written with a strict Time-To-Live (TTL) expiration of 5 hours. Once a game concludes, its live telemetry data deletes itself from RAM automatically, keeping your hosting costs permanently locked onto free or low-cost basic tiers ($0–$7/month).
* 

## 2. Cold Layer: The Audited Ledger (Supabase / PostgreSQL)
When a match passes our 85% Quality Gate, the Top Pick selection data is written to your relational database.

* 
* The Plan: This database stores your user profiles, Stripe metadata, and the Public Graded Ledger.
* The Settlement Protocol: Three hours after kickoff, an automated background cron job triggers a settlement script. It pulls the official final score of the match, matches it against LISA’s prediction, and permanently stamps the row as a WIN or a LOSS. This un-editable table is what renders directly to your public frontend website, proving your system's accuracy to incoming traffic automatically.
* 

------------------------------
