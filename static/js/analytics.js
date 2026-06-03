// HelloCrypto — Shared analytics module
// Used by both Home and Backtest pages. All renderers take explicit data + DOM IDs,
// no page-specific state.

const HC_CHART_COLORS = ['#60a5fa','#34d399','#f59e0b','#a78bfa','#f87171','#38bdf8','#fb923c','#818cf8','#4ade80','#e879f9'];

// ─── HTTP fetch with TTL cache + in-flight dedup ─────────────────────────────
// Multiple concurrent callers asking for the same URL share a single network
// request. Subsequent calls within `ttlMs` reuse the cached response.
//
// Default TTL = 60s. With this:
//   - Tab switches & repeated reads within a minute → 0 calls (cache hit)
//   - Polling timers shorter than 60s → only fire a network call when the cache
//     actually expires (every ~60s, regardless of polling interval)
//   - State-mutating ops invalidate explicitly via invalidateCache()
//   - Manual refresh buttons pass {force:true} to bypass cache
const _httpCache    = new Map();  // url → {ts, data}
const _httpInflight = new Map();  // url → Promise

const DEFAULT_FETCH_TTL = 60_000;

async function fetchJson(url, ttlMs = DEFAULT_FETCH_TTL, opts = {}) {
  const now = Date.now();
  if (!opts.force) {
    const cached = _httpCache.get(url);
    if (cached && (now - cached.ts) < ttlMs) return cached.data;
    // De-dupe parallel requests to the same URL (only when not forcing fresh)
    const inflight = _httpInflight.get(url);
    if (inflight) return inflight;
  }

  const promise = fetch(url)
    .then(r => r.json())
    .then(d => { _httpCache.set(url, { ts: Date.now(), data: d }); return d; })
    .finally(() => _httpInflight.delete(url));
  if (!opts.force) _httpInflight.set(url, promise);
  return promise;
}

// Invalidate cache entries whose URL starts with `prefix` (or all if empty).
// Call after any state-changing operation (place order, start run, delete, etc.).
function invalidateCache(prefix = '') {
  if (!prefix) { _httpCache.clear(); return; }
  for (const k of [..._httpCache.keys()]) {
    if (k.startsWith(prefix)) _httpCache.delete(k);
  }
}

// ─── Utils ───────────────────────────────────────────────────────────────────
function fmt(n, d=2) { return n == null || isNaN(n) ? '—' : Number(n).toLocaleString('fr-FR', {minimumFractionDigits:d,maximumFractionDigits:d}); }
function fmtPct(n)   { return n == null ? '—' : `${n>=0?'+':''}${fmt(n)}%`; }
function fmtPnl(n)   { if (n == null) return '—'; const sign = n >= 0 ? '+' : '-'; return `${sign}$${fmt(Math.abs(n))}`; }
function fmtQty(n)   { return n == null || isNaN(n) ? '—' : Number(n).toLocaleString('fr-FR', {minimumFractionDigits:0, maximumFractionDigits:6}); }
function shortSym(s) { return String(s||'').replace(/USDC$|USDT$/,''); }
function escHtml(s)  { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function pnlClass(n) { return n == null ? 'text-slate-300' : n >= 0 ? 'pnl-pos' : 'pnl-neg'; }

let _toastTimer = null;
function toast(msg, type='ok') {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.className = 'fixed bottom-5 right-5 px-4 py-3 rounded-xl text-sm font-medium shadow-xl z-50 '
    + (type==='ok' ? 'bg-green-800 text-green-100' : type==='warn' ? 'bg-amber-800 text-amber-100' : 'bg-red-800 text-red-100');
  el.classList.remove('hidden');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.add('hidden'), 3500);
}

// Parse a timestamp from various formats coming from API/snapshot.
function parseTs(s) {
  if (s == null) return null;
  if (typeof s === 'number') return new Date(s);
  let str = String(s);
  // ISO with no TZ → assume UTC
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(str) && !/[Z+]/.test(str)) str += 'Z';
  // "YYYY-MM-DD HH:MM" → ISO + UTC
  else if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(str)) str = str.replace(' ', 'T') + 'Z';
  const d = new Date(str);
  return isNaN(d.getTime()) ? null : d;
}

// ─── Crypto dropdown (shared component) ──────────────────────────────────────
function renderCryptoDrop(dropId, universe, selected, onChange) {
  const root  = document.getElementById(dropId);
  if (!root) return;
  const items = root.querySelector('.cdrop-items');
  items.innerHTML = '';
  for (const sym of universe) {
    const lbl = document.createElement('label');
    lbl.className = 'cdrop-item';
    lbl.innerHTML = `<input type="checkbox" data-sym="${sym}" ${selected.has(sym) ? 'checked' : ''}><span>${shortSym(sym)}</span>`;
    items.appendChild(lbl);
  }
  items.addEventListener('change', () => { _updateCdropSummary(root); if (onChange) onChange(); });
  _updateCdropSummary(root);
}

function _updateCdropSummary(root) {
  const inputs  = root.querySelectorAll('input[type="checkbox"]');
  const checked = root.querySelectorAll('input[type="checkbox"]:checked');
  const sum     = root.querySelector('.cdrop-summary');
  if (checked.length === inputs.length) sum.textContent = `Toutes (${inputs.length})`;
  else if (checked.length === 0) sum.textContent = 'Aucune';
  else if (checked.length <= 3) sum.textContent = [...checked].map(c => shortSym(c.dataset.sym)).join(', ');
  else sum.textContent = `${checked.length} sélectionnées`;
}

function toggleCryptoDrop(dropId) {
  const target = document.querySelector(`#${dropId} .cdrop-menu`);
  document.querySelectorAll('.cdrop-menu.open').forEach(m => { if (m !== target) m.classList.remove('open'); });
  target.classList.toggle('open');
}

function cdropSelectAll(dropId, onChange) {
  document.querySelectorAll(`#${dropId} input[type="checkbox"]`).forEach(c => c.checked = true);
  _updateCdropSummary(document.getElementById(dropId));
  if (onChange) onChange();
}
function cdropSelectNone(dropId, onChange) {
  document.querySelectorAll(`#${dropId} input[type="checkbox"]`).forEach(c => c.checked = false);
  _updateCdropSummary(document.getElementById(dropId));
  if (onChange) onChange();
}
function getCryptoSelection(dropId) {
  return [...document.querySelectorAll(`#${dropId} input[type="checkbox"]:checked`)].map(c => c.dataset.sym);
}

