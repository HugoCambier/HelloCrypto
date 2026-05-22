// ═══════════════════════════════════════════════════════════════════════════
// HelloCrypto Cockpit — main.js (run management; analytics in analytics.js)
// ═══════════════════════════════════════════════════════════════════════════

const COIN_UNIVERSE = ["BTCUSDC","ETHUSDC","SOLUSDC","XRPUSDC","BNBUSDC","ADAUSDC","AVAXUSDC","DOGEUSDC","LINKUSDC","MATICUSDC"];

let _cfg          = null;            // last loaded config (from /api/config)
let _llmModels    = {};
let _simRunning   = false;
let _simSnap      = null;
let _simSessionId = null;            // id of currently running simulation (if any)
let _lastPerf     = null;
let _livePortfolio = null;           // live /api/portfolio (real mode)
let _selectedMode = 'real';          // mode displayed in right panel: 'real' | 'simulation'
let _selectedSession = null;         // selected session_id (null = all sim or real)
let _runs         = [];              // list of simulation sessions
let _savedResume  = null;            // saved sim state, if any
let _countdownIv  = null;
let _simCycleStartedAt = null;
let _simCycleSeconds   = null;
let _renameTargetId    = null;

const _refs = {
  pnl: { current: null }, dd: { current: null }, alloc: { current: null },
  pnlBars: { current: null }, volBars: { current: null },
};

// ─── Right-panel tabs ────────────────────────────────────────────────────────
function switchRTab(name, btn) {
  ['cockpit','charts','orders'].forEach(t => {
    document.getElementById('rtab-'+t)?.classList.toggle('hidden', t !== name);
  });
  document.querySelectorAll('.rtab[data-rtab]').forEach(b => {
    b.classList.toggle('active', b === btn);
  });
  // Always refresh on tab show: guarantees charts see visible canvas + fresh data.
  if (name === 'charts') loadCharts();
  if (name === 'orders' && typeof loadOrdersTab === 'function') loadOrdersTab();
}

// Show/hide the Orders tab based on selected mode (real only).
function _updateOrdersTabVisibility() {
  const btn = document.getElementById('rtab-orders-btn');
  const isReal = _selectedMode === 'real';
  btn?.classList.toggle('hidden', !isReal);
  // If we leave real mode while on Orders, switch back to Cockpit
  if (!isReal && !document.getElementById('rtab-orders')?.classList.contains('hidden')) {
    const cockpitBtn = document.querySelector('.rtab[data-rtab="cockpit"]');
    if (cockpitBtn) switchRTab('cockpit', cockpitBtn);
  }
}

// ─── Config (loaded once for defaults; persisted via run launch) ─────────────
async function loadConfig() {
  const r = await fetch('/api/config');
  _cfg = await r.json();
  _llmModels = _cfg.llm_models || {};
}

// ─── Runs list ───────────────────────────────────────────────────────────────
async function loadRunsList() {
  try {
    const r = await fetch('/api/simulation/sessions');
    _runs = (await r.json()) || [];
  } catch { _runs = []; }
  renderRunsList();
}

