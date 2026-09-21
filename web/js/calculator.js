/**
 * Interactive Fractional Kelly Staking & Bankroll Calculator
 */

export function computeKelly(p, odds, bankroll = 10000, fraction = 0.25, maxCap = 0.05, cv = 0.02) {
  if (p <= 0 || p >= 1 || odds <= 1.01 || bankroll <= 0) {
    return {
      fullKelly: 0,
      stakeFraction: 0,
      stakeAmount: 0,
      units: 0,
      ev: 0,
      advice: 'Pass: Invalid parameters',
    };
  }

  const b = odds - 1.0;
  const ev = (p * odds) - 1.0;

  if (ev <= 0 || b <= 0) {
    return {
      fullKelly: 0,
      stakeFraction: 0,
      stakeAmount: 0,
      units: 0,
      ev: ev * 100,
      advice: 'Pass: Non-positive expected value (no edge)',
    };
  }

  const fullKelly = ev / b;
  const dispShrinkage = Math.max(0.2, 1.0 - (cv / 0.10));
  const targetFraction = fullKelly * fraction * dispShrinkage;
  const stakeFraction = Math.min(maxCap, Math.max(0, targetFraction));

  const stakeAmount = bankroll * stakeFraction;
  const units = stakeFraction * 100;

  let advice = 'Standard Execution: Allocate safe stake';
  if (stakeFraction >= 0.03) {
    advice = `Prime Alpha Entry: High conviction allocation (${units.toFixed(1)} units)`;
  } else if (stakeFraction < 0.01) {
    advice = `Marginal Edge: Conservative allocation (${units.toFixed(1)} units)`;
  }

  return {
    fullKelly: fullKelly * 100,
    stakeFraction: stakeFraction * 100,
    stakeAmount,
    units,
    ev: ev * 100,
    advice,
  };
}

export function initCalculator() {
  const bankrollInput = document.getElementById('calc-bankroll');
  const fractionSelect = document.getElementById('calc-fraction');
  const probInput = document.getElementById('calc-prob');
  const oddsInput = document.getElementById('calc-odds');
  const cvInput = document.getElementById('calc-cv');

  const recUnitsEl = document.getElementById('rec-units');
  const recAmountEl = document.getElementById('rec-amount');
  const recAdviceEl = document.getElementById('rec-advice');
  const recEvEl = document.getElementById('rec-ev');

  function update() {
    if (!bankrollInput) return;
    const bankroll = parseFloat(bankrollInput.value) || 10000;
    const fraction = parseFloat(fractionSelect.value) || 0.25;
    const prob = (parseFloat(probInput.value) || 80) / 100;
    const odds = parseFloat(oddsInput.value) || 1.35;
    const cv = (parseFloat(cvInput.value) || 2.0) / 100;

    const res = computeKelly(prob, odds, bankroll, fraction, 0.05, cv);

    if (recUnitsEl) recUnitsEl.textContent = `${res.units.toFixed(1)} Units`;
    if (recAmountEl) recAmountEl.textContent = `$${res.stakeAmount.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    if (recAdviceEl) recAdviceEl.textContent = res.advice;
    if (recEvEl) recEvEl.textContent = `Expected Value: ${res.ev >= 0 ? '+' : ''}${res.ev.toFixed(1)}% | Full Kelly: ${res.fullKelly.toFixed(1)}%`;
  }

  [bankrollInput, fractionSelect, probInput, oddsInput, cvInput].forEach(el => {
    if (el) {
      el.addEventListener('input', update);
      el.addEventListener('change', update);
    }
  });

  update();
  initValueMeter();
}

export function initValueMeter() {
  const unitSizeInput = document.getElementById('meter-unit-size');
  const tierSelect = document.getElementById('meter-tier-select');
  const monthlyUnitsInput = document.getElementById('meter-monthly-units');

  const grossEl = document.getElementById('meter-gross-val');
  const feeEl = document.getElementById('meter-fee-val');
  const netEl = document.getElementById('meter-net-val');
  const roiEl = document.getElementById('meter-roi-val');
  const adviceEl = document.getElementById('meter-advice');

  if (!unitSizeInput || !tierSelect || !monthlyUnitsInput) return;

  function updateMeter() {
    const unitSize = Math.max(1, parseFloat(unitSizeInput.value) || 25);
    const fee = parseFloat(tierSelect.value) || 19;
    const monthlyUnits = Math.max(0.1, parseFloat(monthlyUnitsInput.value) || 14.5);

    const gross = unitSize * monthlyUnits;
    const net = gross - fee;
    const roi = fee > 0 ? (net / fee) * 100 : 0;
    const breakeven = fee / monthlyUnits;
    const daysToCover = Math.max(0.1, (fee / (gross / 30)));

    if (grossEl) grossEl.textContent = `+$${gross.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    if (feeEl) feeEl.textContent = `-$${fee.toFixed(2)}`;
    if (netEl) {
      netEl.textContent = `${net >= 0 ? '+' : '-'}$${Math.abs(net).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
      netEl.style.color = net >= 0 ? 'var(--accent-emerald)' : 'var(--accent-rose)';
    }
    if (roiEl) {
      roiEl.textContent = `${roi >= 0 ? '+' : '-'}${Math.abs(roi).toLocaleString('en-US', { maximumFractionDigits: 0 })}%`;
      roiEl.style.color = roi >= 0 ? 'var(--accent-cyan)' : 'var(--accent-rose)';
    }
    if (adviceEl) {
      if (unitSize >= breakeven) {
        adviceEl.innerHTML = `Breakeven Unit Size: <strong>$${breakeven.toFixed(2)} / unit</strong>. With your $${unitSize.toFixed(0)} unit size, your subscription delivers <strong>${roi.toFixed(0)}% ROI</strong> and pays for itself in just <strong>${daysToCover.toFixed(1)} days</strong> of disciplined wagering.`;
      } else {
        adviceEl.innerHTML = `Breakeven Unit Size: <strong>$${breakeven.toFixed(2)} / unit</strong>. Increase unit size above $${breakeven.toFixed(2)} to ensure net compounding returns over subscription cost.`;
      }
    }
  }

  [unitSizeInput, tierSelect, monthlyUnitsInput].forEach(el => {
    el.addEventListener('input', updateMeter);
    el.addEventListener('change', updateMeter);
  });

  updateMeter();
}