document.addEventListener('click', (e) => {
  if (!e.target.closest('.cdrop')) document.querySelectorAll('.cdrop-menu.open').forEach(m => m.classList.remove('open'));
});

// ─── KPI setters ─────────────────────────────────────────────────────────────
// Tint a `.kpi-hero` parent (pos/neg classes drive background+border colors).
function setHeroColor(parentId, val) {
  const el = document.getElementById(parentId);
  if (!el) return;
  el.classList.remove('pos', 'neg');
  if (val == null) return;
  el.classList.add(val >= 0 ? 'pos' : 'neg');
}

// Update a hero KPI value + its optional sub-text, applying pnl-pos/pnl-neg.
function setHeroVal(elId, val, str, subStr) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.textContent = str;
  el.classList.remove('pnl-pos', 'pnl-neg');
  if (val != null) el.classList.add(val >= 0 ? 'pnl-pos' : 'pnl-neg');
  if (subStr !== undefined) {
    const parent = el.closest('.kpi-hero');
    const sub = parent?.querySelector('.kpi-hero-sub');
    if (sub) {
      sub.textContent = subStr;
      sub.classList.remove('pnl-pos', 'pnl-neg');
      if (val != null) sub.classList.add(val >= 0 ? 'pnl-pos' : 'pnl-neg');
    }
  }
}

// Reconstruct a [{ts, v}] timeseries of TOTAL portfolio value from trade history.
// Each point's v = cash + Σ(qty × last_known_price). Approximate between trades
// (uses last seen price per symbol), useful for past sim sessions and real mode.
// Like strategyTimeseriesFromHistory but emits one point per *decision cycle*
// rather than per trade. cycleTimestamps is the chronological list of every
// cycle the agent ran (sourced from price_snapshots WHERE session_id=X).
// Trades happen *at* cycle boundaries (the agent decides then executes), so
// for each cycle we apply every trade up to that timestamp, then snapshot
// the wallet value. Result: a flat-at-zero curve still has 185 points, not 1.
function strategyTimeseriesFromCycles(history, cycleTimestamps, budget) {
  if (!Array.isArray(cycleTimestamps) || !cycleTimestamps.length) {
    return strategyTimeseriesFromHistory(history, budget);
  }
  const trades = (history || [])
    .filter(t => t.timestamp && t.action && t.action !== 'ANALYSE')
    .sort((a, b) => String(a.timestamp).localeCompare(String(b.timestamp)));

  let cash = budget;
  const positions = {};
  const lastPrice = {};
  const points = [];
  let ti = 0;
  const applyTrade = (t) => {
    if (!t.symbol) return;
    if (t.price) lastPrice[t.symbol] = t.price;
    const qty    = Number(t.qty) || 0;
    const amount = t.amount != null ? Number(t.amount) : qty * (t.price || 0);
    if (/BUY/i.test(t.action))       { cash -= amount; positions[t.symbol] = (positions[t.symbol] || 0) + qty; }
    else if (/SELL/i.test(t.action)) { cash += amount; positions[t.symbol] = (positions[t.symbol] || 0) - qty; }
    if (positions[t.symbol] != null && positions[t.symbol] <= 1e-8) delete positions[t.symbol];
  };
  const snapshotV = () => {
    let posVal = 0;
    for (const [sym, q] of Object.entries(positions)) {
      if (lastPrice[sym]) posVal += q * lastPrice[sym];
    }
    return cash + posVal;
  };

  for (const cycleTs of cycleTimestamps) {
    while (ti < trades.length && trades[ti].timestamp <= cycleTs) {
      applyTrade(trades[ti]);
      ti++;
    }
    points.push({ ts: cycleTs, v: snapshotV() });
  }
  // Catch any trades that happened after the last recorded cycle timestamp
  while (ti < trades.length) {
    applyTrade(trades[ti]);
    points.push({ ts: trades[ti].timestamp, v: snapshotV() });
    ti++;
  }
  return points;
}

function strategyTimeseriesFromHistory(history, budget) {
  if (!Array.isArray(history) || !history.length) return [];
  const sorted = history
    .filter(t => t.timestamp && t.action && t.action !== 'ANALYSE')
    .sort((a, b) => String(a.timestamp).localeCompare(String(b.timestamp)));
  let cash = budget;
  const positions = {};
  const lastPrice = {};
  const points = [];
  for (const t of sorted) {
    if (!t.symbol) continue;
    if (t.price) lastPrice[t.symbol] = t.price;
    const qty    = Number(t.qty) || 0;
    const amount = t.amount != null ? Number(t.amount) : qty * (t.price || 0);
    if (/BUY/i.test(t.action))       { cash -= amount; positions[t.symbol] = (positions[t.symbol] || 0) + qty; }
    else if (/SELL/i.test(t.action)) { cash += amount; positions[t.symbol] = (positions[t.symbol] || 0) - qty; }
    if (positions[t.symbol] != null && positions[t.symbol] <= 1e-8) delete positions[t.symbol];
    let posVal = 0;
    for (const [sym, q] of Object.entries(positions)) {
      if (lastPrice[sym]) posVal += q * lastPrice[sym];
    }
    points.push({ ts: t.timestamp, v: cash + posVal });
  }
  return points;
}

// Walk *history* (newest-first, as returned by /api/performance) oldest→newest
// and return per-symbol open positions valued at their last-seen trade price.
// Tracks running cost basis so the donut + KPIs can show avg entry too.
function positionsFromHistory(history) {
  const acc = {};            // sym → { qty, cost, lastPrice }
  const sorted = [...(history || [])].reverse();
  for (const t of sorted) {
    if (!t.symbol) continue;
    const sym = t.symbol;
    if (!acc[sym]) acc[sym] = { qty: 0, cost: 0, lastPrice: null };
    if (t.price) acc[sym].lastPrice = t.price;
    const qty    = Number(t.qty) || 0;
    const amount = t.amount != null ? Number(t.amount) : qty * (Number(t.price) || 0);
    if (/BUY/i.test(t.action || '')) {
      acc[sym].qty  += qty;
      acc[sym].cost += amount;
    } else if (/SELL/i.test(t.action || '')) {
      // Reduce cost basis proportionally to qty sold.
      const before = acc[sym].qty;
      if (before > 0) acc[sym].cost *= Math.max(0, before - qty) / before;
      acc[sym].qty -= qty;
    }
  }
  const out = [];
  for (const [sym, p] of Object.entries(acc)) {
    if (p.qty <= 1e-8) continue;
    const price = p.lastPrice || 0;
    out.push({
      symbol:        sym,
      qty:           p.qty,
      avg_price:     p.qty > 0 ? p.cost / p.qty : null,
      current_price: price,
      value:         p.qty * price,
    });
  }
  return out;
}

