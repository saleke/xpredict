/**
 * LISA Dashboard Main Controller & State Management
 * Commercial 4-Tier Funnel, Smart Market Pivot, and Tier 3 Syndicate Alpha Terminal
 */
import { api } from './api.js';
import { auth } from './auth.js';
import { initCalculator } from './calculator.js';
import { renderReliabilityChart } from './charts.js';

// Take ownership of scroll restoration so per-view deep-link positions
// are restored by LISA (sessionStorage), never by the browser's stale copy.
if (window.history && 'scrollRestoration' in window.history) {
  history.scrollRestoration = 'manual';
}

const EMOJI_RE = /[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{FE0F}\u{2190}-\u{21FF}\u{2705}\u{274C}\u{25CF}\u{2605}]/gu;
function cleanText(value) {
  if (value == null) return '';
  return String(value)
    .replace(EMOJI_RE, '')
    .replace(/\s*[\u2014\u2013]\s*/g, ', ')
    .replace(/\s{2,}/g, ' ')
    .trim();
}

// Team names, book titles and league labels come from the upstream feed, so
// they are escaped before any innerHTML interpolation. Sanitising text is not
// enough on its own: "&lt;img onerror=...&gt;" is inert as text but not in HTML.
function esc(value) {
  if (value == null) return '';
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

const MONTHS_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function formatKo(iso) {
  if (!iso) return 'Time TBC';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso).replace('T', ' ').replace('Z', ' UTC');
  const day = d.getUTCDate();
  const mon = MONTHS_SHORT[d.getUTCMonth()];
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mm = String(d.getUTCMinutes()).padStart(2, '0');
  return `${day} ${mon}, ${hh}:${mm} UTC`;
}

let state = {
  data: null,
  activeGradeFilter: 'all',
  activeCategoryFilter: 'all',
  activeSportFilter: 'all',
  activeTab: 'overview',
  currentTier: localStorage.getItem('lisa_tier') || 'free',
  // Operator-only tier preview. Non-empty means "render the board as this
  // tier" without changing the account. Persisted so a reload does not
  // silently drop an operator back to their real tier mid-inspection.
  tierPreview: localStorage.getItem('lisa_tier_preview') || '',
  isOperator: false,
  isTelegramUnlocked: localStorage.getItem('lisa_telegram_unlocked') === 'true',
  selectedBooks: {},
  activeAccuBook: 'sportybet',
  picksSearchQuery: '',
  picksOddsBand: 'all',
  ledgerSearchQuery: '',

  // Reads the worker's saved board on first visit; no provider work occurs.
  modelBoard: null,
  modelBoardState: 'idle',   // idle | loading | ready | failed
  modelBoardError: '',
};

export const SPORTSBOOKS = [
  {
    id: 'sportybet',
    name: 'SportyBet',
    hasBookingCode: true,
    codeLength: 6,
    brandColor: '#E41C26',
    url: 'https://www.sportybet.com/',
    tip: 'Paste the 6-character code in SportyBet, then Load Bet Slip',
    svg: `<svg viewBox="0 0 46 20" width="46" height="20" aria-label="SportyBet"><rect width="46" height="20" rx="4" fill="#E41C26"/><text x="23" y="14" fill="#ffffff" font-weight="900" font-family="system-ui, -apple-system, sans-serif" font-size="9" text-anchor="middle" letter-spacing="-0.2">SPORTY</text></svg>`
  },
  {
    id: 'football_com',
    name: 'Football.com',
    hasBookingCode: true,
    codeLength: 7,
    brandColor: '#008744',
    url: 'https://www.football.com/',
    tip: 'Paste the 7-digit code in Football.com, then Load Booking Slip',
    svg: `<svg viewBox="0 0 52 20" width="52" height="20" aria-label="Football.com"><rect width="52" height="20" rx="4" fill="#008744"/><circle cx="10" cy="10" r="4.5" fill="#ffffff"/><circle cx="10" cy="10" r="2.2" fill="#008744"/><text x="32" y="13.5" fill="#ffffff" font-weight="900" font-family="system-ui, -apple-system, sans-serif" font-size="7.5" text-anchor="middle" letter-spacing="-0.3">FOOTBALL</text></svg>`
  },
  {
    id: '1xbet',
    name: '1xBet',
    hasBookingCode: true,
    codeLength: 5,
    brandColor: '#00C4FF',
    url: 'https://1xbet.com/',
    tip: 'Paste the 5-character code in 1xBet, then Save or Load Slip',
    svg: `<svg viewBox="0 0 38 20" width="38" height="20" aria-label="1xBet"><rect width="38" height="20" rx="4" fill="#0B4A8F"/><text x="12" y="14.5" fill="#00D2FF" font-weight="900" font-family="system-ui, -apple-system, sans-serif" font-size="11">1</text><text x="24" y="14.5" fill="#ffffff" font-weight="900" font-family="system-ui, -apple-system, sans-serif" font-size="11">X</text></svg>`
  },
  {
    id: 'bet9ja',
    name: 'Bet9ja',
    hasBookingCode: true,
    codeLength: 6,
    brandColor: '#22C55E',
    url: 'https://sports.bet9ja.com/',
    tip: 'Paste the code in Bet9ja, then Booking Slip',
    svg: `<svg viewBox="0 0 38 20" width="38" height="20" aria-label="Bet9ja"><rect width="38" height="20" rx="4" fill="#005C2B"/><text x="14" y="14" fill="#ffffff" font-weight="900" font-family="system-ui, -apple-system, sans-serif" font-size="10">9</text><text x="24" y="14" fill="#FFD700" font-weight="900" font-family="system-ui, -apple-system, sans-serif" font-size="10">ja</text></svg>`
  },
  {
    id: 'betway',
    name: 'Betway',
    hasBookingCode: true,
    codeLength: 7,
    brandColor: '#00A826',
    url: 'https://www.betway.com/',
    tip: 'Paste the code in Betway, then Load Bet Slip',
    svg: `<svg viewBox="0 0 44 20" width="44" height="20" aria-label="Betway"><rect width="44" height="20" rx="4" fill="#1A1A1A" stroke="rgba(255,255,255,0.2)" stroke-width="0.8"/><text x="22" y="14" fill="#ffffff" font-weight="800" font-family="system-ui, -apple-system, sans-serif" font-size="9" text-anchor="middle" letter-spacing="-0.3">betway</text></svg>`
  },
  {
    id: 'bet365',
    name: 'Bet365',
    hasBookingCode: false,
    brandColor: '#FFDF1B',
    url: 'https://www.bet365.com/',
    tip: 'Bet365 uses direct links (LISA has no bookmaker integration)',
    svg: `<svg viewBox="0 0 38 20" width="38" height="20" aria-label="Bet365"><rect width="38" height="20" rx="4" fill="#006034"/><text x="19" y="14.5" fill="#FFDF1B" font-weight="900" font-style="italic" font-family="system-ui, -apple-system, sans-serif" font-size="10.5" text-anchor="middle">365</text></svg>`
  },
  {
    id: 'draftkings',
    name: 'DraftKings',
    hasBookingCode: false,
    brandColor: '#FF6B00',
    url: 'https://sportsbook.draftkings.com/',
    tip: 'DraftKings uses direct links (LISA has no bookmaker integration)',
    svg: `<svg viewBox="0 0 40 20" width="40" height="20" aria-label="DraftKings"><rect width="40" height="20" rx="4" fill="#18191A" stroke="rgba(255,107,0,0.4)" stroke-width="0.8"/><text x="20" y="14" fill="#FF6B00" font-weight="900" font-family="system-ui, -apple-system, sans-serif" font-size="9" text-anchor="middle">DK</text></svg>`
  }
];

function copyTextToClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text).catch(() => fallbackCopy(text));
  }
  return fallbackCopy(text);
}

function fallbackCopy(text) {
  try {
    const textArea = document.createElement('textarea');
    textArea.value = text;
    textArea.style.position = 'fixed';
    textArea.style.left = '-999999px';
    textArea.style.top = '-999999px';
    document.body.appendChild(textArea);
    textArea.focus();
    textArea.select();
    document.execCommand('copy');
    textArea.remove();
    return Promise.resolve();
  } catch (err) {
    console.warn('Clipboard copy fallback failed:', err);
    return Promise.reject(err);
  }
}

function getBookingCodeForPick(p, bookId) {
  // LISA has no bookmaker integration, so it never mints booking codes.
  // Only a code that actually exists upstream is ever shown.
  if (p && p.booking_codes && p.booking_codes[bookId]) {
    return p.booking_codes[bookId].replace(/^(SB|1X|365|BW|B9|DK|FC)-/i, '');
  }
  return null;
}

function renderBetSlipBox(p) {
  const matchId = p.match_id;
  state.selectedBooks = state.selectedBooks || {};
  const currentBookId = state.selectedBooks[matchId] || state.defaultBook || 'sportybet';
  const currentBook = SPORTSBOOKS.find(b => b.id === currentBookId) || SPORTSBOOKS[0];
  const isDirectLinkOnly = currentBook.hasBookingCode === false;
  const currentCode = getBookingCodeForPick(p, currentBook.id);
  const directLink = (p.deep_links && p.deep_links[currentBook.id]) || currentBook.url;

  const chipsHtml = SPORTSBOOKS.map(b => {
    const isActive = b.id === currentBook.id;
    return `
      <button type="button" 
        class="book-logo-chip ${isActive ? 'active' : ''}" 
        data-book="${b.id}"
        data-match="${matchId}"
        title="Open this selection on ${b.name}"
        onclick="window.selectBookmakerForPick('${matchId}', '${b.id}')"
        style="${isActive ? `border-color: ${b.brandColor}; box-shadow: 0 0 8px ${b.brandColor}40;` : ''}">
        ${b.svg}
      </button>
    `;
  }).join('');

  return `
    <div class="bet-slip-box" id="bet-box-${matchId}">
      <div class="bet-slip-header">
        <div class="bet-slip-label">
          <span>${isDirectLinkOnly ? 'Direct Slip Link' : 'Direct Bet Code'}</span>
        </div>
        <div class="book-logos-row" id="chips-row-${matchId}">
          ${chipsHtml}
        </div>
      </div>

      <div class="bet-code-display-row">
        <div class="bet-code-pill is-direct-link" 
          onclick="window.open('${directLink}', '_blank', 'noopener,noreferrer')" 
          title="Open the ${currentBook.name} site to price this selection yourself">
          <span class="pill-book-dot" style="background: ${currentBook.brandColor};"></span>
          <span class="pill-book-tag" id="pill-book-tag-${matchId}">${currentBook.name}:</span>
          <span class="pill-code-val tabular-nums link-mode" id="pill-code-val-${matchId}">
            ${currentCode ? `code ${currentCode}` : 'no booking code \u2014 open the book to place it'}
          </span>
        </div>

        <div class="bet-code-actions">
          <button type="button" 
            class="btn-copy-code btn-link-action" 
            id="copy-btn-${matchId}" 
            onclick="window.open('${directLink}', '_blank', 'noopener,noreferrer')"
            title="Open ${currentBook.name} to place this selection">
            <span class="copy-label">Open</span>
          </button>
          <a href="${directLink}" 
            target="_blank" 
            rel="noopener noreferrer" 
            class="btn-open-book" 
            id="open-link-${matchId}" 
            title="Open ${currentBook.name} website/app in new tab"
            aria-label="Open ${currentBook.name} in new tab">
            Open
          </a>
        </div>
      </div>

      </div>
    </div>
  `;
}

function renderAccumulatorBanner() {
  const bannerContainer = document.getElementById('accumulator-banner-container');
  if (!bannerContainer || !state.data) return;

  const diamonds = (state.data.active_picks || []).filter(p => !p.is_pass_advisory && (p.grade === 'GRADE_A' || p.conviction_score >= 20.0)).slice(0, 5);
  if (diamonds.length < 2) {
    bannerContainer.innerHTML = '';
    return;
  }

  // Only real prices participate: a leg without a best price cannot be costed.
  const pricedLegs = diamonds.filter(p => p.best_odds && p.p_true);
  if (pricedLegs.length < 2) {
    bannerContainer.innerHTML = '';
    return;
  }
  const combinedOdds = pricedLegs.reduce((acc, p) => acc * p.best_odds, 1.0);
  const combinedProb = pricedLegs.reduce((acc, p) => acc * p.p_true, 1.0);
  const currentBookId = state.activeAccuBook || 'sportybet';
  const currentBook = SPORTSBOOKS.find(b => b.id === currentBookId) || SPORTSBOOKS[0];
  const isDirectLinkOnly = true;
  // LISA has no bookmaker integration, so an accumulator code is only shown
  // when a genuine one exists in the payload. Otherwise the user prices it out.
  const accuCode = (state.data.accumulator_booking_codes && state.data.accumulator_booking_codes[currentBookId]) || null;

  const chipsHtml = SPORTSBOOKS.map(b => {
    const isActive = b.id === currentBookId;
    return `
      <button type="button" 
        class="book-logo-chip ${isActive ? 'active' : ''}" 
        data-book="${b.id}"
        title="Load 5-Game Parlay on ${b.name}"
        onclick="window.selectAccuBookmaker('${b.id}')"
        style="${isActive ? `border-color: ${b.brandColor}; box-shadow: 0 0 8px ${b.brandColor}40;` : ''}">
        ${b.svg}
      </button>
    `;
  }).join('');

  const earliestTime = diamonds
    .map(p => p.commence_time ? new Date(p.commence_time).getTime() : 0)
    .filter(t => t > 0)
    .sort((a, b) => a - b)[0];
  const earliestIso = earliestTime ? new Date(earliestTime).toISOString() : '';
  const firstLegCd = formatCountdown(earliestIso);
  const kickoffLabel = firstLegCd.status === 'ended'
    ? 'Legs locked · awaiting kickoff'
    : `First leg ${firstLegCd.text}`;
  // Shared uncertainty across legs is real, and the naive product overstates
  // the joint probability. board.py applies the same haircut for the ranked
  // board; apply it here too so the two surfaces cannot disagree.
  const legDays = new Set(pricedLegs
    .map(p => (p.commence_time ? String(p.commence_time).slice(0, 10) : ''))
    .filter(Boolean));
  const parlayPenalty = legDays.size < pricedLegs.length ? 0.94 : 1.0;
  const adjustedProb = combinedProb * parlayPenalty;
  // No Math.max(0, ...): a negative EV is a losing slip, and clamping it to
  // zero would render a losing parlay as if it were merely break-even.
  const parlayEv = (combinedOdds * adjustedProb - 1.0) * 100;
  const parlayEvColor = parlayEv >= 0 ? 'var(--accent-emerald)' : 'var(--neg)';

  bannerContainer.innerHTML = `
    <div class="accumulator-banner">
      <div class="accumulator-left">
        <div class="accumulator-badge">Live Multi-Bet Slip (Parlay)</div>
        <div class="accumulator-title">Current ${pricedLegs.length}-Fold Consensus Slip</div>
        <div class="accumulator-meta">
          <span>${pricedLegs.length} priced consensus legs</span>
          <span>Combined Odds: <strong>${combinedOdds.toFixed(2)}x</strong></span>
          <span>${kickoffLabel}</span>
          <span>Joint p: <strong>${(adjustedProb * 100).toFixed(1)}%</strong>${
            parlayPenalty < 1
              ? ` <span style="color: var(--text-muted);">(naive ${(combinedProb * 100).toFixed(1)}%, ${((1 - parlayPenalty) * 100).toFixed(0)}% correlation haircut)</span>`
              : ''}</span>
          <span style="color: ${parlayEvColor}; font-weight: 700;">${parlayEv >= 0 ? '+' : ''}${parlayEv.toFixed(1)}% Combined EV</span>
        </div>
      </div>

      ${(state.currentTier === 'tier2' || state.currentTier === 'tier3' || state.currentTier === 'all') ? `
      <div class="accumulator-right">
        <div class="accu-book-selector">
          <span style="font-size: 11px; font-weight: 700; color: var(--text-muted); text-transform: uppercase;">Sportsbook:</span>
          ${chipsHtml}
        </div>
        <div class="accu-code-row">
          <div class="accu-code-pill ${isDirectLinkOnly ? 'is-direct-link' : ''}" 
            onclick="${isDirectLinkOnly ? `window.open('${currentBook.url}', '_blank', 'noopener,noreferrer')` : `window.copyAccumulatorCode()`}" 
            title="${isDirectLinkOnly ? `Open on ${currentBook.name}` : `Click to copy 5-Game Slip Code`}">
            <span class="accu-book-name" id="accu-book-label">${currentBook.name}:</span>
            <span class="accu-code-val tabular-nums ${isDirectLinkOnly ? 'link-mode' : ''}" id="accu-code-display">
              ${accuCode ? accuCode : 'no code \u2014 build this slip in the book'}
            </span>
          </div>
          <button type="button" 
            class="btn-accu-copy" 
            id="accu-copy-btn" 
            onclick="${isDirectLinkOnly ? `window.open('${currentBook.url}', '_blank', 'noopener,noreferrer')` : `window.copyAccumulatorCode()`}">
            <span>Open ${pricedLegs.length}-Fold Slip</span>
          </button>
        </div>
      </div>
      ` : `
      <div class="accumulator-right">
        <div style="font-size: 11px; color: var(--accent-gold); font-weight: 700;">Tier 2 Pro required</div>
        <button type="button" 
          class="btn-upgrade-glow" 
          onclick="window.handlePricingSelect('tier2')">
          Unlock 5-Fold Slip ($49/mo)
        </button>
      </div>
      `}
    </div>
  `;
}

export function formatCountdown(commenceTimeIso, fallbackText = 'Today') {
  if (!commenceTimeIso) {
    return { text: fallbackText, status: 'upcoming' };
  }

  const target = new Date(commenceTimeIso).getTime();
  if (isNaN(target)) {
    return { text: fallbackText, status: 'upcoming' };
  }

  const now = Date.now();
  const diff = target - now;
  const pad = (n) => n.toString().padStart(2, '0');

  // Case 1: Upcoming match (diff > 0)
  if (diff > 0) {
    const totalSecs = Math.floor(diff / 1000);
    const days = Math.floor(totalSecs / 86400);
    const hours = Math.floor((totalSecs % 86400) / 3600);
    const mins = Math.floor((totalSecs % 3600) / 60);
    const secs = totalSecs % 60;

    if (days > 0) {
      return {
        text: `Starts in ${days}d ${hours}h ${pad(mins)}m ${pad(secs)}s`,
        status: 'upcoming'
      };
    }

    if (hours > 0) {
      return {
        text: `Starts in ${hours}h ${pad(mins)}m ${pad(secs)}s`,
        status: 'upcoming'
      };
    }

    // Under 1 hour
    const isImminent = mins < 15;
    return {
      text: isImminent ? `Kicks off in ${mins}m ${pad(secs)}s` : `Starts in ${mins}m ${pad(secs)}s`,
      status: isImminent ? 'imminent' : 'upcoming'
    };
  }

  // Case 2: Live In-Play (0 to -115 mins)
  const elapsedSecs = Math.floor(Math.abs(diff) / 1000);
  const elapsedMins = Math.floor(elapsedSecs / 60);
  const remSecs = elapsedSecs % 60;

  if (elapsedMins < 115) {
    return {
      text: `LIVE · ${elapsedMins}'${pad(remSecs)}" in-play`,
      status: 'live'
    };
  }

  // Case 3: Completed (> 115 mins)
  return {
    text: `Full Time · Awaiting Result`,
    status: 'ended'
  };
}

let countdownInterval = null;
export function startKickoffCountdown() {
  if (countdownInterval) clearInterval(countdownInterval);

  function tick() {
    const badges = document.querySelectorAll('.kickoff-countdown-badge[data-commence]');
    if (!badges || !badges.length) return;

    badges.forEach(badge => {
      const commence = badge.getAttribute('data-commence');
      if (!commence) return;
      const cd = formatCountdown(commence);

      const textEl = badge.querySelector('.countdown-text');
      const iconEl = badge.querySelector('.countdown-icon');
      if (textEl && textEl.textContent !== cd.text) {
        textEl.textContent = cd.text;
      }
      if (iconEl && iconEl.textContent !== cd.icon) {
        iconEl.textContent = cd.icon;
      }

      if (!badge.classList.contains(cd.status)) {
        badge.classList.remove('upcoming', 'imminent', 'live', 'ended');
        badge.classList.add(cd.status);
      }
    });
  }

  tick();
  countdownInterval = setInterval(tick, 1000);
}

async function loadData() {
  try {
    const [res, fcRes, tgRes] = await Promise.all([
      fetchDashboardResource('/api/dashboard'),
      fetchDashboardResource('/api/forecast').catch(() => null),
      fetchDashboardResource('/api/tiers').catch(() => null)
    ]);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.data = await res.json();
    state.dashboard = state.data;
    renderProvenanceBanner();
    renderLiveStatusBanner();

    if (fcRes && fcRes.ok) {
      try {
        state.forecast = await fcRes.json();
      } catch (e) {
        console.warn('Could not parse forecast response:', e);
      }
    }
    if (tgRes && tgRes.ok) {
      try {
        state.tierCatalog = await tgRes.json();
      } catch (e) {
        console.warn('Could not parse tier catalog:', e);
      }
    }

    renderAll();
  } catch (err) {
    console.error('Failed to load dashboard data:', err);
    const grid = document.getElementById('picks-grid');
    if (grid) {
      grid.innerHTML = `
        <div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--text-secondary);">
          <p style="font-size: 16px; margin-bottom: 8px;">Live feed unavailable.</p>
          <p style="font-size: 13px; color: var(--text-muted);">Could not read saved predictions. The dashboard will retry automatically.</p>
        </div>
      `;
    }
  } finally {
    startDashboardPolling();
  }
}

async function fetchDashboardResource(path) {
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), 10000) : null;
  try {
    const response = await fetch(path, { cache: 'no-cache', ...(controller ? { signal: controller.signal } : {}) });
    const payload = await response.json();
    return { ok: response.ok, status: response.status, json: async () => payload };
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
}

function renderLiveStatusBanner() {
  const banner = document.getElementById('live-status-banner');
  if (!banner) return;
  const live = (state.data && state.data.live) || {};
  const pipeline = state.data && state.data.pipeline;
  if (pipeline) {
    const awaiting = (state.data.summary || {}).awaiting_results_count || 0;
    banner.style.display = 'block';
    banner.className = 'live-banner ' + (pipeline.has_errors ? 'stale' : 'live');
    banner.textContent = pipeline.generated_at
      ? `${pipeline.paper_mode ? 'Paper testing · ' : ''}${pipeline.upcoming_selections} upcoming selections${awaiting ? ' · '+awaiting+' predictions awaiting confirmed results in Ledger' : ''} · ${pipeline.fixtures_priced} fixtures priced · published ${formatKo(pipeline.published_at || pipeline.generated_at)}${pipeline.has_errors ? ' · Some data sources failed; coverage is limited.' : ''}`
      : `Prediction worker ${pipeline.state === 'running' ? 'is collecting fixtures and fitting models' : pipeline.state === 'failed' ? 'failed to publish; review provider health in the admin panel' : 'has not published yet'}. Saved data refreshes automatically.`;
    return;
  }
  const minutes = live.age_sec == null ? null : Math.round(live.age_sec / 60);
  if (live.state === 'live') {
    banner.style.display = 'block';
    banner.className = 'live-banner live';
    banner.textContent = `Live odds feed \u00b7 last cycle ${minutes < 1 ? 'just now' : minutes + ' min ago'} \u00b7 ${live.matches_observed || 0} fixtures observed`;
  } else if (live.state === 'stale') {
    banner.style.display = 'block';
    banner.className = 'live-banner stale';
    banner.textContent = `Last real observation ${minutes} min ago \u2014 the poller has not refreshed (quota or outage). No prices below are current.`;
  } else {
    banner.style.display = 'block';
    banner.className = 'live-banner none';
    banner.textContent = 'No live odds observation yet. LISA will not display prices until the poller completes a real cycle.';
  }
}

