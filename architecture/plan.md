
------------------------------
## 🌐 The Complete System Overview
LISA is a high-speed data-refinery platform. Instead of acting like a traditional sports analyst who watches video tapes and team drama, LISA operates like an automated high-frequency financial trading system.
The core philosophy of the platform is Top-Pick Consensus Convergence. The system scans every single game available globally in our target low-variance sports (like NBA basketball and top-heavy European football leagues). It then runs a multi-layered filtering process to distill all those matches down to exactly one mathematically definitive "Top Pick" per game—completely stripping out high-risk, low-certainty guesswork.
The entire system runs on a 4-stage pipeline that operates silently in the background:

[ Stage 1: Raw Ingestion ] ──> [ Stage 2: The Math Refinery ] ──> [ Stage 3: The Quality Gate ] ──> [ Stage 4: Frontend Delivery ]
  Pulls Multi-Book Data          Strips Hidden Fees (Vig)          Isolates Top Picks ≥ 85%         Renders Tiered Views & Links

------------------------------
## 📥 Step 1: The Raw Data Sources (Where the Intelligence Comes From)
To keep our infrastructure lean and cost-effective, we do not build expensive web scrapers that break every time a sportsbook updates its website. Instead, we plug into centralized, institutional data aggregators.
Our primary upstream sources are B2B Sports Odds APIs (such as The Odds API or Sports Game Odds) [the-odds-api.com]. These aggregators continuously scrape and unify data from two distinct types of global market makers:

   1. The Sharp Anchors (The Source of Truth): Platforms like Pinnacle or Circa Sports. These books allow professional syndicates to bet millions of dollars per match. Their numbers represent the absolute mathematical truth of what is happening in the sports world because they are constantly adjusted by "smart money."
   2. The Soft Targets (The Retail Books): Consumer apps like Bet365, DraftKings, or regional African platforms like Bet9ja. These books change their lines slowly because they are weighed down by emotional, public betting volume.

------------------------------
## ⚙️ Step 2: The Processing Pipeline (The Journey of a Match)
Every single morning, and continuously throughout the day, the system moves every scheduled game through a rigorous 3-step refining process before it is allowed onto the user dashboard:
## 1. The Multi-Book Aggregation Loop
The system pulls a clean, unified data sheet for an upcoming match. This sheet contains the odds for that specific game across 5 to 10 different global sportsbooks simultaneously.
## 2. The "De-Vigging" Extraction (Stripping the House Fee)
Bookmakers never show true probabilities on their apps. They artificially inflate their numbers to build in a hidden house fee called the Vig or Juice. For example, if a team has an 80% chance to win, a sportsbook will display odds that make it look like an 85% chance so they can take a cut of the money.

* LISA’s backend loops through every single sportsbook line collected for that match.
* It applies advanced mathematical distribution algorithms to strip away the hidden house fee completely.
* This extracts the pure, un-juiced mathematical probability from each individual bookmaker.

## 3. Consensus Synthesis
Once the engine has clean, un-juiced probabilities from all 10 sportsbooks, it averages them out. This creates a single, incredibly robust Global Consensus True Probability for the game. Because it combines the math of multiple multi-billion-dollar algorithmic sportsbooks, the variance drops to nearly zero.
## 4. Top-Pick Isolation
The engine reviews all possible outcomes for that match (e.g., Team A Win vs. Team B Win). It automatically isolates the outcome with the highest percentage chance of occurring and crowns it as the Definitive Top Pick for that specific game.
------------------------------
## 🛡️ Step 3: The Quality Gate (Precision Over Quantity)
This is our platform's ultimate competitive edge. Once the system isolates the "Top Pick" for a match, it subjects it to a strict Confidence Threshold Filter (Locked at $\ge 85\%$ or $\ge 90\%$).

* If the Top Pick hits 86% True Probability: The system approves it. It passes the quality gate and is instantly flagged as a high-certainty prediction.
* If the Top Pick hits 74% True Probability: Even though it is the most likely outcome for that game, the system silently discards it. A 74% probability leaves too much room for random sports luck (injuries, bad referee calls). To keep our subscribers happy and winning consistently, LISA completely ignores the noise and only passes absolute, high-certainty math locks.