// Backwards-compatible helper: total unrealized at last known prices.
function unrealizedFromHistory(history) {
  return positionsFromHistory(history).reduce((s, p) => s + (p.value || 0), 0);
}

// ─── KPI setter (secondary KPIs) ─────────────────────────────────────────────
function _setKpi(id, val, valClass='text-slate-300', sub='', subClass='kpi-sub') {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = val;
  el.className   = 'kpi-val ' + (valClass||'text-slate-300');
  const parent = el.closest('.kpi');
  if (!parent) return;
  const subEls = parent.querySelectorAll('.kpi-sub');
  if (sub && subEls[0]) { subEls[0].textContent = sub; if (subClass && subClass!=='kpi-sub') subEls[0].className='kpi-sub '+subClass; }
  if (arguments[4] && subEls[1]) { subEls[1].textContent = arguments[4]; if (arguments[5]) subEls[1].className='kpi-sub '+arguments[5]; }
}

// ─── Time bucketing & filtering ──────────────────────────────────────────────
const PERIOD_MS = {
  '1d':  86400 * 1000,
  '7d':  7  * 86400 * 1000,
  '30d': 30 * 86400 * 1000,
  '90d': 90 * 86400 * 1000,
  'all': Infinity,
};

const GRAN_MS = {
  'minute': 60 * 1000,
  'hour':   3600 * 1000,
  'day':    86400 * 1000,
  'week':   7  * 86400 * 1000,
  'month':  30 * 86400 * 1000,
};

function _bucketTs(date, granularity) {
  const d = new Date(date.getTime());
  if (granularity === 'minute') {
    d.setUTCSeconds(0, 0);
  } else if (granularity === 'hour') {
    d.setUTCMinutes(0, 0, 0);
  } else if (granularity === 'day') {
    d.setUTCHours(0, 0, 0, 0);
  } else if (granularity === 'week') {
    d.setUTCHours(0, 0, 0, 0);
    d.setUTCDate(d.getUTCDate() - d.getUTCDay());
  } else if (granularity === 'month') {
    d.setUTCHours(0, 0, 0, 0);
    d.setUTCDate(1);
  }
  return d;
}

function _formatBucket(date, granularity) {
  const iso = date.toISOString();
  if (granularity === 'minute') return `${iso.slice(5, 10)} ${iso.slice(11, 16)}`;
  if (granularity === 'hour')   return `${iso.slice(5, 10)} ${iso.slice(11, 16)}`;
  if (granularity === 'day')    return iso.slice(5, 10);
  if (granularity === 'week')   return iso.slice(5, 10);
  if (granularity === 'month')  return iso.slice(0, 7);
  return iso.slice(0, 16);
}

// Filter a timeseries (array of {ts, v, ...}) by period, then bucket by granularity.
// Keeps the LAST value within each bucket (so PnL stays cumulative).
function bucketTimeseries(series, period, granularity) {
  if (!Array.isArray(series) || !series.length) return [];
  const cutoff = period === 'all' ? -Infinity : Date.now() - PERIOD_MS[period];
  const filtered = [];
  for (const p of series) {
    const d = parseTs(p.ts);
    if (!d) continue;
    if (d.getTime() < cutoff) continue;
    filtered.push({ ...p, _d: d });
  }
  if (!filtered.length) return [];

  const buckets = new Map();
  for (const p of filtered) {
    const b = _bucketTs(p._d, granularity);
    buckets.set(b.getTime(), { ts: b, label: _formatBucket(b, granularity), ...p });
  }
  return [...buckets.values()].sort((a, b) => a.ts - b.ts);
}

function filterTradesByPeriod(history, period) {
  if (period === 'all') return history;
  const cutoff = Date.now() - PERIOD_MS[period];
  return history.filter(t => {
    const d = parseTs(t.timestamp);
    return d && d.getTime() >= cutoff;
  });
}

// ─── Filter toolbar (granularity + period) ───────────────────────────────────
function renderFilterToolbar(containerId, opts) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const granOpts   = opts.granularity || ['minute','hour','day','week','month'];
  const periodOpts = opts.periods || ['1d','7d','30d','90d','all'];
  const granDef    = opts.granularityDefault || 'day';
  const periodDef  = opts.periodDefault       || 'all';
  const showGran   = opts.showGranularity !== false;

  const granLabels   = {minute:'Min', hour:'Heure', day:'Jour', week:'Semaine', month:'Mois'};
  const periodLabels = {'1d':'24h', '7d':'7j', '30d':'30j', '90d':'90j', 'all':'Tout'};

  el.innerHTML = `
    <div class="flex items-center gap-3 flex-wrap text-xs">
      ${showGran ? `
      <div class="flex items-center gap-1">
        <span class="text-slate-500 mr-1">Granularité</span>
        ${granOpts.map(g => `<button data-gran="${g}" class="fbtn ${g===granDef?'on':''}">${granLabels[g]}</button>`).join('')}
      </div>` : ''}
      <div class="flex items-center gap-1">
        <span class="text-slate-500 mr-1">Période</span>
        ${periodOpts.map(p => `<button data-period="${p}" class="fbtn ${p===periodDef?'on':''}">${periodLabels[p]}</button>`).join('')}
      </div>
    </div>
  `;

  el.dataset.granularity = granDef;
  el.dataset.period      = periodDef;

  el.addEventListener('click', (e) => {
    const t = e.target.closest('button[data-gran], button[data-period]');
    if (!t) return;
    if (t.dataset.gran) {
      el.dataset.granularity = t.dataset.gran;
      el.querySelectorAll('button[data-gran]').forEach(b => b.classList.toggle('on', b === t));
    } else if (t.dataset.period) {
      el.dataset.period = t.dataset.period;
      el.querySelectorAll('button[data-period]').forEach(b => b.classList.toggle('on', b === t));
    }
    if (opts.onChange) opts.onChange(el.dataset.granularity, el.dataset.period);
  });
}