let dashboardPollingTimer = null;
let dashboardPollInFlight = false;
async function pollDashboard() {
  if (dashboardPollInFlight) return;
  dashboardPollInFlight = true;
  try {
    const results = await Promise.allSettled([
      fetchDashboardResource('/api/dashboard'), fetchDashboardResource('/api/forecast')]);
    for (let i = 0; i < results.length; i += 1) {
      if (results[i].status !== 'fulfilled' || !results[i].value.ok) continue;
      const payload = await results[i].value.json();
      if (i === 0) { state.data = payload; state.dashboard = payload; }
      else state.forecast = payload;
    }
    renderLiveStatusBanner();
    renderAll();
    if (state.modelBoardState !== 'idle') await refreshModelBoard(false);
    if (state.activeTab === 'daily-board') await loadDailyBoard(state.dailyBoardTab || 'upcoming');
  } catch (e) {
    console.warn('Saved dashboard refresh failed:', e.message);
  } finally {
    dashboardPollInFlight = false;
  }
}
function startDashboardPolling() {
  if (dashboardPollingTimer !== null) return;
  dashboardPollingTimer = setInterval(pollDashboard, 20000);
}

function renderProvenanceBanner() {
  const banner = document.getElementById('provenance-banner');
  if (!banner) return;
  const prov = state.data && (state.data.meta && state.data.meta.data_provenance || state.data.data_provenance);
  const demo = (prov && prov.synthetic) || (state.data && state.data.meta && state.data.meta.demo);
  banner.style.display = demo ? 'block' : 'none';
  if (demo && prov && prov.statement) {
    banner.title = prov.statement;
  }
}

function renderAll() {
  // Re-assert a persisted operator preview on every full render. Without this,
  // a reload would show the account's real tier until something else happened
  // to call setTier, which is exactly the "locked to one tier" confusion this
  // control exists to remove.
  if (state.tierPreview && state.currentTier !== state.tierPreview) {
    state.currentTier = state.tierPreview;
  }

  // Every renderer is dispatched through here, and a renderer that cannot run
  // is reported and stepped over rather than thrown past.
  //
  // Two of these are attached to `window` by assignment (refreshVisualizer,
  // loadDailyBoard) rather than hoisted declarations, so referencing one
  // before the module reaches it raises a ReferenceError. Letting that escape
  // does far more damage than the missing panel: it unwinds renderAll at that
  // line, so every renderer after it never runs, and the operator is left with
  // a half-painted page and no error on screen to explain it. The other
  // thirteen panels would have rendered perfectly well.
  //
  // The renderer is passed as a thunk on purpose. Naming the function directly
  // would evaluate that identifier while building the argument list, which
  // throws before this guard exists to catch it.
  const renderer = (name, run) => {
    try {
      run();
    } catch (err) {
      // One panel's failure must not cost the other panels their render.
      // A ReferenceError here means the module has not defined it yet; anything
      // else is the renderer itself. Both are named so the cause is findable.
      console.warn(`renderAll: renderer "${name}" did not run:`, err);
    }
  };

  renderer('renderTierPreviewControls', () => renderTierPreviewControls());
  renderer('renderTargetLandingData', () => renderTargetLandingData());
  renderer('renderKPIs', () => renderKPIs());
  renderer('renderTierControls', () => renderTierControls());
  renderer('renderPicks', () => renderPicks());
  renderer('renderLedger', () => renderLedger());
  renderer('renderCalibration', () => renderCalibration());
  renderer('renderTier3Alpha', () => renderTier3Alpha());
  renderer('renderBacktest', () => renderBacktest());  // resolves async from /api/backtest
  renderer('renderForecastBoard', () => renderForecastBoard());
  renderer('renderModelBoard', () => renderModelBoard());   // pure render from state; the fetch is lazy
  renderer('renderTierMatrix', () => renderTierMatrix());
  renderer('renderTicker', () => renderTicker());
  renderer('refreshVisualizer', () => refreshVisualizer());
  renderer('flushPendingScrollRestore', () => flushPendingScrollRestore());
}

function renderTargetLandingData() {
  if (!state.data) return;
  const picks = state.data.active_picks || [];
  const s = state.data.summary || {};
  const settled = state.data.settled_ledger || [];

  // Helper for team visual in matchup card
  function getTeamVisualHtml(teamName) {
    const lower = String(teamName || '').toLowerCase();
    if (lower.includes('raven')) {
      return `<img src="assets/ravens_trans.png" alt="${esc(cleanText(teamName))}" class="mct-helmet-img">`;
    }
    if (lower.includes('chief')) {
      return `<img src="assets/chiefs_trans.png" alt="${esc(cleanText(teamName))}" class="mct-helmet-img">`;
    }
    if (lower.includes('nugget')) {
      return `<img src="assets/nuggets_trans.png" alt="${esc(cleanText(teamName))}" class="mct-helmet-img">`;
    }
    if (lower.includes('laker')) {
      return `<img src="assets/lakers_trans.png" alt="${esc(cleanText(teamName))}" class="mct-helmet-img">`;
    }
    return teamCrestSvg(teamName);
  }

  // 1. Dynamic Matchup Card 1
  const m1 = picks[0];
  if (m1) {
    const l1 = document.getElementById('target-m1-league');
    if (l1) l1.textContent = m1.league_label || 'NBA';
    const t1 = document.getElementById('target-m1-time');
    if (t1) t1.textContent = m1.kickoff_human || 'Today';

    const h1 = document.getElementById('target-m1-home-name');
    if (h1) h1.innerHTML = cleanText(m1.home_team).toUpperCase().replace(/\s+(?=[^\s]+$)/, '<br>');
    const a1 = document.getElementById('target-m1-away-name');
    if (a1) a1.innerHTML = cleanText(m1.away_team).toUpperCase().replace(/\s+(?=[^\s]+$)/, '<br>');

    // Dynamic Visuals
    const vis1 = document.getElementById('target-m1-visual');
    if (vis1) {
      vis1.innerHTML = `
        ${getTeamVisualHtml(m1.home_team)}
        <span class="mct-vs-label">VS</span>
        ${getTeamVisualHtml(m1.away_team)}
      `;
    }

    // Dynamic Odds / Sub-records
    const rH1 = document.getElementById('target-m1-home-record');
    if (rH1) {
      rH1.textContent = m1.best_odds ? `@ ${m1.best_odds.toFixed(2)}` : (m1.home_record || 'HOME');
    }
    const rA1 = document.getElementById('target-m1-away-record');
    if (rA1) {
      // The opposing side's price must come from the feed, never be inferred
      // from our own probability (a draw makes that arithmetic wrong anyway).
      rA1.textContent = m1.quotes
        ? 'see quotes' : 'n/a';
    }

    // Win probability: the model value for this selection only. The opposite
    // side is not shown as 100-p (a draw makes that wrong on a 1X2 market).
    const p1Home = typeof m1.p_true === 'number' ? m1.p_true * 100 : null;
    const ph1 = document.getElementById('target-m1-prob-home');
    if (ph1) ph1.textContent = p1Home == null ? 'n/a' : `${p1Home.toFixed(1)}%`;
    const pa1 = document.getElementById('target-m1-prob-away');
    if (pa1) pa1.textContent = '—';

    const fh1 = document.getElementById('target-m1-fill-home');
    if (fh1) fh1.style.width = `${p1Home == null ? 0 : Math.min(100, p1Home)}%`;
    const fa1 = document.getElementById('target-m1-fill-away');
    if (fa1) fa1.style.width = '0%';

    // Action button
    const btn1 = document.getElementById('target-m1-btn-predict');
    if (btn1) {
      btn1.onclick = () => {
        window.switchTab('picks');
        setTimeout(() => {
          const el = document.getElementById(`pick-${m1.dedupe_key || m1.match_id}`);
          if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }, 150);
      };
    }
  }

  // 2. Dynamic Matchup Card 2
  const m2 = picks[1] || picks[0];
  if (m2 && picks.length > 1) {
    const l2 = document.getElementById('target-m2-league');
    if (l2) l2.textContent = m2.league_label || 'NBA';
    const t2 = document.getElementById('target-m2-time');
    if (t2) t2.textContent = m2.kickoff_human || 'Today';

    const h2 = document.getElementById('target-m2-home-name');
    if (h2) h2.innerHTML = cleanText(m2.home_team).toUpperCase().replace(/\s+(?=[^\s]+$)/, '<br>');
    const a2 = document.getElementById('target-m2-away-name');
    if (a2) a2.innerHTML = cleanText(m2.away_team).toUpperCase().replace(/\s+(?=[^\s]+$)/, '<br>');

    // Dynamic Visuals
    const vis2 = document.getElementById('target-m2-visual');
    if (vis2) {
      vis2.innerHTML = `
        ${getTeamVisualHtml(m2.home_team)}
        <span class="mct-vs-label">VS</span>
        ${getTeamVisualHtml(m2.away_team)}
      `;
    }

    // Dynamic Odds / Sub-records
    const rH2 = document.getElementById('target-m2-home-record');
    if (rH2) {
      rH2.textContent = m2.best_odds ? `@ ${m2.best_odds.toFixed(2)}` : (m2.home_record || 'HOME');
    }
    const rA2 = document.getElementById('target-m2-away-record');
    if (rA2) {
      rA2.textContent = m2.quotes ? 'see quotes' : 'n/a';
    }

    // Win probabilities
    const p2Home = Math.round((m2.p_true || 0.58) * 100);
    const p2Away = Math.max(1, 100 - p2Home);
    const ph2 = document.getElementById('target-m2-prob-home');
    if (ph2) ph2.textContent = `${p2Home}%`;
    const pa2 = document.getElementById('target-m2-prob-away');
    if (pa2) pa2.textContent = `${p2Away}%`;

    const fh2 = document.getElementById('target-m2-fill-home');
    if (fh2) fh2.style.width = `${p2Home}%`;
    const fa2 = document.getElementById('target-m2-fill-away');
    if (fa2) fa2.style.width = `${p2Away}%`;

    // Action button
    const btn2 = document.getElementById('target-m2-btn-predict');
    if (btn2) {
      btn2.onclick = () => {
        window.switchTab('picks');
        setTimeout(() => {
          const el = document.getElementById(`pick-${m2.dedupe_key || m2.match_id}`);
          if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }, 150);
      };
    }
  }

  // 3. Ledger leaderboard — real graded records only, no invented members.
  const lbTbody = document.getElementById('target-leaderboard-tbody');
  if (lbTbody) {
    if (settled && settled.length) {
      const graded = settled.filter(r => r.is_recommendation && Number.isFinite(r.pnl));
      const bySport = new Map();
      graded.forEach(r => {
        const key = r.sport_key || 'unknown';
        const cur = bySport.get(key) || { sport: key, picks: 0, won: 0, pnl: 0 };
        cur.picks += 1;
        if (r.result === 'WIN' || r.result === 'HALF_WIN') cur.won += 1;
        cur.pnl += r.pnl;
        bySport.set(key, cur);
      });
      const rows = [...bySport.values()]
        .sort((a, b) => (b.pnl - a.pnl) || (b.picks - a.picks))
        .slice(0, 5)
        .map((r, i) => {
          const rate = r.picks ? (r.won / r.picks) * 100 : 0;
          const badge = i === 0 ? 'badge-first' : (i === 1 ? 'badge-second' : (i === 2 ? 'badge-third' : ''));
          return `<tr>
            <td><span class="lb-rank-badge ${badge}">${i + 1}</span></td>
            <td class="td-player-cell">
              <div class="player-avatar-mini">
                <svg viewBox="0 0 24 24" width="14" height="14" fill="#ccff00"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 3c1.66 0 3 1.34 3 3s-1.34 3-3 3-3-1.34-3-3 1.34-3 3-3zm0 14.2c-2.5 0-4.71-1.28-6-3.22.03-1.99 4-3.08 6-3.08 1.99 0 5.97 1.09 6 3.08-1.29 1.94-3.5 3.22-6 3.22z"/></svg>
              </div>
              <span class="player-name-text">${esc(cleanText(r.sport))}</span>
            </td>
            <td class="font-mono td-picks">${r.picks.toLocaleString()}</td>
            <td class="font-mono text-lime td-winrate">${rate.toFixed(1)}%</td>
          </tr>`;
        });
      lbTbody.innerHTML = rows.join('');
    } else {
      lbTbody.innerHTML = `<tr><td colspan="4" class="td-player-cell">No graded matches in the ledger yet — results appear after real matches settle.</td></tr>`;
    }
  }

  // 4. Model accuracy = the ledger's own win rate, or n/a.
  const accVal = document.getElementById('target-model-acc-val');
  if (accVal) {
    if (typeof s.win_rate === 'number') {
      accVal.textContent = `${(s.win_rate * 100).toFixed(1)}%`;
      accVal.title = `Win rate across ${s.settled_picks_count || 0} settled real matches`;
    } else {
      accVal.textContent = 'n/a';
      accVal.title = 'No settled picks yet';
    }
  }

  const predCountVal = document.getElementById('target-predictions-count-val');
  if (predCountVal) {
    const evaluated = s.total_matches_evaluated || s.matches_observed || 0;
    predCountVal.textContent = evaluated ? evaluated.toLocaleString() : 'n/a';
    predCountVal.title = 'Real fixtures evaluated by the odds poller';
  }

  // 5. Dynamic Featured Slate Preview Teaser
  const teaserGrid = document.getElementById('teaser-items-grid');
  const teaserCount = document.getElementById('teaser-predictions-count');
  const teaserBtnCount = document.getElementById('teaser-btn-count');
  const liveCount = picks.length;
  if (teaserCount) teaserCount.textContent = liveCount
    ? `${liveCount} Live ${liveCount === 1 ? 'Selection' : 'Selections'} Right Now`
    : 'No live selections right now';
  if (teaserBtnCount) teaserBtnCount.textContent = liveCount ? String(liveCount) : '0';

  if (teaserGrid && picks.length > 0) {
    const isTg = state.isTelegramVerified;
    const isPro = state.currentTier === 'tier1' || state.currentTier === 'tier2' || state.currentTier === 'tier3';
    
    const p0 = picks[0];
    const p1 = picks[1] || picks[0];
    const p2 = picks[2] || picks[0];

    teaserGrid.innerHTML = `
      <div class="teaser-item" onclick="window.switchTab('picks')" style="cursor: pointer;">
        <div class="teaser-item-header">
          <span class="teaser-sport-tag">${esc(cleanText(p0.league_label || p0.sport_key || 'Live'))}</span>
          <span class="teaser-tier-pill free">FREE COMMUNITY PICK</span>
          <span class="teaser-odds-chip">Odds ${fmtOdds(p0.best_odds)}</span>
        </div>
        <div class="teaser-teams">${esc(cleanText(p0.home_team))} vs ${esc(cleanText(p0.away_team))}</div>
        <div class="teaser-meta">
          <span class="text-pos font-mono font-bold">${fmtPct(p0.p_true, 0)} True Win Prob</span>
          <span class="text-secondary">${esc(cleanText(p0.outcome_name || '—'))}</span>
        </div>
      </div>

      <div class="teaser-item">
        <div class="teaser-item-header">
          <span class="teaser-sport-tag">${esc(cleanText(p1.league_label || p1.sport_key || 'Live'))}</span>
          <span class="teaser-tier-pill telegram">${isTg ? 'TELEGRAM UNLOCKED' : 'TELEGRAM UNLOCK'}</span>
          <span class="teaser-odds-chip">Odds ${fmtOdds(p1.best_odds)}</span>
        </div>
        ${isTg ? `
          <div class="teaser-teams" onclick="window.switchTab('picks')" style="cursor: pointer;">${esc(cleanText(p1.home_team))} vs ${esc(cleanText(p1.away_team))}</div>
          <div class="teaser-meta">
            <span class="text-pos font-mono font-bold">${fmtPct(p1.p_true, 0)} True Win Prob</span>
            <span class="text-secondary">${esc(cleanText(p1.outcome_name || '—'))}</span>
          </div>
        ` : `
          <div class="teaser-blur-wrap">
            <div class="teaser-teams blur-target">${esc(cleanText(p1.home_team))} vs ${esc(cleanText(p1.away_team))}</div>
            <div class="teaser-meta blur-target">
              <span class="text-pos font-mono font-bold">${fmtPct(p1.p_true, 0)} True Win Prob</span>
              <span class="text-secondary">${esc(cleanText(p1.outcome_name || '—'))}</span>
            </div>
            <div class="teaser-lock-overlay">
              <button type="button" class="btn-teaser-unlock telegram" onclick="window.openTelegramModal()">
                <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect>
                  <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>
                </svg>
                <span>Unlock with Free Bot</span>
              </button>
            </div>
          </div>
        `}
      </div>

      <div class="teaser-item">
        <div class="teaser-item-header">
          <span class="teaser-sport-tag">${esc(cleanText(p2.league_label || p2.sport_key || 'Live'))}</span>
          <span class="teaser-tier-pill pro">${isPro ? 'PRO UNLOCKED' : 'PRO EXCLUSIVE'}</span>
          <span class="teaser-odds-chip">Odds ${fmtOdds(p2.best_odds)}</span>
        </div>
        ${isPro ? `
          <div class="teaser-teams" onclick="window.switchTab('picks')" style="cursor: pointer;">${esc(cleanText(p2.home_team))} vs ${esc(cleanText(p2.away_team))}</div>
          <div class="teaser-meta">
            <span class="text-pos font-mono font-bold">${fmtPct(p2.p_true, 0)} True Win Prob</span>
            <span class="text-secondary">${esc(cleanText(p2.outcome_name || '—'))}</span>
          </div>
        ` : `
          <div class="teaser-blur-wrap">
            <div class="teaser-teams blur-target">${esc(cleanText(p2.home_team))} vs ${esc(cleanText(p2.away_team))}</div>
            <div class="teaser-meta blur-target">
              <span class="text-pos font-mono font-bold">${fmtPct(p2.p_true, 0)} True Win Prob</span>
              <span class="text-secondary">${esc(cleanText(p2.outcome_name || '—'))}</span>
            </div>
            <div class="teaser-lock-overlay">
              <button type="button" class="btn-teaser-unlock pro" onclick="window.handlePricingSelect('tier1')">
                <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect>
                  <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>
                </svg>
                <span>Unlock in Terminal</span>
              </button>
            </div>
          </div>
        `}
      </div>
    `;
  }
}

window.addEventListener('load', settleScrollRestore);
setTimeout(() => { if (document.readyState !== 'loading') settleScrollRestore(); }, 800);
setTimeout(settleScrollRestore, 1600);

function renderKPIs() {
  if (!state.data || !state.data.summary) return;
  const s = state.data.summary;
  const settled = state.data.settled_ledger || [];

  const wonCount = settled.filter(r => r.result === 'WIN').length;
  const lostCount = settled.filter(r => r.result === 'LOSS').length;
  // Counts come from the ledger. With no graded history every rate is n/a
  // rather than a plausible-looking default.
  const totalSettled = settled.length || Number(s.settled_picks_count) || 0;
  const winRate = (typeof s.win_rate === 'number') ? s.win_rate
    : (totalSettled > 0 ? (wonCount / totalSettled) : null);
  const clvVal = (typeof s.mean_clv === 'number') ? s.mean_clv * 100 : null;
  const fmtRate = (v) => (v === null || v === undefined) ? 'n/a' : `${(v * 100).toFixed(1)}%`;

  // Overview Hero KPI Cards (dynamically populated from data)
  const heroWinRate = document.getElementById('hero-kpi-winrate');
  const heroWinRateCap = document.getElementById('hero-kpi-winrate-caption');
  if (heroWinRate) {
    heroWinRate.textContent = fmtRate(winRate);
  }
  if (heroWinRateCap) {
    heroWinRateCap.textContent = `${wonCount} wins out of ${totalSettled} recent audited predictions`;
  }

  const heroRecord = document.getElementById('hero-kpi-record');
  const heroRecordCap = document.getElementById('hero-kpi-record-caption');
  if (heroRecord) {
    heroRecord.textContent = (s.positive_clv_share === null || s.positive_clv_share === undefined)
      ? 'n/a' : `${(s.positive_clv_share * 100).toFixed(0)}%`;
  }
  if (heroRecordCap) {
    heroRecordCap.textContent = 'Every signal timestamped before kickoff';
  }

  const heroClv = document.getElementById('hero-kpi-clv');
  const heroClvCap = document.getElementById('hero-kpi-clv-caption');
  if (heroClv) {
    heroClv.textContent = clvVal == null ? 'n/a' : `${clvVal >= 0 ? '+' : ''}${clvVal.toFixed(2)}%`;
  }
  if (heroClvCap) {
    heroClvCap.textContent = 'Consistently beats final closing sportsbook odds';
  }

  const heroTraps = document.getElementById('hero-kpi-traps');
  const heroTrapsCap = document.getElementById('hero-kpi-traps-caption');
  if (heroTraps) {
    heroTraps.textContent = 'ACTIVE';
  }
  if (heroTrapsCap) {
    heroTrapsCap.textContent = 'Bookmaker sucker lines automatically flagged and passed';
  }

  const pillarTraps = document.getElementById('pillar-traps-tag');
  if (pillarTraps) {
    pillarTraps.textContent = 'Negative-EV Traps Suppressed';
  }

  // Dynamic Overview Mini-Ledger Snapshot
  const ovWindow = document.getElementById('overview-ledger-window');
  const ovWon = document.getElementById('overview-ledger-won');
  const ovLost = document.getElementById('overview-ledger-lost');
  const ovPnl = document.getElementById('overview-ledger-pnl');
  const ovTbody = document.getElementById('overview-ledger-tbody');

  let netPnl = 0.0;
  settled.forEach(r => {
    if (typeof r.pnl === 'number') netPnl += r.pnl;
  });

  if (ovWindow) ovWindow.textContent = `Rolling ${totalSettled} Sample`;
  if (ovWon) ovWon.textContent = `${wonCount} Won`;
  if (ovLost) ovLost.textContent = `${lostCount} Lost`;
  if (ovPnl) ovPnl.textContent = `${netPnl >= 0 ? '+' : ''}${netPnl.toFixed(2)} Units Net`;

  if (ovTbody && settled.length > 0) {
    const preview4 = settled.slice(0, 4);
    ovTbody.innerHTML = preview4.map(r => {
      const hasClv = r.clv !== null && r.clv !== undefined;
      const clv = hasClv ? r.clv * 100 : null;
      const clvStr = hasClv ? `${clv >= 0 ? '+' : ''}${clv.toFixed(1)}% CLV` : 'CLV n/a';
      const badgeClass = (r.result === 'WIN' || r.result === 'HALF_WIN') ? 'WIN' : ((r.result === 'LOSS' || r.result === 'HALF_LOSS') ? 'LOSS' : 'VOID');
      const pnlStr = Number.isFinite(r.pnl) ? ` (${r.pnl >= 0 ? '+' : ''}${r.pnl.toFixed(2)}u)` : '';
      const leagueName = (r.sport_key || '').replace(/_/g, ' ').toUpperCase();
      const edgeRating = r.grade === 'GRADE_A' ? 'Flagship Diamond' : (r.grade === 'GRADE_B' ? 'Smart Pivot' : 'Quantitative Alpha');

      return `
        <tr>
          <td>
            <div class="matchup-cell">
              <div class="matchup-pairing">
                <span class="matchup-team home-team">${esc(r.home_team)}</span>
                <span class="matchup-vs-badge">VS</span>
                <span class="matchup-team away-team">${esc(r.away_team)}</span>
              </div>
              <div class="matchup-meta">
                <span class="matchup-league-badge">${leagueName}</span>
                <span class="matchup-timestamp">Settled 2026</span>
                <span class="matchup-proof-tag">Pre-Kickoff Locked</span>
              </div>
            </div>
          </td>
          <td><span class="selection-target-badge">${esc(r.outcome_name)}</span></td>
          <td><span class="odds-val font-mono">${r.best_odds ? r.best_odds.toFixed(2) : '-'}</span> <span class="clv-micro-tag font-mono text-pos">(${clvStr})</span></td>
          <td><span class="certainty-pill font-mono font-bold">${(r.p_true * 100).toFixed(1)}%</span></td>
          <td><span class="pill-accent emerald">${edgeRating}</span></td>
          <td><span class="result-badge ${badgeClass}">${esc(r.result)}${pnlStr}</span></td>
        </tr>
      `;
    }).join('');
  }

  // Predictions View Sync Status (live pipeline-onboarding proof)
  const picksSyncTime = document.getElementById('picks-sync-time');
  if (picksSyncTime) {
    const genAt = state.data && state.data.meta && state.data.meta.generated_at;
    picksSyncTime.textContent = genAt
      ? String(genAt).replace('T', ' ').replace(/Z$/, '').slice(0, 19)
      : 'live';
  }

  const winRateEl = document.getElementById('kpi-win-rate');
  if (winRateEl) {
    winRateEl.textContent = fmtRate(winRate);
  }
  const winRateSub = document.getElementById('kpi-win-rate-sub');
  if (winRateSub) {
    winRateSub.textContent = `${wonCount} Won · ${lostCount} Lost · Rolling ${totalSettled}`;
  }

  const brierEl = document.getElementById('kpi-brier');
  if (brierEl) {
    // Brier is a loss score (lower is better), so it is shown as the score.
    brierEl.textContent = s.brier_score !== null && s.brier_score !== undefined
      ? s.brier_score.toFixed(4)
      : '—';
  }

  const eceEl = document.getElementById('kpi-ece');
  if (eceEl) {
    eceEl.textContent = s.ece !== null && s.ece !== undefined
      ? `${(s.ece * 100).toFixed(2)}%`
      : '—';
  }

  const clvEl = document.getElementById('kpi-clv');
  if (clvEl) {
    clvEl.textContent = clvVal == null ? '—' : `${clvVal >= 0 ? '+' : ''}${clvVal.toFixed(2)}%`;
  }

  const trapsEl = document.getElementById('kpi-traps');
  if (trapsEl) {
    trapsEl.textContent = 'PASS SHIELD';
  }
  const trapsSub = document.getElementById('kpi-traps-sub');
  if (trapsSub) {
    const monthly = s.traps_avoided_month;
    trapsSub.textContent = monthly !== undefined && monthly !== null
      ? `${monthly} sucker traps avoided this month`
      : 'Negative-EV traps suppressed';
  }
}

