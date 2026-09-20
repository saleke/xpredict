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
}