function getFilters(containerId) {
  const el = document.getElementById(containerId);
  if (!el) return { granularity: 'day', period: 'all' };
  return { granularity: el.dataset.granularity || 'day', period: el.dataset.period || 'all' };
}

// ─── PnL cumulative chart with strategy + BH + BTC curves ────────────────────
function renderPnlChart(opts) {
  const canvas = document.getElementById(opts.canvasId);
  const empty  = document.getElementById(opts.emptyId);
  if (!canvas) return;

  const filters = getFilters(opts.filterId);
  const series = bucketTimeseries(opts.series || [], filters.period, filters.granularity);
  if (!series.length) {
    canvas.classList.add('hidden'); empty?.classList.remove('hidden');
    if (opts.chartRef && opts.chartRef.current) { opts.chartRef.current.destroy(); opts.chartRef.current = null; }
    return;
  }
  canvas.classList.remove('hidden'); empty?.classList.add('hidden');

  const budget = opts.budget ?? 0;
  const labels = series.map(p => p.label);

  // Strategy: snapshot.timeseries entries already have absolute total values OR cum deltas.
  // We display strategy as PnL relative to budget when 'mode' is 'absolute' (backtest snapshot, v=total_value)
  // and raw value otherwise (home /api/performance, v=cumulative cashflow).
  const valueMode = opts.valueMode || 'absolute'; // 'absolute' | 'delta'
  const stratPts = series.map(p => valueMode === 'absolute' ? p.v - budget : p.v);

  // pointRadius array: marker only at the LAST data point so the user can
  // read its exact value visually (matches the KPI card). Everywhere else
  // the line stays clean.
  const _endMarker = (data) => data.map((_, i) => i === data.length - 1 ? 3 : 0);

  // Strategy line: small marker at every point (= one decision cycle) so the
  // user sees when each cycle fired, even when the strategy is flat. End
  // point keeps a larger radius for the value-read affordance.
  const _cycleMarkers = (data) => data.map((_, i) => i === data.length - 1 ? 4 : 2);

  // Force the last position of a benchmark series to be the truly-latest
  // bench value, regardless of strat-vs-bench timestamp alignment. The KPI
  // cards use bench[last].v - budget; this guarantees the chart's visible
  // end equals that value too. Other points keep their aligned values.
  const _pinLastTo = (alignedPts, raw, budget) => {
    if (!Array.isArray(alignedPts) || !alignedPts.length || !raw.length) return alignedPts;
    const out = alignedPts.slice();
    const lastV = raw[raw.length - 1].v;
    if (lastV != null) out[out.length - 1] = lastV - budget;
    return out;
  };

  const datasets = [{
    label: 'Stratégie',
    data: stratPts,
    fill: false, tension: 0, pointRadius: _cycleMarkers(stratPts),
    pointBackgroundColor: '#60a5fa', pointBorderColor: '#1e293b', pointBorderWidth: 1,
    borderColor: '#60a5fa', borderWidth: 2,
  }];

  // Bucket benchmark series with same period/granularity
  if (Array.isArray(opts.bhSeries) && opts.bhSeries.length) {
    const bh = bucketTimeseries(opts.bhSeries, filters.period, filters.granularity);
    let bhPts  = _alignToLabels(bh, series, budget, 'absolute');
    bhPts      = _pinLastTo(bhPts, opts.bhSeries, budget);
    datasets.push({
      label: 'Buy & Hold',
      data: bhPts,
      fill: false, tension: 0, pointRadius: _endMarker(bhPts), pointBackgroundColor: '#a78bfa',
      borderColor: '#a78bfa', borderWidth: 2, borderDash: [4, 4],
    });
  } else if (series[0].bh != null) {
    // Backtest case: bh is inline on each strategy point
    const bhPts = series.map(p => p.bh != null ? p.bh - budget : null);
    datasets.push({
      label: 'Buy & Hold',
      data: bhPts,
      fill: false, tension: 0, pointRadius: _endMarker(bhPts), pointBackgroundColor: '#a78bfa',
      borderColor: '#a78bfa', borderWidth: 2, borderDash: [4, 4],
    });
  }

  if (Array.isArray(opts.btcSeries) && opts.btcSeries.length) {
    const btc = bucketTimeseries(opts.btcSeries, filters.period, filters.granularity);
    let btcPts = _alignToLabels(btc, series, budget, 'absolute');
    btcPts     = _pinLastTo(btcPts, opts.btcSeries, budget);
    datasets.push({
      label: 'BTC seul',
      data: btcPts,
      fill: false, tension: 0, pointRadius: _endMarker(btcPts), pointBackgroundColor: '#f59e0b',
      borderColor: '#f59e0b', borderWidth: 2, borderDash: [2, 4],
    });
  } else if (series[0].btc != null) {
    const btcPts = series.map(p => p.btc != null ? p.btc - budget : null);
    datasets.push({
      label: 'BTC seul',
      data: btcPts,
      fill: false, tension: 0, pointRadius: _endMarker(btcPts), pointBackgroundColor: '#f59e0b',
      borderColor: '#f59e0b', borderWidth: 2, borderDash: [2, 4],
    });
  }

  // Lightly fill the area under the strategy line
  const lastVal = stratPts[stratPts.length - 1] ?? 0;
  datasets[0].fill        = 'origin';
  datasets[0].backgroundColor = lastVal >= 0 ? 'rgba(96,165,250,0.08)' : 'rgba(248,113,113,0.08)';

  // Update in place when the chart already exists: avoids destroy/recreate
  // on every poll, which would reflow the canvas and steal scroll position
  // from the user (the backtest polls /api/backtest/status ~1s while a
  // run is in progress).
  const existing = opts.chartRef && opts.chartRef.current;
  if (existing) {
    existing.data.labels   = labels;
    existing.data.datasets = datasets;
    existing.update('none');
    return;
  }

  const chart = new Chart(canvas, {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true, animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: true, labels: { color: '#cbd5e1', font: { size: 11 }, boxWidth: 14 } },
        tooltip: {
          callbacks: { label: ctx => ` ${ctx.dataset.label}: $${fmt(ctx.raw)}` },
          backgroundColor: '#1e293b', borderColor: '#334155', borderWidth: 1,
          titleColor: '#94a3b8', bodyColor: '#e2e8f0',
        },
      },
      scales: {
        x: { ticks: { color: '#475569', maxTicksLimit: 8, font:{size:10} }, grid: { color: '#1e293b' } },
        y: { ticks: { color: '#475569', font:{size:10}, callback: v => `$${v}` }, grid: { color: '#1e293b' } },
      },
    },
  });
  if (opts.chartRef) opts.chartRef.current = chart;
}

