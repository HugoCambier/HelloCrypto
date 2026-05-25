// HelloCrypto — Backtest page (orchestrator; uses shared analytics.js)

const COIN_UNIVERSE = ["BTCUSDC","ETHUSDC","SOLUSDC","XRPUSDC","BNBUSDC","ADAUSDC","AVAXUSDC","DOGEUSDC","LINKUSDC","POLUSDC"];

let _btPollIv  = null;
let _latestSnap = null;

// Chart refs (passed to shared renderers)
const _refs = {
  pnl:   { current: null },
  alloc: { current: null },
  pnlBars: { current: null },
  volBars: { current: null },
};

// ── Right-panel tabs ─────────────────────────────────────────────────────────
function switchRTab(name, btn) {
  ['cockpit','charts'].forEach(t => {
    document.getElementById('rtab-'+t)?.classList.toggle('hidden', t !== name);
  });
  document.querySelectorAll('.rtab[data-rtab]').forEach(b => {
    b.classList.toggle('active', b === btn);
  });
  if (_latestSnap) renderFromSnapshot(_latestSnap);
}

// ── Backtest control ─────────────────────────────────────────────────────────
async function startBacktest() {
  const syms = getCryptoSelection('bt-cryptos-drop');
  if (!syms.length) { toast('Sélectionne au moins une crypto', 'warn'); return; }
  const body = {
    symbols:        syms.join(','),
    days:           Number(document.getElementById('bt-days').value),
    budget:         Number(document.getElementById('bt-budget').value),
    stop_loss_pct:  Number(document.getElementById('bt-sl').value),
    trailing_stop_pct: Number(document.getElementById('bt-ts').value),
    buy_threshold:  Number(document.getElementById('bt-buy-thr').value),
    sell_threshold: Number(document.getElementById('bt-sell-thr').value),
    risk_level:     Number(document.getElementById('bt-risk').value),
    speed:          Number(document.getElementById('bt-speed').value),
  };
  const start = document.getElementById('bt-start').value;
  if (start) body.start_date = start;

  try {
    const r = await fetch('/api/backtest/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'failed');
    toast('Backtest lancé', 'ok');
    document.getElementById('bt-start-btn').disabled = true;
    document.getElementById('bt-stop-btn').disabled  = false;
    startPolling();
  } catch (e) { toast(e.message || 'Erreur', 'err'); }
}

async function stopBacktest() {
  try { await fetch('/api/backtest/stop', { method: 'POST' }); toast('Arrêté', 'warn'); } catch {}
}

async function onSpeedChange(val) {
  document.getElementById('bt-speed-val').textContent = val;
  try {
    await fetch('/api/backtest/speed', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ speed: Number(val) }),
    });
  } catch {}
}

function startPolling() {
  if (_btPollIv) clearInterval(_btPollIv);
  pollStatus();
  _btPollIv = setInterval(pollStatus, 1000);
}

async function pollStatus() {
  try {
    const r = await fetch('/api/backtest/status');
    const d = await r.json();
    renderStatus(d);
    if (!d.running && d.snapshot && _btPollIv) {
      clearInterval(_btPollIv); _btPollIv = null;
      document.getElementById('bt-start-btn').disabled = false;
      document.getElementById('bt-stop-btn').disabled  = true;
    }
  } catch {}
}

// ── Rendering ────────────────────────────────────────────────────────────────
function renderStatus(d) {
  const snap = d.snapshot || {};
  _latestSnap = snap;

  let statusText;
  if (d.loading)       statusText = snap.message || 'Chargement des données Binance…';
  else if (d.running)  statusText = 'En cours…';
  else if (snap.error) statusText = 'Erreur : ' + snap.error;
  else if (snap.current_step) statusText = 'Terminé';
  else                 statusText = 'En attente…';
  document.getElementById('bt-status').textContent = statusText;

  const cur = snap.current_step ?? snap.cycle ?? 0;
  const tot = snap.total_steps ?? 0;
  const pct = tot > 0 ? Math.round(100 * cur / tot) : 0;
  document.getElementById('bt-progress').style.width = pct + '%';
  document.getElementById('bt-step').textContent = tot > 0 ? `${cur} / ${tot} (${pct}%)` : '—';

  document.getElementById('bt-start-ts').textContent = snap.start_ts || '—';

  const skippedEl = document.getElementById('bt-skipped');
  if (Array.isArray(snap.skipped_symbols) && snap.skipped_symbols.length) {
    skippedEl.textContent = `Exclu(s) (données insuffisantes) : ${snap.skipped_symbols.map(shortSym).join(', ')}`;
    skippedEl.classList.remove('hidden');
  } else {
    skippedEl.classList.add('hidden');
  }

  renderFromSnapshot(snap);
}

