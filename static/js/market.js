// HelloCrypto — Market Analysis page (Kanban + scatter)
let _toastTimer = null;
let _scatter    = null;
let _coinsByKey = {};

function escHtml(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function shortSym(s) { return String(s||'').replace(/USDC$|USDT$/,''); }
function fmtPct(v, digits=1) { if (v == null || isNaN(v)) return '—'; const sign = v > 0 ? '+' : ''; return `${sign}${Number(v).toFixed(digits)}%`; }
function fmtPrice(v) {
  if (v == null || isNaN(v)) return '—';
  const n = Number(v);
  if (n >= 1000) return `$${n.toLocaleString('en-US', {maximumFractionDigits: 0})}`;
  if (n >= 1)    return `$${n.toLocaleString('en-US', {maximumFractionDigits: 2})}`;
  return `$${n.toFixed(4)}`;
}
function toast(msg, type='ok') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'fixed bottom-5 right-5 px-4 py-3 rounded-xl text-sm font-medium shadow-xl z-50 '
    + (type==='ok' ? 'bg-green-800 text-green-100' : type==='warn' ? 'bg-amber-800 text-amber-100' : 'bg-red-800 text-red-100');
  el.classList.remove('hidden');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.add('hidden'), 3500);
}

function actionOf(coin) {
  const a = String(coin.action || '').toLowerCase();
  if (a === 'buy' || a === 'sell' || a === 'hold') return a;
  // Fallback : déduire de sentiment
  const s = String(coin.sentiment || '').toLowerCase();
  if (/bull|hauss/.test(s)) return 'buy';
  if (/bear|baiss/.test(s)) return 'sell';
  return 'hold';
}

function computeMetrics(coin) {
  const action = actionOf(coin);
  const conf   = Math.max(0, Math.min(10, Number(coin.confidence) || 0));
  // Urgence : -10 (vendre maintenant) ↔ +10 (acheter maintenant)
  let urgency = 0;
  if (action === 'sell') urgency = -conf;
  else if (action === 'buy') urgency = +conf;
  else urgency = 0;

  const cp = Number(coin.current_price) || 0;
  const scenarios = Array.isArray(coin.scenarios) ? coin.scenarios : [];
  const totalP = scenarios.reduce((s, sc) => s + (Number(sc.probability) || 0), 0);

  const expected = (key) => {
    if (!cp || !scenarios.length || totalP <= 0) return null;
    let v = 0;
    for (const sc of scenarios) {
      const p  = (Number(sc.probability) || 0) / totalP;
      const px = Number(sc[key]);
      if (!px) continue;
      v += p * ((px - cp) / cp) * 100;
    }
    return v;
  };

  const e24 = expected('price_24h');
  const e7  = expected('price_7j');
  const e30 = expected('price_30j');

  // Intervalle de confiance à 7j = min/max parmi les scénarios
  let ci_min = null, ci_max = null;
  if (cp && scenarios.length) {
    for (const sc of scenarios) {
      const px = Number(sc.price_7j);
      if (!px) continue;
      const pct = (px - cp) / cp * 100;
      if (ci_min == null || pct < ci_min) ci_min = pct;
      if (ci_max == null || pct > ci_max) ci_max = pct;
    }
  }

  return { action, conf, urgency, e24, e7, e30, ci_min, ci_max };
}

function zoneOf(urgency) {
  if (urgency <= -2) return 'sell';
  if (urgency >=  2) return 'buy';
  return 'hold';
}

async function load() {
  try {
    const list = await fetch('/api/analyses?limit=1').then(r => r.json());
    const latest = Array.isArray(list) && list.length ? list[0] : null;
    render(latest);
  } catch {
    document.getElementById('empty-state').textContent = 'Erreur de chargement';
    document.getElementById('empty-state').classList.remove('hidden');
    document.getElementById('content').classList.add('hidden');
  }
}

