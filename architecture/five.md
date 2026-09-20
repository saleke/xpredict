## 💳 Stage 5 Core Architecture: The Stripe Revenue Loop
The Stripe Payment & Tier Provisioning System is the financial engine of your SaaS platform. It connects user accounts directly to your backend permission layers.
Because we are serving real-time prediction alerts over WebSockets and private Telegram channels, the billing architecture must be completely automated, secure against unauthorized access, and capable of real-time tier synchronization.
------------------------------
## 🧱 The 3-Step Lifecycle of a Subscription

[ Step 1: Frontend Checkout ] ──> [ Step 2: Cryptographic Webhook ] ──> [ Step 3: Global Access Sync ]
 Next.js redirects user to       Stripe fires payment payload to     Supabase sets Tier status;
 Stripe Customer Portal.         API endpoint for validation.        Telegram Bot grants entry.

## 📦 Step 1: The Frictionless Frontend Portal
To ensure top-tier security and stay out of scope for strict financial compliance (PCI compliance), your Next.js application will never handle or store actual credit card numbers.

* The Workflow: When a user clicks "Upgrade to Tier 2" on your pricing dashboard, the web application makes a brief API request to your backend, initializes a Stripe Checkout Session, and redirects the user directly to a secure payment page hosted by Stripe.
* The Sync: When the payment clears, Stripe redirects the user back to your custom success screen (://yourplatform.com).

## 🔒 Step 2: The Cryptographic Webhook (The Gatekeeper)
The moment the user's payment is successful, Stripe's global servers immediately fire a secure POST request to your backend microservice endpoint: /v1/billing/webhook.
This endpoint operates under three strict security guardrails:

   1. Signature Verification: The code utilizes the Stripe SDK to verify an incoming header called Stripe-Signature against your unique STRIPE_WEBHOOK_SECRET. If any malicious user attempts to send a fake "payment successful" packet to trick your app into giving them free tier access, your server rejects it instantly.
   2. Deduplication Layer: Network issues can cause Stripe to send the same payment alert twice. The script captures Stripe's unique event_id and caches it inside Redis with a 24-hour expiration. If the same ID hits the server again, it drops it immediately to prevent profile corruption.
   3. Event Routing: The handler isolates three specific core events:
   * customer.subscription.created (User first subscribes → Unlock access)
      * invoice.payment_succeeded (Monthly renewal successful → Maintain access)
      * customer.subscription.deleted (Card failed or user canceled → Revoke access instantly)
   
## 🔄 Step 3: Global Tier Provisioning
Once the webhook validates the payment payload, the backend initiates an atomic update sequence across your delivery services:

* The Database Update: It targets the user_profiles table in Supabase/PostgreSQL [supabase.com]. It sets the subscription_status to ACTIVE and maps the subscription_tier to the exact product ID purchased (TIER_1 or TIER_2).
* The WebSocket Unblur: Because Supabase utilizes live row-level streaming replication, the user's active screen in the Next.js dashboard catches the database change instantly. The UI immediately unblurs the premium real-time data grid without requiring a page refresh.
* The Telegram Onboarding: The script calls your internal Telegram Bot microservice. It generates a single-use, time-expiring Telegram Private Invite Link for the specific channel the user paid for (e.g., Tier 2 Live Alerts). The bot emails or displays this link to the user. If they cancel their subscription via Stripe later, a deleted event triggers, and the bot uses the Telegram API to automatically kick them out of the group.

------------------------------
## 📂 Database Mapping for Stripe Metadata
To link a random Stripe credit card profile to a registered user account in your app, your coding agent must configure the user_profiles table inside Supabase to hold these exact lookup keys:

ALTER TABLE user_profiles ADD COLUMN stripe_customer_id VARCHAR(255);ALTER TABLE user_profiles ADD COLUMN stripe_subscription_id VARCHAR(255);ALTER TABLE user_profiles ADD COLUMN subscription_tier VARCHAR(50) DEFAULT 'FREE';ALTER TABLE user_profiles ADD COLUMN subscription_status VARCHAR(50) DEFAULT 'INACTIVE';

When a webhook arrives, the backend queries the database using the stripe_customer_id provided in the Stripe JSON payload, identifies the correct user profile, and applies the access adjustments.
------------------------------
## 🛠️ Execution Plan for Your Coding Agent
This is the explicit architectural prompt to hand over to your standby coding agent so they can build this complete billing synchronization layer.

Act as a Senior Backend Security Engineer. Write a complete, production-ready Python FastAPI script named `stripe_handler.py` that processes billing webhooks and manages tiered access control.

The module must fulfill the following operational criteria:

1. Setup & Inclusions:
   - Use the official `stripe` Python SDK.
   - Set up incoming data parsing for the POST endpoint `/v1/billing/webhook`.

2. Security Guardrails:
   - Read the raw request body along with the 'Stripe-Signature' header.
   - Use stripe.Webhook.construct_event() to validate cryptographically that the request originated from Stripe. Drop connections with a 400 error code on failure.

3. Idempotency Check:
   - Parse the incoming event.id.
   - Simulate a Redis client check: if the event.id exists in cache, return a 200 response immediately to prevent double-processing. If not, log it.

4. Event Routing Switch:
   - For 'customer.subscription.created' and 'invoice.payment_succeeded': Parse the stripe_customer_id and subscription data. Target the database profile row, set subscription_status to 'ACTIVE', and assign the correct tier mapping based on the price ID.
   - For 'customer.subscription.deleted': Target the database profile row, reset subscription_tier to 'FREE', and set status to 'INACTIVE'. Trigger a stubbed mock function named `revoke_telegram_access(customer_id)`.

Return a completely clean, functional python module with robust try-except error catching blocks. Do not add comments or pseudocode.