function renderFromSnapshot(snap) {
  if (snap.budget != null) renderKpis(snap);

  // PnL chart (strategy + BH + BTC; backtest has all inline in timeseries)
  renderPnlChart({
    canvasId: 'pnl-chart',
    emptyId:  'chart-empty',
    filterId: 'pnl-filters',
    chartRef: _refs.pnl,
    series:   snap.timeseries || [],
    budget:   snap.budget ?? 0,
    valueMode: 'absolute',
  });

  renderHoldings('holdings-list', snap.positions || []);

  renderTradesTable({
    containerId: 'bt-trades-list',
    headerId:    'bt-trades-header',
    filterId:    'trades-filters',
    history:     snap.history || [],
    limit:       100,
  });

  // Charts tab
  if (!document.getElementById('rtab-charts').classList.contains('hidden')) {
    renderAllocChart({
      canvasId: 'alloc-chart', emptyId: 'alloc-empty', legendId: 'alloc-legend',
      chartRef: _refs.alloc,
      cash: snap.cash ?? 0, positions: snap.positions || [],
    });
    renderPnlBarsChart({
      canvasId: 'pnl-bars-chart', emptyId: 'pnl-bars-empty',
      chartRef: _refs.pnlBars,
      history: snap.history || [], positions: snap.positions || [],
    });
    renderVolBarsChart({
      canvasId: 'vol-bars-chart', emptyId: 'vol-bars-empty',
      chartRef: _refs.volBars,
      history: snap.history || [],
    });
  }
}

function renderKpis(snap) {
  const budget = snap.budget ?? 0;
  const total  = snap.total ?? snap.total_value ?? budget;
  const pnl    = snap.pnl ?? (total - budget);
  const pnlPct = snap.pnl_pct ?? (budget > 0 ? pnl/budget*100 : 0);

  _setKpi('kpi-budget', `$${fmt(budget)}`);
  _setKpi('kpi-total',  `$${fmt(total)}`, pnlClass(pnl));
  _setKpi('kpi-pnl',    fmtPnl(pnl), pnlClass(pnl), fmtPct(pnlPct));
  _setKpi('kpi-winrate',
    snap.win_rate != null ? `${fmt(snap.win_rate)}%` : '—',
    snap.win_rate >= 50 ? 'pnl-pos' : (snap.win_rate != null ? 'pnl-neg' : 'text-slate-300'),
    `${snap.trades_count ?? snap.trades ?? 0} trades`);

  const alphaEl = document.getElementById('kpi-alpha');
  if (alphaEl) {
    alphaEl.textContent = snap.alpha != null ? fmtPnl(snap.alpha) : '—';
    alphaEl.className   = 'kpi-val ' + pnlClass(snap.alpha);
  }

  const btcEl = document.getElementById('kpi-btc-bh');
  if (btcEl) {
    const diff = (snap.pnl != null && snap.btc_bh_pnl != null) ? snap.pnl - snap.btc_bh_pnl : null;
    btcEl.textContent = diff != null ? fmtPnl(diff) : '—';
    btcEl.className   = 'kpi-val ' + pnlClass(diff);
  }

  const sells = (snap.history || []).filter(t => t.pnl != null);
  const best  = sells.length ? Math.max(...sells.map(t => t.pnl)) : null;
  const worst = sells.length ? Math.min(...sells.map(t => t.pnl)) : null;
  _setKpi('kpi-best', best != null ? fmtPnl(best) : '—', pnlClass(best),
          worst != null ? fmtPnl(worst) : '—', pnlClass(worst));
}

// ── Boot ─────────────────────────────────────────────────────────────────────
(async () => {
  renderCryptoDrop('bt-cryptos-drop', COIN_UNIVERSE, new Set(COIN_UNIVERSE));

  renderFilterToolbar('pnl-filters', {
    granularityDefault: 'day',
    periodDefault: 'all',
    onChange: () => _latestSnap && renderFromSnapshot(_latestSnap),
  });
  renderFilterToolbar('trades-filters', {
    showGranularity: false,
    periodDefault: 'all',
    onChange: () => _latestSnap && renderFromSnapshot(_latestSnap),
  });

  try {
    const r = await fetch('/api/backtest/status');
    const d = await r.json();
    if (d.running) {
      document.getElementById('bt-start-btn').disabled = true;
      document.getElementById('bt-stop-btn').disabled  = false;
      startPolling();
    } else if (d.snapshot) {
      renderStatus(d);
    }
  } catch {}
})();