function renderRunsList() {
  const el = document.getElementById('runs-list');
  if (!el) return;

  const cards = [];

  // Pinned "Real" pseudo-session (continuous, non-deletable)
  const realActive = _selectedMode === 'real';
  cards.push(`
    <div class="run-card real ${realActive ? 'active' : ''}" onclick="selectRun('real', null)">
      <div class="flex-1 min-w-0">
        <div class="flex items-center gap-2 mb-0.5">
          <span class="run-tag tag-real">RÉEL</span>
          <span class="text-sm font-semibold text-slate-100 truncate">Activité réelle</span>
        </div>
        <div class="text-[11px] text-slate-500">Flux continu Binance</div>
      </div>
    </div>
  `);

  // Simulation sessions
  for (const s of _runs) {
    const id   = s.id;
    const isRunning = _simRunning && id === _simSessionId;
    const active    = _selectedMode === 'simulation' && _selectedSession === id;
    const name = s.name || id;
    const trades = s.trade_count ?? 0;
    const startTs = (s.start_ts || s.created_at || '').replace('T',' ').slice(0,16);
    const meta = `${trades} trade${trades > 1 ? 's' : ''}${startTs ? ' · ' + startTs : ''}`;
    const nameJson = JSON.stringify(name).replace(/"/g, '&quot;');

    cards.push(`
      <div class="run-card ${active ? 'active' : ''}" onclick="selectRun('simulation', '${id}')">
        <div class="flex-1 min-w-0">
          <div class="flex items-center gap-2 mb-0.5">
            <span class="run-tag tag-sim">SIM</span>
            <span class="text-sm font-semibold text-slate-100 truncate">${escHtml(name)}</span>
            ${isRunning ? '<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse shrink-0"></span>' : ''}
          </div>
          <div class="text-[11px] text-slate-500">${escHtml(meta)}</div>
        </div>
        <div class="run-actions shrink-0">
          <button onclick="event.stopPropagation(); openRenameModal('${id}', ${nameJson})" title="Renommer">✎</button>
          <button class="danger" onclick="event.stopPropagation(); deleteRun('${id}', ${nameJson})" title="Supprimer">×</button>
        </div>
      </div>
    `);
  }

  if (_runs.length === 0) {
    cards.push('<p class="text-xs text-slate-500 italic px-2">Aucune simulation enregistrée</p>');
  }

  el.innerHTML = cards.join('');
}

function selectRun(mode, sessionId) {
  _selectedMode    = mode;
  _selectedSession = sessionId;
  renderRunsList();
  _showContentState();
  _updateOrdersTabVisibility();
  // Clear charts immediately to avoid lingering data from the previous run
  _destroyCharts();
  // Reset logs view so we don't mix entries across runs
  clearLogsDisplay();
  // If the selected run is currently running, render its live snapshot immediately
  // (gives instant feedback even before loadPerformance returns)
  if (_simRunning && sessionId === _simSessionId && _simSnap) {
    renderSimComparisons(_simSnap);
    if (_simSnap.holdings) renderHoldings('holdings-list', _simSnap.holdings, _simSnap.prices||{});
  }
  loadPerformance();
  // Refresh the charts tab too — even if it's currently hidden, the next switch
  // will see fresh data without a stale flash.
  loadCharts();
  if (mode === 'real' && typeof loadOrdersTab === 'function') loadOrdersTab();
  pollLogs();
}

function _destroyCharts() {
  for (const key of ['pnl', 'dd', 'alloc', 'pnlBars', 'volBars']) {
    if (_refs[key]?.current) {
      try { _refs[key].current.destroy(); } catch {}
      _refs[key].current = null;
    }
  }
  // Belt-and-suspenders: clean up any orphaned Chart.js instances on these canvases.
  if (typeof Chart !== 'undefined') {
    for (const id of ['pnl-chart', 'dd-chart', 'alloc-chart', 'pnl-bars-chart', 'vol-bars-chart']) {
      const canvas = document.getElementById(id);
      if (canvas) {
        const existing = Chart.getChart(canvas);
        if (existing) { try { existing.destroy(); } catch {} }
      }
    }
  }
}

async function deleteRun(id, name) {
  if (!confirm(`Supprimer la session "${name}" ?\nLes trades, logs et analyses associés seront définitivement effacés.`)) return;
  try {
    const r = await fetch(`/api/simulation/sessions/${id}`, { method: 'DELETE' });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Erreur');
    toast('Session supprimée', 'ok');
    if (_selectedSession === id) selectRun('real', null);
    await loadRunsList();
  } catch (e) { toast(e.message || 'Erreur suppression', 'err'); }
}

// ─── Rename modal ────────────────────────────────────────────────────────────
function openRenameModal(id, currentName) {
  _renameTargetId = id;
  document.getElementById('rename-input').value = currentName || '';
  document.getElementById('rename-modal').classList.remove('hidden');
  setTimeout(() => document.getElementById('rename-input').focus(), 50);
}

function closeRenameModal() {
  document.getElementById('rename-modal').classList.add('hidden');
  _renameTargetId = null;
}

async function confirmRename() {
  const name = document.getElementById('rename-input').value.trim();
  if (!name || !_renameTargetId) return;
  try {
    const r = await fetch(`/api/simulation/sessions/${_renameTargetId}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    if (!r.ok) throw new Error((await r.json()).error || 'Erreur');
    toast('Renommé', 'ok');
    closeRenameModal();
    await loadRunsList();
  } catch (e) { toast(e.message || 'Erreur', 'err'); }
}

function _closeModalBg(e, id) {
  if (e.target.id === id) document.getElementById(id).classList.add('hidden');
}

// ─── New run modal ───────────────────────────────────────────────────────────
function openNewRunModal() {
  // Pre-fill from current config
  document.getElementById('nr-budget').value = _cfg?.budget ?? 100;
  document.getElementById('nr-cycle').value  = Math.max(5, Math.round((_cfg?.cycle_seconds ?? 300) / 60));
  document.getElementById('nr-sl').value     = _cfg?.stop_loss_pct ?? 10;
  document.getElementById('nr-ts').value     = _cfg?.trailing_stop_pct ?? 5;
  document.getElementById('nr-risk').value   = _cfg?.risk_level ?? 5;
  document.getElementById('nr-risk-val').textContent = _cfg?.risk_level ?? 5;
  document.getElementById('nr-llm-provider').value = _cfg?.llm?.provider || 'gemini';
  _onNrLlmProviderChange();
  if (_cfg?.llm?.model) document.getElementById('nr-llm-model').value = _cfg.llm.model;
  document.getElementById('nr-name').value = '';

  const stored = _cfg?.watchlist;
  const sel = (Array.isArray(stored) && stored.length) ? new Set(stored) : new Set(COIN_UNIVERSE);
  renderCryptoDrop('nr-watchlist-drop', COIN_UNIVERSE, sel);

  _setNrMode('simulation');
  document.getElementById('newrun-modal').classList.remove('hidden');
}

function closeNewRunModal() {
  document.getElementById('newrun-modal').classList.add('hidden');
}

function _setNrMode(mode) {
  document.querySelectorAll('#nr-mode-seg button').forEach(b =>
    b.classList.toggle('active', b.dataset.val === mode));
  document.getElementById('nr-mode-seg').dataset.val = mode;

  const isSim = mode === 'simulation';
  document.getElementById('nr-name-row').classList.toggle('hidden', !isSim);
  document.getElementById('nr-init-row').classList.toggle('hidden', !isSim);

  const resumeRow = document.getElementById('nr-init-resume-row');
  if (resumeRow) resumeRow.classList.toggle('hidden', !_savedResume?.exists);

  const hint = document.getElementById('nr-mode-hint');
  if (isSim) {
    hint.textContent = 'Simulation indépendante. Choisis l\'état initial ci-dessous.';
    hint.className = 'text-[11px] text-slate-500 mt-1.5';
  } else {
    hint.textContent = 'Le run réel reprend automatiquement les positions actuelles sur Binance. Tous les runs réels forment une suite continue.';
    hint.className = 'text-[11px] text-amber-400 mt-1.5';
  }

  if (_savedResume?.exists && document.getElementById('nr-resume-info')) {
    document.getElementById('nr-resume-info').textContent =
      `Dernière simu : cycle ${_savedResume.cycle} — budget $${fmt(_savedResume.budget)}`;
  }
}

function _onNrLlmProviderChange() {
  const prov = document.getElementById('nr-llm-provider').value;
  const sel  = document.getElementById('nr-llm-model');
  sel.innerHTML = (_llmModels[prov]||[]).map(m=>`<option value="${m}">${m}</option>`).join('');
}

async function launchNewRun() {
  const btn = document.getElementById('nr-launch-btn');
  const mode = document.getElementById('nr-mode-seg').dataset.val || 'simulation';
  const body = {
    budget:            +document.getElementById('nr-budget').value,
    cycle_seconds:     Math.max(5, +document.getElementById('nr-cycle').value) * 60,
    stop_loss_pct:     +document.getElementById('nr-sl').value,
    trailing_stop_pct: +document.getElementById('nr-ts').value,
    risk_level:        +document.getElementById('nr-risk').value,
    llm: {
      provider: document.getElementById('nr-llm-provider').value,
      model:    document.getElementById('nr-llm-model').value,
    },
    watchlist: getCryptoSelection('nr-watchlist-drop'),
    mode,
  };

  btn.disabled = true; btn.textContent = 'Lancement…';

  try {
    // Persist config so it's reused by other parts of the app
    await fetch('/api/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    if (mode === 'simulation') {
      const initSel = document.querySelector('input[name="nr-init"]:checked')?.value || 'fresh';
      const resume       = initSel === 'resume';
      const from_binance = initSel === 'binance';
      const name         = document.getElementById('nr-name').value.trim() || null;
      const r = await fetch('/api/simulation/start', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({
          budget: body.budget, cycle_seconds: body.cycle_seconds,
          stop_loss_pct: body.stop_loss_pct, trailing_stop_pct: body.trailing_stop_pct,
          risk_level: body.risk_level,
          resume,
          from_binance,
          session_name: name,
        }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || 'Erreur lancement');
      toast('Simulation lancée', 'ok');
      // Invalidate caches that change when a new sim starts
      invalidateCache('/api/performance');
      invalidateCache('/api/simulation');
      _simRunning = true;
      _simSessionId = d.session_id || null;
      closeNewRunModal();
      _startSimPoll();
      await loadRunsList();
      // Auto-select the new run
      if (_simSessionId) selectRun('simulation', _simSessionId);
    } else {
      // Real mode: just enable the runner — actual cycles run via GitHub Actions
      await fetch('/api/config', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ enabled: true, mode: 'real' }),
      });
      toast('Runner réel activé (GitHub Actions)', 'ok');
      closeNewRunModal();
      _cfg.enabled = true;
      _cfg.mode = 'real';
      selectRun('real', null);
    }
  } catch (e) {
    toast(e.message || 'Erreur', 'err');
  } finally {
    btn.disabled = false; btn.textContent = 'Lancer';
  }
}

// ─── Stop currently running simulation ───────────────────────────────────────
async function stopCurrentRun() {
  if (!confirm('Arrêter le run en cours ?')) return;
  try {
    await fetch('/api/simulation/stop', {method:'POST'});
    toast('Simulation arrêtée','warn');
    _simRunning = false;
    _stopCountdown();
    _stopSimPoll();
    _renderCurrentRunBox();
    await loadRunsList();
    await _checkResume();
  } catch { toast('Erreur arrêt','err'); }
}

// ─── Current-run header rendering ────────────────────────────────────────────
function _renderCurrentRunBox() {
  const box = document.getElementById('current-run-box');
  if (!box) return;
  if (!_simRunning || !_simSnap) {
    box.classList.add('hidden');
    return;
  }
  box.classList.remove('hidden');
  const matched = _runs.find(r => r.id === _simSessionId);
  document.getElementById('current-run-name').textContent =
    matched?.name || _simSnap?.session_name || `Cycle ${_simSnap?.cycle ?? '…'}`;

  const total  = _simSnap?.total ?? _simSnap?.cash ?? 0;
  const budget = _simSnap?.budget ?? _cfg?.budget ?? 0;
  const pnl    = _simSnap?.pnl ?? (total - budget);
  const cycle  = _simSnap?.cycle ?? 0;
  document.getElementById('current-run-meta').innerHTML =
    `Cycle <span class="text-slate-300 font-semibold">${cycle}</span> · `
    + `Total <span class="text-slate-300 font-semibold">$${fmt(total)}</span> · `
    + `PnL <span class="${pnlClass(pnl)} font-semibold">${fmtPnl(pnl)}</span>`;
}

// ─── Simulation polling ──────────────────────────────────────────────────────
let _simPollIv = null;
function _startSimPoll() {
  if (_simPollIv) return;
  _pollSimStatus();
  _simPollIv = setInterval(_pollSimStatus, 3000);
}
function _stopSimPoll() { clearInterval(_simPollIv); _simPollIv = null; }

async function _pollSimStatus() {
  try {
    const d = await fetch('/api/simulation/status').then(r=>r.json());
    _simRunning   = !!d.running;
    _simSnap      = d.snapshot || null;
    _simSessionId = d.session_id || _simSnap?.session_id || null;
    _simCycleStartedAt = d.cycle_started_at || null;
    _simCycleSeconds   = d.cycle_seconds || null;

    if (_simRunning) {
      if (!_countdownIv) _countdownIv = setInterval(_tickCountdown, 1000);
    } else {
      _stopCountdown();
      _stopSimPoll();
    }

    _renderCurrentRunBox();
    renderRunsList(); // refresh "running" indicator dot

    // If the user is viewing the running session, push live updates to the right panel
    if (_selectedMode === 'simulation' && _selectedSession === _simSessionId && _simSnap) {
      renderSimComparisons(_simSnap);
      if (_simSnap.holdings) renderHoldings('holdings-list', _simSnap.holdings, _simSnap.prices||{});
    }
  } catch {}
}

function _tickCountdown() {
  const el = document.getElementById('sim-countdown');
  if (!el || !_simCycleStartedAt || !_simCycleSeconds) { if(el) el.textContent='—'; return; }
  const elapsed = (Date.now() - new Date(_simCycleStartedAt+'Z').getTime())/1000;
  const rem     = Math.max(0, Math.round(_simCycleSeconds - elapsed));
  if (rem === 0) { el.textContent = 'exécution…'; return; }
  const m=Math.floor(rem/60), s=rem%60;
  el.textContent = m>0 ? `${m}m${String(s).padStart(2,'0')}s` : `${s}s`;
}
function _stopCountdown() {
  clearInterval(_countdownIv); _countdownIv = null;
  const el = document.getElementById('sim-countdown');
  if (el) el.textContent = '—';
}

// ─── Resume detection ────────────────────────────────────────────────────────
async function _checkResume() {
  try {
    const d = await fetch('/api/simulation/saved').then(r=>r.json());
    _savedResume = d;
  } catch { _savedResume = null; }
}

// ─── Loaders ─────────────────────────────────────────────────────────────────
function _setLoading(id, on) {
  document.getElementById(id)?.classList.toggle('hidden', !on);
}
function _setLoaders(ids, on) { ids.forEach(id => _setLoading(id, on)); }

// ─── Empty state toggling ────────────────────────────────────────────────────
function _showEmptyState() {
  document.getElementById('cockpit-empty')?.classList.remove('hidden');
  document.getElementById('cockpit-content')?.classList.add('hidden');
  document.getElementById('charts-empty-state')?.classList.remove('hidden');
  document.getElementById('charts-content')?.classList.add('hidden');
}
function _showContentState() {
  document.getElementById('cockpit-empty')?.classList.add('hidden');
  document.getElementById('cockpit-content')?.classList.remove('hidden');
  document.getElementById('charts-empty-state')?.classList.add('hidden');
  document.getElementById('charts-content')?.classList.remove('hidden');
}

// ─── Performance loading (scoped to selected run) ────────────────────────────
// Strategy: fast fetch first (no benchmarks → ~100ms), render immediately;
// then async benchmark fetch updates the PnL chart in the background.
let _perfFetchToken = 0;
async function loadPerformance() {
  if (_selectedMode === null) {
    _showEmptyState();
    return;
  }
  _showContentState();

  const token = ++_perfFetchToken;
  const baseParams = new URLSearchParams({ mode: _selectedMode, period: 'all' });
  if (_selectedMode === 'simulation' && _selectedSession) {
    baseParams.set('session_id', _selectedSession);
  }

  const fastLoaders = ['loading-kpis', 'loading-pnl', 'loading-trades', 'loading-holdings'];
  _setLoaders(fastLoaders, true);

  try {
    // ── Fast path: skip benchmarks ──────────────────────────────────────────
    const fastParams = new URLSearchParams(baseParams);
    fastParams.set('with_benchmarks', '0');

    const fastFetches = [fetchJson(`/api/performance?${fastParams}`)];
    if (_selectedMode === 'real') {
      fastFetches.push(fetchJson('/api/portfolio').catch(()=>null));
    } else {
      fastFetches.push(Promise.resolve(null));
    }
    const [perf, portfolio] = await Promise.all(fastFetches);

    if (token !== _perfFetchToken) return; // user moved on
    _lastPerf = perf;
    _livePortfolio = portfolio;
    _renderKpis(_lastPerf);
    _renderPnlChart();
    _renderTradesList();
    _renderHoldingsForSelection();
    _setLoaders(['loading-kpis', 'loading-trades', 'loading-holdings'], false);
    // Keep PnL loader on — benchmarks still pending

    // ── Slow path: benchmarks in the background (cached longer — slow-changing) ─
    fetchJson(`/api/performance?${baseParams}`, 5 * 60_000).then(perfBench => {
      if (token !== _perfFetchToken) return;
      _lastPerf.bh_timeseries  = perfBench.bh_timeseries;
      _lastPerf.btc_timeseries = perfBench.btc_timeseries;
      _renderPnlChart();
      _renderKpis(_lastPerf);
    }).catch(()=>{}).finally(() => {
      if (token === _perfFetchToken) _setLoading('loading-pnl', false);
    });
  } catch {
    _setLoaders(fastLoaders, false);
  }
}

function _renderHoldingsForSelection() {
  // Sim mode + running selected run → use snapshot
  if (_selectedMode === 'simulation' && _simRunning && _selectedSession === _simSessionId && _simSnap?.holdings) {
    renderHoldings('holdings-list', _simSnap.holdings, _simSnap.prices || {});
    return;
  }
  // Real mode → use live /api/portfolio
  if (_selectedMode === 'real' && _livePortfolio && !_livePortfolio.error) {
    const positions = (_livePortfolio.positions || []).map(p => ({
      symbol: p.symbol, qty: p.qty, value: p.value,
      avg_price: p.avg_price ?? p.entry_price,
      current_price: p.current_price ?? p.price,
    }));
    renderHoldings('holdings-list', positions);
    return;
  }
  // Past sim session or empty → no live positions
  document.getElementById('holdings-list').innerHTML =
    '<span class="text-slate-500 text-xs">Aucune position ouverte</span>';
}

function _renderPnlChart() {
  if (!_lastPerf) return;

  let series    = [];
  let valueMode = 'absolute';
  let budget    = _lastPerf.budget ?? _cfg?.budget ?? 0;

  // Priority 1: live sim → cycle-by-cycle total_value (dense, exact)
  if (_selectedMode === 'simulation' && _simRunning && _selectedSession === _simSessionId
      && Array.isArray(_simSnap?.value_timeseries) && _simSnap.value_timeseries.length) {
    series = _simSnap.value_timeseries;
    budget = _simSnap.budget ?? budget;
  } else {
    // Priority 2: reconstruct from trade history (last-known-price per symbol)
    series = strategyTimeseriesFromHistory(_lastPerf.history || [], budget);
    // For real mode, append a 'now' point with the actual live total
    if (_selectedMode === 'real' && _livePortfolio && !_livePortfolio.error) {
      const cash   = _livePortfolio.cash ?? 0;
      const posVal = (_livePortfolio.positions || []).reduce((s, x) => s + (x.value || 0), 0);
      series = [...series, { ts: new Date().toISOString(), v: cash + posVal }];
    }
  }

  renderPnlChart({
    canvasId: 'pnl-chart',
    emptyId:  'chart-empty',
    filterId: 'pnl-filters',
    chartRef: _refs.pnl,
    series,
    bhSeries:  _lastPerf.bh_timeseries || [],
    btcSeries: _lastPerf.btc_timeseries || [],
    budget,
    valueMode,
  });

  // Drawdown uses the SAME strategy series. We always need absolute values, so
  // for the cashflow-delta path (`valueMode === 'delta'`), we shift by budget.
  const ddSeries = valueMode === 'absolute'
    ? series
    : series.map(p => ({ ...p, v: (p.v || 0) + budget }));
  renderDrawdownChart({
    canvasId: 'dd-chart',
    emptyId:  'dd-empty',
    filterId: 'pnl-filters',  // share the same period/granularity controls
    chartRef: _refs.dd,
    series:   ddSeries,
    budget,
  });

  // Surface max DD next to the title
  const ddCanvas = document.getElementById('dd-chart');
  const ddLabel  = document.getElementById('dd-max-label');
  if (ddCanvas && ddLabel) {
    const maxDd    = parseFloat(ddCanvas.dataset.maxDd || '0');
    const maxDdPct = parseFloat(ddCanvas.dataset.maxDdPct || '0');
    ddLabel.textContent = maxDd < 0
      ? `Max : ${fmtPnl(maxDd)} (${maxDdPct.toFixed(1)}%)`
      : '';
  }
}

function _renderTradesList() {
  if (!_lastPerf) return;
  renderTradesTable({
    containerId: 'trades-list',
    headerId:    'trades-header',
    filterId:    'trades-filters',
    history:     _lastPerf.history || [],
    limit:       100,
  });
}

// Local aliases — analytics.js exports the canonical setHeroColor/setHeroVal.
const _setHeroColor = setHeroColor;
const _setHeroVal   = setHeroVal;

function _renderKpis(p) {
  // When viewing the currently-running sim, defer to snapshot-based renderer
  // (snap.pnl already includes unrealized positions value)
  if (_selectedMode === 'simulation' && _simRunning && _selectedSession === _simSessionId && _simSnap) {
    renderSimComparisons(_simSnap);
    return;
  }

  const budget = p.budget ?? _cfg?.budget ?? 0;

  // Live total: cash + open positions value
  let total, pnl;
  if (_selectedMode === 'real' && _livePortfolio && !_livePortfolio.error) {
    const cash = _livePortfolio.cash ?? 0;
    const posVal = (_livePortfolio.positions || []).reduce((s, x) => s + (x.value || 0), 0);
    total = cash + posVal;
    pnl   = total - budget;
  } else {
    // Past sim session: cashflow + unrealized of remaining positions (last known price)
    const net = p.net ?? 0;
    const unrealized = unrealizedFromHistory(p.history || []);
    total = budget + net + unrealized;
    pnl   = net + unrealized;
  }
  const pnlPct = budget > 0 ? pnl/budget*100 : 0;

  // Secondary KPIs
  _setKpi('kpi-budget', `$${fmt(budget)}`);
  _setKpi('kpi-total',  `$${fmt(total)}`, pnlClass(pnl));
  _setKpi('kpi-winrate',
    p.win_rate != null ? `${fmt(p.win_rate)}%` : '—',
    p.win_rate >= 50 ? 'pnl-pos' : (p.win_rate != null ? 'pnl-neg' : 'text-slate-300'),
    `${p.trades||0} trades`);
  _setKpi('kpi-best',
    p.best_trade != null  ? fmtPnl(p.best_trade)  : '—', pnlClass(p.best_trade),
    p.worst_trade != null ? fmtPnl(p.worst_trade) : '—', pnlClass(p.worst_trade));

  // Hero KPIs (PnL net + comparisons), colored
  _setHeroVal('kpi-pnl', pnl, fmtPnl(pnl), fmtPct(pnlPct));
  _setHeroColor('hero-pnl', pnl);

  const btcVal = _computeVsBtc(p, pnl);
  _setHeroVal('kpi-btc-bh', btcVal,
              btcVal != null ? fmtPnl(btcVal) : '—',
              btcVal != null && budget > 0 ? fmtPct(btcVal/budget*100) : 'stratégie − BTC');
  _setHeroColor('hero-btc', btcVal);

  const alphaVal = _computeVsBH(p, pnl);
  _setHeroVal('kpi-alpha', alphaVal,
              alphaVal != null ? fmtPnl(alphaVal) : '—',
              alphaVal != null && budget > 0 ? fmtPct(alphaVal/budget*100) : 'stratégie − hold');
  _setHeroColor('hero-alpha', alphaVal);
}

// Derive vs BTC & vs B&H from /api/performance benchmark timeseries (final point - budget)
function _computeVsBtc(p, stratPnl) {
  const ts = p.btc_timeseries;
  if (!Array.isArray(ts) || !ts.length || stratPnl == null) return null;
  const btcPnl = (ts[ts.length-1].v ?? 0) - (p.budget ?? _cfg?.budget ?? 0);
  return stratPnl - btcPnl;
}
function _computeVsBH(p, stratPnl) {
  const ts = p.bh_timeseries;
  if (!Array.isArray(ts) || !ts.length || stratPnl == null) return null;
  const bhPnl = (ts[ts.length-1].v ?? 0) - (p.budget ?? _cfg?.budget ?? 0);
  return stratPnl - bhPnl;
}

function renderSimComparisons(snap) {
  if (!snap) return;

  const budget = snap.budget ?? _cfg?.budget ?? 0;
  const total  = snap.total ?? snap.total_value ?? snap.cash ?? 0;
  const pnl    = snap.pnl ?? (total - budget);
  const pnlPct = budget > 0 ? pnl/budget*100 : 0;

  // Hero KPIs
  _setHeroVal('kpi-pnl', pnl, fmtPnl(pnl), fmtPct(pnlPct));
  _setHeroColor('hero-pnl', pnl);

  const btcDiff = (snap.pnl != null && snap.btc_bh_pnl != null) ? snap.pnl - snap.btc_bh_pnl : null;
  _setHeroVal('kpi-btc-bh', btcDiff,
              btcDiff != null ? fmtPnl(btcDiff) : '—',
              btcDiff != null && budget > 0 ? fmtPct(btcDiff/budget*100) : 'stratégie − BTC');
  _setHeroColor('hero-btc', btcDiff);

  _setHeroVal('kpi-alpha', snap.alpha,
              snap.alpha != null ? fmtPnl(snap.alpha) : '—',
              snap.alpha != null && budget > 0 ? fmtPct(snap.alpha/budget*100) : 'stratégie − hold');
  _setHeroColor('hero-alpha', snap.alpha);

  // Secondary
  _setKpi('kpi-budget', `$${fmt(budget)}`);
  _setKpi('kpi-total',  `$${fmt(total)}`, pnlClass(pnl));
  _setKpi('kpi-winrate',
    snap.win_rate != null ? `${fmt(snap.win_rate)}%` : '—',
    snap.win_rate >= 50 ? 'pnl-pos' : 'text-slate-300',
    `${snap.trades_count ?? 0} trades`);

  const sells = (snap.history||[]).filter(t => t.pnl != null);
  const best  = sells.length ? Math.max(...sells.map(t=>t.pnl)) : null;
  const worst = sells.length ? Math.min(...sells.map(t=>t.pnl)) : null;
  _setKpi('kpi-best', best != null ? fmtPnl(best) : '—', pnlClass(best),
          worst != null ? fmtPnl(worst) : '—', pnlClass(worst));
}

// ─── Charts tab ──────────────────────────────────────────────────────────────
let _chartsFetchToken = 0;

function _chartsTabVisible() {
  return !document.getElementById('rtab-charts')?.classList.contains('hidden');
}

async function loadCharts() {
  if (_selectedMode === null) { _showEmptyState(); return; }
  _showContentState();
  // Skip when the tab isn't visible — Chart.js can't measure a 0-size canvas,
  // and the user-triggered switchRTab will call loadCharts again on display.
  if (!_chartsTabVisible()) return;

  const token = ++_chartsFetchToken;
  const chartsLoaders = ['loading-alloc', 'loading-pnlbars', 'loading-volbars'];
  _setLoaders(chartsLoaders, true);
  const params = new URLSearchParams({ mode: _selectedMode, period: 'all', with_benchmarks: '0' });
  if (_selectedMode === 'simulation' && _selectedSession) params.set('session_id', _selectedSession);
  try {
  const [perf, portfolio] = await Promise.all([
    fetchJson(`/api/performance?${params}`).catch(()=>null),
    fetchJson('/api/portfolio').catch(()=>null),
  ]);
  if (token !== _chartsFetchToken) return;
  // Wait one frame so any pending layout changes are applied before Chart.js measures canvases
  await new Promise(r => requestAnimationFrame(r));
  if (token !== _chartsFetchToken) return;

  let cash = 0, positions = [];
  if (_selectedMode === 'simulation' && _simRunning && _selectedSession === _simSessionId && _simSnap?.holdings) {
    cash = _simSnap.cash ?? 0;
    positions = Object.entries(_simSnap.holdings).map(([sym, h]) => {
      const qty = h.qty ?? h;
      const price = (_simSnap.prices||{})[sym] ?? 0;
      return { symbol: sym, qty, current_price: price, value: qty * price, avg_price: h.avg_price ?? price };
    }).filter(p => p.value > 0);
  } else if (portfolio && !portfolio.error && _selectedMode === 'real') {
    cash = portfolio.cash ?? 0;
    positions = (portfolio.positions || []).map(p => ({
      symbol: p.symbol, qty: p.qty, value: p.value,
      avg_price: p.avg_price ?? p.entry_price,
      current_price: p.current_price ?? p.price,
    }));
  }

  renderAllocChart({
    canvasId: 'alloc-chart', emptyId: 'alloc-empty', legendId: 'alloc-legend',
    chartRef: _refs.alloc, cash, positions,
  });
  renderPnlBarsChart({
    canvasId: 'pnl-bars-chart', emptyId: 'pnl-bars-empty',
    chartRef: _refs.pnlBars,
    history: perf?.history || [], positions,
  });
  renderVolBarsChart({
    canvasId: 'vol-bars-chart', emptyId: 'vol-bars-empty',
    chartRef: _refs.volBars,
    history: perf?.history || [],
  });
  } finally {
    if (token === _chartsFetchToken) _setLoaders(chartsLoaders, false);
  }
}

// ─── Logs drawer ─────────────────────────────────────────────────────────────
let _logsOpen=false, _logFilter='all', _logsSeen=new Set(), _logPollIv=null, _logGen=0;

function toggleLogs() {
  _logsOpen = !_logsOpen;
  document.getElementById('logs-drawer').classList.toggle('open', _logsOpen);
  document.getElementById('logs-overlay').classList.toggle('open', _logsOpen);
  if (_logsOpen && !_logPollIv) startLogPolling();
}

function setLogFilter(cat, btn) {
  _logFilter = cat; _logGen++;
  document.querySelectorAll('#logs-drawer .log-filter-btn').forEach(b=>b.classList.toggle('active',b===btn));
  _logsSeen.clear(); document.getElementById('log-container').innerHTML='';
  pollLogs();
}

function clearLogsDisplay() {
  _logsSeen.clear(); document.getElementById('log-container').innerHTML='';
  document.getElementById('log-count').textContent='0';
}

function _classifyLine(t) {
  if (/\[ERROR\]/.test(t)) return 'log-error';
  if (/\[WARNING\]/.test(t)) return 'log-warn';
  if (/BUY|Acheté/.test(t)) return 'log-buy';
  if (/SELL|Vendu|stop/i.test(t)) return 'log-sell';
  if (/HOLD/.test(t)) return 'log-hold';
  if (/Cycle #|═══/.test(t)) return 'log-cycle';
  return 'log-info';
}

async function pollLogs() {
  const gen = _logGen;
  try {
    const params = new URLSearchParams({ limit: '200' });
    if (_logFilter !== 'all') params.set('category', _logFilter);
    // Scope logs to the currently selected run: sim session OR real-mode flux
    if (_selectedMode === 'simulation' && _selectedSession) {
      params.set('session_id', _selectedSession);
      params.set('mode', 'simulation');
    } else if (_selectedMode === 'real') {
      params.set('mode', 'real');
    }
    const logs = await fetch(`/api/logs?${params}`).then(r=>r.json());
    if (_logGen !== gen) return;
    const container = document.getElementById('log-container');
    for (const e of [...logs].reverse()) {
      const key = e.timestamp+e.message;
      if (_logsSeen.has(key)) continue;
      _logsSeen.add(key);
      const div = document.createElement('div');
      div.className = `log-line ${_classifyLine(e.message)}`;
      const ts = e.timestamp ? `<span class="text-slate-500 mr-2">${e.timestamp.slice(11,19)}</span>` : '';
      div.innerHTML = ts + escHtml(e.message);
      container.prepend(div);
    }
    document.getElementById('log-count').textContent = _logsSeen.size;
  } catch {}
}

function startLogPolling() {
  pollLogs();
  if (_logPollIv) clearInterval(_logPollIv);
  _logPollIv = setInterval(pollLogs, 8000);
}

// ─── Boot ────────────────────────────────────────────────────────────────────
async function boot() {
  await loadConfig();
  await _checkResume();

  renderFilterToolbar('pnl-filters', {
    granularityDefault: 'day', periodDefault: 'all',
    onChange: () => _renderPnlChart(),
  });
  renderFilterToolbar('trades-filters', {
    showGranularity: false, periodDefault: 'all',
    onChange: () => _renderTradesList(),
  });

  // No default selection — show empty state until user picks a run
  _selectedMode    = null;
  _selectedSession = null;
  _showEmptyState();

  await loadRunsList();

  // If a simulation is already running, hook into it (poll keeps the live box updated)
  const simStatus = await fetch('/api/simulation/status').then(r=>r.json()).catch(()=>null);
  if (simStatus?.running) {
    _simRunning = true;
    _simSnap = simStatus.snapshot || null;
    _simSessionId = simStatus.session_id || _simSnap?.session_id || null;
    _renderCurrentRunBox();
    _startSimPoll();
  }

  startLogPolling();
  setInterval(() => { if (_selectedMode !== null) loadPerformance(); }, 30000);
  setInterval(loadRunsList, 30000);
}

boot();