// ─── Drawdown chart ──────────────────────────────────────────────────────────
// Displays the running drawdown from the peak: dd_t = value_t − peak_so_far_t (≤ 0).
// Expects opts.series = [{ts, v}] where v is absolute portfolio value.
function renderDrawdownChart(opts) {
  const canvas = document.getElementById(opts.canvasId);
  const empty  = document.getElementById(opts.emptyId);
  if (!canvas) return;

  const filters = opts.filterId ? getFilters(opts.filterId) : { period: 'all', granularity: 'day' };
  const series  = bucketTimeseries(opts.series || [], filters.period, filters.granularity);
  if (!series.length) {
    canvas.classList.add('hidden'); empty?.classList.remove('hidden');
    if (opts.chartRef?.current) { opts.chartRef.current.destroy(); opts.chartRef.current = null; }
    return;
  }
  canvas.classList.remove('hidden'); empty?.classList.add('hidden');

  // Compute drawdown both in absolute USD and in %, plus max DD for the tooltip
  const budget = opts.budget ?? 0;
  let peak = -Infinity;
  const labels = [], ddPts = [], ddPctPts = [];
  let maxDD = 0;
  for (const p of series) {
    if (p.v > peak) peak = p.v;
    const dd     = p.v - peak;
    const ddPct  = peak > 0 ? (p.v - peak) / peak * 100 : 0;
    if (dd < maxDD) maxDD = dd;
    labels.push(p.label);
    ddPts.push(dd);
    ddPctPts.push(ddPct);
  }

  if (opts.chartRef?.current) { opts.chartRef.current.destroy(); opts.chartRef.current = null; }
  const chart = new Chart(canvas, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: 'Drawdown',
        data: ddPts,
        fill: 'origin',
        tension: 0.25,
        pointRadius: 0,
        borderColor: '#f87171',
        borderWidth: 2,
        backgroundColor: 'rgba(248,113,113,0.15)',
      }],
    },
    options: {
      responsive: true, animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const pct = ddPctPts[ctx.dataIndex];
              return ` ${fmtPnl(ctx.raw)}  (${pct.toFixed(1)}%)`;
            },
          },
          backgroundColor: '#1e293b', borderColor: '#334155', borderWidth: 1,
          titleColor: '#94a3b8', bodyColor: '#fca5a5',
        },
      },
      scales: {
        x: { ticks: { color: '#475569', maxTicksLimit: 8, font:{size:10} }, grid: { color: '#1e293b' } },
        y: { ticks: { color: '#475569', font:{size:10}, callback: v => `$${v}` }, grid: { color: '#1e293b' }, max: 0 },
      },
    },
  });
  if (opts.chartRef) opts.chartRef.current = chart;

  // Stash max DD onto the canvas as data attribute for any callers that want to display it
  canvas.dataset.maxDd     = String(maxDD.toFixed(2));
  canvas.dataset.maxDdPct  = budget > 0 ? String((maxDD / budget * 100).toFixed(2)) : '0';
}

// Align a benchmark bucketed series to the strategy buckets (by closest timestamp).
function _alignToLabels(bench, strat, budget, valueMode) {
  if (!bench.length) return strat.map(() => null);
  const out = [];
  let j = 0;
  for (const p of strat) {
    while (j + 1 < bench.length && bench[j + 1].ts <= p.ts) j++;
    const v = bench[j].v;
    out.push(valueMode === 'absolute' ? v - budget : v);
  }
  return out;
}

// ─── Allocation donut ────────────────────────────────────────────────────────
function renderAllocChart(opts) {
  const canvas = document.getElementById(opts.canvasId);
  const empty  = document.getElementById(opts.emptyId);
  const legend = opts.legendId ? document.getElementById(opts.legendId) : null;
  if (!canvas) return;

  const labels = [], values = [], colors = [];
  const cash = opts.cash ?? 0;
  if (cash > 0) { labels.push('Cash'); values.push(cash); colors.push('#60a5fa'); }
  let ci = 1;
  for (const p of (opts.positions || [])) {
    const value = p.value ?? (p.qty * (p.price || p.current_price || 0));
    if (value > 0) {
      labels.push(shortSym(p.symbol));
      values.push(value);
      colors.push(HC_CHART_COLORS[ci++ % HC_CHART_COLORS.length]);
    }
  }

  const total = values.reduce((a, b) => a + b, 0);
  if (!total) {
    canvas.classList.add('hidden'); empty?.classList.remove('hidden');
    if (legend) legend.innerHTML = '';
    if (opts.chartRef && opts.chartRef.current) { opts.chartRef.current.destroy(); opts.chartRef.current = null; }
    return;
  }
  canvas.classList.remove('hidden'); empty?.classList.add('hidden');

  if (opts.chartRef && opts.chartRef.current) { opts.chartRef.current.destroy(); opts.chartRef.current = null; }
  const chart = new Chart(canvas, {
    type: 'doughnut',
    data: { labels, datasets: [{ data: values, backgroundColor: colors, borderWidth: 0 }] },
    options: {
      responsive: true, animation: false, cutout: '62%',
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: { label: ctx => ` ${ctx.label}: $${fmt(ctx.raw)} (${fmt(ctx.raw/total*100,1)}%)` },
          backgroundColor: '#1e293b', borderColor: '#334155', borderWidth: 1,
          titleColor: '#94a3b8', bodyColor: '#e2e8f0',
        },
      },
    },
  });
  if (opts.chartRef) opts.chartRef.current = chart;

  if (legend) {
    legend.innerHTML = labels.map((l, i) =>
      `<div class="flex items-center gap-2">
        <span class="w-2.5 h-2.5 rounded-full shrink-0" style="background:${colors[i]}"></span>
        <span class="text-slate-300">${l}</span>
        <span class="text-slate-400 ml-auto">$${fmt(values[i])}</span>
        <span class="text-slate-600 w-10 text-right">${fmt(values[i]/total*100,1)}%</span>
      </div>`
    ).join('');
  }
}