------------------------------
## 💻 Step 4: Presentation & Seamless Execution (The User View)
Once a prediction passes the quality gate, it is written to our database and instantly pushed to the user interface, organized by the subscription tiers we established:

   1. The Data Grid: On the web dashboard, users see a highly professional, clean grid (no messy sportsbook banners). It simply lists the Match, LISA's calculated True Consensus Probability, and the Fair Market Payout.
   2. The Live Track Curve: For our higher-tier live in-play subscribers, they see a clean graph showing how LISA's confidence curve spikes or dips in real time as the match progresses (e.g., during halftime).
   3. The 1-Click Bet Slip Link: Right next to LISA's prediction, the system renders a dropdown menu of popular sportsbooks. When a user clicks "Execute on DraftKings" or "Execute on Bet365," our system compiles a specialized Deep Link URL [opticodds.com]. This instantly opens their local sportsbook app on their phone with the exact game and selection already loaded into their active bet slip. The user never has to search for the game or risk missing the live odds—it is entirely effortless and eliminates all execution edge cases.

Act as a Principal Quantitative Software Engineer. You are building the first two stages of LISA (Linear Intelligence & Systemic Analytics). 

Implement a robust, asynchronous Python application using FastAPI, NumPy, and Pydantic that ingests multi-bookmaker data and extracts true consensus probabilities using Shin's Method.

Requirements:

1. Data Ingestion Models (Stage 1):
   - Define Pydantic v2 data models to parse incoming JSON payloads containing match details and arrays of bookmaker odds.
   - Explicitly ensure the system can handle identifiers for "pinnacle", "bet365", and other key books.

2. Quantitative Refinery Logic (Stage 2):
   - Implement an optimization function using NumPy to solve Shin's Equation for each bookmaker line. Shin's method must iteratively find the transaction cost fraction (z) that solves the overround condition, correcting for the favorite-longshot bias.
   - For an outcome odds pair (o_fav, o_und) with raw probabilities q_fav = 1/o_fav and q_und = 1/o_und:
     The equation to solve for true probabilities p_fav and p_und satisfies:
     q = z * p / (1 - z + z*p) + (1 - z) * p
     Ensure the sum of extracted true probabilities equals exactly 1.0.
   - Aggregate all de-vigged bookmaker lines to compute:
     a) Global Consensus True Probability (Mean)
     b) Sharp Anchor True Probability (Filtered specifically where bookmaker == 'pinnacle')
     c) Market Agreement Deviation (Standard Deviation)

3. API Endpoint:
   Expose an asynchronous POST endpoint at `/v1/lisa/refine` that accepts the raw ingestion payload, runs the calculations strictly in memory, and returns the processed mathematical metrics object.

Write clean, modular, production-ready code in a single file named `lisa_refinery.py`. Handle potential mathematical boundary exceptions gracefully (e.g., division by zero or non-converging optimization loops). Do not write comments or pseudocode.

To accomodate more leagues

Act as a Principal Software Architect. We have locked down the exact operational scope and data strategy for LISA. Update the Stage 1 Ingestion client and the core configuration settings to exclusively accommodate these 9 globally verified, highly predictable leagues:

1. Basketball Contracts:
   - 'basketball_nba'
   - 'basketball_euroleague'

2. Soccer Contracts:
   - 'soccer_spain_la_liga'
   - 'soccer_germany_bundesliga'
   - 'soccer_france_ligue_one'
   - 'soccer_italy_serie_a'
   - 'soccer_netherlands_eredivisie'
   - 'soccer_portugal_primeira_liga'
   - 'soccer_epl'

3. System Overrides:
   - Hardcode the baseline `confidence_threshold` to 0.75 (75%).
   - Keep the multi-book abstraction layer completely closed to protect vendor data. The output must strictly be LISA's proprietary confidence percentage and fair market payout calculation.