function render(a) {
  const empty = document.getElementById('empty-state');
  const content = document.getElementById('content');
  const meta = document.getElementById('latest-meta');

  if (!a || !Array.isArray(a.analyses) || !a.analyses.length) {
    empty.classList.remove('hidden');
    content.classList.add('hidden');
    meta.classList.add('hidden');
    return;
  }

  empty.classList.add('hidden');
  content.classList.remove('hidden');
  meta.classList.remove('hidden');

  // Meta
  const sentEl = document.getElementById('latest-sentiment');
  const sent = String(a.sentiment || '—');
  sentEl.textContent = sent;
  sentEl.className = 'px-2 py-0.5 rounded font-semibold text-xs '
    + (/haussier|bullish/i.test(sent) ? 'badge-up'
      : /baissier|bearish/i.test(sent) ? 'badge-down' : 'badge-flat');
  document.getElementById('latest-ts').textContent = (a.timestamp || '').replace('T',' ').slice(0,16);
  document.getElementById('latest-summary').textContent = a.summary || '';

  // Compute metrics for each coin
  const coins = a.analyses.map(c => {
    const sym = c.symbol || c.sym || '';
    const m   = computeMetrics(c);
    return { ...c, _key: sym, _short: shortSym(sym), _m: m };
  });
  _coinsByKey = {};
  for (const c of coins) _coinsByKey[c._key] = c;

  renderScatter(coins);
  renderKanban(coins);
}

const zonesPlugin = {
  id: 'zones',
  beforeDatasetsDraw(chart) {
    const { ctx, chartArea: ca, scales: { x } } = chart;
    if (!ca) return;
    const xSellEnd = x.getPixelForValue(-2);
    const xBuyStart = x.getPixelForValue(2);
    ctx.save();
    ctx.fillStyle = 'rgba(239,68,68,0.07)';
    ctx.fillRect(ca.left, ca.top, xSellEnd - ca.left, ca.bottom - ca.top);
    ctx.fillStyle = 'rgba(100,116,139,0.05)';
    ctx.fillRect(xSellEnd, ca.top, xBuyStart - xSellEnd, ca.bottom - ca.top);
    ctx.fillStyle = 'rgba(34,197,94,0.07)';
    ctx.fillRect(xBuyStart, ca.top, ca.right - xBuyStart, ca.bottom - ca.top);
    // axe vertical x=0
    const x0 = x.getPixelForValue(0);
    ctx.strokeStyle = 'rgba(148,163,184,0.25)';
    ctx.setLineDash([3,3]);
    ctx.beginPath(); ctx.moveTo(x0, ca.top); ctx.lineTo(x0, ca.bottom); ctx.stroke();
    ctx.restore();
  }
};

const labelsPlugin = {
  id: 'pointLabels',
  afterDatasetsDraw(chart) {
    const ctx = chart.ctx;
    chart.data.datasets.forEach((ds, i) => {
      const meta = chart.getDatasetMeta(i);
      meta.data.forEach((pt, j) => {
        const raw = ds.data[j];
        if (!raw || !raw.label) return;
        ctx.save();
        ctx.font = '600 10px ui-sans-serif, system-ui, -apple-system, sans-serif';
        ctx.fillStyle = '#cbd5e1';
        ctx.textAlign = 'center';
        ctx.fillText(raw.label, pt.x, pt.y - 9);
        ctx.restore();
      });
    });
  }
};

const COLORS = {
  sell: { bg: 'rgba(251,146,60,0.85)', border: '#fb923c' },
  hold: { bg: 'rgba(148,163,184,0.85)', border: '#94a3b8' },
  buy : { bg: 'rgba(52,211,153,0.85)', border: '#34d399' },
};

