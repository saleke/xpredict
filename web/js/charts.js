/**
 * LISA Canvas Chart Engine - Pure HTML5 Canvas Rendering
 * Provides high-DPI interactive reliability diagram & calibration performance charts.
 */

export function renderReliabilityChart(canvasTarget, calibrationData) {
  const canvas = typeof canvasTarget === 'string' ? document.getElementById(canvasTarget) : canvasTarget;
  if (!canvas) return;

  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const displayWidth = rect.width > 0 ? rect.width : (canvas.width || 680);
  const displayHeight = rect.height > 0 ? rect.height : (canvas.height || 340);

  canvas.width = displayWidth * dpr;
  canvas.height = displayHeight * dpr;
  ctx.resetTransform ? ctx.resetTransform() : ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.scale(dpr, dpr);

  const w = displayWidth;
  const h = displayHeight;
  const pad = { top: 32, right: 36, bottom: 44, left: 54 };

  const plotW = Math.max(100, w - pad.left - pad.right);
  const plotH = Math.max(100, h - pad.top - pad.bottom);

  // Clear
  ctx.clearRect(0, 0, w, h);

  // Subtle background glow for plot area
  const bgGrad = ctx.createLinearGradient(0, pad.top, 0, pad.top + plotH);
  bgGrad.addColorStop(0, 'rgba(204, 255, 0, 0.04)');
  bgGrad.addColorStop(1, 'rgba(7, 10, 8, 0.6)');
  ctx.fillStyle = bgGrad;
  ctx.fillRect(pad.left, pad.top, plotW, plotH);

  // Background grid
  ctx.strokeStyle = 'rgba(148, 163, 184, 0.08)';
  ctx.lineWidth = 1;

  for (let i = 0; i <= 5; i++) {
    const y = pad.top + (plotH / 5) * i;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(pad.left + plotW, y);
    ctx.stroke();

    const val = (1.0 - (i * 0.2)) * 100;
    ctx.fillStyle = '#5C6B82';
    ctx.font = '500 11px "JetBrains Mono", monospace';
    ctx.textAlign = 'right';
    ctx.fillText(`${val.toFixed(0)}%`, pad.left - 10, y + 4);
  }

  // X axis labels
  for (let i = 0; i <= 5; i++) {
    const x = pad.left + (plotW / 5) * i;
    const val = (0.7 + i * 0.06) * 100;
    ctx.fillStyle = '#5C6B82';
    ctx.font = '500 11px "JetBrains Mono", monospace';
    ctx.textAlign = 'center';
    ctx.fillText(`${val.toFixed(0)}%`, x, h - pad.bottom + 22);
  }

  // Axis Titles
  ctx.fillStyle = '#9AA8BC';
  ctx.font = '600 10px Inter, sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText('FORECASTED WIN PROBABILITY', pad.left + plotW / 2, h - 8);

  ctx.save();
  ctx.translate(14, pad.top + plotH / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText('ACTUAL WIN RATE', 0, 0);
  ctx.restore();

  // 45-degree Perfect Calibration Line
  ctx.setLineDash([5, 5]);
  ctx.strokeStyle = 'rgba(148, 163, 184, 0.35)';
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(pad.left, pad.top + plotH);
  ctx.lineTo(pad.left + plotW, pad.top);
  ctx.stroke();
  ctx.setLineDash([]);

  // Extract bins whether passed as { bins: [...] } or direct array
  let bins = [];
  if (Array.isArray(calibrationData)) {
    bins = calibrationData;
  } else if (calibrationData && Array.isArray(calibrationData.bins)) {
    bins = calibrationData.bins;
  }

  const validBins = bins.filter(b => b.count > 0 && b.pred_mean !== null && b.win_rate !== null);

  if (validBins.length > 0) {
    // Sort ascending by predicted mean
    validBins.sort((a, b) => a.pred_mean - b.pred_mean);

    const coords = validBins.map(b => {
      const xNorm = Math.max(0, Math.min(1, (b.pred_mean - 0.70) / 0.30));
      const yNorm = Math.max(0, Math.min(1, b.win_rate));
      return {
        x: pad.left + xNorm * plotW,
        y: pad.top + (1 - yNorm) * plotH,
        bin: b
      };
    });

    // Area under the curve
    if (coords.length > 1) {
      const areaGrad = ctx.createLinearGradient(0, pad.top, 0, pad.top + plotH);
      areaGrad.addColorStop(0, 'rgba(204, 255, 0, 0.22)');
      areaGrad.addColorStop(1, 'rgba(204, 255, 0, 0.00)');

      ctx.beginPath();
      ctx.moveTo(coords[0].x, pad.top + plotH);
      coords.forEach(pt => ctx.lineTo(pt.x, pt.y));
      ctx.lineTo(coords[coords.length - 1].x, pad.top + plotH);
      ctx.closePath();
      ctx.fillStyle = areaGrad;
      ctx.fill();
    }

    // Connect line
    ctx.strokeStyle = '#ccff00';
    ctx.lineWidth = 3;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.beginPath();

    coords.forEach((pt, idx) => {
      if (idx === 0) ctx.moveTo(pt.x, pt.y);
      else ctx.lineTo(pt.x, pt.y);
    });
    ctx.stroke();

    // Data points
    coords.forEach(pt => {
      // Glow ring
      ctx.fillStyle = 'rgba(204, 255, 0, 0.35)';
      ctx.beginPath();
      ctx.arc(pt.x, pt.y, 10, 0, Math.PI * 2);
      ctx.fill();

      // Outer border
      ctx.fillStyle = '#070a08';
      ctx.beginPath();
      ctx.arc(pt.x, pt.y, 6, 0, Math.PI * 2);
      ctx.fill();

      // Solid emerald center
      ctx.fillStyle = '#34D399';
      ctx.beginPath();
      ctx.arc(pt.x, pt.y, 4, 0, Math.PI * 2);
      ctx.fill();

      // Label with sample size and empirical win rate
      const labelText = `N=${pt.bin.count} · ${(pt.bin.win_rate * 100).toFixed(0)}%`;
      ctx.font = '600 11px "JetBrains Mono", monospace';
      const textWidth = ctx.measureText(labelText).width;
      const tagY = pt.y > pad.top + 28 ? pt.y - 14 : pt.y + 24;

      // Pill background behind text
      ctx.fillStyle = 'rgba(11, 16, 24, 0.85)';
      ctx.fillRect(pt.x - textWidth / 2 - 6, tagY - 11, textWidth + 12, 18);
      ctx.strokeStyle = 'rgba(148, 163, 184, 0.2)';
      ctx.lineWidth = 1;
      ctx.strokeRect(pt.x - textWidth / 2 - 6, tagY - 11, textWidth + 12, 18);

      // Pill text
      ctx.fillStyle = '#EEF2F8';
      ctx.textAlign = 'center';
      ctx.fillText(labelText, pt.x, tagY + 2);
    });
  }
}