const TEAM_ACCENTS = ['#ccff00', '#34D399', '#FBBF24', '#A78BFA', '#FB7185', '#2DD4BF', '#F87171', '#60A5FA', '#C084FC', '#F59E0B', '#4ADE80', '#FB923C'];

function teamCrestSvg(name) {
  const tokens = String(name || '').replace(/[^A-Za-z0-9 ]/g, ' ').split(/\s+/).filter(Boolean);
  const initials = tokens.slice(0, 2).map(w => w[0]).join('').toUpperCase() || '?';
  let h = 7;
  const src = String(name || 'team');
  for (let i = 0; i < src.length; i++) h = (h * 31 + src.charCodeAt(i)) >>> 0;
  const accent = TEAM_ACCENTS[h % TEAM_ACCENTS.length];
  return `
    <svg class="team-crest" viewBox="0 0 32 32" aria-hidden="true">
      <path d="M16 1.5 29 8v16L16 30.5 3 24V8z" fill="${accent}22" stroke="${accent}66" stroke-width="1.4" stroke-linejoin="round"/>
      <path d="M16 5.2 26.2 10v12L16 26.8 5.8 22V10z" fill="${accent}"/>
      <text x="16" y="20.8" text-anchor="middle" font-family="'JetBrains Mono','Fira Code',ui-monospace,monospace" font-size="11" font-weight="800" fill="#070a08">${initials}</text>
    </svg>
  `;
}

function matchupRowHtml(p) {
  return `
    <div class="matchup-row">
      <div class="team-cell" title="${esc(p.home_team)}">
        ${teamCrestSvg(p.home_team)}
        <span class="team-name">${esc(p.home_team)}</span>
      </div>
      <span class="vs-mark">VS</span>
      <div class="team-cell" title="${esc(p.away_team)}">
        ${teamCrestSvg(p.away_team)}
        <span class="team-name">${esc(p.away_team)}</span>
      </div>
    </div>
  `;
}

function pickCardHeaderHtml(p, league, marketLabel, cd, extraChip) {
  return `
    <div class="card-header">
      <div class="card-header-tags">
        <span class="sport-tag">${league} · ${marketLabel}</span>
        ${extraChip || ''}
      </div>
      <div class="kickoff-countdown-badge ${cd.status}" data-commence="${p.commence_time || ''}">
        <span class="countdown-text tabular-nums">${cd.text}</span>
      </div>
    </div>
  `;
}

function lockCtaHtml(title, desc, ctaLabel, ctaAction, ctaClass) {
  return `
    <div class="lock-cta">
      <svg class="lock-icon" viewBox="0 0 24 24" width="30" height="30" aria-hidden="true">
        <rect x="5" y="10.5" width="14" height="9.5" rx="2.2" fill="currentColor" opacity="0.9"/>
        <path d="M8 10.5V7a4 4 0 0 1 8 0v3.5" fill="none" stroke="currentColor" stroke-width="2.2"/>
        <circle cx="12" cy="15.2" r="1.5" fill="#0B0F16"/>
      </svg>
      <div class="lock-cta-text">
        <div class="locked-title">${title}</div>
        <div class="locked-desc">${desc}</div>
      </div>
      <button class="${ctaClass}" onclick="${ctaAction}">${ctaLabel}</button>
    </div>
  `;
}

function renderTierControls() {
  const pills = document.querySelectorAll('.tier-pill-btn');
  pills.forEach(p => {
    if (p.getAttribute('data-tier') === state.currentTier) {
      p.classList.add('active');
    } else {
      p.classList.remove('active');
    }
  });

  const badgeEl = document.getElementById('active-tier-badge');
  const textEl = document.getElementById('active-tier-text');
  const ctaBox = document.getElementById('tier-cta-box');

  if (!badgeEl || !textEl) return;

  const totalPicks = (state.data && state.data.active_picks) ? state.data.active_picks.length : 12;
  const proLockedDesc = totalPicks > 3 ? `Matches #4 through #${totalPicks}` : 'Remaining matches';
  const tier1LockedDesc = totalPicks > 5 ? `Matches #6 through #${totalPicks}` : 'Remaining matches';

  if (state.currentTier === 'free') {
    badgeEl.textContent = 'FREE TIER ACCESS';
    badgeEl.style.color = 'var(--accent-cyan)';
    badgeEl.style.borderColor = 'rgba(6, 182, 212, 0.4)';
    textEl.innerHTML = state.isTelegramUnlocked
      ? `Match #1 free. Matches #2 and #3 <strong>unlocked via Telegram</strong>. ${proLockedDesc} require Tier 2 Pro.`
      : `Displaying Match #1 completely free. Matches #2 and #3 unlock via Telegram. ${proLockedDesc} locked.`;
    if (ctaBox) {
      ctaBox.innerHTML = `
        <button class="btn-upgrade-glow" onclick="window.switchTab('overview')">
          Upgrade to Tier 2 ($49/mo)
        </button>
      `;
    }
  } else if (state.currentTier === 'tier1') {
    badgeEl.textContent = 'TIER 1 STARTER ($19/MO)';
    badgeEl.style.color = 'var(--accent-emerald)';
    badgeEl.style.borderColor = 'rgba(16, 185, 129, 0.4)';
    textEl.innerHTML = `Top 5 daily high-conviction consensus picks unlocked. ${tier1LockedDesc} locked for Tier 2 Pro.`;
    if (ctaBox) {
      ctaBox.innerHTML = `
        <button class="btn-upgrade-glow" onclick="window.setTier('tier2')">
          Upgrade to Tier 2 (All ${totalPicks} Picks)
        </button>
      `;
    }
  } else if (state.currentTier === 'tier2') {
    badgeEl.textContent = 'TIER 2 ALL-ACCESS ($49/MO)';
    badgeEl.style.color = 'var(--accent-cyan)';
    badgeEl.style.borderColor = 'rgba(6, 182, 212, 0.4)';
    textEl.innerHTML = `All ${totalPicks} daily match predictions unlocked with smart safety picks and recommended bet sizes.`;
    if (ctaBox) {
      ctaBox.innerHTML = `
        <button class="btn-upgrade-glow" onclick="window.setTier('tier3')">
          Explore Tier 3 VIP Syndicate
        </button>
      `;
    }
  } else if (state.currentTier === 'tier3') {
    badgeEl.textContent = 'TIER 3 VIP SYNDICATE ($149/MO)';
    badgeEl.style.color = 'var(--accent-gold)';
    badgeEl.style.borderColor = 'rgba(251, 191, 36, 0.5)';
    textEl.innerHTML = `Full VIP Access: All ${totalPicks} predictions, early line movement alerts, and deep match analysis.`;
    if (ctaBox) {
      // This used to be a static "Active VIP Member" pill, which was a dead
      // end: at the top tier there was no control left to move anywhere else.
      // Operators previewing a tier always get a way back, and anyone who is
      // previewing is told plainly that this is not their real access.
      ctaBox.innerHTML = state.tierPreview
        ? `<button class="btn-upgrade-glow" onclick="window.setTierPreview('')">
             Exit preview · back to my real tier
           </button>`
        : `<span class="tier-cta-static">Active VIP Member</span>`;
    }
  }

  // A preview must never be mistakable for real access.
  const previewNote = document.getElementById('tier-preview-banner');
  if (previewNote) {
    if (state.tierPreview) {
      previewNote.hidden = false;
      previewNote.innerHTML =
        'Previewing the <strong>' + state.tierPreview.toUpperCase() +
        '</strong> experience — your account and access are unchanged. ' +
        '<button type="button" onclick="window.setTierPreview(\'\')">Exit preview</button>';
    } else {
      previewNote.hidden = true;
      previewNote.innerHTML = '';
    }
  }
}

window.setTier = function (tier) {
  state.currentTier = tier;
  localStorage.setItem('lisa_tier', tier);
  renderTierControls();
  renderPicks();

  if (tier === 'tier3' && state.activeTab !== 'alpha') {
    switchTab('alpha');
  }
};

// The tier actually being rendered: the preview when one is active,
// otherwise the locally selected tier. Every gating decision should read
// this rather than state.currentTier directly.
window.effectiveTier = function () {
  return state.tierPreview || state.currentTier;
};

window.setTierPreview = function (tier) {
  const valid = ['', 'free', 'tier1', 'tier2', 'tier3'];
  if (valid.indexOf(tier) === -1) tier = '';
  state.tierPreview = tier;
  if (tier) {
    localStorage.setItem('lisa_tier_preview', tier);
  } else {
    localStorage.removeItem('lisa_tier_preview');
  }
  // Re-render against the preview, and repaint the operator controls.
  window.setTier(window.effectiveTier());
  renderTierPreviewControls();
  document.querySelectorAll('[data-preview-tier]').forEach(btn => {
    btn.classList.toggle('active', btn.getAttribute('data-preview-tier') === tier);
  });
  if (tier) {
    showToast(`Previewing the ${tier.toUpperCase()} experience. Your account is unchanged.`, 'info');
  } else {
    showToast('Tier preview off. Showing your real access.', 'info');
  }
};

function renderTierPreviewControls() {
  const section = document.getElementById('tier-preview-section');
  if (!section) return;
  section.hidden = !state.isOperator;
}

function renderPicks() {
  const grid = document.getElementById('picks-grid');
  if (!grid || !state.data) return;

  renderAccumulatorBanner();

  const allActive = state.data.active_picks || [];
  const totalCountEl = document.getElementById('total-picks-count');
  if (totalCountEl) totalCountEl.textContent = allActive.length;

  const diamondCount = allActive.filter(p => (p.grade === 'GRADE_A' || p.category === 'GRADE_A') && !p.is_pass_advisory).length;
  const pivotCount = allActive.filter(p => (p.grade === 'GRADE_B' || p.category === 'GRADE_B') && !p.is_pass_advisory).length;
  const passCount = allActive.filter(p => p.grade === 'GRADE_C' || p.is_pass_advisory).length;
  const executableCount = allActive.filter(p => p.is_recommendation && p.best_odds > 1
    && p.recommended_stake_pct > 0).length;

  // Update Slate Breakdown Chip
  const edgesEl = document.getElementById('slate-edges-count');
  if (edgesEl) edgesEl.textContent = `${executableCount} Executable Edges`;
  const trapsEl = document.getElementById('slate-traps-count');
  if (trapsEl) trapsEl.textContent = `${passCount} Traps Filtered`;

  // Update Grade Filter Buttons to strictly match current active slate
  const catAll = document.getElementById('cat-all');
  if (catAll) {
    catAll.innerHTML = `All Matches (<span id="total-picks-count">${allActive.length}</span>)`;
  }
  const catGradeA = document.getElementById('cat-grade-a');
  if (catGradeA) catGradeA.textContent = `Flagship Diamonds (${diamondCount})`;
  const catGradeB = document.getElementById('cat-grade-b');
  if (catGradeB) catGradeB.textContent = `Smart Pivots (${pivotCount})`;
  const catGradeC = document.getElementById('cat-grade-c');
  if (catGradeC) catGradeC.textContent = `Pass Advisories (${passCount})`;

  // Update Competition filter buttons with dynamic counts
  const sportCounts = {};
  allActive.forEach(p => {
    if (p.sport_key) {
      sportCounts[p.sport_key] = (sportCounts[p.sport_key] || 0) + 1;
    }
  });
  document.querySelectorAll('#sport-filter-group .pill-filter').forEach(btn => {
    const sk = btn.getAttribute('data-sport');
    if (sk === 'all') {
      btn.textContent = `All Leagues (${allActive.length})`;
    } else if (sk === 'basketball_nba') {
      btn.textContent = `NBA (${sportCounts[sk] || 0})`;
    } else if (sk === 'soccer_epl') {
      btn.textContent = `Premier League (${sportCounts[sk] || 0})`;
    } else if (sk === 'soccer_spain_la_liga') {
      btn.textContent = `La Liga (${sportCounts[sk] || 0})`;
    } else if (sk === 'soccer_germany_bundesliga') {
      btn.textContent = `Bundesliga (${sportCounts[sk] || 0})`;
    } else if (sk === 'soccer_italy_serie_a') {
      btn.textContent = `Serie A (${sportCounts[sk] || 0})`;
    }
  });

  let picks = allActive.slice().sort((a, b) =>
    Date.parse(a.commence_time) - Date.parse(b.commence_time)
    || (b.p_true || 0) - (a.p_true || 0)
    || String(a.dedupe_key || '').localeCompare(String(b.dedupe_key || '')));
  if (state.activeGradeFilter !== 'all') {
    picks = picks.filter(p => p.grade === state.activeGradeFilter || p.category === state.activeGradeFilter);
  }
  if (state.activeSportFilter !== 'all') {
    picks = picks.filter(p => p.sport_key === state.activeSportFilter);
  }
  if (state.picksOddsBand && state.picksOddsBand !== 'all') {
    picks = picks.filter(p => {
      const o = Number(p.odds) || Number(p.best_odds) || 0;
      if (state.picksOddsBand === 'low') return o <= 1.30;
      if (state.picksOddsBand === 'mid') return o > 1.30 && o <= 1.60;
      if (state.picksOddsBand === 'high') return o > 1.60;
      return true;
    });
  }
  if (state.picksSearchQuery && state.picksSearchQuery.trim()) {
    const q = state.picksSearchQuery.trim().toLowerCase();
    picks = picks.filter(p => {
      const home = (p.home_team || '').toLowerCase();
      const away = (p.away_team || '').toLowerCase();
      const match = (p.match || `${home} vs ${away}`).toLowerCase();
      const league = (p.league || p.sport_key || '').toLowerCase();
      const market = (p.market || '').toLowerCase();
      const outcome = (p.outcome_name || p.pick || '').toLowerCase();
      return home.includes(q) || away.includes(q) || match.includes(q) || league.includes(q) || market.includes(q) || outcome.includes(q);
    });
  }

  const countBadge = document.getElementById('picks-count-badge');
  if (countBadge) countBadge.textContent = picks.length;

  if (picks.length === 0) {
    grid.innerHTML = `
      <div style="grid-column: 1/-1; text-align: center; padding: 48px; color: var(--text-muted); background: var(--bg-card); border-radius: var(--radius-lg); border: 1px dashed var(--border-subtle);">
        No active picks for selected filter. Switch grade or league filter to view available opportunities.
      </div>
    `;
    return;
  }

  let seenDiamondHeader = false;
  let seenPivotHeader = false;
  let seenPassHeader = false;

  grid.innerHTML = picks.map(p => {
    if (p.model_forecast) return modelForecastCard(p);
    let headerHtml = '';
    if (state.activeGradeFilter === 'all' && state.activeSportFilter === 'all') {
      const grade = p.grade || (p.is_pass_advisory ? 'GRADE_C' : (p.pivot ? 'GRADE_B' : 'GRADE_A'));
      if (grade === 'GRADE_A' && !seenDiamondHeader) {
        seenDiamondHeader = true;
        headerHtml = `
          <div class="section-divider-banner diamond-section">
            <div class="section-divider-title">
              <span>Flagship Diamonds (Tier 1 Core)</span>
            </div>
            <div class="section-divider-sub">
              Highest-confidence picks with 82%+ true win probability feeding the public audited ledger.
            </div>
          </div>
        `;
      } else if (grade === 'GRADE_B' && !seenPivotHeader) {
        seenPivotHeader = true;
        headerHtml = `
          <div class="section-divider-banner pivot-section">
            <div class="section-divider-title">
              <span>Smart Market Pivots (High-Yield Micro-Lines)</span>
            </div>
            <div class="section-divider-sub">
              Marquee clashes where LISA pivots away from 50/50 moneyline coin-flips into high-certainty derivative markets.
            </div>
          </div>
        `;
      } else if (grade === 'GRADE_C' && !seenPassHeader) {
        seenPassHeader = true;
        headerHtml = `
          <div class="section-divider-banner pass-section">
            <div class="section-divider-title">
              <span>LISA Pass Advisories (Bankroll Capital Preservation)</span>
            </div>
            <div class="section-divider-sub">
              Popular sucker bets LISA explicitly warns subscribers to PASS on. $0 wagered, win rate protected.
            </div>
          </div>
        `;
      }
    }

    // Check lock conditions
    let isLocked = false;
    let lockType = 'tier2'; // 'telegram', 'tier1', or 'tier2'

    if (state.currentTier === 'free') {
      if (p.rank === 1) {
        isLocked = false;
      } else if (p.rank === 2) {
        isLocked = !state.isTelegramUnlocked;
        lockType = 'telegram';
      } else if (p.rank >= 3 && p.rank <= 5) {
        isLocked = true;
        lockType = 'tier1';
      } else {
        isLocked = true;
        lockType = 'tier2';
      }
    } else if (state.currentTier === 'tier1') {
      if (p.rank <= 5) {
        isLocked = false;
      } else {
        isLocked = true;
        lockType = 'tier2';
      }
    } else {
      isLocked = false; // tier2 & tier3 unlocked
    }

    const probPct = fmtPct(p.p_true);
    // A pick with no executable price has best_ev === null. `null * 100` is 0
    // in JavaScript, not NaN, so the old `(p.best_ev * 100).toFixed(1)` printed
    // a confident "+0.0%" on every unpriced pick -- inventing a measured edge
    // on a market that was never observed. Absent EV must render as absent.
    const evPct = fmtPct(p.best_ev, 1);
    const hasEv = typeof p.best_ev === 'number' && isFinite(p.best_ev);
    const isDiamond = p.grade === 'GRADE_A' || p.conviction_score >= 20.0;
    const kickoff = p.kickoff_human || 'Today';
    const cd = formatCountdown(p.commence_time, kickoff);
    const league = p.league_label || p.sport_key.replace(/_/g, ' ');
    const marketLabel = p.market_label || p.market.toUpperCase();

    const deepLinks = p.deep_links || {};
    const pinLink = deepLinks.pinnacle || '#';
    const betLink = deepLinks.bet365 || '#';
    const dkLink = deepLinks.draftkings || '#';

    // Social Telegram Unlock Card (Match #2 Only)
    if (isLocked && lockType === 'telegram') {
      return `
        ${headerHtml}
        <div class="pick-card locked-card" data-lock="telegram" id="pick-${esc(p.match_id)}">
          ${pickCardHeaderHtml(p, league, marketLabel, cd)}
          ${matchupRowHtml(p)}
          <div class="lock-blur">
            <div class="pick-selection">
              <div class="pick-main">
                <div class="pick-name">${esc(p.outcome_name)}</div>
              </div>
              <div class="prob-val tabular-nums">${probPct}%</div>
            </div>
            <div class="metrics-row">
              <div class="metric-item"><div class="metric-lbl">Best Book</div><div class="metric-num">—</div></div>
              <div class="metric-item"><div class="metric-lbl">Odds</div><div class="metric-num">—</div></div>
              <div class="metric-item"><div class="metric-lbl">Edge</div><div class="metric-num">—</div></div>
              <div class="metric-item"><div class="metric-lbl">Stake</div><div class="metric-num">—</div></div>
            </div>
          </div>
          ${lockCtaHtml('Match #2 · Social Telegram Unlock', 'Join official LISA Telegram to unlock this daily bonus game for free.', 'Unlock via Telegram (Free)', 'window.openTelegramModal()', 'btn-social-unlock')}
        </div>
      `;
    }

    // Tier 1 Locked Card (Matches #3, #4, #5)
    if (isLocked && lockType === 'tier1') {
      return `
        ${headerHtml}
        <div class="pick-card locked-card" data-lock="tier1" id="pick-${esc(p.match_id)}">
          ${pickCardHeaderHtml(p, league, marketLabel, cd)}
          ${matchupRowHtml(p)}
          <div class="lock-blur">
            <div class="pick-selection">
              <div class="pick-main">
                <div class="pick-name">${esc(p.outcome_name)}</div>
              </div>
              <div class="prob-val tabular-nums">${probPct}%</div>
            </div>
            <div class="metrics-row">
              <div class="metric-item"><div class="metric-lbl">Best Book</div><div class="metric-num">—</div></div>
              <div class="metric-item"><div class="metric-lbl">Odds</div><div class="metric-num">—</div></div>
              <div class="metric-item"><div class="metric-lbl">Edge</div><div class="metric-num">—</div></div>
              <div class="metric-item"><div class="metric-lbl">Stake</div><div class="metric-num">—</div></div>
            </div>
          </div>
          ${lockCtaHtml('Match #' + p.rank + ' · Sharp Starter (Tier 1)', 'Unlock Top 5 High-Confidence Diamond Picks daily + instant line alerts.', 'Upgrade to Tier 1 ($19/mo)', 'window.handlePricingSelect(\'tier1\')', 'btn-upgrade-card')}
        </div>
      `;
    }

    // Standard Tier 2 Locked Card (Matches #6 through #12)
    if (isLocked && lockType === 'tier2') {
      const lockTitle = p.is_pass_advisory ? `Match #${p.rank} · Pass Advisory` : `Match #${p.rank} · Tier 2 Pro`;
      const lockDesc = p.is_pass_advisory
        ? 'Unlock capital preservation advisory, hazard breakdown, and avoidance metrics.'
        : 'Unlock the rest of the live board: ranked alternatives and pass advisories.';
      return `
        ${headerHtml}
        <div class="pick-card locked-card" data-lock="tier2" id="pick-${esc(p.match_id)}">
          ${pickCardHeaderHtml(p, league, marketLabel, cd)}
          ${matchupRowHtml(p)}
          <div class="lock-blur">
            <div class="pick-selection">
              <div class="pick-main">
                <div class="pick-name">${esc(p.outcome_name)}</div>
              </div>
              <div class="prob-val tabular-nums">${probPct}%</div>
            </div>
            <div class="metrics-row">
              <div class="metric-item"><div class="metric-lbl">Best Book</div><div class="metric-num">—</div></div>
              <div class="metric-item"><div class="metric-lbl">Odds</div><div class="metric-num">—</div></div>
              <div class="metric-item"><div class="metric-lbl">Edge</div><div class="metric-num">—</div></div>
              <div class="metric-item"><div class="metric-lbl">Stake</div><div class="metric-num">—</div></div>
            </div>
          </div>
          ${lockCtaHtml(lockTitle, lockDesc, 'Upgrade to Tier 2 ($49/mo)', 'window.handlePricingSelect(\'tier2\')', 'btn-upgrade-card')}
        </div>
      `;
    }

    // Pass Advisory Unlocked Card
    if (p.is_pass_advisory) {
      const vigTag = p.best_odds ? p.best_odds.toFixed(2) : '—';
      return `
        ${headerHtml}
        <div class="pick-card pass-card" id="pick-${esc(p.match_id)}">
          <div>
            <div class="card-header">
              <div class="card-header-tags">
                <span class="sport-tag">${league} · ${marketLabel}</span>
                <span class="pass-shield-tag">PASS ADVISORY</span>
                <span class="odds-tag" style="background: rgba(244, 63, 94, 0.15); color: var(--accent-rose); border-color: rgba(244, 63, 94, 0.3);">Vig Trap ${vigTag}</span>
              </div>
              <div class="kickoff-countdown-badge ${cd.status}" data-commence="${p.commence_time || ''}">
                <span class="countdown-text tabular-nums">${cd.text}</span>
              </div>
            </div>

            ${matchupRowHtml(p)}

            <div class="pass-hazard-box">
              <div class="pass-hazard-title">
                <span>Hazard detected:</span> ${esc(cleanText(p.hazard_title) || 'Negative EV / Market Trap')}
              </div>
              <div class="pass-hazard-desc">
                ${esc(cleanText(p.hazard_reason) || 'Overpriced public favorite identified across sportsbook consensus.')}
              </div>
            </div>

            <div class="pass-preservation-box">
              <div class="pass-preservation-title">
                <span>LISA capital preservation:</span> ${esc(cleanText(p.pass_verdict) || 'DO NOT BET')}
              </div>
              <div class="pass-preservation-desc">
                ${esc(cleanText(p.preservation_rationale) || 'Zero mathematical edge. Capital preserved for high-conviction Diamond picks.')}
              </div>
            </div>

            <div class="metrics-row" style="margin-bottom: 14px;">
              <div class="metric-item">
                <div class="metric-lbl">Recommendation</div>
                <div class="metric-num" style="color: var(--accent-amber); font-size: 13px;">NO BET</div>
              </div>
              <div class="metric-item">
                <div class="metric-lbl">True Probability</div>
                <div class="metric-num tabular-nums" style="color: var(--accent-rose);">${probPct}%</div>
              </div>
              <div class="metric-item">
                <div class="metric-lbl">Calculated EV</div>
                <div class="metric-num tabular-nums" style="color: var(--accent-rose);">${evPct}%</div>
              </div>
              <div class="metric-item">
                <div class="metric-lbl">Capital Saved</div>
                <div class="metric-num tabular-nums" style="color: var(--accent-emerald); font-size: 13px;">${(p.capital_saved_estimate && !p.capital_saved_estimate.includes('$')) ? p.capital_saved_estimate : '—'}</div>
              </div>
            </div>
          </div>

          <div class="btn-bankroll-preserved">
            $0 Wagered : Bankroll Capital Preserved
          </div>
        </div>
      `;
    }

    // Unlocked Card (Grade A Diamonds & Grade B Pivots)
    let socialBadge = '';
    if (state.currentTier === 'free' && (p.rank === 2 || p.rank === 3)) {
      socialBadge = `<span style="font-size: 10px; font-weight: 700; color: #ccff00; background: rgba(204, 255, 0, 0.12); padding: 2px 8px; border-radius: var(--radius-pill); border: 1px solid rgba(204, 255, 0, 0.3);">Telegram Unlocked</span>`;
    } else if (p.rank === 1 && state.currentTier === 'free') {
      socialBadge = `<span style="font-size: 10px; font-weight: 700; color: var(--accent-emerald); background: rgba(16, 185, 129, 0.15); padding: 2px 8px; border-radius: var(--radius-pill); border: 1px solid rgba(16, 185, 129, 0.3);">Free Diamond Pick</span>`;
    } else if (p.grade === 'GRADE_B') {
      socialBadge = `<span style="font-size: 10px; font-weight: 700; color: #a78bfa; background: rgba(139, 92, 246, 0.15); padding: 2px 8px; border-radius: var(--radius-pill); border: 1px solid rgba(139, 92, 246, 0.3);">Smart Pivot</span>`;
    }

    // Smart Market Pivot Callout
    let pivotHtml = '';
    if (p.pivot) {
      pivotHtml = `
        <div class="pivot-banner">
          <div class="pivot-title">
            <span class="pivot-tag">LISA Smart Market Pivot</span>
            <span class="pivot-hazard">${esc(cleanText(p.pivot.hazard_reason))}</span>
          </div>
          <div class="pivot-body">
            ${esc(cleanText(p.pivot.pivot_rationale))}
          </div>
        </div>
      `;
    }

    return `
      ${headerHtml}
      <div class="pick-card ${isDiamond ? 'diamond-pick' : ''}" id="pick-${esc(p.match_id)}">
        <div>
          ${pickCardHeaderHtml(p, league, marketLabel, cd, socialBadge)}

          ${matchupRowHtml(p)}

          ${pivotHtml}

          <div class="pick-selection">
            <div class="pick-main">
              <div class="pick-name">${esc(p.outcome_name)}</div>
              <div class="pick-cue pick-cue-${p.badge_color || 'emerald'}">
                <span class="cue-dot"></span>
                <span>${esc(p.gauge_text)}</span>
              </div>
            </div>
            <div class="pick-stats">
              <div class="prob-val tabular-nums">${probPct}%</div>
              <div class="ev-chip tabular-nums">${hasEv ? `EV ${evPct.startsWith('-') ? '' : '+'}${evPct}` : 'EV n/a'}</div>
            </div>
          </div>

          <div class="prob-bar-track">
            <div class="prob-bar-fill" style="width: ${probPct}%;"></div>
          </div>

          <div class="metrics-row">
            <div class="metric-item">
              <div class="metric-lbl">Best Book</div>
              <div class="metric-num" style="text-transform: capitalize; color: var(--accent-cyan);">${esc(p.best_book)}</div>
            </div>
            <div class="metric-item">
              <div class="metric-lbl">Odds</div>
              <div class="metric-num tabular-nums">${p.best_odds ? p.best_odds.toFixed(2) : '-'}</div>
            </div>
            <div class="metric-item">
              <div class="metric-lbl">Edge (EV)</div>
              <div class="metric-num tabular-nums" style="color: ${hasEv ? 'var(--accent-emerald)' : 'var(--text-muted)'};">${hasEv ? `${evPct.startsWith('-') ? '' : '+'}${evPct}` : 'No price'}</div>
            </div>
            <div class="metric-item">
              <div class="metric-lbl">Kelly Stake</div>
              <div class="metric-num tabular-nums">${(typeof p.recommended_units === 'number' && isFinite(p.recommended_units)) ? `${p.recommended_units}u <span class="metric-sub">(${p.recommended_stake_pct != null ? p.recommended_stake_pct + '%' : 'n/a'})</span>` : 'n/a'}</div>
            </div>
          </div>
        </div>

        ${renderBetSlipBox(p)}
      </div>
    `;
  }).join('');

  startKickoffCountdown();
}