// ─── PnL bars per crypto ─────────────────────────────────────────────────────
function renderPnlBarsChart(opts) {
  const canvas = document.getElementById(opts.canvasId);
  const empty  = document.getElementById(opts.emptyId);
  if (!canvas) return;

  const pnlMap = {};
  for (const t of (opts.history || [])) {
    if (t.pnl == null || t.action === 'BUY' || t.action === 'HOLD' || t.action === 'ANALYSE') continue;
    const sym = shortSym(t.symbol || '');
    if (sym) pnlMap[sym] = (pnlMap[sym] || 0) + t.pnl;
  }
  for (const p of (opts.positions || [])) {
    const price = p.current_price ?? p.price;
    const entry = p.avg_price ?? p.entry_price;
    const qty   = p.qty ?? 0;
    if (qty > 0 && price && entry > 0) {
      const sym = shortSym(p.symbol);
      pnlMap[sym] = (pnlMap[sym] || 0) + (price - entry) * qty;
    }
  }

  const entries = Object.entries(pnlMap).sort((a, b) => b[1] - a[1]);
  if (!entries.length) {
    canvas.classList.add('hidden'); empty?.classList.remove('hidden');
    if (opts.chartRef && opts.chartRef.current) { opts.chartRef.current.destroy(); opts.chartRef.current = null; }
    return;
  }
  canvas.classList.remove('hidden'); empty?.classList.add('hidden');

  const labels = entries.map(([s]) => s);
  const data   = entries.map(([, v]) => v);
  const bg     = entries.map(([, v]) => v >= 0 ? 'rgba(52,211,153,0.65)' : 'rgba(248,113,113,0.65)');
  const bd     = entries.map(([, v]) => v >= 0 ? '#34d399' : '#f87171');

  // Update in place when the chart already exists: avoids destroy/recreate
  // on every poll, which would reflow the canvas and steal scroll position
  // from the user (each Charts-tab poll is ~1s during a running backtest).
  const existing = opts.chartRef && opts.chartRef.current;
  if (existing) {
    existing.data.labels = labels;
    existing.data.datasets[0].data = data;
    existing.data.datasets[0].backgroundColor = bg;
    existing.data.datasets[0].borderColor     = bd;
    existing.update('none');
    return;
  }

  const chart = new Chart(canvas, {
    type: 'bar',
    data: {
      labels,
      datasets: [{ data, backgroundColor: bg, borderColor: bd, borderWidth: 1, borderRadius: 3 }],
    },
    options: {
      responsive: true, animation: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: { label: ctx => ` PnL: $${fmt(ctx.raw)}` },
          backgroundColor: '#1e293b', borderColor: '#334155', borderWidth: 1,
          titleColor: '#94a3b8', bodyColor: '#e2e8f0',
        },
      },
      scales: {
        x: { ticks: { color: '#475569', font: { size: 10 } }, grid: { color: '#1e293b' } },
        y: { ticks: { color: '#475569', font: { size: 10 }, callback: v => `$${v}` }, grid: { color: '#1e293b' } },
      },
    },
  });
  if (opts.chartRef) opts.chartRef.current = chart;
}

// ─── Volume bars per crypto ──────────────────────────────────────────────────
function renderVolBarsChart(opts) {
  const canvas = document.getElementById(opts.canvasId);
  const empty  = document.getElementById(opts.emptyId);
  if (!canvas) return;

  const volMap = {};
  for (const t of (opts.history || [])) {
    if (t.action === 'ANALYSE') continue;
    const sym = shortSym(t.symbol || '');
    if (!sym) continue;
    const amount = t.amount ?? ((t.qty || 0) * (t.price || 0));
    if (amount > 0) volMap[sym] = (volMap[sym] || 0) + amount;
  }
  const entries = Object.entries(volMap).sort((a, b) => b[1] - a[1]);
  if (!entries.length) {
    canvas.classList.add('hidden'); empty?.classList.remove('hidden');
    if (opts.chartRef && opts.chartRef.current) { opts.chartRef.current.destroy(); opts.chartRef.current = null; }
    return;
  }
  canvas.classList.remove('hidden'); empty?.classList.add('hidden');

  const labels = entries.map(([s]) => s);
  const data   = entries.map(([, v]) => v);

  // Update in place to avoid canvas reflow on every poll (see notes in
  // renderPnlBarsChart).
  const existing = opts.chartRef && opts.chartRef.current;
  if (existing) {
    existing.data.labels = labels;
    existing.data.datasets[0].data = data;
    existing.update('none');
    return;
  }

  const chart = new Chart(canvas, {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        data,
        backgroundColor: 'rgba(96,165,250,0.45)', borderColor: '#60a5fa',
        borderWidth: 1, borderRadius: 3,
      }],
    },
    options: {
      responsive: true, animation: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: { label: ctx => ` Volume: $${fmt(ctx.raw)}` },
          backgroundColor: '#1e293b', borderColor: '#334155', borderWidth: 1,
          titleColor: '#94a3b8', bodyColor: '#e2e8f0',
        },
      },
      scales: {
        x: { ticks: { color: '#475569', font: { size: 10 } }, grid: { color: '#1e293b' } },
        y: { ticks: { color: '#475569', font: { size: 10 }, callback: v => `$${v}` }, grid: { color: '#1e293b' } },
      },
    },
  });
  if (opts.chartRef) opts.chartRef.current = chart;
}

// ─── Holdings list ───────────────────────────────────────────────────────────
function renderHoldings(containerId, positions, prices) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const list = Array.isArray(positions)
    ? positions
    : Object.entries(positions || {}).map(([symbol, h]) => ({
        symbol,
        qty: h.qty ?? h,
        avg_price: h.avg_price ?? h.entry_price,
        current_price: (prices || {})[symbol],
        value: (h.qty ?? h) * ((prices || {})[symbol] ?? 0),
      }));
  if (!list.length) { el.innerHTML = '<span class="text-slate-500 text-xs">Aucune position ouverte</span>'; return; }
  el.innerHTML = list.map(p => {
    const price = p.current_price ?? p.price ?? 0;
    const entry = p.avg_price ?? p.entry_price ?? price;
    const pnl   = price && entry > 0 ? (price - entry) * p.qty : 0;
    return `<div class="flex items-center justify-between py-1.5 border-b border-slate-800 text-xs">
      <span class="text-slate-300 font-semibold w-12">${shortSym(p.symbol)}</span>
      <span class="text-slate-400">${fmt(p.qty,4)} u</span>
      <span class="text-slate-400">$${fmt(p.value ?? (p.qty * price))}</span>
      <span class="${pnlClass(pnl)}">${fmtPnl(pnl)}</span>
    </div>`;
  }).join('');
}

