// HelloCrypto — Backtest page (orchestrator; uses shared analytics.js)

// Watchlist injected server-side from config.json (see dashboard.py
// context_processor). Empty-array fallback keeps the UI from crashing if
// the template render somehow misses the injection.
const COIN_UNIVERSE = window.COIN_UNIVERSE || [];

let _btPollIv  = null;
let _latestSnap = null;
// Snapshot of the params used to launch the current/last backtest. We only
// freeze this on a successful POST to /api/backtest/start — that way the
// "Paramètres" tab keeps showing the run's params even after the user edits
// the left form for the next run.
let _btRunParams = null;

// Chart refs (passed to shared renderers)
const _refs = {
  pnl:   { current: null },
  alloc: { current: null },
  pnlBars: { current: null },
  volBars: { current: null },
};

// ── Right-panel tabs ─────────────────────────────────────────────────────────
function switchRTab(name, btn) {
  ['cockpit','charts','params','recap'].forEach(t => {
    document.getElementById('rtab-'+t)?.classList.toggle('hidden', t !== name);
  });
  document.querySelectorAll('.rtab[data-rtab]').forEach(b => {
    b.classList.toggle('active', b === btn);
  });
  if (name === 'params')      renderRunParamsTab();
  else if (name === 'recap')  renderRecapTab();
  else if (_latestSnap)       renderFromSnapshot(_latestSnap);
}

// Render the cached params of the currently/last-running backtest. The cache
// (_btRunParams) is populated by startBacktest on a successful POST.
function renderRunParamsTab() {
  const empty   = document.getElementById('bt-params-empty');
  const content = document.getElementById('bt-params-content');
  const grid    = document.getElementById('bt-params-grid');
  const wl      = document.getElementById('bt-params-watchlist');
  const sumEl   = document.getElementById('bt-params-summary');
  if (!grid || !wl) return;

  if (!_btRunParams) {
    if (empty)   empty.classList.remove('hidden');
    if (content) content.classList.add('hidden');
    return;
  }
  if (empty)   empty.classList.add('hidden');
  if (content) content.classList.remove('hidden');

  const p = _btRunParams;
  const fmtVal = (v) => (v === null || v === undefined || v === '') ? '—' : v;
  const items = [
    ['Période',        p.days ? `${p.days} jour${p.days > 1 ? 's' : ''}` : '—'],
    ['Date de début',  p.start_date || 'auto'],
    ['Budget',         p.budget != null ? `$${typeof fmt === 'function' ? fmt(p.budget) : p.budget}` : '—'],
    ['Stop-loss',      p.stop_loss_pct != null ? `${p.stop_loss_pct}%` : '—'],
    ['Trailing',       p.trailing_stop_pct != null ? `${p.trailing_stop_pct}%` : '—'],
    ['Risque',         p.risk_level != null ? `${p.risk_level} / 10` : '—'],
    ['Seuil achat',    fmtVal(p.buy_threshold)],
    ['Seuil vente',    fmtVal(p.sell_threshold)],
    ['Vitesse',        p.speed ? `${p.speed}x` : '—'],
  ];
  grid.innerHTML = items.map(([k, v]) => `
    <div class="bg-slate-800/40 border border-slate-700/50 rounded-lg px-3 py-2">
      <div class="text-[10px] uppercase tracking-wider text-slate-500">${k}</div>
      <div class="text-sm text-slate-200 mt-0.5">${v}</div>
    </div>
  `).join('');

  const syms = (p.symbols || '').split(',').filter(Boolean);
  wl.textContent = syms.length ? syms.join(' · ') : '—';

  if (sumEl) {
    const bits = [
      p.days ? `${p.days}j` : null,
      p.budget != null ? `$${p.budget}` : null,
      syms.length ? `${syms.length} cryptos` : null,
    ].filter(Boolean);
    sumEl.textContent = bits.join(' · ');
  }
}

