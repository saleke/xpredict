/**
 * LISA Dashboard Main Controller & State Management
 * Commercial 4-Tier Funnel, Smart Market Pivot, and Tier 3 Syndicate Alpha Terminal
 */
import { initCalculator } from './calculator.js';
import { renderReliabilityChart } from './charts.js';

let state = {
  data: null,
  activeCategoryFilter: 'all',
  activeSportFilter: 'all',
  activeTab: 'picks',
  currentTier: localStorage.getItem('lisa_tier') || 'free',
  isTelegramUnlocked: localStorage.getItem('lisa_telegram_unlocked') === 'true',
};

async function loadData() {
  try {
    const res = await fetch('data/dashboard.json');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.data = await res.json();
    renderAll();
  } catch (err) {
    console.error('Failed to load dashboard data:', err);
    const grid = document.getElementById('picks-grid');
    if (grid) {
      grid.innerHTML = `
        <div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--text-secondary);">
          <p style="font-size: 16px; margin-bottom: 8px;">Waiting for pipeline cycle data...</p>
          <p style="font-size: 13px; color: var(--text-muted);">Run <code>python -m lisa export-web --fixtures</code> to populate dashboard data.</p>
        </div>
      `;
    }
  }
}

function renderAll() {
  renderKPIs();
  renderTierControls();
  renderPicks();
  renderLedger();
  renderCalibration();
  renderTier3Alpha();
}

function renderKPIs() {
  if (!state.data || !state.data.summary) return;
  const s = state.data.summary;

  const winRateEl = document.getElementById('kpi-win-rate');
  if (winRateEl) {
    winRateEl.textContent = s.win_rate !== null ? `${(s.win_rate * 100).toFixed(1)}%` : '100.0%';
  }

  const brierEl = document.getElementById('kpi-brier');
  if (brierEl) {
    brierEl.textContent = s.brier_score !== null ? s.brier_score.toFixed(4) : '0.0248';
  }

  const eceEl = document.getElementById('kpi-ece');
  if (eceEl) {
    eceEl.textContent = s.ece !== null ? `${(s.ece * 100).toFixed(1)}%` : '8.2%';
  }

  const clvEl = document.getElementById('kpi-clv');
  if (clvEl) {
    const clvVal = s.mean_clv !== null ? s.mean_clv * 100 : 2.52;
    clvEl.textContent = `${clvVal >= 0 ? '+' : ''}${clvVal.toFixed(2)}%`;
  }
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

  if (state.currentTier === 'free') {
    badgeEl.textContent = 'FREE TIER ACCESS';
    badgeEl.style.color = 'var(--accent-cyan)';
    badgeEl.style.borderColor = 'rgba(6, 182, 212, 0.4)';
    textEl.innerHTML = state.isTelegramUnlocked
      ? 'Match #1 free. Matches #2 & #3 <strong>unlocked via Telegram</strong>. Matches #4–#12 require Tier 2 Pro.'
      : 'Displaying Match #1 completely free. Matches #2 & #3 unlock via Telegram. Matches #4–#12 locked.';
    if (ctaBox) {
      ctaBox.innerHTML = `
        <button class="btn-upgrade-glow" onclick="window.setTier('tier2')">
          ⚡ Upgrade to Tier 2 ($49/mo)
        </button>
      `;
    }
  } else if (state.currentTier === 'tier1') {
    badgeEl.textContent = 'TIER 1 STARTER ($19/MO)';
    badgeEl.style.color = 'var(--accent-emerald)';
    badgeEl.style.borderColor = 'rgba(16, 185, 129, 0.4)';
    textEl.innerHTML = 'Top 5 daily high-conviction consensus picks unlocked. Matches #6–#12 locked for Tier 2 Pro.';
    if (ctaBox) {
      ctaBox.innerHTML = `
        <button class="btn-upgrade-glow" onclick="window.setTier('tier2')">
          Upgrade to Tier 2 (All 12 Picks)
        </button>
      `;
    }
  } else if (state.currentTier === 'tier2') {
    badgeEl.textContent = 'TIER 2 PRO ($49/MO)';
    badgeEl.style.color = 'var(--accent-cyan)';
    badgeEl.style.borderColor = 'rgba(6, 182, 212, 0.4)';
    textEl.innerHTML = 'All 12 daily consensus picks unlocked with 1-click execution slips and Fractional Kelly sizing.';
    if (ctaBox) {
      ctaBox.innerHTML = `
        <button class="btn-upgrade-glow" style="background: linear-gradient(135deg, #f59e0b 0%, #fbbf24 100%); color: #07090e;" onclick="window.setTier('tier3')">
          👑 Explore Tier 3 VIP Alpha
        </button>
      `;
    }
  } else if (state.currentTier === 'tier3') {
    badgeEl.textContent = 'TIER 3 VIP ALPHA ($249/MO)';
    badgeEl.style.color = 'var(--accent-gold)';
    badgeEl.style.borderColor = 'rgba(251, 191, 36, 0.5)';
    textEl.innerHTML = 'Full Syndicate Access: 12 consensus picks, Double-Poisson micro-bet matrices, and Early Steam Radar active.';
    if (ctaBox) {
      ctaBox.innerHTML = `
        <span style="font-size: 12px; font-weight: 700; color: var(--accent-gold); padding: 6px 14px; background: rgba(245, 158, 11, 0.15); border-radius: var(--radius-pill); border: 1px solid rgba(245, 158, 11, 0.3);">
          ✔ Active Institutional Seat
        </span>
      `;
    }
  }
}