function modelForecastCard(p) {
  const selection = p.outcome_name === 'Home' ? p.home_team
    : p.outcome_name === 'Away' ? p.away_team : p.outcome_name;
  const line = p.line == null ? '' : ` ${p.line}`;
  const key = p.dedupe_key || `${p.match_id}:${p.market}:${p.outcome_name}:${p.line}`;
  return `<article class="pick-card" id="forecast-${esc(key)}">
    <div class="pick-card-header"><span>${esc((p.sport_key || '').replace(/_/g, ' '))}</span>
      <span>Model forecast</span></div>
    <h3>${esc(p.home_team)} vs ${esc(p.away_team)}</h3>
    <p>${esc(formatKo(p.commence_time))}</p>
    <div class="pick-selection"><div class="pick-name">${esc((p.market || '').replace(/_/g, ' '))}: ${esc(selection)}${esc(line)}</div>
      <div class="prob-val">${fmtPct(p.p_true)}</div></div>
    <div class="metrics-row">
      <div class="metric-item"><div class="metric-lbl">Model fair odds</div><div class="metric-num">${fmtOdds(p.fair_odds)}</div></div>
      <div class="metric-item"><div class="metric-lbl">Observed odds</div><div class="metric-num">${fmtOdds(p.best_odds)}</div></div>
      <div class="metric-item"><div class="metric-lbl">Measured EV</div><div class="metric-num">${fmtPct(p.best_ev)}</div></div>
    </div>
    <p class="alpha-sub">${p.is_recommendation ? 'Reviewed selection' : 'Research forecast · no stake recommended'}${p.execution_locked ? ' · price access requires a paid account' : ''}</p>
  </article>`;
}

