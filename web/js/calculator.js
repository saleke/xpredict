/**
 * Interactive Fractional Kelly Staking & Bankroll Calculator
 * Real-time math engine with preset quick-chips, sliders, dynamic ROI simulator,
 * and high-DPI 30-wager bankroll growth projection curve.
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
      advice: 'Pass: Non-positive expected value (no mathematical edge detected)',
    };
  }

  const fullKelly = ev / b;
  const dispShrinkage = Math.max(0.2, 1.0 - (cv / 0.10));
  const targetFraction = fullKelly * fraction * dispShrinkage;
  const stakeFraction = Math.min(maxCap, Math.max(0, targetFraction));

  const stakeAmount = bankroll * stakeFraction;
  const units = stakeFraction * 100;

  let advice = 'Standard Execution: Allocate safe mathematical stake';
  if (stakeFraction >= 0.03) {
    advice = `Prime Alpha Entry: High structural conviction allocation (${units.toFixed(1)} units)`;
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

export function renderGrowthSimulation(canvasTarget, initialBankroll = 10000, prob = 0.84, odds = 1.23, stakeFraction = 0.024) {
  const canvas = typeof canvasTarget === 'string' ? document.getElementById(canvasTarget) : canvasTarget;
  if (!canvas) return;

  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const displayWidth = rect.width > 0 ? rect.width : (canvas.width || 420);
  const displayHeight = rect.height > 0 ? rect.height : (canvas.height || 180);

  canvas.width = displayWidth * dpr;
  canvas.height = displayHeight * dpr;
  ctx.resetTransform ? ctx.resetTransform() : ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.scale(dpr, dpr);

  const w = displayWidth;
  const h = displayHeight;
  const pad = { top: 18, right: 20, bottom: 28, left: 52 };
  const plotW = Math.max(10, w - pad.left - pad.right);
  const plotH = Math.max(10, h - pad.top - pad.bottom);

  ctx.clearRect(0, 0, w, h);

  // Simulate 30 sequential wagers under expected value model
  const steps = 30;
  const kellyGrowth = [initialBankroll];
  const flatGrowth = [initialBankroll];
  const flatStake = initialBankroll * 0.02;

  const b = odds - 1.0;
  const safeFraction = Math.max(0.005, Math.min(0.05, stakeFraction));
  const expFactor = 1.0 + (prob * b * safeFraction) - ((1 - prob) * safeFraction);
  const flatExp = (prob * b * flatStake) - ((1 - prob) * flatStake);

  let curKelly = initialBankroll;
  let curFlat = initialBankroll;

  for (let i = 1; i <= steps; i++) {
    curKelly *= Math.max(0.8, expFactor);
    curFlat += flatExp;
    kellyGrowth.push(curKelly);
    flatGrowth.push(curFlat);
  }

  const allVals = [...kellyGrowth, ...flatGrowth, initialBankroll];
  const minVal = Math.min(...allVals) * 0.96;
  const maxVal = Math.max(...allVals) * 1.04;

  // Background grid
  ctx.strokeStyle = 'rgba(148, 163, 184, 0.08)';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 3; i++) {
    const y = pad.top + (plotH / 3) * i;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(pad.left + plotW, y);
    ctx.stroke();

    const val = maxVal - (i / 3) * (maxVal - minVal);
    ctx.fillStyle = '#5C6B82';
    ctx.font = '500 10px "JetBrains Mono", monospace';
    ctx.textAlign = 'right';
    ctx.fillText(`$${Math.round(val).toLocaleString()}`, pad.left - 6, y + 3);
  }

  // Draw Flat line (dashed)
  ctx.setLineDash([3, 3]);
  ctx.strokeStyle = 'rgba(148, 163, 184, 0.45)';
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  flatGrowth.forEach((v, idx) => {
    const x = pad.left + (idx / steps) * plotW;
    const y = pad.top + (1 - (v - minVal) / (maxVal - minVal)) * plotH;
    if (idx === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.setLineDash([]);

  // Draw Kelly Compounding line
  const grad = ctx.createLinearGradient(0, pad.top, 0, pad.top + plotH);
  grad.addColorStop(0, 'rgba(204, 255, 0, 0.22)');
  grad.addColorStop(1, 'rgba(204, 255, 0, 0.00)');

  ctx.beginPath();
  ctx.moveTo(pad.left, pad.top + (1 - (kellyGrowth[0] - minVal) / (maxVal - minVal)) * plotH);
  kellyGrowth.forEach((v, idx) => {
    const x = pad.left + (idx / steps) * plotW;
    const y = pad.top + (1 - (v - minVal) / (maxVal - minVal)) * plotH;
    ctx.lineTo(x, y);
  });
  ctx.lineTo(pad.left + plotW, pad.top + plotH);
  ctx.lineTo(pad.left, pad.top + plotH);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  ctx.strokeStyle = '#ccff00';
  ctx.lineWidth = 2.5;
  ctx.beginPath();
  kellyGrowth.forEach((v, idx) => {
    const x = pad.left + (idx / steps) * plotW;
    const y = pad.top + (1 - (v - minVal) / (maxVal - minVal)) * plotH;
    if (idx === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // Legend at bottom
  ctx.font = '600 10px Inter, sans-serif';
  ctx.textAlign = 'left';
  ctx.fillStyle = '#ccff00';
  ctx.fillText('● Fractional Kelly Compounding', pad.left, h - 8);

  ctx.fillStyle = '#9AA8BC';
  ctx.fillText('--- Flat 2% Stake Baseline', pad.left + 170, h - 8);
}

export function initCalculator() {
  const bankrollInput = document.getElementById('calc-bankroll');
  const bankrollSlider = document.getElementById('calc-bankroll-slider');
  const fractionSelect = document.getElementById('calc-fraction');
  const probInput = document.getElementById('calc-prob');
  const probSlider = document.getElementById('calc-prob-slider');
  const oddsInput = document.getElementById('calc-odds');
  const cvInput = document.getElementById('calc-cv');

  const recUnitsEl = document.getElementById('rec-units');
  const recAmountEl = document.getElementById('rec-amount');
  const recAdviceEl = document.getElementById('rec-advice');
  const recEvEl = document.getElementById('rec-ev');

  function update() {
    if (!bankrollInput) return;
    const bankroll = Math.max(10, parseFloat(bankrollInput.value) || 10000);
    const fraction = parseFloat(fractionSelect?.value) || 0.25;
    const prob = (parseFloat(probInput?.value) || 84.8) / 100;
    const odds = Math.max(1.02, parseFloat(oddsInput?.value) || 1.23);
    const cv = (parseFloat(cvInput?.value) || 1.5) / 100;

    // Sync sliders if present
    if (bankrollSlider && bankrollSlider.value != bankroll) {
      bankrollSlider.value = Math.min(bankroll, parseFloat(bankrollSlider.max || 50000));
    }
    if (probSlider && probSlider.value != (prob * 100)) {
      probSlider.value = (prob * 100).toFixed(1);
    }

    const res = computeKelly(prob, odds, bankroll, fraction, 0.05, cv);

    if (recUnitsEl) recUnitsEl.textContent = `${res.units.toFixed(1)} Units`;
    if (recAmountEl) recAmountEl.textContent = `$${res.stakeAmount.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    if (recAdviceEl) recAdviceEl.textContent = res.advice;
    if (recEvEl) recEvEl.textContent = `Expected Value: ${res.ev >= 0 ? '+' : ''}${res.ev.toFixed(1)}% | Full Kelly: ${res.fullKelly.toFixed(1)}%`;

    // Render trajectory projection if canvas is in DOM
    const growthCanvas = document.getElementById('kelly-growth-canvas');
    if (growthCanvas) {
      renderGrowthSimulation(growthCanvas, bankroll, prob, odds, res.stakeFraction / 100);
    }
  }

  // Sliders sync
  if (bankrollSlider && bankrollInput) {
    bankrollSlider.addEventListener('input', () => {
      bankrollInput.value = bankrollSlider.value;
      update();
    });
  }

  if (probSlider && probInput) {
    probSlider.addEventListener('input', () => {
      probInput.value = probSlider.value;
      update();
    });
  }

  // Quick preset bankroll chips
  document.querySelectorAll('.calc-preset-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      document.querySelectorAll('.calc-preset-chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      const val = chip.getAttribute('data-value');
      if (val && bankrollInput) {
        bankrollInput.value = val;
        update();
      }
    });
  });

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
      netEl.style.color = net >= 0 ? 'var(--pos)' : 'var(--neg)';
    }
    if (roiEl) {
      roiEl.textContent = `${roi >= 0 ? '+' : '-'}${Math.abs(roi).toLocaleString('en-US', { maximumFractionDigits: 0 })}%`;
      roiEl.style.color = roi >= 0 ? 'var(--brand-400)' : 'var(--neg)';
    }
    if (adviceEl) {
      if (unitSize >= breakeven) {
        adviceEl.innerHTML = `Breakeven Unit Size: <strong>$${breakeven.toFixed(2)} / unit</strong>. With your $${unitSize.toFixed(0)} unit size, your subscription delivers <strong>${roi.toFixed(0)}% Net ROI</strong> and covers its cost in just <strong>${daysToCover.toFixed(1)} days</strong> of disciplined wagering.`;
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
