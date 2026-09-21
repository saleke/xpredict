/**
 * LISA Canvas Chart Engine - Pure HTML5 Canvas Rendering
 * Provides high-DPI interactive reliability diagram & CLV performance charts.
 */

export function renderReliabilityChart(canvasId, calibrationData) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;

  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();

  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);

  const w = rect.width;
  const h = rect.height;
  const pad = { top: 30, right: 30, bottom: 40, left: 50 };

  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;

  // Clear
  ctx.clearRect(0, 0, w, h);

  // Background grid
  ctx.strokeStyle = 'rgba(255, 255, 255, 0.06)';
  ctx.lineWidth = 1;

  for (let i = 0; i <= 5; i++) {
    const y = pad.top + (plotH / 5) * i;
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(pad.left + plotW, y);
    ctx.stroke();

    const val = (1.0 - (i * 0.2)) * 100;
    ctx.fillStyle = '#64748b';
    ctx.font = '11px Inter, sans-serif';
    ctx.textAlign = 'right';
    ctx.fillText(`${val.toFixed(0)}%`, pad.left - 8, y + 4);
  }

  // X axis labels
  for (let i = 0; i <= 5; i++) {
    const x = pad.left + (plotW / 5) * i;
    const val = (0.7 + i * 0.06) * 100;
    ctx.fillStyle = '#64748b';
    ctx.font = '11px Inter, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(`${val.toFixed(0)}%`, x, h - pad.bottom + 20);
  }

  // 45-degree Perfect Calibration Line
  ctx.setLineDash([4, 4]);
  ctx.strokeStyle = 'rgba(148, 163, 184, 0.4)';
  ctx.beginPath();
  ctx.moveTo(pad.left, pad.top + plotH);
  ctx.lineTo(pad.left + plotW, pad.top);
  ctx.stroke();
  ctx.setLineDash([]);

  // Plot Empirical Bins
  if (calibrationData && calibrationData.bins) {
    const validBins = calibrationData.bins.filter(b => b.count > 0 && b.pred_mean !== null && b.win_rate !== null);

    if (validBins.length > 0) {
      // Connect line
      ctx.strokeStyle = '#06b6d4';
      ctx.lineWidth = 3;
      ctx.beginPath();

      validBins.forEach((b, idx) => {
        // Map 0.70 .. 1.00 to plot coordinates
        const xNorm = Math.max(0, Math.min(1, (b.pred_mean - 0.70) / 0.30));
        const yNorm = Math.max(0, Math.min(1, b.win_rate));

        const px = pad.left + xNorm * plotW;
        const py = pad.top + (1 - yNorm) * plotH;

        if (idx === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      });
      ctx.stroke();

      // Points & Labels
      validBins.forEach(b => {
        const xNorm = Math.max(0, Math.min(1, (b.pred_mean - 0.70) / 0.30));
        const yNorm = Math.max(0, Math.min(1, b.win_rate));

        const px = pad.left + xNorm * plotW;
        const py = pad.top + (1 - yNorm) * plotH;

        // Glowing circle
        ctx.fillStyle = '#10b981';
        ctx.beginPath();
        ctx.arc(px, py, 6, 0, Math.PI * 2);
        ctx.fill();

        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 2;
        ctx.stroke();

        // Label count
        ctx.fillStyle = '#f8fafc';
        ctx.font = 'bold 11px Inter, sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText(`N=${b.count} (${(b.win_rate * 100).toFixed(0)}%)`, px, py - 12);
      });
    }
  }
}