function renderScatter(coins) {
  const canvas = document.getElementById('scatter-chart');
  if (!canvas) return;

  // Group by zone for distinct legends/colors
  const buckets = { sell: [], hold: [], buy: [] };
  for (const c of coins) {
    const z = zoneOf(c._m.urgency);
    const y = c._m.e7 == null ? 0 : c._m.e7;
    buckets[z].push({ x: c._m.urgency, y, label: c._short, _key: c._key });
  }

  const datasets = ['sell','hold','buy'].map(z => ({
    label: z === 'sell' ? 'Vendre' : z === 'buy' ? 'Acheter' : 'Conserver',
    data: buckets[z],
    backgroundColor: COLORS[z].bg,
    borderColor: COLORS[z].border,
    pointRadius: 7,
    pointHoverRadius: 9,
  }));

  if (_scatter) { _scatter.destroy(); _scatter = null; }

  _scatter = new Chart(canvas.getContext('2d'), {
    type: 'scatter',
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      onClick(evt, items) {
        if (!items?.length) return;
        const it  = items[0];
        const raw = _scatter.data.datasets[it.datasetIndex].data[it.index];
        const coin = _coinsByKey[raw._key];
        if (coin) openModal(coin);
      },
      plugins: {
        legend: { labels: { color: '#cbd5e1', font: { size: 11 } } },
        tooltip: {
          callbacks: {
            title: (items) => items[0].raw.label,
            label: (item) => {
              const r = item.raw;
              const c = _coinsByKey[r._key];
              const m = c?._m;
              return [
                `Urgence: ${r.x.toFixed(1)}`,
                `Potentiel 7j: ${fmtPct(r.y)}`,
                m?.ci_min != null ? `IC 7j: ${fmtPct(m.ci_min)} → ${fmtPct(m.ci_max)}` : '',
              ].filter(Boolean);
            },
          },
        },
      },
      scales: {
        x: {
          min: -10, max: 10,
          title: { display: true, text: 'Urgence d\'action', color: '#94a3b8', font: { size: 11 } },
          ticks: {
            color: '#64748b',
            stepSize: 2,
            callback: (v) => {
              const labels = { '-10': 'Vendre tout', '-6': 'Vendre fort', '-2': 'Léger sell', 0: 'Neutre', 2: 'Léger buy', 6: 'Acheter fort', 10: 'Acheter tout' };
              return labels[String(v)] ?? v;
            },
          },
          grid: { color: 'rgba(148,163,184,0.08)' },
        },
        y: {
          beginAtZero: false,
          suggestedMin: 0,
          title: { display: true, text: 'Potentiel à 7j (%)', color: '#94a3b8', font: { size: 11 } },
          ticks: { color: '#64748b', callback: (v) => `${Number(v).toFixed(1)}%` },
          grid: {
            color: (ctx) => ctx.tick.value === 0 ? 'rgba(148,163,184,0.35)' : 'rgba(148,163,184,0.08)',
            lineWidth: (ctx) => ctx.tick.value === 0 ? 1.5 : 1,
          },
        },
      },
    },
    plugins: [zonesPlugin, labelsPlugin],
  });
}

function renderKanban(coins) {
  const cols = { sell: [], hold: [], buy: [] };
  for (const c of coins) cols[zoneOf(c._m.urgency)].push(c);

  // Tri par urgence : sell = plus négatif d'abord ; buy = plus positif d'abord ; hold = par potentiel
  cols.sell.sort((a, b) => a._m.urgency - b._m.urgency);
  cols.buy .sort((a, b) => b._m.urgency - a._m.urgency);
  cols.hold.sort((a, b) => (b._m.e7 ?? 0) - (a._m.e7 ?? 0));

  for (const z of ['sell','hold','buy']) {
    const el = document.getElementById(`col-${z}`);
    document.getElementById(`col-${z}-count`).textContent = cols[z].length;
    if (!cols[z].length) {
      el.innerHTML = '<p class="text-xs text-slate-600 italic">—</p>';
      continue;
    }
    el.innerHTML = cols[z].map(c => cardHtml(c)).join('');
  }
  // Bind click handlers
  document.querySelectorAll('[data-card-key]').forEach(node => {
    node.addEventListener('click', () => {
      const coin = _coinsByKey[node.getAttribute('data-card-key')];
      if (coin) openModal(coin);
    });
  });
}

function cardHtml(c) {
  const m = c._m;
  const pillCls = m.action === 'sell' ? 'pill-sell' : m.action === 'buy' ? 'pill-buy' : 'pill-hold';
  const pillLabel = m.action === 'sell' ? 'SELL' : m.action === 'buy' ? 'BUY' : 'HOLD';
  const e7str = m.e7 == null ? '—' : fmtPct(m.e7);
  const e7cls = (m.e7 ?? 0) > 0 ? 'text-green-400' : (m.e7 ?? 0) < 0 ? 'text-orange-400' : 'text-slate-400';
  return `<div class="kanban-card" data-card-key="${escHtml(c._key)}">
    <div class="flex items-center justify-between mb-1">
      <span class="sym">${escHtml(c._short)}</span>
      <span class="pill ${pillCls}">${pillLabel}</span>
    </div>
    <div class="flex items-center justify-between meta">
      <span>Conf. ${m.conf}/10 · urg. ${m.urgency.toFixed(1)}</span>
      <span class="${e7cls} font-semibold">${e7str}</span>
    </div>
  </div>`;
}

