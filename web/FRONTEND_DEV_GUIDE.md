# LISA Front-End Engineering Handover Guide

Welcome to the **LISA Institutional Sports Analytics & Quantitative Prediction Platform**! This guide is designed to get you productive immediately on the front-end codebase without needing to configure backend services.

---

## 1. Quick Start for Front-End Developers

The backend is completely self-contained with **zero third-party dependencies** (powered by Python 3.12 standard library, SQLite WAL mode, and a multi-threaded HTTP server).

### Running Locally
To start the production server with the web frontend and live REST API:
```bash
# From repository root:
python3 -m lisa start --port 8080
```
Open your browser at: **`http://localhost:8080/`**

### Pre-Flight Diagnostics
To inspect backend connectivity, database status, and Telegram bot health at any time:
```bash
python3 -m lisa doctor
```

---

## 2. Directory Structure & Architecture

```
web/
├── css/
│   ├── index.css            # Core design system tokens, typography, grid, tables, cards
│   └── auth.css             # Glassmorphic auth modals, user dropdowns, forms, toasts
├── js/
│   ├── api.js               # Structured REST API client SDK with cookie & error handling
│   ├── auth.js              # Stateful Auth Controller (sessions, login, logout, subscriptions)
│   ├── app.js               # Main page orchestrator (renders picks, ledger, KPIs, tabs)
│   ├── calculator.js        # Institutional Fractional Kelly Staking calculator module
│   └── charts.js            # Brier calibration & Murphy decomposition SVG chart renderer
├── data/
│   ├── dashboard.json       # Fallback cached ledger & rolling fixtures
│   └── verified_users.json  # Fallback Telegram verification registry
├── index.html               # Semantic, accessibility-friendly single page dashboard
└── FRONTEND_DEV_GUIDE.md    # This developer guide
```

---

## 3. Design System & CSS Tokens

LISA uses an institutional, high-contrast dark theme inspired by Bloomberg Terminal and Linear. All styles are defined with CSS custom properties in [`css/index.css`](css/index.css) and [`css/auth.css`](css/auth.css).

### Core Palette
```css
--bg-primary: #07090e;        /* Canvas background */
--bg-card: #0d121d;           /* Component card background */
--bg-card-hover: #131a2a;     /* Interactive card hover */
--border-subtle: rgba(255, 255, 255, 0.08); /* Clean crisp dividers */
--border-card: rgba(255, 255, 255, 0.12);

/* Accent Colors */
--accent-blue: #3b82f6;       /* Primary CTA & hyperlinks */
--accent-cyan: #06b6d4;       /* Institutional highlights & badges */
--accent-emerald: #10b981;    /* Winning outcomes, positive EV, high confidence */
--accent-gold: #f59e0b;       /* Marquee pivots & warnings */
--accent-rose: #f43f5e;       /* Loss outcomes, slippage drift */
--text-primary: #ffffff;      /* Primary text */
--text-secondary: #94a3b8;    /* Subtext & descriptions */
--text-muted: #64748b;        /* Minor metadata & timestamps */
```

### Typography
- **UI Font**: `'Inter', -apple-system, sans-serif`
- **Tabular / Monospace Font**: `'JetBrains Mono', monospace` (applied via `.tabular-nums` for alignment of odds, odds percentages, and financial scores).

### Radii & Transitions
```css
--radius-sm: 6px;
--radius-md: 10px;
--radius-lg: 14px;
--radius-xl: 20px;
--radius-pill: 9999px;
--transition-fast: 0.15s ease;
--transition-normal: 0.25s ease;
```

---

## 4. Production Authentication System

The backend features an OWASP-compliant, zero-dependency authentication system using **PBKDF2-HMAC-SHA256** (600,000 iterations) with 32-byte cryptographic random salts and 256-bit URL-safe session tokens.

### How Authentication Works
1. **Cookie-Based Sessions**: Successful login/signup issues an `HttpOnly; SameSite=Lax; Path=/` session cookie named `lisa_session` (valid for 30 days).
2. **Bearer Token Fallback**: The client SDK also supports `Authorization: Bearer <session_id>` for non-browser clients.
3. **Session Persistence**: On page load, `auth.init()` automatically queries `GET /api/auth/me` to refresh the session state without requiring user re-entry.

### Client-Side Auth Controller (`js/auth.js`)
Use `auth` anywhere in your scripts to interact with the authentication engine:

```javascript
import { auth } from './auth.js';

// 1. Check if user is logged in
if (auth.isAuthenticated()) {
  const user = auth.getUser();
  console.log('Logged in as:', user.email, 'Tier:', user.tier);
}

// 2. React to login, logout, or tier changes
const unsubscribe = auth.onAuthStateChanged((user) => {
  if (user) {
    console.log('User logged in:', user.display_name);
  } else {
    console.log('User is logged out');
  }
});

// 3. Perform Sign In
try {
  const user = await auth.signin('trader@example.com', 'SecurePassword123!');
  console.log('Signed in:', user);
} catch (err) {
  console.error('Login error:', err.message);
}

// 4. Perform Sign Up
try {
  const newUser = await auth.signup('new@example.com', 'Pass12345678!', 'Trader Joe', 'tier2');
} catch (err) {
  console.error('Signup error:', err.message);
}

// 5. Sign Out
await auth.signout();
```