// ─── Per-table symbol filter (compact multi-select dropdown) ─────────────────
// Persists selection in the container's dataset so it survives re-renders.
// Empty dataset.symbols == "all selected" — auto-grows when a new symbol
// appears in history.
//
// Idempotent: re-rendering with the same {symbols, selection} state is a
// no-op. This matters because backtest.js polls /api/backtest/status every
// 1s and calls renderTradesTable on every poll; without idempotence the
// dropdown DOM would be torn down and rebuilt 60×/min.
const _symFilterClickListenerAttached = { v: false };
function _renderSymbolFilter(containerId, allSymbols, selectedSet, onChange) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (!allSymbols.length) {
    if (el.innerHTML !== '') el.innerHTML = '';
    el.dataset.symbols = '';
    return;
  }
  const allOn = selectedSet.size === allSymbols.length;
  el.dataset.symbols = allOn ? '' : [...selectedSet].join(',');

  // Skip the DOM write when the rendered state hasn't changed.
  const sig = `${allSymbols.length}|${allOn ? '*' : [...selectedSet].sort().join(',')}|${allSymbols.join(',')}`;
  if (el.dataset.sig === sig) return;
  el.dataset.sig = sig;
  // Preserve open-state across rebuilds so toggling a checkbox doesn't
  // close the dropdown.
  const wasOpen = !!el.querySelector('.symfilter-menu.open');

  const label = allOn
    ? `Toutes (${allSymbols.length})`
    : selectedSet.size === 0 ? 'Aucune' : `${selectedSet.size}/${allSymbols.length}`;
  const items = allSymbols.map(s => {
    const on = selectedSet.has(s);
    return `<label class="symfilter-item">
      <input type="checkbox" data-sym="${escHtml(s)}" ${on ? 'checked' : ''}>
      <span>${escHtml(shortSym(s))}</span>
    </label>`;
  }).join('');
  el.innerHTML = `<div class="symfilter">
    <button type="button" class="symfilter-btn" data-toggle>Cryptos · ${label} <span class="text-slate-500">▾</span></button>
    <div class="symfilter-menu">
      <div class="symfilter-actions">
        <button type="button" class="symfilter-action" data-action="all">Tout</button>
        <button type="button" class="symfilter-action" data-action="none">Aucune</button>
      </div>
      ${items}
    </div>
  </div>`;

  const menu = el.querySelector('.symfilter-menu');
  if (wasOpen) menu.classList.add('open');
  el.onclick = (e) => {
    if (e.target.closest('[data-toggle]')) {
      menu.classList.toggle('open');
      return;
    }
    const action = e.target.closest('[data-action]')?.dataset.action;
    if (action === 'all')  { selectedSet.clear(); allSymbols.forEach(s => selectedSet.add(s)); }
    if (action === 'none') { selectedSet.clear(); }
    if (action) {
      _renderSymbolFilter(containerId, allSymbols, selectedSet, onChange);
      if (onChange) onChange();
      return;
    }
    const cb = e.target.closest('input[type="checkbox"][data-sym]');
    if (cb) {
      const sym = cb.dataset.sym;
      if (cb.checked) selectedSet.add(sym); else selectedSet.delete(sym);
      _renderSymbolFilter(containerId, allSymbols, selectedSet, onChange);
      if (onChange) onChange();
    }
  };

  // Single outside-click listener for all dropdowns on the page.
  if (!_symFilterClickListenerAttached.v) {
    document.addEventListener('click', (e) => {
      document.querySelectorAll('.symfilter-menu.open').forEach(m => {
        if (!m.parentElement.contains(e.target)) m.classList.remove('open');
      });
    });
    _symFilterClickListenerAttached.v = true;
  }
}

// ─── Numbered pagination control (compact with ellipses) ─────────────────────
// Idempotent (same signature → no DOM write). « / » jump to first/last page.
function _renderPagination(el, current, totalPages, totalItems, onChange) {
  if (!el) return;
  if (totalPages <= 1) {
    const sig = `single|${totalItems}`;
    if (el.dataset.sig === sig) return;
    el.dataset.sig = sig;
    el.innerHTML = totalItems
      ? `<span class="text-xs text-slate-500">1 page · ${totalItems} trade${totalItems === 1 ? '' : 's'}</span>`
      : '';
    el.onclick = null;
    return;
  }
  const sig = `${current}/${totalPages}/${totalItems}`;
  if (el.dataset.sig === sig) return;
  el.dataset.sig = sig;

  const set = new Set([1, totalPages, current, current - 1, current + 1, 2, totalPages - 1]);
  const pages = [...set].filter(p => p >= 1 && p <= totalPages).sort((a, b) => a - b);
  const items = [];
  let prev = 0;
  for (const p of pages) {
    if (p - prev > 1) items.push('…');
    items.push(p);
    prev = p;
  }
  const btns = items.map(it => {
    if (it === '…') return `<span class="text-slate-600 px-1">…</span>`;
    const on = it === current;
    return `<button data-page="${it}" class="fbtn ${on ? 'on' : ''}">${it}</button>`;
  }).join('');
  const atStart = current === 1;
  const atEnd   = current === totalPages;
  el.innerHTML = `<div class="flex items-center gap-1 flex-wrap text-xs">
    <button data-page="1" class="fbtn" ${atStart ? 'disabled' : ''} title="Première page">«</button>
    <button data-page="${Math.max(1, current - 1)}" class="fbtn" ${atStart ? 'disabled' : ''} title="Page précédente">‹</button>
    ${btns}
    <button data-page="${Math.min(totalPages, current + 1)}" class="fbtn" ${atEnd ? 'disabled' : ''} title="Page suivante">›</button>
    <button data-page="${totalPages}" class="fbtn" ${atEnd ? 'disabled' : ''} title="Dernière page">»</button>
    <span class="text-slate-500 ml-2">${totalPages} pages · ${totalItems} trades</span>
  </div>`;
  el.onclick = (e) => {
    const b = e.target.closest('button[data-page]');
    if (!b || b.hasAttribute('disabled')) return;
    const p = parseInt(b.dataset.page, 10);
    if (p && p !== current) onChange(p);
  };
}