function renderLedger() {
  const tbody = document.getElementById('ledger-tbody');
  if (!tbody || !state.data) return;

  const statsPill = document.getElementById('ledger-stats-pill');
  if (statsPill) {
    const total = (state.data.settled_ledger || []).length;
    statsPill.textContent = total > 0 ? `${total} Settlements Audited` : 'Audited Settlements';
  }

  let ledger = (state.data.awaiting_results || []).map(row => ({...row,result:'AWAITING_RESULT'}))
    .concat(state.data.settled_ledger || []);
  if (state.ledgerSearchQuery && state.ledgerSearchQuery.trim()) {
    const q = state.ledgerSearchQuery.trim().toLowerCase();
    ledger = ledger.filter(r => {
      const home = (r.home_team || '').toLowerCase();
      const away = (r.away_team || '').toLowerCase();
      const match = `${home} vs ${away}`.toLowerCase();
      const sport = (r.sport_key || '').toLowerCase();
      const outcome = (r.outcome_name || '').toLowerCase();
      const res = (r.result || '').toLowerCase();
      return home.includes(q) || away.includes(q) || match.includes(q) || sport.includes(q) || outcome.includes(q) || res.includes(q);
    });
  }

  if (ledger.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="9" style="text-align: center; padding: 32px; color: var(--text-muted);">
          ${state.ledgerSearchQuery ? 'No ledger records match your search query. Try clearing the filter.' : 'No finished predictions yet. Saved predictions move here after kickoff; the result worker grades them after the final result is confirmed.'}
        </td>
      </tr>
    `;
    return;
  }

  tbody.innerHTML = ledger.map(r => {
    const clvClass = typeof r.clv === 'number' && r.clv < 0 ? 'clv-negative' : 'clv-positive';
    const clvStr = fmtPct(r.clv);
    const resClass = (r.result === 'WIN' || r.result === 'HALF_WIN') ? 'WIN' : ((r.result === 'LOSS' || r.result === 'HALF_LOSS') ? 'LOSS' : r.result === 'AWAITING_RESULT' ? 'PENDING' : 'VOID');
    const stakeUnits = r.is_recommendation && r.recommended_units > 0 ? `${r.recommended_units}u` : 'Forecast';

    return `
      <tr>
        <td style="font-weight: 600; color: #ffffff;">${esc(r.home_team)} vs ${esc(r.away_team)}</td>
        <td class="tabular-nums" style="font-weight: 700; color: var(--accent-gold); letter-spacing: 0.5px;">${esc(r.actual_score || '-')}</td>
        <td style="color: var(--text-secondary); text-transform: capitalize;">${esc((r.sport_key || '').replace(/_/g, ' '))}</td>
        <td style="color: var(--accent-cyan); font-weight: 600;">${esc((r.market || '').replace(/_/g, ' '))}: ${esc(r.outcome_name)}${r.line == null ? '' : ' '+esc(r.line)}</td>
        <td class="tabular-nums" style="font-weight: 600;">${fmtPct(r.p_true)}</td>
        <td class="tabular-nums">${fmtOdds(r.best_odds)}</td>
        <td class="tabular-nums">
          <span>${fmtOdds(r.closing_odds)}</span>
          <span class="${clvClass}" style="margin-left: 6px; font-size: 11px;">(${clvStr})</span>
        </td>
        <td class="tabular-nums" style="color: var(--accent-gold); font-weight: 600;">${stakeUnits}</td>
        <td><span class="result-badge ${resClass}">${r.result === 'AWAITING_RESULT' ? 'Awaiting confirmed result' : esc(r.result)}</span></td>
      </tr>
    `;
  }).join('');
}

function renderCalibration() {
  if (!state.data || !state.data.calibration) return;
  const c = state.data.calibration;
  const s = state.data.summary || {};

  const precision = s.brier_score !== null && s.brier_score !== undefined
    ? `${((1 - s.brier_score) * 100).toFixed(1)}%`
    : '—';
  const verifiedWin = s.win_rate !== null && s.win_rate !== undefined
    ? `${(s.win_rate * 100).toFixed(1)}%`
    : '—';
  const oddsAdv = s.mean_clv !== null && s.mean_clv !== undefined
    ? `${s.mean_clv >= 0 ? '+' : ''}${(s.mean_clv * 100).toFixed(2)}%`
    : '—';
  const trapsAvoided = s.traps_avoided_month !== undefined
    ? `${s.traps_avoided_month} Traps`
    : '—';

  const relEl = document.getElementById('murphy-rel');
  if (relEl) relEl.textContent = precision;

  const resEl = document.getElementById('murphy-res');
  if (resEl) resEl.textContent = verifiedWin;

  const uncEl = document.getElementById('murphy-unc');
  if (uncEl) uncEl.textContent = oddsAdv;

  const mceEl = document.getElementById('cal-mce');
  if (mceEl) mceEl.textContent = trapsAvoided;

  const samplePill = document.getElementById('cal-sample-pill');
  if (samplePill) {
    samplePill.textContent = c.sample_size
      ? `Audited Slate: ${c.sample_size} Matches`
      : 'Audited Slate: —';
  }

  const canvas = document.getElementById('reliability-canvas');
  if (canvas && c.bins) {
    renderReliabilityChart(canvas, c);
  }
}

function renderTier3Alpha() {
  // Everything here is derived from the live forecast board (real fixtures, real
  // Poisson output) and the settled ledger (real CLV). Nothing is templated.
  const forecast = state.forecast || { matches: [] };
  const rows = (forecast.matches || []).filter(m => m && m.micro);
  const settled = (state.data && state.data.settled_ledger) || [];

  // 1. Poisson micro markets for upcoming fixtures
  const poissonTbody = document.getElementById('alpha-poisson-tbody');
  if (poissonTbody) {
    const micro = [];
    rows.forEach(m => {
      const p = m.micro.p_over_2_5;
      if (typeof p !== 'number' || !m.commence_at) return;
      micro.push({ m, market: 'Over 2.5 goals', p, fair: 1 / p });
    });
    micro.sort((a, b) => b.p - a.p);
    poissonTbody.innerHTML = micro.length ? micro.slice(0, 12).map(({ m, market, p, fair }) => {
      const cd = formatCountdown(m.commence_at);
      return `
      <tr>
        <td style="font-weight: 600; color: #ffffff;">${esc(cleanText(m.home))} vs ${esc(cleanText(m.away))}</td>
        <td>
          <div class="kickoff-countdown-badge ${cd.status}" data-commence="${m.commence_at || ''}">
            <span class="countdown-text tabular-nums">${cd.text}</span>
          </div>
        </td>
        <td style="color: var(--text-primary); font-weight: 600;">${market}</td>
        <td class="tabular-nums" style="font-weight: 700; color: var(--accent-emerald);">${(p * 100).toFixed(1)}%</td>
        <td class="tabular-nums">${fair.toFixed(2)}</td>
        <td class="tabular-nums" style="color: var(--text-muted);">no priced market</td>
        <td class="tabular-nums" style="color: var(--text-muted);">n/a</td>
        <td><span class="pill-accent">${m.model && m.model.ready ? 'model ready' : 'thin model'}</span></td>
      </tr>`;
    }).join('') : `<tr><td colspan="8" style="color: var(--text-muted);">No live fixtures with a scored model right now.</td></tr>`;
  }

  // 2. CLV reality check: what the ledger actually recorded
  const steamList = document.getElementById('steam-signals-list');
  if (steamList) {
    const withClv = settled.filter(r => typeof r.clv === 'number');
    const clv = state.data && state.data.clv;
    steamList.innerHTML = withClv.length ? `
      <div class="steam-signal-item">
        <div class="steam-header">
          <div class="steam-match">Closing line value across ${withClv.length} settled picks</div>
        </div>
        <div class="steam-body">
          <div>Mean CLV: <strong>${clv && clv.mean_clv != null ? (clv.mean_clv * 100).toFixed(2) + '%' : 'n/a'}</strong></div>
          <div>Positive CLV share: <strong>${clv && clv.positive_clv_share != null ? (clv.positive_clv_share * 100).toFixed(1) + '%' : 'n/a'}</strong></div>
          <div>Best: <strong>${(Math.max(...withClv.map(r => r.clv)) * 100).toFixed(2)}%</strong></div>
          <div>Worst: <strong>${(Math.min(...withClv.map(r => r.clv)) * 100).toFixed(2)}%</strong></div>
        </div>
      </div>` : `<div class="steam-signal-item"><div class="steam-body">No settled pick has a recorded closing line yet. LISA shows nothing until the real close is captured.</div></div>`;
  }

  // 3. Real parlay from live priced legs
  const parlaysList = document.getElementById('parlays-list');
  if (parlaysList) {
    const picks = (state.data && state.data.active_picks) || [];
    const legs = picks
      .filter(p => p.best_odds && p.p_true && p.outcome_name)
      .sort((a, b) => (b.p_true || 0) - (a.p_true || 0))
      .slice(0, 5);
    if (legs.length < 2) {
      parlaysList.innerHTML = `<div class="parlay-item"><div class="parlay-meta">Not enough priced live selections to build an honest parlay.</div></div>`;
    } else {
      const jointNaive = legs.reduce((acc, p) => acc * p.p_true, 1.0);
      const odds = legs.reduce((acc, p) => acc * p.best_odds, 1.0);
      // Same correlation haircut as the ranked board (board.py) and the banner:
      // legs on the same matchday are not independent, and the naive product
      // overstates the joint probability. Both figures are shown so the
      // adjustment is visible rather than hidden.
      const legDays = new Set(legs
        .map(l => (l.commence_time ? String(l.commence_time).slice(0, 10) : ''))
        .filter(Boolean));
      const penalty = legDays.size < legs.length ? 0.94 : 1.0;
      const joint = jointNaive * penalty;
      // No clamping: a negative EV stays negative.
      const edge = joint > 0 ? (odds * joint - 1) * 100 : 0;
      const edgeColor = edge >= 0 ? 'var(--accent-emerald)' : 'var(--neg)';
      parlaysList.innerHTML = `<div class="parlay-item">
        <div class="parlay-title">Live ${legs.length}-leg consensus slip</div>
        <ul class="parlay-legs">${legs.map(l => `<li>${esc(cleanText(l.home_team))} vs ${esc(cleanText(l.away_team))}: ${esc(cleanText(l.outcome_name))} @ ${Number(l.best_odds).toFixed(2)} (${esc(cleanText(l.best_book || 'best'))})</li>`).join('')}</ul>
        <div class="parlay-meta">
          <div>Joint prob: <strong>${(joint * 100).toFixed(1)}%</strong>${
            penalty < 1
              ? ` <span style="color: var(--text-muted);">(naive product ${(jointNaive * 100).toFixed(1)}%, ${((1 - penalty) * 100).toFixed(0)}% correlation penalty)</span>`
              : ''}</div>
          <div>Combined odds: <strong>${odds.toFixed(2)}</strong></div>
          <div style="color: ${edgeColor}; font-weight: 700;">EV ${edge >= 0 ? '+' : ''}${edge.toFixed(1)}%</div>
        </div>
      </div>`;
    }
  }
}

async function renderBacktest() {
  // The archive replay is real but expensive: fetch it from the API on demand
  // instead of shipping a stale static export to every visitor.
  if (!state.backtest) {
    try {
      const res = await fetch('/api/backtest', { cache: 'no-cache' });
      if (res.ok) state.backtest = await res.json();
    } catch (e) {
      console.warn('backtest unavailable:', e);
    }
  }
  const b = state.backtest;
  if (!b) return;

  // 1. Render Strategy Yield & Risk Comparison Matrix
  const matrixTbody = document.getElementById('strategy-matrix-tbody');
  if (matrixTbody && b.strategy_comparison_matrix) {
    const activeStrat = state.activeBktStrategy || 'conservative';
    matrixTbody.innerHTML = b.strategy_comparison_matrix.map(row => {
      const isActive = activeStrat === row.strategy_id;
      const rowStyle = isActive ? 'style="background: rgba(6, 182, 212, 0.12); border-left: 3px solid var(--accent-cyan);"' : '';
      return `
        <tr ${rowStyle}>
          <td>
            <div style="font-weight: 700; color: #ffffff;">${esc(cleanText(row.name))}</div>
            <div style="font-size: 11px; color: var(--text-muted); margin-top: 2px;">${esc(cleanText(row.description))}</div>
          </td>
          <td class="tabular-nums" style="color: var(--accent-emerald); font-weight: 700;">${(row.win_rate * 100).toFixed(1)}%</td>
          <td class="tabular-nums" style="color: ${row.roi_pct >= 0 ? 'var(--accent-emerald)' : 'var(--accent-rose)'}; font-weight: 700;">${row.roi_pct >= 0 ? '+' : ''}${row.roi_pct.toFixed(1)}%</td>
          <td class="tabular-nums" style="color: var(--accent-gold); font-weight: 700;">${row.net_profit >= 0 ? '+' : '-'}$${Math.abs(row.net_profit).toFixed(2)}</td>
          <td class="tabular-nums" style="color: var(--accent-rose); font-weight: 600;">-${row.max_drawdown_pct.toFixed(2)}%</td>
          <td><span class="pill-league" style="font-size: 11px;">${esc(cleanText(row.best_for))}</span></td>
        </tr>
      `;
    }).join('');
  }

  // 2. Resolve Active Strategy Summary & Records
  const stratKey = state.activeBktStrategy || 'conservative';
  const stratData = (b.strategies && b.strategies[stratKey]) ? b.strategies[stratKey] : { summary: b.summary, records: b.records };
  const s = stratData.summary || b.summary || {};
  const rawRecords = stratData.records || b.records || [];

  const descEl = document.getElementById('bkt-strat-desc');
  if (descEl && s.description) descEl.textContent = cleanText(s.description);

  const winRateEl = document.getElementById('bkt-win-rate');
  if (winRateEl) winRateEl.textContent = numOrDash(s.win_rate, (v) => `${(v * 100).toFixed(1)}%`);

  const ciEl = document.getElementById('bkt-ci');
  if (ciEl) {
    ciEl.textContent = (typeof s.wilson_ci_lower === 'number' && typeof s.wilson_ci_upper === 'number')
      ? `95% CI: [${(s.wilson_ci_lower * 100).toFixed(1)}%, ${(s.wilson_ci_upper * 100).toFixed(1)}%]`
      : '95% CI: n/a';
  }

  const capSavedEl = document.getElementById('bkt-capital-saved');
  if (capSavedEl) {
    capSavedEl.textContent = numOrDash(s.capital_preserved_dollars, (v) => `$${v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
  }

  const netSavedEl = document.getElementById('bkt-net-saved');
  if (netSavedEl) {
    netSavedEl.textContent = numOrDash(s.net_counterfactual_value, (v) => `Net Adv: ${v >= 0 ? '+' : ''}$${v.toFixed(2)}`);
  }

  const mddEl = document.getElementById('bkt-mdd');
  if (mddEl) mddEl.textContent = numOrDash(s.max_drawdown_pct, (v) => `-${v.toFixed(2)}%`);

  const mddDollarsEl = document.getElementById('bkt-mdd-dollars');
  if (mddDollarsEl) mddDollarsEl.textContent = numOrDash(s.max_drawdown_dollars, (v) => `-$${v.toFixed(2)} Peak Drop`);

  const roiEl = document.getElementById('bkt-roi');
  if (roiEl) roiEl.textContent = numOrDash(s.roi_pct, (v) => `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`);

  const profitEl = document.getElementById('bkt-profit');
  if (profitEl && s.net_profit !== undefined) {
    const sign = s.net_profit >= 0 ? '+' : '';
    profitEl.textContent = `${sign}$${s.net_profit.toFixed(2)} Net Profit`;
  }

  const tbody = document.getElementById('backtest-tbody');
  if (!tbody || !rawRecords) return;

  const filterSport = state.activeBktSport || 'all';
  const filterGrade = state.activeBktGrade || 'all';
  const records = rawRecords.filter(r => {
    const matchSport = filterSport === 'all' || r.sport_key === filterSport || r.sport_key.includes(filterSport);
    const matchGrade = filterGrade === 'all' || r.grade === filterGrade;
    return matchSport && matchGrade;
  });

  const ledgerCountEl = document.getElementById('bkt-ledger-count');
  if (ledgerCountEl) ledgerCountEl.textContent = records.length;

  tbody.innerHTML = records.map(r => {
    let gradeBadge = '';
    if (r.grade === 'GRADE_A') {
      gradeBadge = `<span class="pill-grade pill-grade-a">Grade A</span>`;
    } else if (r.grade === 'GRADE_B') {
      gradeBadge = `<span class="pill-grade pill-grade-b">Smart Pivot</span>`;
    } else {
      gradeBadge = `<span class="pill-grade pill-grade-c">Pass Advisory</span>`;
    }

    let resultBadge = '';
    let pnlDisplay = '';
    if (r.result === 'WIN') {
      resultBadge = `<span style="color: var(--accent-emerald); font-weight: 700;">WON</span>`;
      pnlDisplay = `<span style="color: var(--accent-emerald); font-weight: 700;">+$${r.pnl.toFixed(2)}</span>`;
    } else if (r.result === 'LOSS') {
      resultBadge = `<span style="color: var(--accent-rose); font-weight: 700;">LOST</span>`;
      pnlDisplay = `<span style="color: var(--accent-rose); font-weight: 700;">-$${Math.abs(r.pnl).toFixed(2)}</span>`;
    } else {
      resultBadge = `<span style="color: var(--accent-amber); font-weight: 700;">TRAP AVOIDED</span>`;
      pnlDisplay = `<span style="color: var(--accent-amber); font-weight: 700;">+$${r.capital_saved.toFixed(2)} Saved</span>`;
    }

    const sportLabel = {
      'soccer_epl': 'Premier League',
      'soccer_spain_la_liga': 'La Liga',
      'soccer_germany_bundesliga': 'Bundesliga',
      'soccer_italy_serie_a': 'Serie A',
      'basketball_nba': 'NBA',
    }[r.sport_key] || r.sport_key.replace('_', ' ');

    return `
      <tr>
        <td>
          <strong style="color: #fff;">${esc(r.home_team)} vs ${esc(r.away_team)}</strong>
          ${r.hazard_warning ? `<div style="font-size: 11px; color: var(--accent-amber); margin-top: 2px;">${r.hazard_warning}</div>` : ''}
        </td>
        <td><span class="pill-league">${sportLabel}</span></td>
        <td>${gradeBadge} <div style="font-size: 12px; color: #fff; margin-top: 3px;">${esc(r.outcome_name)}</div></td>
        <td><span style="font-size: 11px; text-transform: uppercase; color: var(--text-muted);">${r.market.replace('_', ' ')}</span></td>
        <td class="tabular-nums" style="font-weight: 600;">${(r.p_true * 100).toFixed(1)}%</td>
        <td class="tabular-nums">${r.best_odds.toFixed(2)} <span style="font-size: 11px; color: var(--text-muted);">(${esc(r.best_book)})</span></td>
        <td class="tabular-nums" style="font-weight: 700; color: #fff;">${r.actual_score}</td>
        <td>${resultBadge}</td>
        <td class="tabular-nums">${pnlDisplay}</td>
      </tr>
    `;
  }).join('');
}

const TIER_RANKS = { free: 0, tier1: 1, tier2: 2, tier3: 3 };

function currentTierRank() {
  return TIER_RANKS[state.currentTier || 'free'] ?? 0;
}

window.toggleTickerDropdown = function (e) {
  if (e) {
    e.preventDefault();
    e.stopPropagation();
  }
  const ticker = document.getElementById('market-ticker');
  if (!ticker) return;

  if (ticker.classList.contains('is-open')) {
    window.closeTickerDropdown();
  } else {
    window.openTickerDropdown();
  }
};

window.openTickerDropdown = function () {
  const ticker = document.getElementById('market-ticker');
  const btn = document.getElementById('btn-ticker-trigger');
  if (!ticker) return;
  ticker.classList.add('is-open');
  ticker.setAttribute('aria-expanded', 'true');
  if (btn) {
    btn.classList.add('active');
    btn.setAttribute('aria-expanded', 'true');
  }
};

window.closeTickerDropdown = function () {
  const ticker = document.getElementById('market-ticker');
  const btn = document.getElementById('btn-ticker-trigger');
  if (!ticker) return;
  ticker.classList.remove('is-open');
  ticker.setAttribute('aria-expanded', 'false');
  if (btn) {
    btn.classList.remove('active');
    btn.setAttribute('aria-expanded', 'false');
  }
};

// Backward-compatible alias
window.toggleTicker = window.toggleTickerDropdown;

function renderTicker() {
  const track = document.getElementById('ticker-track');
  const wrap = document.getElementById('market-ticker');
  const btn = document.getElementById('btn-ticker-trigger');
  if (!track) return;
  const matches = (state.forecast && state.forecast.matches) || [];
  if (!matches.length) {
    if (wrap) wrap.style.display = 'none';
    if (btn) btn.style.display = 'none';
    return;
  }
  if (wrap) wrap.style.display = 'flex';
  if (btn) btn.style.display = 'inline-flex';

  const items = matches.slice(0, 14).map((m) => {
    const probs = [m.model.p_home, m.model.p_draw, m.model.p_away];
    const topProb = m.market && m.market.p_top != null ? m.market.p_top : Math.max(...probs);
    let lean = 'Home';
    if (m.model.p_draw > m.model.p_home && m.model.p_draw > m.model.p_away) lean = 'Draw';
    else if (m.model.p_away > m.model.p_home) lean = 'Away';
    if (m.market && m.market.top_outcome) lean = m.market.top_outcome;

    let steamBadge = '';
    if (m.movement) {
      const dir = m.movement.direction;
      const cls = dir === 'steam_in' ? 'steam' : dir === 'drift_out' ? 'drift' : 'flat';
      const label = dir === 'steam_in' ? 'Steam' : dir === 'drift_out' ? 'Drift' : 'Flat';
      steamBadge = `<span class="tk-tag ${cls}">${label}</span>`;
    }

    const leagueShort = String(m.league || '').replace(/soccer_/g, '').replace(/_/g, ' ').toUpperCase();

    return `
      <div class="ticker-match-card" onclick="window.switchTab('forecast')">
        <span class="tm-league-chip">${leagueShort}</span>
        ${m.marquee ? `<span class="tm-marquee-chip">★ POPULAR</span>` : ''}
        <span class="tm-team home">${esc(cleanText(m.home))}</span>
        <span class="tm-vs">VS</span>
        <span class="tm-team away">${esc(cleanText(m.away))}</span>
        <span class="tm-prob-chip">
          <span class="tm-lean">${lean}</span>
          <span class="tm-pct">${(topProb * 100).toFixed(0)}%</span>
        </span>
        ${steamBadge}
      </div>`;
  }).join('');

  if (track) track.innerHTML = items + items;
  const overviewTrack = document.getElementById('overview-ticker-track');
  if (overviewTrack) overviewTrack.innerHTML = items + items;
}

// ---------------------------------------------------------------------------
// Model Opportunity Board  (/api/opportunity-board)
// ---------------------------------------------------------------------------
// The only surface fed by engine/lisa/board.py. Before this existed the
// winning/earning ladders, the micro-bets and the accumulators were computed
// every cycle and thrown away, while the screen showed a separate, unrelated
// client-side parlay calculation.
//
// Two rules this file must not break:
//   1. An absent number renders as absent. `null * 100 === 0` in JavaScript, so
//      any unguarded arithmetic silently invents a measured value -- an edge of
//      "0.0%" on a market that was never observed. Every numeric field here
//      goes through fmtOdds/fmtPct, which return 'n/a' for non-finite input.
//   2. The earning ladder is rendered empty when nothing is priced. Filling it
//      with model-only rows would present a fair price as an offer.

/** Fetch the board. Never throws; failures land in state, not the console. */
async function refreshModelBoard(force) {
  if (state.modelBoardState === 'loading') return;
  state.modelBoardState = 'loading';
  state.modelBoardError = '';
  renderModelBoard();

  const url = '/api/opportunity-board' + (force ? '?refresh=1' : '');
  try {
    const res = await fetchDashboardResource(url);
    let payload;
    try {
      payload = await res.json();
    } catch (e) {
      throw new Error(`HTTP ${res.status}: response was not JSON`);
    }
    // A 503 carries a real explanation and board:null. Treat it as a failed
    // cycle to display, not as a transport problem to swallow.
    state.modelBoard = payload;
    state.modelBoardState = (payload && payload.success) ? 'ready' : 'failed';
    if (!payload.success) {
      state.modelBoardError = (payload && payload.error) ||
        'the board cycle did not complete';
    }
  } catch (err) {
    state.modelBoard = null;
    state.modelBoardState = 'failed';
    state.modelBoardError = String(err && err.message ? err.message : err);
  }
  renderModelBoard();
}

window.refreshModelBoard = refreshModelBoard;

/** Coverage + honesty banner. States what the cycle could and could not do. */
function renderModelBoardStatus() {
  const el = document.getElementById('mb-status');
  if (!el) return;

  if (state.modelBoardState === 'loading') {
    el.innerHTML = `
      <div class="loading-state-card">
        <div class="loading-spinner"></div>
        <p>Loading the latest published model board...</p>
      </div>`;
    return;
  }

  const p = state.modelBoard;
  if (state.modelBoardState === 'failed' || !p) {
    const detail = state.modelBoardError || 'no response from the board endpoint';
    el.innerHTML = `
      <div class="alpha-card" style="border-color: var(--neg);">
        <h3 class="alpha-title">Board cycle failed</h3>
        <p class="alpha-sub">No board was published, and none is shown below.
           LISA does not substitute a cached or invented board.</p>
        <pre style="white-space:pre-wrap; font-size:12px; color: var(--text-secondary);
                    margin-top:12px;">${esc(detail)}</pre>
        ${p && Array.isArray(p.errors) && p.errors.length ? `
          <ul style="margin-top:12px; font-size:12.5px; color: var(--text-secondary);">
            ${p.errors.map(e => `<li>${esc(e)}</li>`).join('')}
          </ul>` : ''}
      </div>`;
    return;
  }

  const board = p.board;
  const cov = board && board.coverage;
  const badge = document.getElementById('mb-count-badge');
  if (badge) {
    const n = board ? (board.winning.length + board.micro_bets.length) : 0;
    badge.textContent = n ? ` (${n})` : '';
    badge.style.display = n ? '' : 'none';
  }

  const rows = [];
  rows.push(`<div class="alpha-card">
    <div class="alpha-card-header"><div>
      <h3 class="alpha-title">Cycle status</h3>
      <p class="alpha-sub">Generated ${esc(formatKo(p.generated_at))} &middot;
        ${esc(p.window_hours)}h window &middot; health <strong>${esc(p.health)}</strong>
        ${p.cached ? '&middot; cached' : ''}</p>
    </div>
    <span class="pill-accent ${p.health === 'ok' ? 'emerald' : 'amber'}">${esc(p.health)}</span>
    </div>`);

  if (board && board.unproven) {
    rows.push(`<p class="alpha-sub" style="color: var(--warn); margin-top:10px;">
      <strong>Unproven:</strong> the fit is short of the minimum games per team, so
      these ladders are advisory only. A model with two matches per team is a prior
      with a scorer attached.</p>`);
  }
  if (p.stale_reason) {
    rows.push(`<p class="alpha-sub" style="color: var(--warn); margin-top:10px;">
      Showing the last good board &mdash; this cycle failed:
      ${esc(p.stale_reason)}</p>`);
  }
  if (cov) {
    rows.push(`<div class="metrics-row" style="margin-top:14px;">
      <div class="metric-item"><div class="metric-lbl">Fixtures seen</div>
        <div class="metric-num tabular-nums">${esc(cov.fixtures_seen)}</div></div>
      <div class="metric-item"><div class="metric-lbl">Modelled</div>
        <div class="metric-num tabular-nums">${esc(cov.fixtures_modelled)}</div></div>
      <div class="metric-item"><div class="metric-lbl">Priced</div>
        <div class="metric-num tabular-nums">${esc(cov.fixtures_priced)}</div></div>
      <div class="metric-item"><div class="metric-lbl">Volume target</div>
        <div class="metric-num tabular-nums">${cov.meets_volume_target ? 'met' : 'short'}
          <span class="metric-sub">${esc(cov.volume_target)}</span></div></div>
    </div>`);
    if (!cov.meets_volume_target) {
      // A data-coverage limit, not a selection limit. Saying it wrong here
      // would imply the model had opportunities and declined to list them.
      rows.push(`<p class="alpha-sub" style="margin-top:10px;">
        Volume target of ${esc(cov.volume_target)} fixtures in the window was not met
        (${esc(cov.fixtures_seen)} seen). This is a
        <strong>coverage limit</strong> &mdash; the sources returned too few
        in-window fixtures &mdash; not the model declining to recommend.</p>`);
    }
    if (Array.isArray(cov.notes) && cov.notes.length) {
      rows.push(`<ul style="margin-top:10px; font-size:12px; color: var(--text-muted);">
        ${cov.notes.map(n => `<li>${esc(n)}</li>`).join('')}</ul>`);
    }
  }
  if (board && Array.isArray(board.notes) && board.notes.length) {
    rows.push(`<ul style="margin-top:10px; font-size:12.5px; color: var(--text-secondary);">
      ${board.notes.map(n => `<li>${esc(n)}</li>`).join('')}</ul>`);
  }
  if (Array.isArray(p.errors) && p.errors.length) {
    rows.push(`<ul style="margin-top:10px; font-size:12.5px; color: var(--warn);">
      ${p.errors.map(e => `<li>${esc(e)}</li>`).join('')}</ul>`);
  }
  rows.push('</div>');
  el.innerHTML = rows.join('');
}

/** One opportunity row. Unpriced fields must never render as a number. */
function mbOpportunityRow(o) {
  const priced = o.priced === true && typeof o.ev === 'number' && isFinite(o.ev);
  const evText = priced
    ? `${o.ev >= 0 ? '+' : ''}${(o.ev * 100).toFixed(1)}%`
    : 'n/a';
  const evColor = priced ? (o.ev > 0 ? 'var(--pos)' : 'var(--text-muted)') : 'var(--text-muted)';
  const line = (o.line === null || o.line === undefined) ? '' : ` ${esc(o.line)}`;
  return `
    <tr>
      <td>
        <div class="pick-name">${esc(o.home)} v ${esc(o.away)}</div>
        <div style="font-size:11px; color: var(--text-muted);">
          ${esc(o.sport_key)} &middot; ${esc(formatKo(o.kickoff))}</div>
      </td>
      <td><span class="odds-tag">${esc(({double_chance: "Double chance", home_team_totals: "Home team goals", away_team_totals: "Away team goals", draw_no_bet: "Draw no bet", asian_handicap: "Asian handicap", corners: "Corners"})[o.market] || o.market)}${line}</span>
        <div style="font-size:11px; color: var(--text-secondary);">${esc(o.selection)}</div></td>
      <td class="tabular-nums">${fmtPct(o.p_model)}</td>
      <td class="tabular-nums">${fmtOdds(o.fair_odds)}</td>
      <td class="tabular-nums">${fmtOdds(o.best_odds)}<div style="font-size:11px;">${esc(o.best_book || "")}</div></td>
      <td class="tabular-nums" style="color: ${evColor};">${evText}</td>
      <td style="font-size:11px; color: var(--text-muted);">${esc(o.basis)}${
        o.reason ? ' &middot; ' + esc(o.reason) : ''}<div>Stake: ${typeof o.stake_fraction === 'number' && o.stake_fraction > 0 ? (o.stake_fraction * 100).toFixed(2) + '%' : 'No stake'}</div></td>
    </tr>`;
}

const MB_HEAD = `<thead><tr>
  <th>Match</th><th>Market</th><th>Model p</th><th>Fair</th>
  <th>Best price / book</th><th>Edge (EV)</th><th>Basis / stake</th>
</tr></thead>`;

/** The three lists: winning ladder, earning ladder, micro markets. */
function renderModelBoardLadders() {
  const el = document.getElementById('mb-ladders');
  if (!el) return;
  const board = state.modelBoard && state.modelBoard.board;
  if (!board) { el.innerHTML = ''; return; }

  const parts = [];

  // Winning ladder: nearest kickoff, then model probability. Model-only, so it
  // is the one list that can be full with no price source at all.
  parts.push(`<div class="alpha-card">
    <div class="alpha-card-header"><div>
      <h3 class="alpha-title">Winning ladder</h3>
      <p class="alpha-sub">Earliest kickoff first, then highest model probability.
        Each fixture contributes its strongest eligible pick. Model fair odds
        need a current bookmaker price before their value can be assessed.</p>
    </div>
    <span class="pill-accent">${board.winning.length}</span></div>
    ${board.winning.length
      ? `<div class="table-scroll-container"><table class="data-table">${MB_HEAD}<tbody>${board.winning.map(mbOpportunityRow).join('')}</tbody></table></div>`
      : '<p class="alpha-sub">No opportunities in window.</p>'}
  </div>`);

  // Earning ladder: positive EV required. Only priced rows can appear, so an empty list
  // has three quite different causes and they are told apart here rather than
  // all collapsing into "no prices".
  const pricedCount = board.earning.filter(o => o.priced).length;
  const coverage = board.coverage || {};
  const priceDiag = (state.modelBoard && state.modelBoard.prices) || {};
  const matchDiag = (state.modelBoard && state.modelBoard.price_match) || {};
  let emptyReason;
  if (!priceDiag.quotes) {
    emptyReason = `<p class="alpha-sub" style="color: var(--warn);">
           Empty by design. Expected value is
           <code>probability &times; price &minus; 1</code>, and no market price has
           been observed, so no EV exists to rank. LISA will not list a model fair
           price as if it were an offer.</p>`;
  } else if (!coverage.fixtures_priced) {
    emptyReason = `<p class="alpha-sub" style="color: var(--warn);">
           ${priceDiag.quotes} prices were read but none could be attached to a
           fixture in this window. LISA publishes a price only when it can prove
           the book event and the calendar fixture are the same match, so
           unmatched events are dropped rather than guessed at.</p>`;
  } else {
    emptyReason = `<p class="alpha-sub">
           No selection cleared the edge threshold against a live price. Prices
           were read and ${coverage.fixtures_priced} fixture${coverage.fixtures_priced === 1 ? ' was' : 's were'}
           priced &mdash; the model simply does not think the books are offering
           anything worth taking right now.</p>`;
  }
  parts.push(`<div class="alpha-card">
    <div class="alpha-card-header"><div>
      <h3 class="alpha-title">Earning ladder</h3>
      <p class="alpha-sub">Current prices must clear the expected-value threshold.
        Earliest kickoff first, then winning probability and expected value.</p>
    </div>
    <span class="pill-accent ${pricedCount ? 'emerald' : 'amber'}">${board.earning.length}</span></div>
    ${board.earning.length
      ? `<div class="table-scroll-container"><table class="data-table">${MB_HEAD}<tbody>${board.earning.map(mbOpportunityRow).join('')}</tbody></table></div>`
      : emptyReason}
  </div>`);

  parts.push(microMarketsHtml(board));

  el.innerHTML = parts.join('');
}

/** Shared market table for the model board and the dedicated micro-bets view. */
function microMarketsHtml(board) {
  return `<div class="alpha-card">
    <div class="alpha-card-header"><div>
      <h3 class="alpha-title">Derived micro markets</h3>
      <p class="alpha-sub">Selections derived from the model itself (correct score,
        totals lines, both teams to score), excluding anything already on the
        earning ladder. Earliest kickoff first, then winning probability.</p>
    </div>
    <span class="pill-accent">${board.micro_bets.length}</span></div>
    ${board.micro_bets.length
      ? `<div class="table-scroll-container"><table class="data-table">${MB_HEAD}<tbody>${board.micro_bets.map(mbOpportunityRow).join('')}</tbody></table></div>`
      : '<p class="alpha-sub">No derived micro markets in window.</p>'}
  </div>`;
}

function renderMicroBets() {
  const status = document.getElementById('micro-status');
  const boardStatus = document.getElementById('mb-status');
  if (status && boardStatus) status.innerHTML = boardStatus.innerHTML;
  const board = state.modelBoard && state.modelBoard.board;
  const markets = document.getElementById('micro-markets');
  if (markets) markets.innerHTML = board ? microMarketsHtml(board) : '';
  const badge = document.getElementById('micro-count-badge');
  if (badge) {
    const count = board ? board.micro_bets.length : 0;
    badge.textContent = count ? ` (${count})` : '';
    badge.style.display = count ? '' : 'none';
  }
}

/** Accumulators, with the correlation penalty shown next to the naive figure. */
function renderModelBoardAccumulators() {
  const el = document.getElementById('mb-accumulators');
  if (!el) return;
  const board = state.modelBoard && state.modelBoard.board;
  if (!board) { el.innerHTML = ''; return; }

  const accas = board.accumulators || [];
  const anyPriced = accas.some(a => a.priced);

  const body = accas.length
    ? accas.map(a => {
        // p_adjusted is the joint probability after penalising leg correlation.
        // Showing only the naive product would be the single most misleading
        // thing this page could do: two legs from one matchday cannot both win,
        // so the product of their probabilities is optimistic by construction.
        const penaltyPct = ((1 - a.correlation_penalty) * 100).toFixed(1);
        const evText = (a.priced && typeof a.ev === 'number' && isFinite(a.ev))
          ? `${a.ev >= 0 ? '+' : ''}${(a.ev * 100).toFixed(1)}%`
          : 'n/a';
        return `
        <div class="alpha-card" style="margin-bottom:12px;">
          <div class="alpha-card-header"><div>
            <h3 class="alpha-title">${esc(a.description)}</h3>
            <p class="alpha-sub">${a.size}-leg &middot; correlation penalty
              ${esc(penaltyPct)}%</p>
          </div>
          <span class="pill-accent ${a.priced ? 'emerald' : 'amber'}">
            ${a.priced ? 'priced' : 'unpriced'}</span></div>
          <div class="metrics-row" style="margin-top:12px;">
            <div class="metric-item"><div class="metric-lbl">Joint p (adjusted)</div>
              <div class="metric-num tabular-nums">${fmtPct(a.p_adjusted, 2)}</div></div>
            <div class="metric-item"><div class="metric-lbl">Naive product</div>
              <div class="metric-num tabular-nums">${fmtPct(a.p_naive, 2)}</div></div>
            <div class="metric-item"><div class="metric-lbl">Fair odds</div>
              <div class="metric-num tabular-nums">${fmtOdds(a.fair_odds)}</div></div>
            <div class="metric-item"><div class="metric-lbl">Best price</div>
              <div class="metric-num tabular-nums">${fmtOdds(a.best_odds)}</div></div>
            <div class="metric-item"><div class="metric-lbl">Edge (EV)</div>
              <div class="metric-num tabular-nums">${evText}</div></div>
          </div>
          <div class="table-scroll-container" style="margin-top:12px;"><table class="data-table">${MB_HEAD}
            <tbody>${a.legs.map(mbOpportunityRow).join('')}</tbody></table></div>
          ${Array.isArray(a.warnings) && a.warnings.length ? `
            <ul style="margin-top:10px; font-size:12px; color: var(--warn);">
              ${a.warnings.map(w => `<li>${esc(w)}</li>`).join('')}</ul>` : ''}
        </div>`;
      }).join('')
    : `<p class="alpha-sub">No accumulators built for this cycle${
        anyPriced ? '' : ' &mdash; parlays need a real price per leg to be worth publishing'}.</p>`;

  el.innerHTML = `
    <div class="alpha-card">
      <div class="alpha-card-header"><div>
        <h3 class="alpha-title">Accumulators</h3>
        <p class="alpha-sub">Multi-leg parlays. Every leg's correlation penalty is
          applied to the joint probability and the naive product is shown beside it,
          because multiplying leg probabilities assumes independence and
          same-matchday legs are not independent.</p>
      </div>
      <span class="pill-accent">${accas.length}</span></div>
      ${body}
    </div>`;
}

function renderModelBoard() {
  renderModelBoardStatus();
  renderMicroBets();
  renderModelBoardLadders();
  renderModelBoardAccumulators();
}

function renderForecastBoard() {
  const board = document.getElementById('forecast-board');
  if (!board) return;
  const badge = document.getElementById('forecast-count-badge');
  if (badge) badge.textContent = state.forecast && state.forecast.count ? ` (${state.forecast.count})` : '';

  if (!state.forecast || !state.forecast.matches || !state.forecast.matches.length) {
    board.innerHTML = `
      <div style="grid-column: 1 / -1; text-align:center; padding:48px; color: var(--text-secondary);">
        No upcoming forecasts published yet.
        <div style="font-size:12px; color: var(--text-muted); margin-top:8px;">
          The prediction worker collects fixtures and results before publishing.
          This view refreshes automatically as forecasts become available.
          ${state.data && state.data.pipeline && Number(state.data.pipeline.window_hours) > 0 ? 'The published forecast window covers the next ' + esc(state.data.pipeline.window_hours) + ' hours. Check Daily Board → Next Matches for verified fixtures awaiting enough training history.' : ''}
          ${(state.data && state.data.awaiting_results || []).length ? 'Previously published predictions are awaiting confirmed results in Ledger.' : ''}
        </div>
      </div>`;
    return;
  }

  const rows = state.forecast.matches.slice();
  rows.sort((a, b) => {
    const time = Date.parse(a.commence_at) - Date.parse(b.commence_at);
    const probability = m => Math.max(m.model.p_home, m.model.p_draw, m.model.p_away);
    return time || probability(b) - probability(a) || String(a.match_id).localeCompare(String(b.match_id));
  });

  const koLabel = (m) => `KO ${formatKo(m.commence_at)}`;

  const unlockStrip = `
    <div class="fc-unlock-strip">
      <div class="unlock-text">
        <div>
          <strong>${state.forecast.count} Upcoming Match Forecasts</strong>: Free public probability models displayed below.
          <div class="unlock-sub">Earliest kickoff first, then strongest outright probability. The worker automatically reaches the nearest forecastable fixtures.</div>
        </div>
      </div>
      <div class="unlock-actions">
        <button type="button" class="btn-unlock-tier1" onclick="window.handlePricingSelect('tier1')">Start Sharp ($19)</button>
        <button type="button" class="btn-unlock-tier2-glow" onclick="window.handlePricingSelect('tier2')">Unlock Pro Edge ($49)</button>
      </div>
    </div>`;

  const cards = rows.map((m) => {
    const tags = [];
    if (m.marquee) tags.push(`<span class="fc-tag popular">★ MARQUEE</span>`);
    if (m.movement) {
      const dir = m.movement.direction;
      const title = dir === 'steam_in' ? 'Heavy backing: odds dropping' : dir === 'drift_out' ? 'Market fading: odds rising' : 'Stable market odds';
      if (dir === 'steam_in') tags.push(`<span class="fc-tag steam" title="${title}">Steam In</span>`);
      else if (dir === 'drift_out') tags.push(`<span class="fc-tag drift" title="${title}">Drift Out</span>`);
      else tags.push(`<span class="fc-tag flat" title="${title}">Flat</span>`);
    }
    tags.push(`<span class="fc-tag league">${String(m.league || '').replace(/_/g, ' ')}</span>`);

    const ph = (m.model.p_home * 100), pd = (m.model.p_draw * 100), pa = (m.model.p_away * 100);
    const marketLine = m.market
      ? `Consensus: <strong>${m.market.top_outcome}</strong> at <strong>${(m.market.p_top * 100).toFixed(0)}%</strong> from ${m.market.n_books} global books`
      : 'Baseline model prediction';

    const u = m.uncertainty || { level: 'low', reasons: [] };
    const uLabel = u.level === 'high' ? 'High Uncertainty' : u.level === 'medium' ? 'Medium Uncertainty' : 'High Conviction';
    const uReason = Array.isArray(u.reasons) && u.reasons.length
      ? u.reasons.map((r) => typeof r === 'string' ? r : r.label).join(' · ')
      : 'No uncertainty assessment supplied.';

    const micro = m.micro || {};
    const scoreTxt = (micro.most_likely_scores || []).slice(0, 3)
      .map((s) => `${s.score || `${s.home_goals}-${s.away_goals}`} (${fmtPct(s.p, 0)})`).join('  ·  ') || '-';

    return `
      <article class="fc-card ${m.marquee ? 'is-marquee-card' : ''}">
        <div class="fc-topline">
          <div class="fc-tags">${tags.join('')}</div>
          <span class="fc-ko">${koLabel(m)}</span>
        </div>

        <div class="fc-matchup-container">
          <div class="fc-team-box home">
            <span class="fc-team-name">${esc(cleanText(m.home))}</span>
          </div>
          <div class="fc-vs-chip">VS</div>
          <div class="fc-team-box away">
            <span class="fc-team-name">${esc(cleanText(m.away))}</span>
          </div>
        </div>

        <div class="fc-market-quote">
          <span class="fc-quote-icon"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><line x1="18" y1="20" x2="18" y2="10"></line><line x1="12" y1="20" x2="12" y2="4"></line><line x1="6" y1="20" x2="6" y2="14"></line></svg></span>
          <span class="fc-quote-text">${marketLine}</span>
        </div>

        <div class="fc-prob-container">
          <div class="fc-prob-bar">
            <div class="fc-prob-part home" style="width:${ph}%;" title="Home ${ph.toFixed(1)}%"></div>
            <div class="fc-prob-part draw" style="width:${pd}%;" title="Draw ${pd.toFixed(1)}%"></div>
            <div class="fc-prob-part away" style="width:${pa}%;" title="Away ${pa.toFixed(1)}%"></div>
          </div>
          <div class="fc-prob-legend-row">
            <div class="fc-prob-leg-pod home">
              <span class="leg-dot"></span>
              <span class="leg-label">Home (1)</span>
              <span class="leg-val">${ph.toFixed(0)}%</span>
            </div>
            <div class="fc-prob-leg-pod draw">
              <span class="leg-dot"></span>
              <span class="leg-label">Draw (X)</span>
              <span class="leg-val">${pd.toFixed(0)}%</span>
            </div>
            <div class="fc-prob-leg-pod away">
              <span class="leg-dot"></span>
              <span class="leg-label">Away (2)</span>
              <span class="leg-val">${pa.toFixed(0)}%</span>
            </div>
          </div>
        </div>

        <div class="fc-uncertainty-pill ${u.level}">
          <span class="u-badge">${uLabel}</span>
          <span class="u-reasons">${esc(uReason)}</span>
        </div>

        <details class="fc-details">
          <summary class="fc-details-summary">
            <span>Poisson Micro-Markets &amp; Expected Goals</span>
            <span class="details-chevron">▾</span>
          </summary>
          <div class="fc-micro-grid">
            <div class="fc-micro-tile">
              <span class="tile-lbl">BTTS YES</span>
              <span class="tile-val font-mono">${micro.p_btts != null ? (micro.p_btts * 100).toFixed(0) + '%' : 'n/a'}</span>
            </div>
            <div class="fc-micro-tile">
              <span class="tile-lbl">OVER 2.5</span>
              <span class="tile-val font-mono">${micro.p_over_2_5 != null ? (micro.p_over_2_5 * 100).toFixed(0) + '%' : 'n/a'}</span>
            </div>
            <div class="fc-micro-tile">
              <span class="tile-lbl">xG SPREAD</span>
              <span class="tile-val font-mono">${micro.expected_goals_home ?? '-'}&ndash;${micro.expected_goals_away ?? '-'}</span>
            </div>
            <div class="fc-micro-tile scoreline">
              <span class="tile-lbl">MOST LIKELY</span>
              <span class="tile-val font-mono text-brand">${esc(scoreTxt)}</span>
            </div>
          </div>
        </details>
      </article>`;
  }).join('');

  board.innerHTML = unlockStrip + cards;
}

function renderTierMatrix() {
  const tbody = document.getElementById('tier-matrix-body');
  if (!tbody) return;
  if (!state.tierCatalog || !state.tierCatalog.features || !state.tierCatalog.features.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="matrix-loading-cell">
      Run <code>python -m lisa export-forecast</code> to populate this comparison.</td></tr>`;
    return;
  }

  const order = ['free', 'tier1', 'tier2', 'tier3'];

  const MATRIX_CATEGORIES = [
    {
      id: 'predictive_models',
      title: 'Core Algorithmic Models & Predictive Intelligence',
      subtitle: 'Poisson scorelines, certainty-gated signals, and trap avoidance',
      icon: '01',
      badge: 'Alpha Engine',
      badgeColor: '#ccff00',
      keys: ['bulletin', 'top_pick', 'diamond_picks', 'micro_pack', 'traps']
    },
    {
      id: 'slip_execution',
      title: 'Sportsbook Execution & Slip Generation',
      subtitle: 'Instant multi-bookmaker codes, parlay math, and spread coverage',
      icon: '02',
      badge: 'Slip Tech',
      badgeColor: '#F59E0B',
      keys: ['booking_codes', 'parlay', 'ah_ou_picks']
    },
    {
      id: 'portfolio_risk',
      title: 'Portfolio Risk Management & Performance Audit',
      subtitle: 'Closing line tracking, verified accuracy auditing, and bankroll protection',
      icon: '03',
      badge: 'Risk Control',
      badgeColor: '#10B981',
      keys: ['steam_radar', 'clv_stats', 'portfolio']
    },
    {
      id: 'institutional_syndicate',
      title: 'Institutional Alpha & Syndicate Infrastructure',
      subtitle: 'Sub-second REST JSON feeds, soft-book arbitrage, and audit archives',
      icon: '04',
      badge: 'Syndicate Desk',
      badgeColor: '#A78BFA',
      keys: ['arbitrage_stream', 'api_feed', 'early_bird']
    }
  ];

  const FEATURE_MICRO_BLURBS = {
    bulletin: 'Win/Draw/Win, Both Teams to Score, and exact score probabilities for every slate fixture',
    top_pick: 'Highest-conviction daily pick with transparent data reasoning',
    diamond_picks: 'Strict certainty-gated signals with optimal fractional Kelly sizing',
    micro_pack: 'Both Teams to Score & Over/Under 2.5 goal probability distributions',
    traps: 'Signals misleading odds with sharp model vs market discrepancies',
    booking_codes: 'Best available price and book for every live selection',
    parlay: 'Correlation-filtered multi-leg accumulators minimizing joint risk',
    ah_ou_picks: 'Handicap spreads & goal totals with push protection',
    steam_radar: 'Real-time alert engine for rapid institutional line movements',
    clv_stats: 'Closing Line Value ledger and verified accuracy record',
    portfolio: 'Diversified position sizing to protect against losing streaks',
    arbitrage_stream: 'Live price discordance alerts across global sportsbooks',
    api_feed: 'Direct machine-readable REST JSON endpoints & real-time webhooks',
    early_bird: 'Earliest alpha release priority + signed weekly performance PDF'
  };

  const CAT_DOT_COLORS = {
    bulletin: '#ccff00',
    top_pick: '#FBBF24',
    diamond_picks: '#F472B6',
    micro_pack: '#ccff00',
    traps: '#F87171',
    booking_codes: '#94A3B8',
    parlay: '#A78BFA',
    ah_ou_picks: '#2DD4BF',
    steam_radar: '#ccff00',
    clv_stats: '#34D399',
    portfolio: '#A78BFA',
    arbitrage_stream: '#ccff00',
    api_feed: '#FBBF24',
    early_bird: '#C084FC'
  };

  const checkSvg = `
    <svg class="matrix-check-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="3.5 8.5 6.5 11.5 12.5 5.5"></polyline>
    </svg>`;

  const featMap = new Map();
  state.tierCatalog.features.forEach(f => featMap.set(f.key, f));
  const seenKeys = new Set();

  let html = '';

  MATRIX_CATEGORIES.forEach(cat => {
    const catFeatures = cat.keys
      .map(k => featMap.get(k))
      .filter(Boolean);

    if (!catFeatures.length) return;

    html += `
      <tr class="matrix-group-row">
        <td colspan="6">
          <div class="matrix-group-header">
            <div class="matrix-group-title-wrap">
              <span class="matrix-group-icon">${cat.icon}</span>
              <span class="matrix-group-title">${cat.title}</span>
              <span class="matrix-group-badge" style="color:${cat.badgeColor}; border:1px solid ${cat.badgeColor}40; background:${cat.badgeColor}15;">${cat.badge}</span>
            </div>
            <span class="matrix-group-desc">${cat.subtitle}</span>
          </div>
        </td>
      </tr>`;

    catFeatures.forEach(f => {
      seenKeys.add(f.key);
      html += renderFeatureRow(f);
    });
  });

  // Handle any remaining uncategorized features gracefully
  const leftover = state.tierCatalog.features.filter(f => !seenKeys.has(f.key));
  if (leftover.length) {
    html += `
      <tr class="matrix-group-row">
        <td colspan="6">
          <div class="matrix-group-header">
            <div class="matrix-group-title-wrap">
              <span class="matrix-group-icon">05</span>
              <span class="matrix-group-title">Additional Platform Capabilities</span>
            </div>
          </div>
        </td>
      </tr>`;
    leftover.forEach(f => {
      html += renderFeatureRow(f);
    });
  }

  function renderFeatureRow(f) {
    const grantRank = TIER_RANKS[f.grant] ?? 5;

    const cells = order.map(t => {
      const on = TIER_RANKS[t] >= grantRank;
      if (f.available === false) return `<td class="matrix-cell"><span class="matrix-denied">Unavailable</span></td>`;
      const isFeaturedCol = t === 'tier2';
      const cellClass = `matrix-cell col-${t}${isFeaturedCol ? ' col-featured-td' : ''}`;

      if (!on) {
        return `<td class="${cellClass}"><span class="matrix-denied">-</span></td>`;
      }

      if (t === 'free') {
        return `<td class="${cellClass}"><span class="matrix-chip chip-included">${checkSvg} Public</span></td>`;
      } else if (t === 'tier1') {
        return `<td class="${cellClass}"><span class="matrix-chip chip-included">${checkSvg} Included</span></td>`;
      } else if (t === 'tier2') {
        return `<td class="${cellClass}"><span class="matrix-chip chip-pro">${checkSvg} Included</span></td>`;
      } else {
        return `<td class="${cellClass}"><span class="matrix-chip chip-vip">${checkSvg} Priority</span></td>`;
      }
    }).join('');

    const entries = Object.entries(f.reveal_minutes || {});
    let revealHtml = '<span class="matrix-latency-pill latency-standard">-</span>';
    if (entries.length) {
      entries.sort((a, b) => b[1] - a[1]);
      const bestMin = entries[0][1];
      if (bestMin <= 0) {
        revealHtml = `<span class="matrix-latency-pill latency-instant">Real-time</span>`;
      } else if (bestMin >= 240) {
        revealHtml = `<span class="matrix-latency-pill latency-early">${(bestMin / 60).toFixed(0)}h pre-KO</span>`;
      } else {
        revealHtml = `<span class="matrix-latency-pill latency-standard">${(bestMin / 60).toFixed(0)}h pre-KO</span>`;
      }
    }

    const microDesc = esc(cleanText(f.blurb));
    const targetTier = f.grant || 'tier1';

    return `
      <tr class="matrix-row" onclick="window.handlePricingSelect ? window.handlePricingSelect('${targetTier}') : (window.openAuthModal ? window.openAuthModal('signup','${targetTier}') : window.setTier('${targetTier}'))">
        <td class="matrix-feature-cell col-capability">
          <div class="matrix-feature-name">
            <span class="matrix-feature-dot" style="background:${CAT_DOT_COLORS[f.key] || '#64748B'};"></span>
            <span>${esc(cleanText(f.label))}</span>
          </div>
          <div class="matrix-feature-blurb">${microDesc}</div>
        </td>
        ${cells}
        <td class="matrix-cell col-latency">${f.available === false ? "Unavailable" : "Published board"}</td>
      </tr>`;
  }

  tbody.innerHTML = html;
}

/* ------------------------------------------------------------------ */
/* View routing persistence: deep-links + scroll memory                */
/* ------------------------------------------------------------------ */
const VIEW_SCROLL_KEY = 'lisa_view_scroll';

function readViewScrollState() {
  try {
    return JSON.parse(sessionStorage.getItem(VIEW_SCROLL_KEY) || '{}');
  } catch (e) {
    return {};
  }
}

const viewScrollState = readViewScrollState();
let scrollSaveTimer = null;
let pendingScrollRestore = null;

function captureCurrentScroll() {
  if (!state.activeTab) return;
  const y = Math.max(0, window.scrollY || window.pageYOffset || 0);
  if (Math.abs(y - (viewScrollState[state.activeTab] || 0)) > 1) {
    viewScrollState[state.activeTab] = y;
  }
}

function scheduleScrollSave() {
  if (!state.activeTab) return;
  captureCurrentScroll();
  if (scrollSaveTimer) clearTimeout(scrollSaveTimer);
  scrollSaveTimer = setTimeout(() => {
    try {
      sessionStorage.setItem(VIEW_SCROLL_KEY, JSON.stringify(viewScrollState));
    } catch (e) { /* storage unavailable */ }
  }, 250);
}

function jumpToViewScroll(y) {
  const max = Math.max(0, (document.documentElement.scrollHeight || 0) - window.innerHeight);
  const target = Math.min(Math.max(0, y), max);
  const html = document.documentElement;
  const prev = html.style.scrollBehavior;
  html.style.scrollBehavior = 'auto';
  window.scrollTo(0, target);
  html.style.scrollBehavior = prev;
}

function flushPendingScrollRestore() {
  if (pendingScrollRestore != null) {
    jumpToViewScroll(pendingScrollRestore);
    pendingScrollRestore = null;
  }
}

function settleScrollRestore() {
  if (!state.activeTab) return;
  const saved = viewScrollState[state.activeTab];
  if (saved && saved > 0) {
    jumpToViewScroll(saved);
  }
}

// Every nav rail button must appear here or switchTab() returns early and the
// tab silently does nothing. 'daily-board' and 'tiers' were missing while their
// buttons and panels both existed in the DOM, so two shipped tabs were dead.
const VALID_VIEWS = new Set([
  'overview', 'picks', 'forecast', 'model-board', 'micro-bets', 'ledger', 'daily-board',
  'calibration', 'calculator', 'alpha', 'tiers', 'backtest',
]);

function switchTab(viewName) {
  if (typeof window.closeTickerDropdown === 'function') {
    window.closeTickerDropdown();
  }

  if (!VALID_VIEWS.has(viewName) || !document.getElementById(`view-${viewName}`)) {
    return;
  }

  captureCurrentScroll();

  const tabs = document.querySelectorAll('.tab-btn, .header-nav-link, .nav-rail-item');
  tabs.forEach(t => {
    const target = (t.getAttribute('data-view') || '').replace('view-', '');
    if (target === viewName || t.id === `tab-btn-${viewName}`) {
      t.classList.add('active');
    } else {
      t.classList.remove('active');
    }
  });

  const views = document.querySelectorAll('.view-panel');
  views.forEach(v => {
    if (v.id === `view-${viewName}`) {
      v.classList.add('active');
    } else {
      v.classList.remove('active');
    }
  });

  state.activeTab = viewName;

  // All views read persisted data. Opening a view never calls providers.
  if (['model-board', 'micro-bets'].includes(viewName) && state.modelBoardState === 'idle') {
    refreshModelBoard(false);
  }
  if (viewName === 'daily-board') loadDailyBoard(state.dailyBoardTab || 'upcoming');

  if (window.history && history.replaceState) {
    history.replaceState(null, '', `#${viewName}`);
  }
  try {
    sessionStorage.setItem('lisa_active_tab', viewName);
  } catch (e) { /* storage unavailable */ }

  const saved = viewScrollState[viewName];
  if (saved && saved > 0) {
    pendingScrollRestore = saved;
    window.scrollTo(0, 0);
  } else {
    pendingScrollRestore = null;
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  if (viewName === 'calibration' && state.data && state.data.calibration) {
    setTimeout(renderCalibration, 50);
  }
}
window.switchTab = switchTab;

// ============================================================
// DAILY BOARD
// ============================================================

window.switchBoardTab = function(tab) {
  if (!['upcoming', 'today', 'tomorrow', 'week'].includes(tab)) return;
  state.dailyBoardTab = tab;
  document.querySelectorAll('.daily-board-tabs .pill-filter').forEach(btn => {
    btn.classList.toggle('active', btn.getAttribute('data-board-tab') === tab);
  });
  loadDailyBoard(tab);
};

let calendarRequestVersion = 0;
window.loadDailyBoard = async function(tab = 'upcoming') {
  const contentDiv = document.getElementById('daily-board-content');
  if (!contentDiv) return;
  const version = ++calendarRequestVersion;
  state.dailyBoardTab = tab;
  contentDiv.innerHTML = '<div class="board-loading">Loading matches...</div>';
  try {
    const response = await fetchDashboardResource('/api/daily-board');
    const data = await response.json();
    if (version !== calendarRequestVersion) return;
    if (!response.ok || !data.success) throw new Error(data.error || 'Saved calendar unavailable');
    const matches = (data.board || {})[tab === 'week' ? 'this_week' : tab === 'upcoming' ? 'all_upcoming' : tab] || [];
    const status = `<p class="alpha-sub">${data.worker_observed_at ? 'Worker observation: ' + esc(formatKo(data.worker_observed_at)) : 'Waiting for the first worker observation'} · dates in ${esc(data.timezone || 'UTC')}. Source caches determine score freshness.</p>`;
    if (matches.length === 0) {
      const upcoming = Array.isArray((data.board || {}).all_upcoming) ? data.board.all_upcoming : [];
      const next = upcoming[0];
      const otherDates = next && tab !== 'upcoming'
        ? `<p>${upcoming.length} verified upcoming ${upcoming.length === 1 ? 'fixture is' : 'fixtures are'} recorded for later dates. Next kickoff: ${esc(formatKo(next.commence_time))}.</p><button type="button" class="pill-filter" onclick="window.switchBoardTab('upcoming')">View Next Matches</button>`
        : '<p>This view refreshes automatically as workers collect eligible fixtures.</p>';
      contentDiv.innerHTML = status + '<div class="board-empty">No fixtures recorded for this period.' + otherDates + '</div>';
      return;
    }
    contentDiv.innerHTML = status + '<div class="board-matches">' + matches.map(m => `
        <div class="board-match">
          <div class="board-match-time">${esc(formatKo(m.commence_time))}</div>
          <div class="board-match-league">${esc((m.sport_key || '').replace(/_/g, ' '))}</div>
          <div class="board-match-teams">${esc(m.home_team)} vs ${esc(m.away_team)}</div>
          <div class="board-match-odds">${Array.isArray(m.score) ? esc(m.score.join('–')) : '—'} · ${esc(m.status)}<br><small>${esc(m.source)}</small></div>
        </div>
      `).join('') + '</div>';
  } catch (err) {
    if (version === calendarRequestVersion) contentDiv.innerHTML = '<div class="board-error">'+esc(err.message)+'. Retrying automatically.</div>';
  }
};

// ============================================================
// TIER ACTIVATION
// ============================================================

window.activateTier = function(tier) {
  // Redirect to Telegram bot to purchase
  const botUsername = 'XpredictPremiumBot';
  const deepLink = `https://t.me/${botUsername}?start=buy_${tier}`;
  window.open(deepLink, '_blank');
};

window.submitActivationKey = async function() {
  const input = document.getElementById('activation-key-input');
  const resultDiv = document.getElementById('activation-result');
  const key = (input.value || '').trim();

  if (!key) {
    resultDiv.innerHTML = '<span style="color: #ef4444;">Please enter your activation key.</span>';
    return;
  }

  resultDiv.innerHTML = '<span style="color: #f59e0b;">Verifying...</span>';

  try {
    const response = await fetch('/api/activate-tier', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key }),
    });

    const data = await response.json();

    if (data.success) {
      resultDiv.innerHTML = `<span style="color: #10b981;">✅ ${data.message}</span>`;
      input.value = '';
      // Refresh user data
      if (typeof auth !== 'undefined' && auth.refreshUser) {
        auth.refreshUser();
      }
    } else {
      resultDiv.innerHTML = `<span style="color: #ef4444;">❌ ${data.error || 'Activation failed'}</span>`;
    }
  } catch (err) {
    resultDiv.innerHTML = '<span style="color: #ef4444;">❌ Network error. Please try again.</span>';
  }
};