// Build a markdown-flavoured recap of the run that the user can copy and
// paste into a discussion (e.g. to iterate on the deterministic decider).
// Pulls from the cached launch params + the latest snapshot — no extra
// server roundtrip.
function _buildRecapMarkdown() {
  const p    = _btRunParams || {};
  const snap = _latestSnap || {};
  const syms = (p.symbols || '').split(',').filter(Boolean);

  const budget = snap.budget ?? p.budget ?? 0;
  const total  = snap.total ?? snap.total_value ?? budget;
  const pnl    = snap.pnl ?? (total - budget);
  const pnlPct = snap.pnl_pct ?? (budget > 0 ? pnl / budget * 100 : 0);
  const wr     = snap.win_rate;
  const tn     = snap.trades_count ?? snap.trades ?? (snap.history || []).filter(t => t.action !== 'ANALYSE').length;
  const alpha  = snap.alpha;
  const btcDiff = (snap.pnl != null && snap.btc_bh_pnl != null) ? snap.pnl - snap.btc_bh_pnl : null;

  const sells = (snap.history || []).filter(t => t.pnl != null);
  const best  = sells.length ? Math.max(...sells.map(t => t.pnl)) : null;
  const worst = sells.length ? Math.min(...sells.map(t => t.pnl)) : null;

  // Per-crypto breakdown: realised PnL + trade count.
  const perCrypto = {};
  for (const t of (snap.history || [])) {
    const sym = (t.symbol || '').toUpperCase();
    if (!sym) continue;
    if (!perCrypto[sym]) perCrypto[sym] = { trades: 0, pnl: 0 };
    if (t.action !== 'ANALYSE') perCrypto[sym].trades += 1;
    if (t.pnl != null) perCrypto[sym].pnl += t.pnl;
  }
  const perCryptoRows = Object.entries(perCrypto)
    .sort((a, b) => b[1].pnl - a[1].pnl)
    .map(([sym, s]) => `- ${sym} : ${s.trades} trade${s.trades > 1 ? 's' : ''}, PnL réalisé ${s.pnl >= 0 ? '+' : ''}$${s.pnl.toFixed(2)}`);

  const fmtNum = (v, d = 2) => (v == null || isNaN(v)) ? '—' : Number(v).toFixed(d);
  const fmtDollar = (v) => v == null ? '—' : (v >= 0 ? '+$' : '-$') + Math.abs(v).toFixed(2);

  const startTs = (snap.start_ts || '').replace('T', ' ').slice(0, 16) || (p.start_date || 'auto');
  const periodLine = p.days ? `${p.days} jour${p.days > 1 ? 's' : ''}${startTs ? ' (à partir du ' + startTs + ')' : ''}` : '—';

  const lines = [
    '# Backtest — récap',
    '',
    '## Paramètres',
    `- Cryptos : ${syms.length ? syms.join(' · ') : '—'} (${syms.length})`,
    `- Période : ${periodLine}`,
    `- Budget initial : $${fmtNum(p.budget)}`,
    `- Stop-loss : ${fmtNum(p.stop_loss_pct, 1)}% · Trailing : ${fmtNum(p.trailing_stop_pct, 1)}%`,
    `- Risque : ${p.risk_level ?? '—'}/10`,
    `- Seuils : achat ${p.buy_threshold ?? '—'} · vente ${p.sell_threshold ?? '—'}`,
    `- Vitesse de simulation : ${p.speed ?? '—'}x`,
    '',
    '## Performance',
    `- Total final : $${fmtNum(total)} (${fmtDollar(pnl)}, ${pnl >= 0 ? '+' : ''}${fmtNum(pnlPct, 2)}%)`,
    `- vs Buy & Hold (alpha) : ${alpha != null ? fmtDollar(alpha) : '—'}`,
    `- vs BTC seul : ${btcDiff != null ? fmtDollar(btcDiff) : '—'}`,
    `- Win rate : ${wr != null ? fmtNum(wr, 1) + '%' : '—'} sur ${tn} trade${tn > 1 ? 's' : ''}`,
    `- Best trade : ${best != null ? fmtDollar(best) : '—'} · Worst trade : ${worst != null ? fmtDollar(worst) : '—'}`,
    '',
    '## Trades par crypto',
    perCryptoRows.length ? perCryptoRows.join('\n') : '- Aucun trade enregistré',
    '',
    '## Contexte (à compléter)',
    '- Régime marché : (fear/greed moyen, dominance BTC, événements macro…)',
    '- Hypothèse testée : (ex: "réduire trailing à 7% sur DOGEUSDC")',
    '- Observation : (anomalie, pattern remarqué…)',
  ];
  return lines.join('\n');
}

function renderRecapTab() {
  const empty   = document.getElementById('bt-recap-empty');
  const content = document.getElementById('bt-recap-content');
  const ta      = document.getElementById('bt-recap-text');
  if (!ta) return;

  const hasRun = !!(_btRunParams && _latestSnap);
  if (empty)   empty.classList.toggle('hidden', hasRun);
  if (content) content.classList.toggle('hidden', !hasRun);
  if (!hasRun) return;

  ta.value = _buildRecapMarkdown();
}

async function copyRecapToClipboard() {
  const ta = document.getElementById('bt-recap-text');
  if (!ta) return;
  try {
    await navigator.clipboard.writeText(ta.value);
    const flag = document.getElementById('bt-recap-copied');
    if (flag) {
      flag.classList.remove('hidden');
      setTimeout(() => flag.classList.add('hidden'), 1500);
    }
  } catch {
    // Fallback for old browsers / non-https contexts
    ta.select();
    document.execCommand('copy');
  }
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
    // Freeze the params used for this run so the Paramètres tab keeps
    // reflecting the launched config even if the user edits the form for
    // a future run.
    _btRunParams = { ...body };
    renderRunParamsTab();
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
  // Keep the cached snapshot's speed in sync with what's actually applied.
  if (_btRunParams) {
    _btRunParams.speed = Number(val);
    renderRunParamsTab();
  }
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
    // Hydrate the run-params cache from the server. The backend stashes the
    // launch params into _bt_state at start, so a page reload mid-run still
    // shows the running config in the Paramètres tab.
    if (d.params && !_btRunParams) {
      _btRunParams = d.params;
      renderRunParamsTab();
    }
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
    // Hydrate the params cache from server-side state so the Paramètres
    // tab shows the right run after a page reload, whether the backtest
    // is still running or already finished.
    if (d.params) _btRunParams = d.params;
    if (d.running) {
      document.getElementById('bt-start-btn').disabled = true;
      document.getElementById('bt-stop-btn').disabled  = false;
      startPolling();
    } else if (d.snapshot) {
      renderStatus(d);
    }
  } catch {}
})();