---

## 5. REST API Client SDK (`js/api.js`)

All network requests are pre-configured with `credentials: 'include'` to pass session cookies seamlessly.

### API Reference Table

| Method | Endpoint | Description | Auth Required |
| :--- | :--- | :--- | :---: |
| `POST` | `/api/auth/signup` | Register user (`email`, `password`, `display_name`, `tier`) | No |
| `POST` | `/api/auth/signin` | Log in user (`email`, `password`) | No |
| `POST` | `/api/auth/signout` | Invalidate active session & clear cookie | Yes |
| `GET` | `/api/auth/me` | Retrieve profile of authenticated user | Session Cookie / Bearer |
| `POST` | `/api/auth/link-telegram` | Link Telegram ID (`telegram_id`, `telegram_username`) | Yes |
| `GET` | `/api/picks` | Active picks (unmasked according to tier & Telegram status) | Optional |
| `GET` | `/api/ledger` | 50+ audited settled match records & CLV metrics | No |
| `GET` | `/api/verify-status` | Check Telegram channel membership (`user_id`) | No |
| `GET` | `/api/status` | Backend health, database latency, active daemons | No |

### Usage Example with `api.js`:
```javascript
import { api } from './api.js';

// Fetch active predictions for Tier 2 view
const response = await api.picks.get({ tier: 'tier2' });
console.log('Active picks:', response.active_picks);

// Fetch audited ledger
const ledger = await api.ledger.get();
console.log('Settled matches:', ledger.settled_ledger);

// Fetch system health
const health = await api.status.get();
console.log('System status:', health.status, 'Storage:', health.storage_driver);
```

---

## 6. Commercial 4-Tier Gating Architecture

Predictions on the dashboard are structured into 4 distinct commercial tiers:

| Tier Key | Public Name | Price | Features & Access |
| :--- | :--- | :--- | :--- |
| `free` | **Free Access** | $0 | **Match #1** completely open. **Matches #2 & #3** unlock via Telegram. Matches #4–#12 locked. |
| `tier1` | **Tier 1 Pro** | $19/mo | Top 5 Grade A Flagship Diamonds with EV &gt; 2.5%. |
| `tier2` | **Tier 2 Syndicate** | $49/mo | Full access to all 12 picks (Flagship Diamonds + Smart Market Pivots). |
| `tier3` | **Tier 3 VIP Alpha** | $149/mo | Syndicate Alpha Terminal: instant raw odds consensus delta, Sharp volume indicators, CLV drift alarms. |

### Server-Enforced Prediction Masking
- The server strips `outcome_name`, `best_odds`, `fair_odds`, `best_ev`, and deep execution links for locked predictions.
- **You do not need to hide predictions with CSS alone!** The server enforces this truth. Even if a user inspects network DevTools, locked picks will contain `is_locked: true` and sanitized outcomes until the user logs in with the appropriate tier or confirms Telegram membership.

---

## 7. UI Components & Extension Points

Here are the key UI components you can enhance:

1. **Auth Modal (`#auth-modal` in `index.html`)**:
   - Styled in [`css/auth.css`](css/auth.css).
   - Form switching handled in `setupAuthUI()` inside [`js/app.js`](js/app.js).
   - Feel free to split into dedicated Web Component or template if you wish.

2. **Toast Notifications**:
   - Trigger anytime using:
     ```javascript
     import { showToast } from './app.js';
     showToast('Prediction copied to clipboard!', 'success');
     showToast('Network latency detected.', 'error');
     ```

3. **Picks Grid (`#picks-grid` in `index.html`)**:
   - Rendered by `renderPicks()` in [`js/app.js`](js/app.js).
   - Supports filtering by Grade (`all`, `GRADE_A`, `GRADE_B`, `GRADE_C`) and Sport (`soccer_epl`, `basketball_nba`, etc.).

4. **Institutional Kelly Calculator (`js/calculator.js`)**:
   - Interactive modal calculating recommended fractional bankroll stakes based on Kelly criterion and Poisson edge.

5. **Calibration & Reliability Charts (`js/charts.js`)**:
   - Renders SVG calibration reliability diagrams and Brier decomposition histograms.

---

## 8. Suggested Next Steps for the Front-End Engineer

- [ ] **Account Settings Drawer / Page**: Allow users to change display name, update password, or view active session tokens.
- [ ] **Stripe / Payment Gateway Integration**: Connect the Tier Upgrade buttons (`#btn-upgrade-tier2`) to Stripe Checkout or LemonSqueezy.
- [ ] **Live Odds Ticker**: Add a subtle animated ticker bar at the top showing real-time line movement deltas between Pinnacle and DraftKings.
- [ ] **Push Notification Webhook / PWA**: Enable desktop browser push notifications for Tier 1 Flagship Diamond alerts before kickoff.

Happy building! If you have questions regarding the backend architecture, run `python3 -m lisa doctor` or inspect [`engine/lisa/server.py`](../engine/lisa/server.py).