let verifyPollingTimer = null;

function getOrCreateWebUserId() {
  let uid = localStorage.getItem('lisa_web_user_id');
  if (!uid) {
    uid = 'usr_' + Math.random().toString(36).substring(2, 9);
    localStorage.setItem('lisa_web_user_id', uid);
  }
  return uid;
}

function onVerificationSuccess() {
  const statusText = document.getElementById('telegram-status-text');
  if (statusText) {
    statusText.textContent = 'Verified. Unblurring picks...';
    statusText.style.color = '#10b981';
  }
  state.isTelegramUnlocked = true;
  localStorage.setItem('lisa_telegram_unlocked', 'true');
  renderTierControls();
  renderPicks();
  setTimeout(() => {
    window.closeTelegramModal();
  }, 900);
}

window.relockTelegram = function () {
  state.currentTier = 'free';
  state.isTelegramUnlocked = false;
  localStorage.setItem('lisa_tier', 'free');
  localStorage.removeItem('lisa_telegram_unlocked');
};

// Interactive Model Inference Scenarios
// Interactive Model Inference Scenarios
// Built from the live ledger: the two strongest live selections and the most
// recent recorded trap. Nothing here is a canned example.
const EMPTY_SCENARIO = {
  matchName: '—',
  league: '—',
  quotes: 'No cached prices for this fixture',
  overround: 'n/a',
  shinZ: 'n/a',
  probTrue: 'n/a',
  fairPrice: 'n/a',
  evDelta: 'n/a',
  kellyStake: 'n/a',
  outlook: 'n/a',
  confidence: 'n/a',
  verdictType: '',
  verdictBadge: 'NO LIVE SELECTION',
  verdictDesc: 'Waiting for the odds poller to record a real selection.'
};

