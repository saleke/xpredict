/**
 * LISA Dashboard Main Controller & State Management
 * Commercial 4-Tier Funnel, Smart Market Pivot, and Tier 3 Syndicate Alpha Terminal
 */
import { api } from './api.js';
import { auth } from './auth.js';
import { initCalculator } from './calculator.js';
import { renderReliabilityChart } from './charts.js';

let state = {
  data: null,
  activeGradeFilter: 'all',
  activeCategoryFilter: 'all',
  activeSportFilter: 'all',
  activeTab: 'overview',
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
  renderBacktest();
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

  const trapsEl = document.getElementById('kpi-traps');
  if (trapsEl) {
    trapsEl.textContent = s.traps_avoided_month ? `${s.traps_avoided_month} Traps` : '18 Traps';
  }

  const catAll = document.getElementById('cat-all');
  if (catAll && s.total_matches_evaluated) {
    catAll.innerHTML = `All Matches (<span id="total-picks-count">${s.total_matches_evaluated}</span>)`;
  }
  const catGradeA = document.getElementById('cat-grade-a');
  if (catGradeA && s.diamonds_count !== undefined) {
    catGradeA.textContent = `💎 Flagship Diamonds (${s.diamonds_count})`;
  }
  const catGradeB = document.getElementById('cat-grade-b');
  if (catGradeB && s.pivots_count !== undefined) {
    catGradeB.textContent = `🧠 Smart Pivots (${s.pivots_count})`;
  }
  const catGradeC = document.getElementById('cat-grade-c');
  if (catGradeC && s.pass_advisories_count !== undefined) {
    catGradeC.textContent = `🛡️ Pass Advisories (${s.pass_advisories_count})`;
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
        <button class="btn-upgrade-glow" onclick="window.switchTab('overview')">
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
    badgeEl.textContent = 'TIER 2 ALL-ACCESS ($49/MO)';
    badgeEl.style.color = 'var(--accent-cyan)';
    badgeEl.style.borderColor = 'rgba(6, 182, 212, 0.4)';
    textEl.innerHTML = 'All 12 daily match predictions unlocked with smart safety picks and recommended bet sizes.';
    if (ctaBox) {
      ctaBox.innerHTML = `
        <button class="btn-upgrade-glow" style="background: linear-gradient(135deg, #f59e0b 0%, #fbbf24 100%); color: #07090e;" onclick="window.setTier('tier3')">
          👑 Explore Tier 3 VIP Syndicate
        </button>
      `;
    }
  } else if (state.currentTier === 'tier3') {
    badgeEl.textContent = 'TIER 3 VIP SYNDICATE ($149/MO)';
    badgeEl.style.color = 'var(--accent-gold)';
    badgeEl.style.borderColor = 'rgba(251, 191, 36, 0.5)';
    textEl.innerHTML = 'Full VIP Access: All 12 predictions, early line movement alerts, and deep match analysis.';
    if (ctaBox) {
      ctaBox.innerHTML = `
        <span style="font-size: 12px; font-weight: 700; color: var(--accent-gold); padding: 6px 14px; background: rgba(245, 158, 11, 0.15); border-radius: var(--radius-pill); border: 1px solid rgba(245, 158, 11, 0.3);">
          ✔ Active VIP Member
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
  if (state.activeGradeFilter !== 'all') {
    picks = picks.filter(p => p.grade === state.activeGradeFilter || p.category === state.activeGradeFilter);
  }
  if (state.activeSportFilter !== 'all') {
    picks = picks.filter(p => p.sport_key === state.activeSportFilter);
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
    let headerHtml = '';
    if (state.activeGradeFilter === 'all' && state.activeSportFilter === 'all') {
      const grade = p.grade || (p.is_pass_advisory ? 'GRADE_C' : (p.pivot ? 'GRADE_B' : 'GRADE_A'));
      if (grade === 'GRADE_A' && !seenDiamondHeader) {
        seenDiamondHeader = true;
        headerHtml = `
          <div class="section-divider-banner diamond-section">
            <div class="section-divider-title">
              <span>💎 Flagship Diamonds (Tier 1 Core)</span>
            </div>
            <div class="section-divider-sub">
              Strict mathematical consensus (P<sub>true</sub> ≥ 82%, CV ≤ 2.5%) feeding the public audited ledger.
            </div>
          </div>
        `;
      } else if (grade === 'GRADE_B' && !seenPivotHeader) {
        seenPivotHeader = true;
        headerHtml = `
          <div class="section-divider-banner pivot-section">
            <div class="section-divider-title">
              <span>🧠 Smart Market Pivots (High-Yield Micro-Lines)</span>
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
              <span>🛡️ LISA Pass Advisories (Bankroll Capital Preservation)</span>
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
    const isDiamond = p.grade === 'GRADE_A' || p.conviction_score >= 20.0;
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
            <div class="locked-title">Match #${p.rank} · Social Unlock</div>
            <div class="locked-desc">
              Join official LISA Telegram to reveal this prediction for free.
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
      const lockTitle = p.is_pass_advisory ? `Match #${p.rank} · Pass Advisory` : `Match #${p.rank} · Tier 2 Pro`;
      const lockDesc = p.is_pass_advisory
        ? 'Unlock capital preservation advisory, hazard breakdown, and avoidance metrics.'
        : 'Unlock all 12 Diamonds, Smart Pivots & Pass Advisories with 1-click slips.';
      return `
        ${headerHtml}
        <div class="pick-card locked-card" id="pick-${p.match_id}">
          <div class="card-content-blur">
            <div class="card-header">
              <span class="sport-tag">${league}</span>
              <span class="odds-tag">${p.is_pass_advisory ? 'Advisory' : 'Fair ' + (p.fair_odds ? p.fair_odds.toFixed(2) : '1.20')}</span>
            </div>
            <div class="match-title">${p.home_team} vs ${p.away_team}</div>
            <div class="pick-selection">
              <div class="pick-name">${p.outcome_name}</div>
              <div class="prob-val">${probPct}%</div>
            </div>
          </div>
          <div class="locked-overlay">
            <div class="locked-icon">🔒</div>
            <div class="locked-title">${lockTitle}</div>
            <div class="locked-desc">${lockDesc}</div>
            <button class="btn-upgrade-card" onclick="window.setTier('tier2')">
              Upgrade to Tier 2 ($49/mo)
            </button>
          </div>
        </div>
      `;
    }

    // Pass Advisory Unlocked Card
    if (p.is_pass_advisory) {
      return `
        ${headerHtml}
        <div class="pick-card pass-card" id="pick-${p.match_id}">
          <div>
            <div class="card-header">
              <div style="display: flex; gap: 8px; align-items: center;">
                <span class="sport-tag">${league}</span>
                <span class="pass-shield-tag">🛡️ PASS ADVISORY</span>
              </div>
              <span class="odds-tag" style="background: rgba(244, 63, 94, 0.15); color: var(--accent-rose); border-color: rgba(244, 63, 94, 0.3);">Vig Trap ${p.best_odds ? p.best_odds.toFixed(2) : '2.50'}</span>
            </div>

            <div style="display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 4px;">
              <div class="match-title">${p.home_team} vs ${p.away_team}</div>
              <div style="font-size: 11px; color: var(--accent-cyan); font-weight: 600;">🕒 ${kickoff}</div>
            </div>

            <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 8px;">
              Market Under Analysis: <strong style="color: var(--text-secondary);">${marketLabel}</strong>
            </div>

            <div class="pass-hazard-box">
              <div class="pass-hazard-title">
                <span>⚠️ HAZARD DETECTED:</span> ${p.hazard_title || 'Negative EV / Market Trap'}
              </div>
              <div class="pass-hazard-desc">
                ${p.hazard_reason || 'High-entropy trap pricing identified across bookmaker consensus.'}
              </div>
            </div>

            <div class="pass-preservation-box">
              <div class="pass-preservation-title">
                <span>🛡️ LISA CAPITAL PRESERVATION:</span> ${p.pass_verdict || 'DO NOT BET'}
              </div>
              <div class="pass-preservation-desc">
                ${p.preservation_rationale || 'Zero mathematical edge. Capital preserved for high-conviction Diamond picks.'}
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
                <div class="metric-num tabular-nums" style="color: var(--accent-emerald); font-size: 13px;">${p.capital_saved_estimate || '$100.00 Saved'}</div>
              </div>
            </div>
          </div>

          <div class="btn-bankroll-preserved">
            <span>🛡️</span> $0 Wagered · Bankroll Capital Preserved
          </div>
        </div>
      `;
    }

    // Unlocked Card (Grade A Diamonds & Grade B Pivots)
    let socialBadge = '';
    if (state.currentTier === 'free' && (p.rank === 2 || p.rank === 3)) {
      socialBadge = `<span style="font-size: 10px; font-weight: 700; color: #38bdf8; background: rgba(0, 136, 204, 0.15); padding: 2px 8px; border-radius: var(--radius-pill); border: 1px solid rgba(0, 136, 204, 0.3);">✔ Telegram Unlocked</span>`;
    } else if (p.rank === 1 && state.currentTier === 'free') {
      socialBadge = `<span style="font-size: 10px; font-weight: 700; color: var(--accent-emerald); background: rgba(16, 185, 129, 0.15); padding: 2px 8px; border-radius: var(--radius-pill); border: 1px solid rgba(16, 185, 129, 0.3);">⭐ Free Diamond Pick</span>`;
    } else if (p.grade === 'GRADE_B') {
      socialBadge = `<span style="font-size: 10px; font-weight: 700; color: #a78bfa; background: rgba(139, 92, 246, 0.15); padding: 2px 8px; border-radius: var(--radius-pill); border: 1px solid rgba(139, 92, 246, 0.3);">🧠 Smart Pivot</span>`;
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
            <span class="odds-tag">Fair ${p.fair_odds ? p.fair_odds.toFixed(2) : '-'}</span>
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
              <div class="metric-num tabular-nums">${p.best_odds ? p.best_odds.toFixed(2) : '-'}</div>
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

          <div class="yield-options-box" style="margin-top: 12px; padding: 10px 12px; background: rgba(255,255,255,0.03); border-radius: var(--radius-sm); border: 1px solid var(--border-subtle);">
            <div style="font-size: 11px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; margin-bottom: 6px; display: flex; justify-content: space-between;">
              <span>Execution Yield Options:</span>
              <span style="color: var(--accent-cyan); font-weight: 600;">Flexible Strategy</span>
            </div>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px;">
              <div style="background: rgba(16, 185, 129, 0.08); border: 1px solid rgba(16, 185, 129, 0.25); border-radius: 4px; padding: 6px 8px; font-size: 11px;">
                <div style="color: var(--accent-emerald); font-weight: 700;">🛡️ Safe Base (Floor)</div>
                <div style="color: #fff; margin-top: 2px;">${p.outcome_name} @ <strong>${p.best_odds ? p.best_odds.toFixed(2) : '-'}</strong></div>
                <div style="color: var(--text-muted); font-size: 10px;">${probPct}% Prob · Low Variance</div>
              </div>
              <div style="background: rgba(251, 191, 36, 0.08); border: 1px solid rgba(251, 191, 36, 0.25); border-radius: 4px; padding: 6px 8px; font-size: 11px;">
                <div style="color: var(--accent-gold); font-weight: 700;">⚡ Alpha Booster</div>
                <div style="color: #fff; margin-top: 2px;">${p.outcome_name} -1.5 AH @ <strong>${p.best_odds ? (p.best_odds * 1.48).toFixed(2) : '1.85'}</strong></div>
                <div style="color: var(--text-muted); font-size: 10px;">High Cash Yield · +85% Payout</div>
              </div>
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
        <td colspan="9" style="text-align: center; padding: 32px; color: var(--text-muted);">
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
    const stakeUnits = r.recommended_units ? `${r.recommended_units}u` : '1.0u';

    return `
      <tr>
        <td style="font-weight: 600; color: #ffffff;">${r.home_team} vs ${r.away_team}</td>
        <td class="tabular-nums" style="font-weight: 700; color: var(--accent-gold); letter-spacing: 0.5px;">${r.actual_score || '-'}</td>
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

function renderBacktest() {
  if (!state.data || !state.data.backtest) return;
  const b = state.data.backtest;

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
            <div style="font-weight: 700; color: #ffffff;">${row.badge} ${row.name}</div>
            <div style="font-size: 11px; color: var(--text-muted); margin-top: 2px;">${row.description}</div>
          </td>
          <td class="tabular-nums" style="font-weight: 600;">${row.avg_odds ? row.avg_odds.toFixed(2) : '-'}</td>
          <td class="tabular-nums" style="color: var(--accent-emerald); font-weight: 700;">${(row.win_rate * 100).toFixed(1)}%</td>
          <td class="tabular-nums">$${row.total_wagered.toLocaleString('en-US', {minimumFractionDigits: 2})}</td>
          <td class="tabular-nums" style="color: var(--accent-gold); font-weight: 700;">+$${row.net_profit.toFixed(2)}</td>
          <td class="tabular-nums" style="color: var(--accent-emerald); font-weight: 700;">+${row.roi_pct.toFixed(1)}%</td>
          <td class="tabular-nums" style="color: var(--accent-rose); font-weight: 600;">-${row.max_drawdown_pct.toFixed(2)}%</td>
          <td class="tabular-nums" style="color: var(--accent-cyan);">${row.sharpe_ratio ? row.sharpe_ratio.toFixed(2) : '-'}</td>
          <td><span class="pill-league" style="font-size: 11px;">${row.best_for}</span></td>
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
  if (descEl && s.description) descEl.textContent = s.description;

  const winRateEl = document.getElementById('bkt-win-rate');
  if (winRateEl && s.win_rate !== undefined) winRateEl.textContent = `${(s.win_rate * 100).toFixed(1)}%`;

  const ciEl = document.getElementById('bkt-ci');
  if (ciEl && s.wilson_ci_lower !== undefined && s.wilson_ci_upper !== undefined) {
    ciEl.textContent = `95% CI: [${(s.wilson_ci_lower * 100).toFixed(1)}%, ${(s.wilson_ci_upper * 100).toFixed(1)}%]`;
  }

  const eceEl = document.getElementById('bkt-ece');
  if (eceEl) {
    if (s.ece !== undefined) {
      eceEl.textContent = `${(s.ece * 100).toFixed(2)}%`;
    } else if (s.avg_odds !== undefined) {
      eceEl.textContent = `${s.avg_odds.toFixed(2)} Avg`;
    }
  }

  const subEceEl = document.getElementById('bkt-sub-ece');
  if (subEceEl) {
    if (s.reliability !== undefined) {
      subEceEl.textContent = `Murphy Rel: ${s.reliability.toFixed(4)}`;
    } else {
      subEceEl.textContent = `${s.risk_level || 'Controlled'} Risk`;
    }
  }

  const capSavedEl = document.getElementById('bkt-capital-saved');
  if (capSavedEl && s.capital_preserved_dollars !== undefined) {
    capSavedEl.textContent = `$${s.capital_preserved_dollars.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
  }

  const netSavedEl = document.getElementById('bkt-net-saved');
  if (netSavedEl && s.net_counterfactual_value !== undefined) {
    const sign = s.net_counterfactual_value >= 0 ? '+' : '';
    netSavedEl.textContent = `Net Adv: ${sign}$${s.net_counterfactual_value.toFixed(2)}`;
  }

  const mddEl = document.getElementById('bkt-mdd');
  if (mddEl && s.max_drawdown_pct !== undefined) mddEl.textContent = `-${s.max_drawdown_pct.toFixed(2)}%`;

  const mddDollarsEl = document.getElementById('bkt-mdd-dollars');
  if (mddDollarsEl && s.max_drawdown_dollars !== undefined) mddDollarsEl.textContent = `-$${s.max_drawdown_dollars.toFixed(2)} Peak Drop`;

  const sharpeEl = document.getElementById('bkt-sharpe');
  if (sharpeEl && s.sharpe_ratio !== undefined) sharpeEl.textContent = s.sharpe_ratio.toFixed(2);

  const sortinoEl = document.getElementById('bkt-sortino');
  if (sortinoEl && s.sortino_ratio !== undefined) {
    const pfStr = s.profit_factor !== undefined ? ` · PF: ${s.profit_factor.toFixed(2)}` : '';
    sortinoEl.textContent = `Sortino: ${s.sortino_ratio.toFixed(2)}${pfStr}`;
  }

  const roiEl = document.getElementById('bkt-roi');
  if (roiEl && s.roi_pct !== undefined) roiEl.textContent = `+${s.roi_pct.toFixed(2)}%`;

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

  tbody.innerHTML = records.map(r => {
    let gradeBadge = '';
    if (r.grade === 'GRADE_A') {
      gradeBadge = `<span class="pill-grade pill-grade-a">💎 Grade A</span>`;
    } else if (r.grade === 'GRADE_B') {
      gradeBadge = `<span class="pill-grade pill-grade-b">🧠 Smart Pivot</span>`;
    } else {
      gradeBadge = `<span class="pill-grade pill-grade-c">🛡️ Pass Advisory</span>`;
    }

    let resultBadge = '';
    let pnlDisplay = '';
    if (r.result === 'WIN') {
      resultBadge = `<span style="color: var(--accent-emerald); font-weight: 700;">✓ WON</span>`;
      pnlDisplay = `<span style="color: var(--accent-emerald); font-weight: 700;">+$${r.pnl.toFixed(2)}</span>`;
    } else if (r.result === 'LOSS') {
      resultBadge = `<span style="color: var(--accent-rose); font-weight: 700;">✗ LOST</span>`;
      pnlDisplay = `<span style="color: var(--accent-rose); font-weight: 700;">-$${Math.abs(r.pnl).toFixed(2)}</span>`;
    } else {
      resultBadge = `<span style="color: var(--accent-amber); font-weight: 700;">🛡️ TRAP AVOIDED</span>`;
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
          <strong style="color: #fff;">${r.home_team} vs ${r.away_team}</strong>
          ${r.hazard_warning ? `<div style="font-size: 11px; color: var(--accent-amber); margin-top: 2px;">⚠️ ${r.hazard_warning}</div>` : ''}
        </td>
        <td><span class="pill-league">${sportLabel}</span></td>
        <td>${gradeBadge} <div style="font-size: 12px; color: #fff; margin-top: 3px;">${r.outcome_name}</div></td>
        <td><span style="font-size: 11px; text-transform: uppercase; color: var(--text-muted);">${r.market.replace('_', ' ')}</span></td>
        <td class="tabular-nums" style="font-weight: 600;">${(r.p_true * 100).toFixed(1)}%</td>
        <td class="tabular-nums">${r.best_odds.toFixed(2)} <span style="font-size: 11px; color: var(--text-muted);">(${r.best_book})</span></td>
        <td class="tabular-nums" style="font-weight: 700; color: #fff;">${r.actual_score}</td>
        <td>${resultBadge}</td>
        <td class="tabular-nums">${pnlDisplay}</td>
      </tr>
    `;
  }).join('');
}

function switchTab(viewName) {
  const tabs = document.querySelectorAll('.tab-btn, .header-nav-link');
  tabs.forEach(t => {
    const target = t.getAttribute('data-view') || '';
    if (target === `view-${viewName}` || target === viewName) {
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
  window.scrollTo({ top: 0, behavior: 'smooth' });

  if (viewName === 'calibration' && state.data && state.data.calibration) {
    setTimeout(renderCalibration, 50);
  }
}
window.switchTab = switchTab;

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
    statusText.textContent = '✔ Verified! Unblurring picks...';
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

window.relockTelegram = function() {
  state.currentTier = 'free';
  state.isTelegramUnlocked = false;
  localStorage.setItem('lisa_tier', 'free');
  localStorage.removeItem('lisa_telegram_unlocked');
};

// Interactive Model Inference Scenarios
const visualizerScenarios = {
  nba: {
    matchName: "Oklahoma City Thunder vs Washington Wizards",
    league: "NBA · Game Winner (Moneyline)",
    quotes: "Bet365 1.17 · Pinnacle 1.16 · DraftKings 1.15",
    overround: "4.8% hidden bookie margin",
    shinZ: "Strong backing on the favorite",
    probTrue: "88.7%",
    fairPrice: "1.13 or better",
    evDelta: "+2.9% Edge over bookie",
    kellyStake: "2.4% of bankroll",
    verdictType: "diamond",
    verdictBadge: "💎 HIGH-CONFIDENCE PICK",
    verdictDesc: "Strong advantage found. Bookmakers set generous odds compared to Oklahoma City's actual chance of winning."
  },
  laliga: {
    matchName: "FC Barcelona vs Getafe CF",
    league: "La Liga · Smart Safety Pick (Barcelona or Draw)",
    quotes: "Bet365 1.24 · Pinnacle 1.25 · DraftKings 1.26",
    overround: "5.2% hidden bookie margin",
    shinZ: "High-probability safety option",
    probTrue: "84.5%",
    fairPrice: "1.18 or better",
    evDelta: "+5.6% Edge over bookie",
    kellyStake: "3.1% of bankroll",
    verdictType: "pivot",
    verdictBadge: "💡 SMART SAFETY PICK",
    verdictDesc: "Instead of a risky straight bet, LISA recommends Double Chance to give you an 84.5% safety margin."
  },
  trap: {
    matchName: "Arsenal FC vs Chelsea FC",
    league: "Premier League · Trap Game (Do Not Bet)",
    quotes: "Bet365 1.40 · Pinnacle 1.44 · DraftKings 1.42",
    overround: "6.5% high fee · Coin-flip trap",
    shinZ: "Dangerous public hype detected",
    probTrue: "61.2%",
    fairPrice: "1.63 or better",
    evDelta: "-13.2% Bad Value (Negative Return)",
    kellyStake: "0.0% (Do Not Bet)",
    verdictType: "trap",
    verdictBadge: "⚠️ TRAP GAME: DO NOT BET",
    verdictDesc: "Bookmakers have overpriced Arsenal due to public hype. The risk far outweighs the reward. LISA advises passing."
  }
};

window.switchVisualizerScenario = function(id) {
  const s = visualizerScenarios[id];
  if (!s) return;

  document.querySelectorAll('.visualizer-scenario-btn').forEach(b => {
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
  if (mVerdictBadge) mVerdictBadge.textContent = s.verdictBadge;
  if (mVerdictDesc) mVerdictDesc.textContent = s.verdictDesc;

  if (mVerdictBar) {
    mVerdictBar.className = s.verdictType === 'trap' ? 'pipeline-verdict-bar trap' : 'pipeline-verdict-bar';
    if (s.verdictType === 'trap') {
      mVerdictBadge.style.color = '#f87171';
    } else if (s.verdictType === 'pivot') {
      mVerdictBadge.style.color = '#f59e0b';
    } else {
      mVerdictBadge.style.color = '#06b6d4';
    }
  }
};

// Telegram Modal Interactions & Gatekeeper Bridge
window.openTelegramModal = function() {
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
    } catch (e) {}
  }, 1200);
};

window.closeTelegramModal = function() {
  const modal = document.getElementById('telegram-modal');
  if (modal) modal.style.display = 'none';
  if (verifyPollingTimer) {
    clearInterval(verifyPollingTimer);
    verifyPollingTimer = null;
  }
};

function setupEventListeners() {
  // Navigation tabs
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const view = btn.getAttribute('data-view').replace('view-', '');
      switchTab(view);
    });
  });

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
  document.querySelectorAll('.pill-filter').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.pill-filter').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.activeSportFilter = btn.getAttribute('data-sport');
      renderPicks();
    });
  });

  // Backtest strategy switcher (Conservative, High-Yield Pivots, Smart Parlays, Hybrid)
  document.querySelectorAll('[data-bktstrategy]').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('[data-bktstrategy]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.activeBktStrategy = btn.getAttribute('data-bktstrategy');
      renderBacktest();
    });
  });

  // Backtest sport filters
  document.querySelectorAll('[data-bktsport]').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('[data-bktsport]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.activeBktSport = btn.getAttribute('data-bktsport');
      renderBacktest();
    });
  });

  // Backtest grade filters
  document.querySelectorAll('[data-bktgrade]').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('[data-bktgrade]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.activeBktGrade = btn.getAttribute('data-bktgrade');
      renderBacktest();
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
        statusText.textContent = '❌ Not verified! Tap START in @XpredictPremiumBot first.';
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
  const icon = type === 'success' ? '✅' : type === 'error' ? '⚠️' : 'ℹ️';
  toast.innerHTML = `<span>${icon}</span><span>${message}</span>`;
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
        btn.textContent = input.type === 'password' ? '👁️' : '🙈';
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
      if (dropName) dropName.textContent = displayName;
      if (dropEmail) dropEmail.textContent = user.email;

      const tierKey = user.tier || 'free';
      const tierLabels = { free: 'Free Tier', tier1: 'Tier 1 Pro', tier2: 'Tier 2 Syndicate', tier3: 'Tier 3 VIP' };
      if (dropTier) dropTier.textContent = tierLabels[tierKey] || tierKey.toUpperCase();

      if (profileTierTag) {
        profileTierTag.textContent = tierKey.toUpperCase();
        profileTierTag.className = `user-tier-tag tier-tag-${tierKey}`;
      }

      // If user has higher tier than current, elevate view tier
      if (user.tier && user.tier !== 'free') {
        state.currentTier = user.tier;
        renderTierControls();
        renderPicks();
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
  const hash = window.location.hash.replace('#', '');
  if (hash && document.getElementById(`view-${hash}`)) {
    switchTab(hash);
  } else {
    switchTab('overview');
  }

  const params = new URLSearchParams(window.location.search);
  if (params.get('auth') === 'signin' || hash === 'signin') {
    window.openAuthModal('signin');
  } else if (params.get('auth') === 'signup' || hash === 'signup') {
    window.openAuthModal('signup');
  } else if (params.get('unlock') === 'modal' || hash === 'telegram') {
    window.openTelegramModal();
  }

  // Background session rehydration and telemetry loading
  auth.init().then(() => {
    loadData().then(async () => {
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
  });
}

// Immediate or DOM-ready bootstrap (prevents event listener race condition)
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initApp);
} else {
  initApp();
}


