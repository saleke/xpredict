## 🎨 Stage 2 Structural Architecture: The Quantitative Math Refinery
Stage 2 is the core data-processing engine of LISA. Its absolute, non-negotiable objective is to ingest the raw, multi-bookmaker data payload fetched in Stage 1, isolate the multi-billion-dollar market makers, strip out their hidden transaction fees (the vig), and establish a single, un-juiced Global Consensus True Probability.
------------------------------
## 🧱 Architectural Layer Breakdown
The backend processing loop inside Stage 2 is divided into four highly synchronized logical sequences:

[ Ingest Raw Multi-Book Array ] 
               │
               ▼
   [ 1. Isolation Filter ]     ──> Targets "pinnacle" (Sharp Anchor) & Retail Books
               │
               ▼
   [ 2. The De-Vig Engine ]    ──> Applies Multiplicative/Shin's Formula to remove fees
               │
               ▼
   [ 3. Consensus Synthesis ]  ──> Blends values via NumPy to find Global True Prob (P_true)
               │
               ▼
[ Handoff to Stage 3 Quality Gate ] 

## 🔍 Layer 2.1: Targeted Bookmaker Isolation Filter
The data array coming from Stage 1 contains mixed inputs. Layer 2.1 performs an extraction loop to group the bookmakers into two distinct mathematical weight classes:

* The Anchor Data Class: Filters explicitly for "key": "pinnacle" [the-odds-api.com]. This represents the institutional market-maker backed by sharp syndicate volume.
* The Retail Data Class: Filters for soft books ("key": "bet365", "key": "draftkings", etc.) [the-odds-api.com]. These represent the consumer endpoints where our subscribers will ultimately execute their deep-linked bets.

## 🧮 Layer 2.2: The De-Vig Engine (Stripping the House Fees)
Bookmakers inflate their odds to secure a profit margin, meaning the implied percentages across a match always add up to more than 100% (typically 104% to 107%). Layer 2.2 enforces the Multiplicative De-Vig Model to find the absolute truth:

* The Conversion: It reads the decimal odds for the favorite ($O_{\text{fav}}$) and underdog ($O_{\text{und}}$) and converts them to raw, over-inflated implied probabilities:
$$P_{\text{raw}} = \frac{1}{O}$$ 
* The Extraction: It sums the two raw values to find the exact market overround pool. It then divides each raw probability by the total pool. This mathematically eliminates the hidden fee, stripping the data down to clean, pure mathematical percentages.

## 📊 Layer 2.3: Consensus Synthesis & Discrepancy Matching
Once the engine has un-juiced probabilities from all isolated bookmakers, it executes a high-speed vector calculation:

* True Average Calculation: It computes the statistical mean ($\mu$) of the un-juiced favorite probabilities across all books. This value is locked in as the Global Consensus True Probability ($P_{\text{true}}$).
* Market Divergence Audit: It calculates the standard deviation ($\sigma$) across the books. A low standard deviation means the global market giants are in complete algorithmic agreement, verifying that the signal is incredibly steady and low-variance.

------------------------------
## 📂 Operational Data Mapping for Your Coding Agent
When your coding agent implements Stage 2, the pipeline must accept a structured payload, process it through the math refinery, and output a clean mathematical state object.
## 📩 Incoming Payload Schema from Stage 1:

{
  "match_id": "nba_2026_0920_boston",
  "sport": "Basketball",
  "home_team": "Boston Celtics",
  "away_team": "Miami Heat",
  "market_telemetry": [
    {"bookmaker": "pinnacle", "odds_favorite": 1.20, "odds_underdog": 5.00},
    {"bookmaker": "bet365", "odds_favorite": 1.22, "odds_underdog": 4.50},
    {"bookmaker": "draftkings", "odds_favorite": 1.19, "odds_underdog": 5.20}
  ]
}

## 📤 Processed Output Schema Handed Off to Stage 3:

{
  "match_id": "nba_2026_0920_boston",
  "target_pick": "Boston Celtics (Match Favorite)",
  "mathematical_metrics": {
    "sharp_anchor_true_probability": 0.8065,
    "global_consensus_true_probability": 0.8124,
    "market_agreement_deviation": 0.0052,
    "average_extracted_house_fee": "4.24%"
  }
}

------------------------------
## 💰 Cost & Resource Optimization Guardrails for the Agent
To ensure this layer runs with sub-second execution speed while consuming next to nothing in hosting overhead, your agent must follow these two infrastructure constraints:

   1. Pure Functional Compute: Stage 2 must be written as a pure, stateless mathematical service. It should perform all calculations using highly optimized vector math libraries like NumPy or standard in-memory arrays.
   2. Zero Database Operations: The engine must never read or write to a hard disk or SQL database mid-calculation. All data operations happen strictly in RAM. Relational database handoffs are completely forbidden until a prediction successfully passes the Stage 3 quality gate.