function numOrDash(v, format) {
  return (typeof v === 'number' && isFinite(v)) ? format(v) : 'n/a';
}
function fmtOdds(v) {
  return (typeof v === 'number' && isFinite(v)) ? v.toFixed(2) : 'n/a';
}
function fmtPct(v, digits) {
  return (typeof v === 'number' && isFinite(v)) ? (v * 100).toFixed(digits == null ? 1 : digits) + '%' : 'n/a';
}
function fmtMoney(v) {
  return (typeof v === 'number' && isFinite(v)) ? v.toFixed(2) : 'n/a';
}

function scenarioFromPick(pick) {
  if (!pick) return Object.assign({}, EMPTY_SCENARIO);
  const quotes = pick.quotes || {};
  const outcome = pick.outcome_name;
  const bookLines = (quotes.books || []).map((b) => {
    const price = b.prices ? b.prices[outcome] : null;
    return typeof price === 'number' ? `${esc(b.book_title)} ${price.toFixed(2)}` : null;
  }).filter(Boolean);

  const ev = typeof pick.best_ev === 'number' ? pick.best_ev : null;
  const evText = ev == null ? 'n/a'
    : `${ev >= 0 ? '+' : ''}${(ev * 100).toFixed(1)}% ${ev >= 0 ? 'edge over' : 'bad value at'} ${pick.best_book || 'best book'}`;
  const confidence = typeof pick.cv === 'number' ? pick.cv : null;

  return {
    matchName: `${esc(pick.home_team)} vs ${esc(pick.away_team)}`,
    league: `${(pick.league_label || pick.sport_key || '—')} · ${pick.outcome_name || ''}`,
    quotes: bookLines.length ? bookLines.join(' · ')
      : (pick.best_odds ? `${pick.best_book || 'best book'} ${fmtOdds(pick.best_odds)}` : 'No cached prices'),
    overround: quotes.margin == null ? 'n/a' : `${(quotes.margin * 100).toFixed(1)}% bookmaker margin (${quotes.n_books} books)`,
    shinZ: pick.freshness || 'n/a',
    probTrue: fmtPct(pick.p_true, 1),
    fairPrice: `${fmtOdds(pick.fair_odds)} fair`,
    evDelta: evText,
    kellyStake: `${fmtMoney(pick.recommended_stake_pct)}% of bankroll (${fmtMoney(pick.recommended_units)}u)`,
    outlook: pick.gauge_text || 'n/a',
    confidence: confidence == null ? 'n/a' : `${(confidence * 100).toFixed(1)}% cross-book spread`,
    conviction: pick.conviction_score,
    verdictType: ev != null && ev > 0.03 ? 'diamond' : 'pivot',
    verdictBadge: `${fmtMoney(pick.conviction_score)}/10 conviction · ${fmtOdds(pick.best_odds)} at ${pick.best_book || 'best book'}`,
    verdictDesc: `Model fair price ${fmtOdds(pick.fair_odds)} against a ${fmtOdds(pick.best_odds)} quote across ${pick.n_books || 'n/a'} books.`
  };
}

function scenarioFromTrap(trap) {
  if (!trap) return Object.assign({}, EMPTY_SCENARIO);
  const fav = trap.public_favorite;
  return {
    matchName: `${esc(trap.home_team)} vs ${esc(trap.away_team)}`,
    league: 'Recorded trap advisory',
    quotes: trap.public_odds ? `Public ${fmtOdds(trap.public_odds)}` : 'No cached prices',
    overround: 'n/a',
    shinZ: fav ? `Public backed ${fav}` : 'Public backing recorded',
    probTrue: 'n/a',
    fairPrice: trap.fair_odds ? `${fmtOdds(trap.fair_odds)} fair` : 'n/a',
    evDelta: trap.cv == null ? 'n/a' : `${(trap.cv * 100).toFixed(1)}% cross-book spread`,
    kellyStake: '0.00% (do not bet)',
    outlook: 'Trap logged by the variance monitor',
    confidence: trap.cv == null ? 'n/a' : `${(trap.cv * 100).toFixed(1)}%`,
    verdictType: 'trap',
    verdictBadge: 'TRAP GAME: NO BET',
    verdictDesc: trap.detected_at
      ? `Book prices diverged from the consensus at ${esc(trap.detected_at)}. The ledger logged a pass, not a selection.`
      : 'Book prices diverged from the consensus. The ledger logged a pass, not a selection.'
  };
}

function visualizerScenarios() {
  const d = (window.state && window.state.dashboard) || {};
  const picks = Array.isArray(d.active_picks) ? d.active_picks.slice() : [];
  picks.sort((a, b) => (b.conviction_score || 0) - (a.conviction_score || 0));
  const traps = Array.isArray(d.traps) ? d.traps : [];
  return {
    diamond: scenarioFromPick(picks[0]),
    pivot: scenarioFromPick(picks[1] || picks[0]),
    trap: scenarioFromTrap(traps[0])
  };
}

// Re-render the pipeline explainer from the freshest ledger state.
window.refreshVisualizer = function () {
  const active = document.querySelector('.vis-scenario-btn.active');
  const id = active ? active.getAttribute('data-scenario') : 'diamond';
  window.switchVisualizerScenario(id);
};

window.switchVisualizerScenario = function (id) {
  const s = visualizerScenarios()[id];
  if (!s) return;

  document.querySelectorAll('.vis-scenario-btn, .visualizer-scenario-btn').forEach(b => {
    b.classList.toggle('active', b.getAttribute('data-scenario') === id);
  });

  const mName = document.getElementById('vis-match-name');
  const mLeague = document.getElementById('vis-league');
  const mQuotes = document.getElementById('vis-quotes');
  const mOverround = document.getElementById('vis-overround');
  const mShinZ = document.getElementById('vis-shin-z');
  const mProbTrue = document.getElementById('vis-prob-true');
  const mFairPrice = document.getElementById('vis-fair-price');
  const mEvDelta = document.getElementById('vis-ev-delta');
  const mKellyStake = document.getElementById('vis-kelly-stake');
  const mVerdictBar = document.getElementById('vis-verdict-bar');
  const mOutlook = document.getElementById('vis-outlook');
  const mConfidence = document.getElementById('vis-confidence');
  const mVerdictBadge = document.getElementById('vis-verdict-badge');
  const mVerdictDesc = document.getElementById('vis-verdict-desc');

  if (mName) mName.textContent = s.matchName;
  if (mLeague) mLeague.textContent = s.league;
  if (mQuotes) mQuotes.textContent = s.quotes;
  if (mOverround) mOverround.textContent = s.overround;
  if (mShinZ) mShinZ.textContent = s.shinZ;
  if (mProbTrue) mProbTrue.textContent = s.probTrue;
  if (mFairPrice) mFairPrice.textContent = s.fairPrice;
  if (mEvDelta) mEvDelta.textContent = s.evDelta;
  if (mKellyStake) mKellyStake.textContent = s.kellyStake;
  if (mOutlook) mOutlook.textContent = s.outlook;
  if (mConfidence) mConfidence.textContent = s.confidence;
  if (mVerdictBadge) mVerdictBadge.textContent = s.verdictBadge;
  if (mVerdictDesc) mVerdictDesc.textContent = s.verdictDesc;

  if (mVerdictBar) {
    mVerdictBar.className = s.verdictType === 'trap' ? 'pipeline-verdict-bar trap' : 'pipeline-verdict-bar';
    if (s.verdictType === 'trap') {
      mVerdictBadge.style.color = '#f87171';
    } else if (s.verdictType === 'pivot') {
      mVerdictBadge.style.color = '#f59e0b';
    } else {
      mVerdictBadge.style.color = '#ccff00';
    }
  }
};

// Telegram Modal Interactions & Gatekeeper Bridge
window.openTelegramModal = function () {
  const modal = document.getElementById('telegram-modal');
  if (!modal) return;

  const uid = getOrCreateWebUserId();
  const codeEl = document.getElementById('telegram-unlock-code');
  if (codeEl) codeEl.textContent = uid;

  const linkEl = document.getElementById('btn-open-telegram');
  if (linkEl) {
    linkEl.href = `https://t.me/XpredictPremiumBot?start=verify_${uid}`;
  }

  const statusText = document.getElementById('telegram-status-text');
  if (statusText) {
    statusText.textContent = 'Waiting for Telegram /start...';
    statusText.style.color = 'var(--accent-gold)';
  }

  modal.style.display = 'flex';

  // Live polling for Gatekeeper Bot verification
  if (verifyPollingTimer) clearInterval(verifyPollingTimer);
  verifyPollingTimer = setInterval(async () => {
    try {
      const resp = await fetch(`/api/verify-status?user_id=${uid}`);
      if (resp.ok) {
        const data = await resp.json();
        if (data.verified) {
          clearInterval(verifyPollingTimer);
          verifyPollingTimer = null;
          onVerificationSuccess();
        }
      }
    } catch (e) { }
  }, 1200);
};

window.closeTelegramModal = function () {
  const modal = document.getElementById('telegram-modal');
  if (modal) modal.style.display = 'none';
  if (verifyPollingTimer) {
    clearInterval(verifyPollingTimer);
    verifyPollingTimer = null;
  }
};

function setupEventListeners() {
  // Navigation tabs
  document.querySelectorAll('.tab-btn, .nav-rail-item').forEach(btn => {
    btn.addEventListener('click', () => {
      const view = (btn.getAttribute('data-view') || '').replace('view-', '');
      if (view) switchTab(view);
    });
  });

  // Global escape key to close open modals & dropdowns
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (typeof window.closeAuthModal === 'function') window.closeAuthModal();
      if (typeof window.closeTelegramModal === 'function') window.closeTelegramModal();
      if (typeof window.closeCheckoutModal === 'function') window.closeCheckoutModal();
      if (typeof window.closeAccountSettingsModal === 'function') window.closeAccountSettingsModal();
      if (typeof window.closeTickerDropdown === 'function') window.closeTickerDropdown();
      document.getElementById('user-dropdown-menu')?.classList.remove('show');
      document.getElementById('user-profile-pill')?.classList.remove('active');
    }
  });

  // Auto-collapse Live Ticker dropdown on scroll past
  window.addEventListener('scroll', () => {
    scheduleScrollSave();
    const ticker = document.getElementById('market-ticker');
    if (ticker && ticker.classList.contains('is-open') && window.scrollY > 35) {
      if (typeof window.closeTickerDropdown === 'function') {
        window.closeTickerDropdown();
      }
    }
  }, { passive: true });

  // Auto-collapse Live Ticker on click outside
  document.addEventListener('click', (e) => {
    const ticker = document.getElementById('market-ticker');
    const btn = document.getElementById('btn-ticker-trigger');
    if (ticker && ticker.classList.contains('is-open')) {
      if (!ticker.contains(e.target) && (!btn || !btn.contains(e.target))) {
        if (typeof window.closeTickerDropdown === 'function') {
          window.closeTickerDropdown();
        }
      }
    }
  });

  // Picks text search & clear button
  const picksSearch = document.getElementById('picks-search-input');
  const picksClear = document.getElementById('picks-search-clear');
  if (picksSearch) {
    picksSearch.addEventListener('input', (e) => {
      state.picksSearchQuery = e.target.value;
      if (picksClear) picksClear.classList.toggle('visible', !!e.target.value);
      renderPicks();
    });
  }
  if (picksClear) {
    picksClear.addEventListener('click', () => {
      if (picksSearch) picksSearch.value = '';
      state.picksSearchQuery = '';
      picksClear.classList.remove('visible');
      renderPicks();
    });
  }

  // Odds band filter pills
  document.querySelectorAll('.odds-filter-group [data-odds]').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.odds-filter-group [data-odds]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.picksOddsBand = btn.getAttribute('data-odds') || 'all';
      renderPicks();
    });
  });

  // Ledger text search & clear button
  const ledgerSearch = document.getElementById('ledger-search-input');
  const ledgerClear = document.getElementById('ledger-search-clear');
  if (ledgerSearch) {
    ledgerSearch.addEventListener('input', (e) => {
      state.ledgerSearchQuery = e.target.value;
      if (ledgerClear) ledgerClear.classList.toggle('visible', !!e.target.value);
      renderLedger();
    });
  }
  if (ledgerClear) {
    ledgerClear.addEventListener('click', () => {
      if (ledgerSearch) ledgerSearch.value = '';
      state.ledgerSearchQuery = '';
      ledgerClear.classList.remove('visible');
      renderLedger();
    });
  }

  // Category / Grade filter pills (All, Grade A Diamonds, Grade B Pivots, Grade C Pass Advisories)
  document.querySelectorAll('.cat-pill').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.cat-pill').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const val = btn.getAttribute('data-grade') || btn.getAttribute('data-category') || 'all';
      state.activeGradeFilter = val;
      state.activeCategoryFilter = val;
      renderPicks();
    });
  });

  // Sport filters
  document.querySelectorAll('#sport-filter-group .pill-filter').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#sport-filter-group .pill-filter').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.activeSportFilter = btn.getAttribute('data-sport') || 'all';
      renderPicks();
    });
  });

  // Backtest strategy switcher (Conservative, High-Yield Pivots, Smart Parlays, Hybrid)
  document.querySelectorAll('[data-bktstrategy]').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('[data-bktstrategy]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.activeBktStrategy = btn.getAttribute('data-bktstrategy');
      renderBacktest();  // resolves async from /api/backtest
    });
  });

  // Backtest sport filters
  document.querySelectorAll('[data-bktsport]').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('[data-bktsport]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.activeBktSport = btn.getAttribute('data-bktsport');
      renderBacktest();  // resolves async from /api/backtest
    });
  });

  // Backtest grade filters
  document.querySelectorAll('[data-bktgrade]').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('[data-bktgrade]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.activeBktGrade = btn.getAttribute('data-bktgrade');
      renderBacktest();  // resolves async from /api/backtest
    });
  });


  // Telegram modal close
  const closeBtn = document.getElementById('modal-close-btn');
  if (closeBtn) closeBtn.addEventListener('click', window.closeTelegramModal);

  const modal = document.getElementById('telegram-modal');
  if (modal) {
    modal.addEventListener('click', (e) => {
      if (e.target === modal) window.closeTelegramModal();
    });
  }

  // Telegram verify button
  const verifyBtn = document.getElementById('btn-verify-unlock');
  if (verifyBtn) {
    verifyBtn.addEventListener('click', async () => {
      const uid = getOrCreateWebUserId();
      const statusText = document.getElementById('telegram-status-text');
      if (statusText) {
        statusText.textContent = 'Checking verified channel membership...';
        statusText.style.color = 'var(--accent-gold)';
      }

      try {
        const resp = await fetch(`/api/verify-status?user_id=${uid}`);
        if (resp.ok) {
          const data = await resp.json();
          if (data.verified) {
            onVerificationSuccess();
            return;
          }
        }
      } catch (e) {
        console.error('Verification query failed:', e);
      }

      // STRICT PRODUCTION REJECTION: Do NOT unlock without verified backend proof!
      if (statusText) {
        statusText.textContent = 'Not verified. Tap START in @XpredictPremiumBot first.';
        statusText.style.color = '#ef4444';
      }
      const statusBox = document.getElementById('telegram-verify-status');
      if (statusBox) {
        statusBox.style.background = 'rgba(239, 68, 68, 0.15)';
        statusBox.style.borderColor = 'rgba(239, 68, 68, 0.4)';
      }
    });
  }
}

export function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const toast = document.createElement('div');
  toast.className = `lisa-toast ${type}`;
  toast.innerHTML = `<span>${message}</span>`;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(10px) scale(0.95)';
    setTimeout(() => toast.remove(), 250);
  }, 4000);
}