// ── Modal ─────────────────────────────────────────────────────────────────────
function openModal(coin) {
  const m = coin._m;
  document.getElementById('modal-symbol').textContent = coin._short;
  document.getElementById('modal-price').textContent  = `Prix actuel : ${fmtPrice(coin.current_price)}`;

  const actEl = document.getElementById('modal-action');
  actEl.textContent = m.action.toUpperCase();
  actEl.className = 'px-2 py-1 rounded text-xs font-bold ' + (
    m.action === 'sell' ? 'pill-sell' : m.action === 'buy' ? 'pill-buy' : 'pill-hold');

  document.getElementById('modal-reason').textContent =
    coin.action_reason || coin.summary || '—';

  const setExp = (id, v) => {
    const el = document.getElementById(id);
    el.textContent = v == null ? '—' : fmtPct(v);
    el.className = 'font-semibold mt-1 ' + (
      v == null ? 'text-slate-400' : v > 0 ? 'text-green-400' : v < 0 ? 'text-orange-400' : 'text-slate-300');
  };
  setExp('modal-e24', m.e24);
  setExp('modal-e7',  m.e7);
  setExp('modal-e30', m.e30);

  const ciEl = document.getElementById('modal-ci');
  if (m.ci_min == null || m.ci_max == null) {
    ciEl.textContent = 'Indisponible';
  } else {
    ciEl.innerHTML = `<span class="text-orange-400">${fmtPct(m.ci_min)}</span>`
      + ` <span class="text-slate-500">→</span> `
      + `<span class="text-green-400">${fmtPct(m.ci_max)}</span>`;
  }

  const scWrap = document.getElementById('modal-scenarios-wrap');
  const scList = document.getElementById('modal-scenarios');
  const scs = Array.isArray(coin.scenarios) ? coin.scenarios : [];
  if (!scs.length) {
    scWrap.classList.add('hidden');
  } else {
    scWrap.classList.remove('hidden');
    scList.innerHTML = scs.map(sc => {
      const name = String(sc.name || '').toLowerCase();
      const tag = name === 'bull' ? 'text-green-400' : name === 'bear' ? 'text-orange-400' : 'text-slate-300';
      return `<div class="bg-slate-800/60 rounded-lg p-2 text-xs space-y-1">
        <div class="flex items-center justify-between">
          <span class="font-semibold ${tag} uppercase">${escHtml(sc.name || '—')}</span>
          <span class="text-slate-400">${sc.probability != null ? sc.probability + '%' : ''}</span>
        </div>
        <div class="text-slate-400 grid grid-cols-3 gap-1">
          <span>24h: ${fmtPrice(sc.price_24h)}</span>
          <span>7j: ${fmtPrice(sc.price_7j)}</span>
          <span>30j: ${fmtPrice(sc.price_30j)}</span>
        </div>
        ${sc.trigger ? `<p class="text-slate-300 leading-relaxed">${escHtml(sc.trigger)}</p>` : ''}
      </div>`;
    }).join('');
  }

  document.getElementById('modal').classList.remove('hidden');
}

function closeModal() { document.getElementById('modal').classList.add('hidden'); }
function closeModalBg(e) { if (e.target.id === 'modal') closeModal(); }
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

// ── Run analysis ──────────────────────────────────────────────────────────────
async function runAnalysis() {
  const btn = document.getElementById('run-btn');
  const bar = document.getElementById('status-bar');
  btn.disabled = true; btn.textContent = 'En cours…';
  bar.classList.remove('hidden');
  try {
    const r = await fetch('/api/analysis/start', {method:'POST'});
    if (!r.ok) throw new Error('failed');
    toast('Analyse lancée');
    let tries = 0;
    const iv = setInterval(async () => {
      tries++;
      const s = await fetch('/api/analysis/status').then(r=>r.json()).catch(()=>null);
      if (!s?.running || tries > 60) {
        clearInterval(iv);
        btn.disabled = false; btn.textContent = 'Analyser maintenant';
        bar.classList.add('hidden');
        load();
      }
    }, 2000);
  } catch {
    toast('Erreur lors de l\'analyse','err');
    btn.disabled = false; btn.textContent = 'Analyser maintenant';
    bar.classList.add('hidden');
  }
}

load();