// Client-side fallback (sim/backtest snapshots that aren't in the DB).
function _slicePageFromHistory(history, ctx) {
  const all = (history || []).filter(t =>
    t.action && t.action !== 'ANALYSE' && t.action !== 'HOLD');
  const allSymbols = [...new Set(all.map(t => t.symbol).filter(Boolean))].sort();
  let filtered = filterTradesByPeriod(all, ctx.period);
  if (ctx.symbols && ctx.symbols.length) {
    const set = new Set(ctx.symbols);
    filtered = filtered.filter(t => !t.symbol || set.has(t.symbol));
  }
  filtered.sort((a, b) => String(b.timestamp || '').localeCompare(String(a.timestamp || '')));
  const total = filtered.length;
  const offset = (ctx.page - 1) * ctx.pageSize;
  return {
    trades: filtered.slice(offset, offset + ctx.pageSize),
    total, page: ctx.page, pageSize: ctx.pageSize, symbols: allSymbols,
  };
}

// ─── Trade history with period + symbol filter + pagination ──────────────────
// Two modes (mutually exclusive):
//   - opts.fetcher  → async ({page, pageSize, period, symbols}) → server result
//                     Used for real + live sims (DB-backed).
//   - opts.history  → static array, sliced client-side. Used for backtests
//                     (in-memory snapshot, never hits the DB).
async function renderTradesTable(opts) {
  const list = document.getElementById(opts.containerId);
  const header = opts.headerId ? document.getElementById(opts.headerId) : null;
  if (!list) return;

  const filterEl       = opts.filterId       ? document.getElementById(opts.filterId)       : null;
  const symbolFilterEl = opts.symbolFilterId ? document.getElementById(opts.symbolFilterId) : null;
  const paginationEl   = opts.paginationId   ? document.getElementById(opts.paginationId)   : null;

  const period   = filterEl ? (getFilters(opts.filterId).period || 'all') : 'all';
  const pageSize = opts.pageSize || 100;
  const reqPage  = parseInt(list.dataset.page || '1', 10) || 1;

  const symbolsCsv     = symbolFilterEl?.dataset.symbols || '';
  const explicitSyms   = symbolsCsv ? symbolsCsv.split(',').filter(Boolean) : null;

  // Loading placeholder only on first render; avoid flashing on filter clicks.
  if (!list.dataset.loaded) {
    list.innerHTML = '<p class="text-xs text-slate-600 italic">Chargement…</p>';
  }

  let result;
  try {
    if (opts.fetcher) {
      result = await opts.fetcher({
        page: reqPage, pageSize, period, symbols: explicitSyms,
      });
    } else {
      result = _slicePageFromHistory(opts.history, {
        page: reqPage, pageSize, period, symbols: explicitSyms,
      });
    }
  } catch (e) {
    list.innerHTML = `<p class="text-xs text-red-400">Erreur chargement (${escHtml(e.message || 'inconnu')})</p>`;
    return;
  }
  list.dataset.loaded = '1';

  const trades     = result.trades || [];
  const total      = result.total || 0;
  const currentPg  = result.page || reqPage;
  const allSymbols = result.symbols || [];
  list.dataset.page = String(currentPg);

  // Re-mount chip filter against the up-to-date symbol set.
  if (symbolFilterEl) {
    let selectedSet;
    if (!symbolsCsv) {
      selectedSet = new Set(allSymbols);
    } else {
      const prev = new Set(symbolsCsv.split(',').filter(Boolean));
      selectedSet = new Set(allSymbols.filter(s => prev.has(s)));
      if (!selectedSet.size) selectedSet = new Set(allSymbols);
    }
    _renderSymbolFilter(opts.symbolFilterId, allSymbols, selectedSet, () => {
      list.dataset.page = '1';
      renderTradesTable(opts);
    });
  }

  if (!trades.length) {
    header?.classList.add('hidden');
    if (list.dataset.sig !== 'empty') {
      list.dataset.sig = 'empty';
      list.innerHTML = '<p class="text-xs text-slate-600 italic">Aucun trade dans la période sélectionnée</p>';
    }
    if (paginationEl) {
      if (paginationEl.dataset.sig !== 'empty') {
        paginationEl.dataset.sig = 'empty';
        paginationEl.innerHTML = '';
      }
    }
    return;
  }
  header?.classList.remove('hidden');

  // Skip the heavy innerHTML write when the page contents haven't changed
  // (eg. backtest polls every 1s but the visible page may be stable).
  const rowsSig = `${currentPg}|${total}|${trades.map(t => `${t.id ?? t.timestamp}:${t.cycle ?? ''}`).join(';')}`;
  if (list.dataset.sig !== rowsSig) {
    list.dataset.sig = rowsSig;
    list.innerHTML = trades.map(t => {
      const action = t.action || '';
      const color = /BUY|Acheté/.test(action) ? 'text-green-400'
                  : /SELL|Vendu|stop/i.test(action) ? 'text-orange-400'
                  : 'text-slate-400';
      const pnlVal = t.pnl;
      const pnlStr = pnlVal == null ? '—' : `${pnlVal >= 0 ? '+' : ''}$${fmt(pnlVal)}`;
      const pnlCls = pnlVal == null ? 'text-slate-500' : pnlVal >= 0 ? 'pnl-pos' : 'pnl-neg';
      const reason = (t.reason || '').trim() || '—';
      return `<div class="trade-row">
        <span class="text-slate-500">${(t.timestamp || '').slice(0,16).replace('T',' ')}</span>
        <span class="${color}">${escHtml(action)}</span>
        <span class="text-slate-300">${escHtml(shortSym(t.symbol || ''))}</span>
        <span class="text-slate-400 text-right">${t.price != null ? '$' + fmt(t.price, 4) : '—'}</span>
        <span class="text-slate-400 text-right">${fmtQty(t.qty)}</span>
        <span class="text-slate-400 text-right">${t.amount != null ? '$' + fmt(t.amount) : '—'}</span>
        <span class="${pnlCls} text-right">${pnlStr}</span>
        <span class="text-slate-400 truncate" title="${escHtml(reason)}">${escHtml(reason)}</span>
      </div>`;
    }).join('');
  }

  if (paginationEl) {
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    _renderPagination(paginationEl, currentPg, totalPages, total, (p) => {
      list.dataset.page = String(p);
      renderTradesTable(opts);
    });
  }
}
