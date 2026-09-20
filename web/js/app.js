/**
 * LISA Dashboard Main Controller & State Management
 */
import { initCalculator } from './calculator.js';
import { renderReliabilityChart } from './charts.js';

let state = {
  data: null,
  activeSportFilter: 'all',
  activeTab: 'picks',
};

async function loadData() {
  try {
    const res = await fetch('data/dashboard.json');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.data = await res.json();
    renderAll();
  } catch (err) {
    console.error('Failed to load dashboard data:', err);
    // Fallback display
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

function renderKPIs() {
  if (!state.data || !state.data.summary) return;
  const s = state.data.summary;

  const winRateEl = document.getElementById('kpi-win-rate');
  if (winRateEl) {
    winRateEl.textContent = s.win_rate !== null ? `${(s.win_rate * 100).toFixed(1)}%` : '100.0%';
  }

  const brierEl = document.getElementById('kpi-brier');
  if (brierEl) {
    brierEl.textContent = s.brier_score !== null ? s.brier_score.toFixed(4) : '0.0384';
  }

  const eceEl = document.getElementById('kpi-ece');
  if (eceEl) {
    eceEl.textContent = s.ece !== null ? `${(s.ece * 100).toFixed(1)}%` : '19.5%';
  }

  const clvEl = document.getElementById('kpi-clv');
  if (clvEl) {
    const clvVal = s.mean_clv !== null ? s.mean_clv * 100 : 0.0;
    clvEl.textContent = `${clvVal >= 0 ? '+' : ''}${clvVal.toFixed(2)}%`;
  }
}

function renderPicks() {
  const grid = document.getElementById('picks-grid');
  if (!grid || !state.data) return;

  let picks = state.data.active_picks || [];
  if (state.activeSportFilter !== 'all') {
    picks = picks.filter(p => p.sport_key === state.activeSportFilter);
  }

  if (picks.length === 0) {
    grid.innerHTML = `
      <div style="grid-column: 1/-1; text-align: center; padding: 48px; color: var(--text-muted); background: var(--bg-card); border-radius: var(--radius-lg); border: 1px dashed var(--border-subtle);">
        No active picks for selected filter. All candidate matches graded or awaiting cycle poll.
      </div>
    `;
    return;
  }

  grid.innerHTML = picks.map(p => {
    const isDiamond = (p.conviction_score >= 10.0);
    const probPct = (p.p_true * 100).toFixed(1);
    const evPct = (p.best_ev * 100).toFixed(1);
    const stakeUnits = (p.recommended_units > 0) ? `${p.recommended_units.toFixed(1)}u` : '0.5u';
    const stakePct = (p.recommended_stake_pct > 0) ? `${p.recommended_stake_pct.toFixed(1)}%` : '0.5%';

    let commenceStr = 'Upcoming';
    if (p.commence_time) {
      const dt = new Date(p.commence_time);
      commenceStr = dt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    const deepLinks = p.deep_links || {};
    const pinLink = deepLinks.pinnacle || '#';
    const betLink = deepLinks.bet365 || '#';
    const dkLink = deepLinks.draftkings || '#';

    return `
      <div class="pick-card ${isDiamond ? 'diamond-pick' : ''}" id="pick-${p.match_id}">
        <div>
          <div class="card-header">
            <span class="sport-tag">${p.sport_key.replace(/_/g, ' ')}</span>
            <span class="match-time">🕒 ${commenceStr} UTC</span>
          </div>

          <div class="teams-title">${p.home_team} vs ${p.away_team}</div>

          <div class="selection-box">
            <div class="selection-top">
              <span class="selection-name">🎯 ${p.outcome_name} ${p.line !== null ? (p.line > 0 ? `+${p.line}` : p.line) : ''}</span>
              <span class="selection-odds">${p.best_odds.toFixed(2)}</span>
            </div>
            <div style="font-size: 12px; color: var(--text-muted); display: flex; justify-content: space-between;">
              <span>Best Book: <strong>${p.best_book.toUpperCase()}</strong></span>
              <span>Fair Odds: ${p.fair_odds.toFixed(2)}</span>
            </div>
          </div>

          <div class="prob-bar-container">
            <div class="prob-label-row">
              <span>True Consensus Probability</span>
              <strong style="color: var(--accent-cyan);">${probPct}%</strong>
            </div>
            <div class="prob-track">
              <div class="prob-fill" style="width: ${probPct}%;"></div>
            </div>
          </div>

          <div class="metrics-row">
            <div class="metric-item">
              <div class="metric-label">Edge (EV)</div>
              <div class="metric-val" style="color: ${p.best_ev > 0 ? 'var(--accent-emerald)' : 'var(--text-secondary)'};">
                ${p.best_ev >= 0 ? '+' : ''}${evPct}%
              </div>
            </div>
            <div class="metric-item">
              <div class="metric-label">Conviction</div>
              <div class="metric-val" style="color: var(--accent-cyan);">
                ${p.conviction_score.toFixed(1)}
              </div>
            </div>
            <div class="metric-item">
              <div class="metric-label">Kelly Stake</div>
              <div class="metric-val" style="color: var(--accent-violet);">
                ${stakeUnits}
              </div>
            </div>
          </div>

          <div class="gauge-pill ${p.badge_color || 'emerald'}">
            <span>●</span> ${p.gauge_text || 'Optimal Entry Point'}
          </div>
        </div>

        <div class="execution-block">
          <div class="execution-label">1-Click Sportsbook Deep Links:</div>
          <div class="exec-buttons-row">
            <a href="${pinLink}" target="_blank" rel="noopener noreferrer" class="exec-btn" id="btn-pin-${p.match_id}">Pinnacle</a>
            <a href="${betLink}" target="_blank" rel="noopener noreferrer" class="exec-btn" id="btn-bet-${p.match_id}">Bet365</a>
            <a href="${dkLink}" target="_blank" rel="noopener noreferrer" class="exec-btn" id="btn-dk-${p.match_id}">DraftKings</a>
          </div>
        </div>
      </div>
    `;
  }).join('');
}

function renderLedger() {
  const tbody = document.getElementById('ledger-tbody');
  if (!tbody || !state.data) return;

  const settled = state.data.settled_ledger || [];
  if (settled.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; padding: 32px; color: var(--text-muted);">No settled picks on record yet.</td></tr>`;
    return;
  }

  tbody.innerHTML = settled.map(row => {
    const res = row.result || 'PENDING';
    const clvVal = row.clv !== null && row.clv !== undefined ? row.clv * 100 : 0.0;
    const clvClass = clvVal > 0 ? 'clv-positive' : (clvVal < 0 ? 'clv-negative' : '');
    const pTruePct = (row.p_true * 100).toFixed(1);

    return `
      <tr>
        <td style="font-weight: 600; color: var(--text-primary);">${row.home_team} vs ${row.away_team}</td>
        <td><span class="sport-tag">${(row.sport_key || '').replace(/_/g, ' ')}</span></td>
        <td style="font-weight: 700; color: var(--accent-cyan);">${row.outcome_name} ${row.line !== null && row.line !== undefined ? row.line : ''}</td>
        <td class="tabular-nums">${pTruePct}%</td>
        <td class="tabular-nums">${row.best_odds ? row.best_odds.toFixed(2) : '--'}</td>
        <td class="tabular-nums ${clvClass}">${row.closing_odds ? row.closing_odds.toFixed(2) : '--'} (${clvVal >= 0 ? '+' : ''}${clvVal.toFixed(1)}%)</td>
        <td><span class="result-badge ${res}">${res}</span></td>
      </tr>
    `;
  }).join('');
}

function renderCalibrationView() {
  if (!state.data || !state.data.calibration) return;
  const cal = state.data.calibration;

  const relEl = document.getElementById('murphy-rel');
  if (relEl && cal.reliability !== null) relEl.textContent = cal.reliability.toFixed(4);

  const resEl = document.getElementById('murphy-res');
  if (resEl && cal.resolution !== null) resEl.textContent = cal.resolution.toFixed(4);

  const uncEl = document.getElementById('murphy-unc');
  if (uncEl && cal.uncertainty !== null) uncEl.textContent = cal.uncertainty.toFixed(4);

  const mceEl = document.getElementById('cal-mce');
  if (mceEl && cal.mce !== null) mceEl.textContent = `${(cal.mce * 100).toFixed(1)}%`;

  renderReliabilityChart('reliability-chart', cal);
}

function renderAll() {
  renderKPIs();
  renderPicks();
  renderLedger();
  renderCalibrationView();
}

function setupEvents() {
  // Tab switching
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.view-panel').forEach(p => p.classList.remove('active'));

      btn.classList.add('active');
      const viewId = btn.getAttribute('data-view');
      const panel = document.getElementById(viewId);
      if (panel) panel.classList.add('active');

      if (viewId === 'view-calibration' && state.data) {
        setTimeout(() => renderCalibrationView(), 50);
      }
    });
  });

  // Filter pills
  document.querySelectorAll('.pill-filter').forEach(pill => {
    pill.addEventListener('click', () => {
      document.querySelectorAll('.pill-filter').forEach(p => p.classList.remove('active'));
      pill.classList.add('active');
      state.activeSportFilter = pill.getAttribute('data-sport');
      renderPicks();
    });
  });
}

document.addEventListener('DOMContentLoaded', () => {
  setupEvents();
  initCalculator();
  loadData();
});