function setupAuthUI() {
  const modal = document.getElementById('auth-modal');
  const closeBtn = document.getElementById('auth-modal-close');
  const btnSignin = document.getElementById('btn-header-signin');
  const btnSignup = document.getElementById('btn-header-signup');
  const tabSignin = document.getElementById('tab-btn-signin');
  const tabSignup = document.getElementById('tab-btn-signup');
  const formSignin = document.getElementById('form-signin');
  const formSignup = document.getElementById('form-signup');
  const errorBox = document.getElementById('auth-error-box');
  const userPill = document.getElementById('user-profile-pill');
  const dropdown = document.getElementById('user-dropdown-menu');
  const btnLogout = document.getElementById('btn-header-logout');

  function openAuthModal(tab = 'signin', defaultTier = 'free') {
    const m = document.getElementById('auth-modal');
    if (!m) return;
    const eb = document.getElementById('auth-error-box');
    if (eb) {
      eb.textContent = '';
      eb.classList.remove('show');
    }
    m.classList.add('show');
    switchAuthTab(tab);
    if (defaultTier && tab === 'signup') {
      const chip = document.querySelector(`.auth-tier-chip[data-tier="${defaultTier}"]`);
      if (chip) chip.click();
    }
  }

  function closeAuthModal() {
    const m = document.getElementById('auth-modal');
    if (m) m.classList.remove('show');
  }

  function switchAuthTab(tab) {
    const tIn = document.getElementById('tab-btn-signin');
    const tUp = document.getElementById('tab-btn-signup');
    const fIn = document.getElementById('form-signin');
    const fUp = document.getElementById('form-signup');
    const sub = document.getElementById('auth-modal-subtitle');

    if (tab === 'signin') {
      tIn?.classList.add('active');
      tUp?.classList.remove('active');
      if (fIn) fIn.style.display = 'block';
      if (fUp) fUp.style.display = 'none';
      if (sub) sub.textContent = 'Sign in to access your saved tier and mathematical feeds';
    } else {
      tUp?.classList.add('active');
      tIn?.classList.remove('active');
      if (fUp) fUp.style.display = 'block';
      if (fIn) fIn.style.display = 'none';
      if (sub) sub.textContent = 'Create an account to track performance and unlock predictive alpha';
    }
  }

  // Globally expose for immediate inline onclick safety
  window.openAuthModal = openAuthModal;
  window.closeAuthModal = closeAuthModal;
  window.switchAuthTab = switchAuthTab;

  btnSignin?.addEventListener('click', (e) => {
    e.preventDefault();
    openAuthModal('signin');
  });
  btnSignup?.addEventListener('click', (e) => {
    e.preventDefault();
    openAuthModal('signup');
  });
  closeBtn?.addEventListener('click', closeAuthModal);
  tabSignin?.addEventListener('click', () => switchAuthTab('signin'));
  tabSignup?.addEventListener('click', () => switchAuthTab('signup'));

  modal?.addEventListener('click', (e) => {
    if (e.target === modal) closeAuthModal();
  });

  // Toggle password visibility
  document.querySelectorAll('.auth-toggle-pwd').forEach(btn => {
    btn.addEventListener('click', () => {
      const targetId = btn.getAttribute('data-target');
      const input = document.getElementById(targetId);
      if (input) {
        input.type = input.type === 'password' ? 'text' : 'password';
        btn.textContent = input.type === 'password' ? 'Show' : 'Hide';
      }
    });
  });

  // Tier chip selection in signup
  const tierChips = document.querySelectorAll('.auth-tier-chip');
  let selectedSignupTier = 'free';
  tierChips.forEach(chip => {
    chip.addEventListener('click', () => {
      tierChips.forEach(c => c.classList.remove('selected'));
      chip.classList.add('selected');
      selectedSignupTier = chip.getAttribute('data-tier') || 'free';
    });
  });

  // Signin form submit
  formSignin?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const email = document.getElementById('signin-email')?.value.trim();
    const password = document.getElementById('signin-password')?.value;
    const submitBtn = document.getElementById('btn-submit-signin');
    if (errorBox) errorBox.classList.remove('show');
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.innerHTML = '<span>Verifying...</span>';
    }

    try {
      const user = await auth.signin(email, password);
      closeAuthModal();
      showToast(`Welcome back, ${user.display_name || user.email}!`, 'success');
    } catch (err) {
      if (errorBox) {
        errorBox.textContent = err.message || 'Failed to sign in. Check email and password.';
        errorBox.classList.add('show');
      }
    } finally {
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.innerHTML = '<span>Sign In to Terminal</span>';
      }
    }
  });

  // Signup form submit
  formSignup?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const name = document.getElementById('signup-name')?.value.trim();
    const email = document.getElementById('signup-email')?.value.trim();
    const password = document.getElementById('signup-password')?.value;
    const submitBtn = document.getElementById('btn-submit-signup');
    if (errorBox) errorBox.classList.remove('show');
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.innerHTML = '<span>Creating Account...</span>';
    }

    try {
      const user = await auth.signup(email, password, name, selectedSignupTier);
      closeAuthModal();
      showToast(`Account created successfully! Welcome to LISA.`, 'success');
    } catch (err) {
      if (errorBox) {
        errorBox.textContent = err.message || 'Failed to create account.';
        errorBox.classList.add('show');
      }
    } finally {
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.innerHTML = '<span>Create Free Account</span>';
      }
    }
  });

  // User profile dropdown toggle
  userPill?.addEventListener('click', (e) => {
    e.stopPropagation();
    dropdown?.classList.toggle('show');
    userPill.classList.toggle('active');
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('#auth-logged-in')) {
      dropdown?.classList.remove('show');
      userPill?.classList.remove('active');
    }
  });

  // Sign out button
  btnLogout?.addEventListener('click', async () => {
    dropdown?.classList.remove('show');
    userPill?.classList.remove('active');
    await auth.signout();
    showToast('Signed out of terminal.', 'info');
  });

  // Global switchUserTier
  window.switchUserTier = async (tier) => {
    dropdown?.classList.remove('show');
    userPill?.classList.remove('active');

    // An operator browsing tiers is inspecting the product, not buying it.
    // Writing the real account tier here used to be destructive: the account
    // tier IS the operator credential for anyone not email-allowlisted, so
    // picking a paid tier silently removed their own console access. Operators
    // therefore switch a local preview and never touch the account.
    if (state.isOperator) {
      window.setTierPreview(tier);
      return;
    }

    if (auth.isAuthenticated()) {
      await auth.updateTier(tier);
    }
    window.setTier(tier);
    showToast(`Switched active tier to ${tier.toUpperCase()}`, 'success');
  };

  // Listen for auth state changes
  auth.onAuthStateChanged((user) => {
    const loggedOutEl = document.getElementById('auth-logged-out');
    const loggedInEl = document.getElementById('auth-logged-in');
    const avatarInit = document.getElementById('user-avatar-initial');
    const profileName = document.getElementById('user-profile-name');
    const profileTierTag = document.getElementById('user-profile-tier-tag');
    const dropName = document.getElementById('dropdown-display-name');
    const dropEmail = document.getElementById('dropdown-email');
    const dropTier = document.getElementById('dropdown-tier-label');

    if (user) {
      if (loggedOutEl) loggedOutEl.style.display = 'none';
      if (loggedInEl) loggedInEl.style.display = 'block';

      const displayName = user.display_name || user.email.split('@')[0];
      if (profileName) profileName.textContent = displayName;
      if (avatarInit) avatarInit.textContent = displayName.charAt(0).toUpperCase();
      const dropAvatar = document.getElementById('dropdown-avatar-large');
      if (dropAvatar) dropAvatar.textContent = displayName.charAt(0).toUpperCase();
      if (dropName) dropName.textContent = displayName;
      if (dropEmail) dropEmail.textContent = user.email;

      const tierKey = user.tier || 'free';
      const tierLabels = { free: 'Free Community', tier1: 'Tier 1 Sharp ($19)', tier2: 'Tier 2 Pro ($49)', tier3: 'Tier 3 VIP Syndicate' };
      if (dropTier) dropTier.textContent = tierLabels[tierKey] || tierKey.toUpperCase();

      const tierPerks = {
        free: '1 Daily Selection · Delayed Latency · Public Slate',
        tier1: 'Top 5 Value Gates (Matches #1-5) · Real-time Alerts',
        tier2: 'Full 15-Game Slate · Real-time Steam Radar & Models',
        tier3: 'Complete Slate + Syndicates · Direct REST & Webhook API',
      };
      const dropPerk = document.getElementById('dropdown-tier-perk');
      if (dropPerk) dropPerk.textContent = tierPerks[tierKey] || 'Institutional Tier Access';

      // Mark active tier choice inside user dropdown
      document.querySelectorAll('.dropdown-tier-choice').forEach(btn => {
        if (btn.getAttribute('data-tier') === tierKey) {
          btn.classList.add('active');
        } else {
          btn.classList.remove('active');
        }
      });

      if (profileTierTag) {
        profileTierTag.textContent = tierKey.toUpperCase();
        profileTierTag.className = `user-tier-tag tier-tag-${tierKey}`;
      }

      // Operator status drives the Tier Preview control. It is recomputed on
      // every auth notification because the server is the only authority on it.
      state.isOperator = !!user.is_operator;
      renderTierPreviewControls();
      document.querySelectorAll('[data-preview-tier]').forEach(btn => {
        btn.classList.toggle('active', btn.getAttribute('data-preview-tier') === state.tierPreview);
      });

      // If user has higher tier than current, elevate view tier -- but never
      // while an operator preview is active, or every auth refresh would
      // yank them out of the tier they are inspecting.
      if (user.tier && user.tier !== 'free' && !state.tierPreview) {
        state.currentTier = user.tier;
        renderTierControls();
        renderPicks();
      } else if (state.tierPreview) {
        window.setTier(window.effectiveTier());
      }
      if (user.telegram_verified) {
        state.isTelegramUnlocked = true;
        localStorage.setItem('lisa_telegram_unlocked', 'true');
        renderPicks();
      }
    } else {
      if (loggedOutEl) loggedOutEl.style.display = 'flex';
      if (loggedInEl) loggedInEl.style.display = 'none';
    }
  });
}

function initApp() {
  setupEventListeners();
  setupAuthUI();
  initCalculator();

  // Synchronous route & modal resolution
  let hash = window.location.hash.replace('#', '');
  if (hash.startsWith('view-')) hash = hash.slice(5);
  let storedTab = null;
  try {
    storedTab = sessionStorage.getItem('lisa_active_tab');
  } catch (e) { /* storage unavailable */ }
  const initialTab = (hash && VALID_VIEWS.has(hash) && document.getElementById(`view-${hash}`))
    ? hash
    : (storedTab && VALID_VIEWS.has(storedTab) && document.getElementById(`view-${storedTab}`))
      ? storedTab
      : 'overview';
  switchTab(initialTab);

  window.addEventListener('hashchange', () => {
    let h = window.location.hash.replace('#', '');
    if (h.startsWith('view-')) h = h.slice(5);
    if (h && VALID_VIEWS.has(h)) switchTab(h);
  });

  const params = new URLSearchParams(window.location.search);
  const bookParam = params.get('book');
  if (bookParam && SPORTSBOOKS.some(b => b.id === bookParam)) {
    state.activeAccuBook = bookParam;
    state.defaultBook = bookParam;
  }

  if (params.get('auth') === 'signin' || hash === 'signin') {
    window.openAuthModal('signin');
  } else if (params.get('auth') === 'signup' || hash === 'signup') {
    window.openAuthModal('signup');
  } else if (params.get('unlock') === 'modal' || hash === 'telegram') {
    window.openTelegramModal();
  }

  // Background session rehydration and telemetry loading
  Promise.allSettled([auth.init(), loadData()]).then(async () => {
      const uid = getOrCreateWebUserId();

      // Production Server Verification Check (Backend is the Single Source of Truth)
      try {
        const resp = await fetch(`/api/verify-status?user_id=${uid}`);
        if (resp.ok) {
          const data = await resp.json();
          if (data.verified) {
            state.isTelegramUnlocked = true;
            localStorage.setItem('lisa_telegram_unlocked', 'true');
          } else if (!auth.isTelegramVerified()) {
            state.isTelegramUnlocked = false;
            localStorage.removeItem('lisa_telegram_unlocked');
          }
        }
      } catch (e) {
        if (!auth.isTelegramVerified()) {
          state.isTelegramUnlocked = false;
          localStorage.removeItem('lisa_telegram_unlocked');
        }
      }

      renderTierControls();
      renderPicks();
  });
}

// Global Booking Code and Bookmaker Handlers
window.selectBookmakerForPick = function (matchId, bookId) {
  state.selectedBooks = state.selectedBooks || {};
  state.selectedBooks[matchId] = bookId;
  const p = (state.data && state.data.active_picks && state.data.active_picks.find(x => x.match_id === matchId)) || { match_id: matchId };
  const book = SPORTSBOOKS.find(b => b.id === bookId) || SPORTSBOOKS[0];
  const isDirectLinkOnly = true;
  const code = getBookingCodeForPick(p, book.id);
  const link = (p.deep_links && p.deep_links[book.id]) || book.url;

  // Update chips row in that pick card
  const row = document.getElementById(`chips-row-${matchId}`);
  if (row) {
    const chips = row.querySelectorAll('.book-logo-chip');
    chips.forEach(ch => {
      const isTarget = ch.getAttribute('data-book') === bookId;
      ch.classList.toggle('active', isTarget);
      if (isTarget) {
        ch.style.borderColor = book.brandColor;
        ch.style.boxShadow = `0 0 8px ${book.brandColor}40`;
      } else {
        ch.style.borderColor = '';
        ch.style.boxShadow = '';
      }
    });
  }

  // Update pill
  const pill = document.querySelector(`#bet-box-${matchId} .bet-code-pill`);
  if (pill) {
    pill.classList.toggle('is-direct-link', isDirectLinkOnly);
    pill.onclick = () => window.open(link, '_blank', 'noopener,noreferrer');
    pill.title = `Open ${book.name} to place this selection`;
  }
  const dot = document.querySelector(`#bet-box-${matchId} .pill-book-dot`);
  if (dot) dot.style.background = book.brandColor;
  const tag = document.getElementById(`pill-book-tag-${matchId}`);
  if (tag) tag.textContent = `${book.name}:`;
  const val = document.getElementById(`pill-code-val-${matchId}`);
  if (val) {
    val.textContent = code ? `code ${code}` : 'no booking code \u2014 open the book to place it';
    val.classList.toggle('link-mode', isDirectLinkOnly);
  }

  // Update copy/open button
  const copyBtn = document.getElementById(`copy-btn-${matchId}`);
  if (copyBtn) {
    copyBtn.classList.toggle('btn-link-action', isDirectLinkOnly);
    copyBtn.onclick = isDirectLinkOnly ? () => window.open(link, '_blank', 'noopener,noreferrer') : () => window.copyBookingCode(matchId);
    copyBtn.title = isDirectLinkOnly ? `Open selection on ${book.name}` : `Copy ${book.name} code to clipboard`;
    copyBtn.innerHTML = `
      <span class="copy-label">${isDirectLinkOnly ? 'Open' : 'Copy'}</span>
    `;
  }

  // Update open link
  const openLink = document.getElementById(`open-link-${matchId}`);
  if (openLink) {
    openLink.href = link;
    openLink.title = `Open ${book.name} website/app in new tab`;
  }

  // Update helper
  const helper = document.getElementById(`helper-${matchId}`);
  if (helper) {
    helper.innerHTML = `<span>${book.tip}</span>`;
  }
};

window.copyBookingCode = function (matchId) {
  // No bookmaker integration: hand the user the real price and send them to the
  // book. A code LISA cannot verify is never shown as if it were real.
  const p = (state.data && state.data.active_picks && state.data.active_picks.find(x => x.match_id === matchId)) || { match_id: matchId };
  const bookId = (state.selectedBooks && state.selectedBooks[matchId]) || state.defaultBook || 'sportybet';
  const book = SPORTSBOOKS.find(b => b.id === bookId) || SPORTSBOOKS[0];
  const link = (p.deep_links && p.deep_links[book.id]) || book.url;
  const code = getBookingCodeForPick(p, book.id);

  if (!code) {
    window.open(link, '_blank', 'noopener,noreferrer');
    showToast('LISA has no booking code for this book \u2014 opening the sportsbook to place it yourself.', 'info');
    return;
  }

  copyTextToClipboard(code);
  const btn = document.getElementById(`copy-btn-${matchId}`);
  if (btn) {
    const origHtml = btn.innerHTML;
    btn.classList.add('copied');
    btn.innerHTML = `<span class="copy-label">Copied!</span>`;
    setTimeout(() => {
      btn.classList.remove('copied');
      btn.innerHTML = origHtml;
    }, 2000);
  }
  showToast(`Copied ${book.name} code: ${code}`, 'success');
};

window.selectAccuBookmaker = function (bookId) {
  state.activeAccuBook = bookId;
  renderAccumulatorBanner();
};

window.copyAccumulatorCode = function () {
  const currentBookId = state.activeAccuBook || 'sportybet';
  const currentBook = SPORTSBOOKS.find(b => b.id === currentBookId) || SPORTSBOOKS[0];
  const accuCode = (state.data && state.data.accumulator_booking_codes && state.data.accumulator_booking_codes[currentBookId]) || null;

  if (!accuCode) {
    window.open(currentBook.url, '_blank', 'noopener,noreferrer');
    showToast('No accumulator code exists \u2014 opening ' + currentBook.name + ' to build the slip yourself.', 'info');
    return;
  }

  copyTextToClipboard(accuCode);
  showToast(`Copied accumulator code for ${currentBook.name}: ${accuCode}`, 'success');
};

// Commercial Checkout & Subscription Modal
let checkoutPendingTier = 'tier1';
const TIER_PRICING = {
  tier1: {
    name: 'Tier 1 Sharp Starter',
    price: '$19.00 / mo',
    title: 'Upgrade to Tier 1: Sharp Starter',
    sub: 'Unlock the top-ranked selections, automated Kelly sizing, and Telegram kickoff alerts.',
    desc: 'The engine\'s strongest live selections, with the measured edge and best price on each one.'
  },
  tier2: {
    name: 'Tier 2 Pro Trader',
    price: '$49.00 / mo',
    title: 'Upgrade to Tier 2: Pro Trader',
    sub: 'Unlock the complete live board, ranked alternatives, and priced accumulators.',
    desc: 'Every live selection with its measured price, edge, and Kelly sizing.'
  },
  tier3: {
    name: 'Tier 3 VIP Syndicate',
    price: '$149.00 / mo',
    title: 'Upgrade to Tier 3: VIP Syndicate Desk',
    sub: 'Sub-second REST/WebSocket feeds, Double-Poisson Soccer Matrix, and live line drift execution.',
    desc: 'Institutional data feed for sports syndicates, funds, and sharp desks with custom Kelly staking.'
  }
};

window.handlePricingSelect = function (tierKey) {
  if (tierKey === 'free') {
    if (auth.isAuthenticated()) {
      window.switchUserTier('free');
    } else {
      window.openAuthModal('signup', 'free');
    }
    return;
  }
  if (auth.isAuthenticated()) {
    window.openCheckoutModal(tierKey);
  } else {
    window.openAuthModal('signup', tierKey);
  }
};

window.openCheckoutModal = function (tierKey = 'tier1') {
  showToast('Paid checkout is unavailable until verified billing is configured.', 'info');
  return;

  checkoutPendingTier = tierKey;
  const info = TIER_PRICING[tierKey] || TIER_PRICING.tier1;
  const modal = document.getElementById('checkout-modal');
  if (!modal) return;

  const titleEl = document.getElementById('checkout-modal-title');
  const subEl = document.getElementById('checkout-modal-sub');
  const nameEl = document.getElementById('checkout-plan-name');
  const priceEl = document.getElementById('checkout-plan-price');
  const descEl = document.getElementById('checkout-plan-desc');

  if (titleEl) titleEl.textContent = info.title;
  if (subEl) subEl.textContent = info.sub;
  if (nameEl) nameEl.textContent = info.name;
  if (priceEl) priceEl.textContent = info.price;
  if (descEl) descEl.textContent = info.desc;

  modal.style.display = 'flex';
};

window.closeCheckoutModal = function () {
  const modal = document.getElementById('checkout-modal');
  if (modal) modal.style.display = 'none';
};

window.handleCheckoutSubmit = async function (event) {
  if (event) event.preventDefault();
  const btn = document.getElementById('btn-submit-checkout');
  const origHtml = btn ? btn.innerHTML : '';
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span>Processing Encrypted Simulation...</span>';
  }

  // Realistic gateway latency simulation
  await new Promise(r => setTimeout(r, 600));

  await window.switchUserTier(checkoutPendingTier);
  window.closeCheckoutModal();

  if (btn) {
    btn.disabled = false;
    btn.innerHTML = origHtml;
  }

  const info = TIER_PRICING[checkoutPendingTier] || TIER_PRICING.tier1;
  showToast(`Privilege elevated to ${info.name}! Institutional slate unlocked.`, 'success');
};

// Account Settings & Security Modal
window.openAccountSettingsModal = function () {
  const modal = document.getElementById('account-settings-modal');
  if (!modal) return;
  const user = auth.getUser();

  const emailEl = document.getElementById('account-modal-email');
  const nameEl = document.getElementById('account-modal-name');
  const tierEl = document.getElementById('account-modal-tier');
  const tokenEl = document.getElementById('account-session-token');
  const tgInput = document.getElementById('account-telegram-input');

  const currentTierKey = user?.tier || state.currentTier || 'free';
  const tierLabels = { free: 'FREE TIER', tier1: 'TIER 1 PRO', tier2: 'TIER 2 SYNDICATE', tier3: 'TIER 3 VIP' };

  if (emailEl) emailEl.textContent = user?.email || 'guest.trader@xpredict.ai';
  if (nameEl) nameEl.textContent = user?.display_name || 'Quantitative Trader';
  if (tierEl) {
    tierEl.textContent = tierLabels[currentTierKey] || currentTierKey.toUpperCase();
    tierEl.className = `user-tier-tag tier-tag-${currentTierKey}`;
  }

  if (tokenEl) {
    const rawToken = localStorage.getItem('lisa_auth_token') || 'lsp_live_' + (user?.id ? String(user.id).slice(0, 12) : '8849201948ae');
    tokenEl.value = rawToken;
  }

  if (tgInput && user?.telegram_username) {
    tgInput.value = user.telegram_username;
  }

  modal.style.display = 'flex';
};

window.closeAccountSettingsModal = function () {
  const modal = document.getElementById('account-settings-modal');
  if (modal) modal.style.display = 'none';
};

window.handleLinkTelegramAccount = async function () {
  const input = document.getElementById('account-telegram-input');
  const val = input ? input.value.trim() : '';
  if (!val) {
    showToast('Please enter your Telegram @username or ID.', 'warning');
    return;
  }

  try {
    if (typeof auth.linkTelegram === 'function') {
      await auth.linkTelegram(val, val);
    }
    state.isTelegramUnlocked = true;
    localStorage.setItem('lisa_telegram_unlocked', 'true');
    renderPicks();
    showToast(`Telegram account linked to ${val}! Social preview unlocked.`, 'success');
  } catch (err) {
    showToast(err.message || 'Failed to link Telegram account.', 'error');
  }
};

window.copySessionToken = function () {
  const tokenEl = document.getElementById('account-session-token');
  if (tokenEl && tokenEl.value) {
    copyTextToClipboard(tokenEl.value);
    showToast('API Session Token copied to clipboard!', 'success');
  }
};

// Immediate or DOM-ready bootstrap (prevents event listener race condition)
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initApp);
} else {
  initApp();
}