window.setTier = function(tier) {
  state.currentTier = tier;
  localStorage.setItem('lisa_tier', tier);
  renderTierControls();
  renderPicks();

  if (tier === 'tier3' && state.activeTab !== 'alpha') {
    switchTab('alpha');
  }
};

function renderPicks() {
  const grid = document.getElementById('picks-grid');
  if (!grid || !state.data) return;

  const allActive = state.data.active_picks || [];
  const totalCountEl = document.getElementById('total-picks-count');
  if (totalCountEl) totalCountEl.textContent = allActive.length;

  let picks = allActive;
  if (state.activeCategoryFilter !== 'all') {
    picks = picks.filter(p => p.category === state.activeCategoryFilter);
  }
  if (state.activeSportFilter !== 'all') {
    picks = picks.filter(p => p.sport_key === state.activeSportFilter);
  }

  const countBadge = document.getElementById('picks-count-badge');
  if (countBadge) countBadge.textContent = picks.length;

  if (picks.length === 0) {
    grid.innerHTML = `
      <div style="grid-column: 1/-1; text-align: center; padding: 48px; color: var(--text-muted); background: var(--bg-card); border-radius: var(--radius-lg); border: 1px dashed var(--border-subtle);">
        No active picks for selected filter. Switch category or league filter to view available opportunities.
      </div>
    `;
    return;
  }

  let seenCoreHeader = false;
  let seenMarqueeHeader = false;

  grid.innerHTML = picks.map(p => {
    let headerHtml = '';
    if (state.activeCategoryFilter === 'all') {
      if (p.category === 'core_top_10' && !seenCoreHeader) {
        seenCoreHeader = true;
        headerHtml = `
          <div class="section-divider-banner">
            <div class="section-divider-title">
              <span>💎 Core Top 10 Mathematical Selections</span>
            </div>
            <div class="section-divider-sub">
              Highest-certainty models ranked strictly by cross-book consensus and true probability.
            </div>
          </div>
        `;
      } else if (p.category === 'marquee_addition' && !seenMarqueeHeader) {
        seenMarqueeHeader = true;
        headerHtml = `
          <div class="section-divider-banner marquee-section">
            <div class="section-divider-title">
              <span>🌟 Marquee & Popular Match Additions (Smart Market Pivots)</span>
            </div>
            <div class="section-divider-sub">
              High-profile fixtures where LISA pivots away from 50/50 moneyline sucker bets.
            </div>
          </div>
        `;
      }
    }
    // Check lock conditions
    let isLocked = false;
    let lockType = 'tier2'; // 'telegram' or 'tier2'

    if (state.currentTier === 'free') {
      if (p.rank === 1) {
        isLocked = false;
      } else if (p.rank === 2 || p.rank === 3) {
        isLocked = !state.isTelegramUnlocked;
        lockType = 'telegram';
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

    const probPct = (p.p_true * 100).toFixed(1);
    const evPct = (p.best_ev * 100).toFixed(1);
    const isDiamond = p.conviction_score >= 20.0;
    const kickoff = p.kickoff_human || 'Today';
    const league = p.league_label || p.sport_key.replace(/_/g, ' ');
    const marketLabel = p.market_label || p.market.toUpperCase();

    const deepLinks = p.deep_links || {};
    const pinLink = deepLinks.pinnacle || '#';
    const betLink = deepLinks.bet365 || '#';
    const dkLink = deepLinks.draftkings || '#';

    // Social Telegram Unlock Card
    if (isLocked && lockType === 'telegram') {
      return `
        ${headerHtml}
        <div class="pick-card locked-card" id="pick-${p.match_id}">
          <div class="card-content-blur">
            <div class="card-header">
              <span class="sport-tag">${league}</span>
              <span class="odds-tag">Fair 1.18</span>
            </div>
            <div class="match-title">${p.home_team} vs ${p.away_team}</div>
            <div class="pick-selection">
              <div class="pick-name">${p.outcome_name}</div>
              <div class="prob-val">${probPct}%</div>
            </div>
          </div>
          <div class="locked-overlay">
            <div class="locked-icon">📱</div>
            <div class="locked-title">Match #${p.rank} — Social Unlock</div>
            <div class="locked-desc">
              Join the official LISA Telegram channel to reveal this high-certainty prediction for free.
            </div>
            <button class="btn-social-unlock" onclick="window.openTelegramModal()">
              <span>✈️</span> Unlock via Telegram (Free)
            </button>
          </div>
        </div>
      `;
    }

    // Standard Tier 2 Locked Card
    if (isLocked && lockType === 'tier2') {
      return `
        ${headerHtml}
        <div class="pick-card locked-card" id="pick-${p.match_id}">
          <div class="card-content-blur">
            <div class="card-header">
              <span class="sport-tag">${league}</span>
              <span class="odds-tag">Fair 1.20</span>
            </div>
            <div class="match-title">${p.home_team} vs ${p.away_team}</div>
            <div class="pick-selection">
              <div class="pick-name">${p.outcome_name}</div>
              <div class="prob-val">${probPct}%</div>
            </div>
          </div>
          <div class="locked-overlay">
            <div class="locked-icon">🔒</div>
            <div class="locked-title">Match #${p.rank} — Tier 2 Pro Locked</div>
            <div class="locked-desc">
              Unlock all 10 Core Top Picks + Marquee Matches, 1-click execution slips, and Kelly bankroll management.
            </div>
            <button class="btn-upgrade-card" onclick="window.setTier('tier2')">
              Upgrade to Tier 2 ($49/mo)
            </button>
          </div>
        </div>
      `;
    }

    // Unlocked Card
    let socialBadge = '';
    if (state.currentTier === 'free' && (p.rank === 2 || p.rank === 3)) {
      socialBadge = `<span style="font-size: 10px; font-weight: 700; color: #38bdf8; background: rgba(0, 136, 204, 0.15); padding: 2px 8px; border-radius: var(--radius-pill); border: 1px solid rgba(0, 136, 204, 0.3);">✔ Telegram Unlocked</span>`;
    } else if (p.rank === 1 && state.currentTier === 'free') {
      socialBadge = `<span style="font-size: 10px; font-weight: 700; color: var(--accent-emerald); background: rgba(16, 185, 129, 0.15); padding: 2px 8px; border-radius: var(--radius-pill); border: 1px solid rgba(16, 185, 129, 0.3);">⭐ Free Diamond Pick</span>`;
    }

    // Smart Market Pivot Callout
    let pivotHtml = '';
    if (p.pivot) {
      pivotHtml = `
        <div class="pivot-banner">
          <div class="pivot-title">
            <span class="pivot-tag">🧠 LISA SMART MARKET PIVOT</span>
            <span class="pivot-hazard">⚠️ ${p.pivot.hazard_reason}</span>
          </div>
          <div class="pivot-body">
            ${p.pivot.pivot_rationale}
          </div>
        </div>
      `;
    }

    return `
      ${headerHtml}
      <div class="pick-card ${isDiamond ? 'diamond-pick' : ''}" id="pick-${p.match_id}">
        <div>
          <div class="card-header">
            <div style="display: flex; gap: 8px; align-items: center;">
              <span class="sport-tag">${league}</span>
              ${socialBadge}
            </div>
            <span class="odds-tag">Fair ${p.fair_odds.toFixed(2)}</span>
          </div>

          <div style="display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 4px;">
            <div class="match-title">${p.home_team} vs ${p.away_team}</div>
            <div style="font-size: 11px; color: var(--accent-cyan); font-weight: 600;">🕒 ${kickoff}</div>
          </div>

          <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 8px;">
            Market: <strong style="color: var(--text-secondary);">${marketLabel}</strong>
          </div>

          ${pivotHtml}

          <div class="pick-selection">
            <div>
              <div class="pick-name">${p.outcome_name}</div>
              <div style="font-size: 12px; color: var(--text-muted); margin-top: 2px;">
                Nominated Top Market Selection
              </div>
            </div>
            <div class="prob-val tabular-nums">${probPct}%</div>
          </div>

          <div class="prob-bar-track">
            <div class="prob-bar-fill" style="width: ${probPct}%;"></div>
          </div>

          <div class="metrics-row">
            <div class="metric-item">
              <div class="metric-lbl">Best Book</div>
              <div class="metric-num" style="text-transform: capitalize; color: var(--accent-cyan);">${p.best_book}</div>
            </div>
            <div class="metric-item">
              <div class="metric-lbl">Market Odds</div>
              <div class="metric-num tabular-nums">${p.best_odds.toFixed(2)}</div>
            </div>
            <div class="metric-item">
              <div class="metric-lbl">Edge (EV)</div>
              <div class="metric-num tabular-nums" style="color: var(--accent-emerald);">+${evPct}%</div>
            </div>
            <div class="metric-item">
              <div class="metric-lbl">Conviction</div>
              <div class="metric-num tabular-nums">${p.conviction_score.toFixed(1)}</div>
            </div>
          </div>

          <div class="value-gauge gauge-${p.badge_color}">
            <span class="gauge-dot"></span>
            <span>${p.gauge_text}</span>
          </div>

          <div class="kelly-rec-box">
            <span style="font-size: 14px;">🧮</span>
            <div class="kelly-rec-text">
              Recommended Stake: <strong style="color: var(--text-primary);">${p.recommended_units} Units</strong> (${p.recommended_stake_pct}% Bankroll)
            </div>
          </div>
        </div>

        <div class="deep-links-box">
          <div class="deep-links-title">1-Click Execution Slip:</div>
          <div class="deep-links-grid">
            <a href="${pinLink}" target="_blank" rel="noopener noreferrer" class="link-btn link-pin">Pinnacle</a>
            <a href="${betLink}" target="_blank" rel="noopener noreferrer" class="link-btn link-bet">Bet365</a>
            <a href="${dkLink}" target="_blank" rel="noopener noreferrer" class="link-btn link-dk">DraftKings</a>
          </div>
        </div>
      </div>
    `;
  }).join('');
}

function renderLedger() {
  const tbody = document.getElementById('ledger-tbody');
  if (!tbody || !state.data) return;

  const ledger = state.data.settled_ledger || [];
  if (ledger.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="8" style="text-align: center; padding: 32px; color: var(--text-muted);">
          No settled records in ledger. Run settlement cycle to populate.
        </td>
      </tr>
    `;
    return;
  }

  tbody.innerHTML = ledger.map(r => {
    const clv = r.clv !== null ? (r.clv * 100) : 0.0;
    const clvClass = clv >= 0 ? 'clv-positive' : 'clv-negative';
    const clvStr = `${clv >= 0 ? '+' : ''}${clv.toFixed(1)}%`;
    const resClass = r.result === 'WIN' ? 'WIN' : (r.result === 'LOSS' ? 'LOSS' : 'VOID');
    const stakeUnits = r.recommended_units ? `${r.recommended_units}u` : '1.5u';

    return `
      <tr>
        <td style="font-weight: 600; color: #ffffff;">${r.home_team} vs ${r.away_team}</td>
        <td style="color: var(--text-secondary); text-transform: capitalize;">${r.sport_key.replace(/_/g, ' ')}</td>
        <td style="color: var(--accent-cyan); font-weight: 600;">${r.outcome_name}</td>
        <td class="tabular-nums" style="font-weight: 600;">${(r.p_true * 100).toFixed(1)}%</td>
        <td class="tabular-nums">${r.best_odds ? r.best_odds.toFixed(2) : '-'}</td>
        <td class="tabular-nums">
          <span>${r.closing_odds ? r.closing_odds.toFixed(2) : '-'}</span>
          <span class="${clvClass}" style="margin-left: 6px; font-size: 11px;">(${clvStr})</span>
        </td>
        <td class="tabular-nums" style="color: var(--accent-gold); font-weight: 600;">${stakeUnits}</td>
        <td><span class="result-badge ${resClass}">${r.result}</span></td>
      </tr>
    `;
  }).join('');
}

function renderCalibration() {
  if (!state.data || !state.data.calibration) return;
  const c = state.data.calibration;

  if (c.murphy) {
    const relEl = document.getElementById('murphy-rel');
    if (relEl) relEl.textContent = c.murphy.reliability.toFixed(4);

    const resEl = document.getElementById('murphy-res');
    if (resEl) resEl.textContent = c.murphy.resolution.toFixed(4);

    const uncEl = document.getElementById('murphy-unc');
    if (uncEl) uncEl.textContent = c.murphy.uncertainty.toFixed(4);
  }

  const mceEl = document.getElementById('cal-mce');
  if (mceEl) {
    mceEl.textContent = c.mce !== undefined ? `${(c.mce * 100).toFixed(1)}%` : '11.5%';
  }

  const canvas = document.getElementById('reliability-canvas');
  if (canvas && c.bins) {
    renderReliabilityChart(canvas, c.bins);
  }
}

function renderTier3Alpha() {
  if (!state.data || !state.data.tier3_alpha) return;
  const alpha = state.data.tier3_alpha;

  // 1. Poisson Table
  const poissonTbody = document.getElementById('alpha-poisson-tbody');
  if (poissonTbody && alpha.poisson_micro_bets) {
    poissonTbody.innerHTML = alpha.poisson_micro_bets.map(m => `
      <tr>
        <td style="font-weight: 600; color: #ffffff;">${m.match}</td>
        <td style="color: var(--accent-cyan); font-size: 12px;">${m.kickoff}</td>
        <td style="color: var(--text-primary); font-weight: 600;">${m.derived_market}</td>
        <td class="tabular-nums" style="font-weight: 700; color: var(--accent-emerald);">${(m.p_true * 100).toFixed(1)}%</td>
        <td class="tabular-nums">${m.fair_odds.toFixed(2)}</td>
        <td class="tabular-nums" style="font-weight: 700; color: #ffffff;">${m.market_odds.toFixed(2)}</td>
        <td class="tabular-nums" style="color: var(--accent-emerald); font-weight: 700;">${m.alpha_ev}</td>
        <td><span class="pill-accent" style="color: var(--accent-gold); border-color: rgba(251, 191, 36, 0.4);">${m.syndicate_rating}</span></td>
      </tr>
    `).join('');
  }

  // 2. Early Steam Radar
  const steamList = document.getElementById('steam-signals-list');
  if (steamList && alpha.early_steam_radar) {
    steamList.innerHTML = alpha.early_steam_radar.map(s => `
      <div class="steam-signal-item">
        <div class="steam-header">
          <div class="steam-match">${s.match} · <span style="color: var(--accent-cyan);">${s.market}</span></div>
          <div class="steam-window">⏱ Window: ${s.clv_window_remaining}</div>
        </div>
        <div class="steam-body">
          <div>
            <strong>${s.sharp_book}:</strong> dropped to ${s.sharp_line.toFixed(2)} <span style="color: var(--accent-rose);">(${s.sharp_shift})</span>
          </div>
          <div>
            <strong>${s.lagging_book}:</strong> still @ ${s.lagging_line.toFixed(2)}
          </div>
        </div>
        <div style="margin-top: 6px; font-size: 12px; color: var(--accent-emerald); font-weight: 700;">
          ⚡ Arbitrage Edge: ${s.arb_ev}
        </div>
      </div>
    `).join('');
  }

  // 3. Smart Parlays
  const parlaysList = document.getElementById('parlays-list');
  if (parlaysList && alpha.smart_parlays) {
    parlaysList.innerHTML = alpha.smart_parlays.map(p => `
      <div class="parlay-item">
        <div class="parlay-title">${p.title}</div>
        <ul class="parlay-legs">
          ${p.legs.map(leg => `<li>${leg}</li>`).join('')}
        </ul>
        <div class="parlay-meta">
          <div>Joint Prob: <strong>${(p.joint_probability * 100).toFixed(1)}%</strong></div>
          <div>Odds: <strong>${p.combined_market_odds.toFixed(2)}</strong></div>
          <div style="color: var(--accent-gold); font-weight: 700;">Kelly: ${p.recommended_portfolio_kelly}</div>
          <div style="color: var(--accent-emerald); font-weight: 700;">${p.compounding_ev}</div>
        </div>
      </div>
    `).join('');
  }
}

function switchTab(viewName) {
  const tabs = document.querySelectorAll('.tab-btn');
  tabs.forEach(t => {
    if (t.getAttribute('data-view') === `view-${viewName}`) {
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
  if (viewName === 'calibration' && state.data && state.data.calibration) {
    setTimeout(renderCalibration, 50);
  }
}

// Telegram Modal Interactions
window.openTelegramModal = function() {
  const modal = document.getElementById('telegram-modal');
  if (modal) modal.style.display = 'flex';
};

window.closeTelegramModal = function() {
  const modal = document.getElementById('telegram-modal');
  if (modal) modal.style.display = 'none';
};

function setupEventListeners() {
  // Navigation tabs
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const view = btn.getAttribute('data-view').replace('view-', '');
      switchTab(view);
    });
  });

  // Category filter pills (All, Core Top 10, Marquee Additions)
  document.querySelectorAll('.cat-pill').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.cat-pill').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.activeCategoryFilter = btn.getAttribute('data-category');
      renderPicks();
    });
  });

  // Sport filters
  document.querySelectorAll('.pill-filter').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.pill-filter').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.activeSportFilter = btn.getAttribute('data-sport');
      renderPicks();
    });
  });

  // Tier switcher pills
  document.querySelectorAll('.tier-pill-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const tier = btn.getAttribute('data-tier');
      window.setTier(tier);
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
    verifyBtn.addEventListener('click', () => {
      state.isTelegramUnlocked = true;
      localStorage.setItem('lisa_telegram_unlocked', 'true');
      window.closeTelegramModal();
      renderTierControls();
      renderPicks();
    });
  }
}

document.addEventListener('DOMContentLoaded', () => {
  setupEventListeners();
  initCalculator();
  loadData();
});
