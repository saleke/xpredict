## 🎨 Stage 4 Structural Architecture: The Transient Telemetry Cache & Audited Ledger
Stage 4 is LISA’s data distribution, storage, and monetization layer. Its absolute, non-negotiable objective is to handle user traffic securely, cache data efficiently to eliminate database bottlenecks, and serve real-time predictions based on subscriber payment tiers.
This stage bridges the mathematical output from Stage 3 into an active subscription business model, using Redis Stack for volatile data [redis.io] and Supabase (PostgreSQL) for permanent data and user profiles [supabase.com].
------------------------------
## 🧱 Architectural Layer Breakdown
The routing loop inside Stage 4 splits the data stream based on the validation flags established in Stage 3:

                  [ Ingest Stage 3 Routing Payload ]
                                   │
         ┌─────────────────────────┴─────────────────────────┐
         ▼ (routing_status: HOLD_SILENT)                     ▼ (routing_status: TRIGGER_ALERT)
 [ 4.1 Transient Cache Layer ]                       [ 4.2 Relational Storage Layer ]
   - Save to Redis JSON Hash                           - INSERT into Supabase PostgreSQL
   - Set 5-Hour Expiration TTL                         - Push to WebSockets (Next.js App)
   - Stays hidden from Users                           - Trigger Premium Telegram Bot API
                                                                     │
                                                                     ▼
                                                     [ 4.3 Automated Settlement Loop ]
                                                       - Cron Job pulls real final score
                                                       - Grades row as 'WIN' or 'LOSS'
                                                       - Updates Public Graded Ledger

## ⚡ Layer 4.1: The Transient Cache Layer (Redis Stack)
When a game results in a HOLD_SILENT flag, the database completely bypasses PostgreSQL.

* The Process: The system caches the match data inside Redis as an in-memory JSON object [redis.io].
* The Expiration Rule: The row is hardcoded with a 5-hour Time-to-Live (TTL).
* Why it saves money: By storing these low-certainty matches exclusively in memory, your primary PostgreSQL database avoids thousands of unneeded data entries, keeping your database footprint tiny and entirely free.

## 🗄️ Layer 4.2: The Relational Storage Layer (Supabase / PostgreSQL)
When a match hits a TRIGGER_ALERT status, it enters your primary database engine.

* The Process: The payload is instantly inserted into the lisa_predictions table in Supabase.
* Real-Time Broadcast: Supabase's real-time replication engine automatically intercepts the insert and broadcasts the new prediction to all active Next.js web dashboard grids over a secured WebSocket connection.
* Tier Filtering: The Next.js frontend checks the logged-in user’s profile token (Free, Tier 1, Tier 2, Tier 3). If a Tier 2 user is active, they see the live prediction immediately. If a Free user is active, the prediction is visually blurred out on their screen, with a prompt to upgrade via Stripe.

## 🤖 Layer 4.3: The Mass Distribution Node (Telegram Bot API)
Simultaneously, the backend triggers a lightweight POST request to the Telegram Bot API.

* The Process: LISA's automated bot crafts a premium alert message (e.g., "🔥 LISA HIGH-CONVICTION ALERT: NBA Matchup Boston Celtics has passed the 85% confidence gate. Click here to execute bet slip.").
* The Channels: The bot blasts this message into private, exclusive Telegram channels mapped directly to Tier 1 (Pre-Match) or Tier 2 (Live In-Play) subscription groups.

## 📈 Layer 4.4: The Automated Settlement Loop (The Graded Ledger)
To maintain absolute public trust and drive word-of-mouth sales, the system must grade itself automatically.

* The Process: Every hour, a lightweight background cron job queries the Sports API for completed match scores.
* The Grading: It compares the final score against LISA's Top Pick. The row in PostgreSQL is permanently updated with a status of WIN or LOSS. This table renders directly onto your public marketing landing page (://yourplatform.com) as an un-editable record of LISA's exact win rate.

------------------------------
## 📂 Operational Database Schema Mapping for Your Coding Agent
Pass these specific schema tables directly to your coding agent so they can build the permanent PostgreSQL database structures inside Supabase.
## 1. Table: lisa_predictions (Stores Verified Predictions)

CREATE TABLE lisa_predictions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    match_id VARCHAR(255) NOT NULL UNIQUE,
    sport VARCHAR(50) NOT NULL,
    home_team VARCHAR(100) NOT NULL,
    away_team VARCHAR(100) NOT NULL,
    selected_top_pick VARCHAR(150) NOT NULL,
    true_consensus_probability NUMERIC(5,4) NOT NULL,
    prediction_type VARCHAR(20) NOT NULL, -- 'PRE_MATCH' or 'LIVE_HALFTIME'
    settlement_status VARCHAR(20) DEFAULT 'PENDING', -- 'PENDING', 'WIN', 'LOSS'
    created_at TIMESTAMP WITH TIME ZONE DEFAULT TIMEZONE('utc'::text, NOW()) NOT NULL
);

## 2. Table: user_profiles (Manages Stripe Billing Access Tiers)

CREATE TABLE user_profiles (
    id UUID REFERENCES auth.users NOT NULL PRIMARY KEY,
    email VARCHAR(255) NOT NULL,
    stripe_customer_id VARCHAR(255),
    subscription_tier VARCHAR(50) DEFAULT 'FREE', -- 'FREE', 'TIER_1', 'TIER_2', 'TIER_3'
    subscription_status VARCHAR(50) DEFAULT 'INACTIVE', -- 'ACTIVE', 'CANCELED', 'PAST_DUE'
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT TIMEZONE('utc'::text, NOW()) NOT NULL
);

------------------------------
