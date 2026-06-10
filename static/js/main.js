// ═══════════════════════════════════════════════════════════════════════════
// HelloCrypto — main.js (run management; analytics in analytics.js)
// ═══════════════════════════════════════════════════════════════════════════

// Watchlist injected server-side from config.json (see dashboard.py
// context_processor). Empty-array fallback keeps the UI from crashing if
// the template render somehow misses the injection.
const COIN_UNIVERSE = window.COIN_UNIVERSE || [];

let _cfg          = null;            // last loaded config (from /api/config)
let _llmModels    = {};
let _simRunning   = false;
let _simSnap      = null;
let _simSessionId = null;            // id of the session currently driving the right panel
let _simSessions  = {};              // session_id -> live status dict (all running sessions)
let _lastPerf     = null;
let _livePortfolio = null;           // live /api/portfolio (real mode)
let _selectedMode = 'real';          // mode displayed in right panel: 'real' | 'simulation'
let _selectedSession = null;         // selected session_id (null = all sim or real)
let _runs         = [];              // list of simulation sessions
let _realRuns     = [];              // list of real-mode sessions (one per Resume→Stop)
let _activeRealSessionId = null;     // currently-armed real session, if any
// Sidebar filter toggles, scoped per section (En cours / Historique).
let _runFilters   = {
  active:  { real: true, sim: true },
  history: { real: true, sim: true },
};
let _savedResume  = null;            // saved sim state, if any
let _countdownIv  = null;
let _simCycleStartedAt = null;
let _simCycleSeconds   = null;
let _simNextCycleAt    = null;
let _renameTargetId    = null;

const _refs = {
  pnl: { current: null }, dd: { current: null }, alloc: { current: null },
  pnlBars: { current: null }, volBars: { current: null },
};

// ─── Right-panel tabs ────────────────────────────────────────────────────────
function switchRTab(name, btn) {
  ['performance','charts','params','runs','orders'].forEach(t => {
    document.getElementById('rtab-'+t)?.classList.toggle('hidden', t !== name);
  });
  document.querySelectorAll('.rtab[data-rtab]').forEach(b => {
    b.classList.toggle('active', b === btn);
  });
  // Always refresh on tab show: guarantees charts see visible canvas + fresh data.
  if (name === 'charts') loadCharts();
  if (name === 'params') loadRunParams();
  if (name === 'runs')   renderRunsTab();
  if (name === 'orders' && typeof loadOrdersTab === 'function') loadOrdersTab();
}

// Conditional tabs visibility (Runs and Orders).
// Visible ONLY on the "Activité réelle" catch-all (real mode + no specific
// session selected). Selecting a real session — active or finished — drills
// into that run's performance view and hides the global-scoped tabs:
//   - Runs lists every real session, which doesn't belong inside one of them.
//   - Orders posts trades against the live Binance account, so it's a
//     portfolio-wide action, not a per-session one.
function _updateOrdersTabVisibility() {
  const isReal       = _selectedMode === 'real';
  const isCatchAll   = isReal && !_selectedSession;  // pinned "Activité réelle"
  const ordersVisible = isCatchAll;
  const runsVisible   = isCatchAll;

  document.getElementById('rtab-orders-btn')?.classList.toggle('hidden', !ordersVisible);
  document.getElementById('rtab-runs-btn')?.classList.toggle('hidden', !runsVisible);

  // If the user was on a now-hidden tab, switch back to Performance so they
  // don't see a stale view.
  const ordersOpen = !document.getElementById('rtab-orders')?.classList.contains('hidden');
  const runsOpen   = !document.getElementById('rtab-runs')?.classList.contains('hidden');
  if ((ordersOpen && !ordersVisible) || (runsOpen && !runsVisible)) {
    const perfBtn = document.querySelector('.rtab[data-rtab="performance"]');
    if (perfBtn) switchRTab('performance', perfBtn);
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
  // Fetch sim + real session lists in parallel. Either failure leaves the
  // other untouched — partial render is better than a broken sidebar.
  const [simR, realR] = await Promise.allSettled([
    fetch('/api/simulation/sessions').then(r => r.json()),
    fetch('/api/real/sessions').then(r => r.json()),
  ]);
  _runs = (simR.status === 'fulfilled' ? simR.value : null) || [];
  if (realR.status === 'fulfilled' && realR.value) {
    _realRuns = realR.value.sessions || [];
    _activeRealSessionId = realR.value.active_session_id || null;
  } else {
    _realRuns = [];
    _activeRealSessionId = null;
  }
  renderRunsList();
}

// Toggle the Real/Sim filter pills above a runs section (active | history).
// Both default to on, and we let the user disable both (resulting in an empty
// list) rather than forcing at least one — keeps the toggle behaviour predictable.
function toggleRunFilter(section, kind) {
  if (!_runFilters[section]) return;
  _runFilters[section][kind] = !_runFilters[section][kind];
  const btn = document.getElementById(`filter-${section}-${kind}`);
  if (btn) btn.classList.toggle('on', _runFilters[section][kind]);
  renderRunsList();
}

// Right-panel context bar: shows the selected run's identity (tag + name) with
// quick rename/delete actions, plus the next-cycle countdown when the live sim
// driving the panel is this run. Hidden when no specific run is selected (e.g.
// the "Activité réelle" catch-all view).
function _renderRunCtxBar() {
  const bar = document.getElementById('run-ctx-bar');
  if (!bar) return;
  const id = _selectedSession;
  if (!id) { bar.classList.add('hidden'); return; }

  const isSim = _selectedMode === 'simulation';
  const src   = isSim ? _runs : _realRuns;
  const s     = src.find(r => r.id === id);
  if (!s) { bar.classList.add('hidden'); return; }

  const name      = s.name || id;
  const isRunning = isSim ? !!_simSessions[id] : id === _activeRealSessionId;

  bar.classList.remove('hidden');
  const tag = document.getElementById('run-ctx-tag');
  tag.textContent = isSim ? 'SIM' : 'RÉEL';
  tag.className   = 'run-tag ' + (isSim ? 'tag-sim' : 'tag-real');
  document.getElementById('run-ctx-name').textContent = name;
  document.getElementById('run-ctx-running-dot').classList.toggle('hidden', !isRunning);

  const renameBtn = document.getElementById('run-ctx-rename');
  renameBtn.onclick = (e) => { e.stopPropagation(); openRenameModal(id, name); };

  // Mirror the sidebar rule: an active real session can't be deleted from here
  // (must be stopped first via the pinned card).
  const deleteBtn = document.getElementById('run-ctx-delete');
  const blockDelete = !isSim && isRunning;
  deleteBtn.classList.toggle('hidden', blockDelete);
  deleteBtn.onclick = (e) => {
    e.stopPropagation();
    if (isSim) deleteRun(id, name);
    else       deleteRealRun(id, name, s.trade_count || 0);
  };

  // Countdown: only meaningful when this run is the live sim feeding the panel.
  const showCd = isSim && isRunning && id === _simSessionId && (_simNextCycleAt || _simCycleStartedAt);
  document.getElementById('run-ctx-next').classList.toggle('hidden', !showCd);

  // Cycle counter: prefer the live snapshot when running (most accurate),
  // otherwise fall back to the cycle_count served by the sessions list
  // (MAX(cycle) over logs, populated for finished runs too).
  const cyclesEl = document.getElementById('run-ctx-cycles');
  const cyclesVal = document.getElementById('run-ctx-cycles-val');
  let nCycles = null;
  if (isRunning && isSim) {
    nCycles = _simSessions[id]?.snapshot?.cycle ?? s.cycle_count ?? null;
  } else {
    nCycles = s.cycle_count ?? null;
  }
  if (nCycles != null && nCycles > 0) {
    cyclesVal.textContent = nCycles;
    cyclesEl.classList.remove('hidden');
  } else {
    cyclesEl.classList.add('hidden');
  }
}

function _renderPinnedRealCard() {
  // Source of truth for "is real armed?" is the DB-backed
  // ``active_real_session_id`` (loaded into _activeRealSessionId). The
  // legacy ``cfg.enabled`` flag is no longer consulted.
  const slot = document.getElementById('pinned-real-card');
  if (!slot) return;
  const realRunning = !!_activeRealSessionId;
  // The pinned card is the PERMANENT real aggregate: it always shows every
  // real trade (all real sessions + manual orders + Binance re-syncs), never
  // a single session. Per-session views live in their own cards below.
  const pinnedActive = _selectedMode === 'real' && !_selectedSession;
  slot.innerHTML = `
    <div class="run-card real ${pinnedActive ? 'active' : ''}" onclick="selectRun('real', null)">
      <div class="flex-1 min-w-0">
        <div class="flex items-center gap-2 mb-0.5">
          <span class="run-tag tag-real">RÉEL</span>
          <span class="text-sm font-semibold text-slate-100 truncate">Activité réelle</span>
          ${realRunning ? '<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse shrink-0"></span>' : ''}
        </div>
        <div class="text-[11px] text-slate-500">${realRunning ? 'Runner armé · cumul de toutes les sessions réelles' : 'Cumul de toutes les sessions réelles'}</div>
      </div>
      <div class="run-actions shrink-0">
        ${realRunning
          ? `<button onclick="event.stopPropagation(); stopReal()" title="Désactiver le runner réel">■</button>`
          : `<button onclick="event.stopPropagation(); resumeReal()" title="Activer le runner réel">▶</button>`}
      </div>
    </div>
  `;
}

function _runCardHtml(s) {
  const id = s.id;
  const name = s.name || id;
  const trades = s.trade_count ?? 0;
  // created_at = session launch date (what the user picks runs by).
  // start_ts = MIN(trade.timestamp) — for a backtest it's a simulated past date.
  const launchTs = (s.created_at || s.start_ts || '').replace('T',' ').slice(0,16);
  const meta = `${trades} trade${trades > 1 ? 's' : ''}${launchTs ? ' · ' + launchTs : ''}`;
  const nameJson = JSON.stringify(name).replace(/"/g, '&quot;');

  if (s._kind === 'real') {
    const isActive = id === _activeRealSessionId;
    const selected = _selectedMode === 'real' && _selectedSession === id;
    return `
      <div class="run-card ${selected ? 'active' : ''}" onclick="selectRun('real', '${id}')">
        <div class="flex-1 min-w-0">
          <div class="flex items-center gap-2 mb-0.5">
            <span class="run-tag tag-real">RÉEL</span>
            <span class="text-sm font-semibold text-slate-100 truncate">${escHtml(name)}</span>
            ${isActive ? '<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse shrink-0"></span>' : ''}
          </div>
          <div class="text-[11px] text-slate-500">${escHtml(meta)}</div>
        </div>
        <div class="run-actions shrink-0">
          ${isActive
            ? ''  /* active session: stop via the pinned card */
            : `<button class="danger" onclick="event.stopPropagation(); deleteRealRun('${id}', ${nameJson}, ${trades})" title="Supprimer (run réel)">×</button>`}
        </div>
      </div>
    `;
  }
  const live = _simSessions[id];
  const isRunning = !!live;
  const selected  = _selectedMode === 'simulation' && _selectedSession === id;
  const decider   = live?.decider || s.decider;
  const decTag = decider === 'deterministic'
    ? '<span class="run-tag" style="background:#312e81;color:#c7d2fe" title="Décideur déterministe (C)">⚙︎ DÉT</span>'
    : (decider === 'llm' ? '<span class="run-tag" style="background:#164e63;color:#a5f3fc" title="Décideur LLM">🤖 LLM</span>' : '');
  return `
    <div class="run-card ${selected ? 'active' : ''}" onclick="selectRun('simulation', '${id}')">
      <div class="flex-1 min-w-0">
        <div class="flex items-center gap-2 mb-0.5">
          <span class="run-tag tag-sim">SIM</span>
          ${decTag}
          <span class="text-sm font-semibold text-slate-100 truncate">${escHtml(name)}</span>
          ${isRunning ? '<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse shrink-0"></span>' : ''}
        </div>
        <div class="text-[11px] text-slate-500">${escHtml(meta)}</div>
      </div>
      <div class="run-actions shrink-0">
        ${isRunning ? `<button onclick="event.stopPropagation(); stopRun('${id}', ${nameJson})" title="Arrêter cette session">■</button>` : ''}
        <button onclick="event.stopPropagation(); openRenameModal('${id}', ${nameJson})" title="Renommer">✎</button>
        <button class="danger" onclick="event.stopPropagation(); deleteRun('${id}', ${nameJson})" title="Supprimer">×</button>
      </div>
    </div>
  `;
}

function _isRunning(s) {
  return s._kind === 'real'
    ? s.id === _activeRealSessionId
    : !!_simSessions[s.id];
}

function renderRunsList() {
  _renderPinnedRealCard();

  const activeEl  = document.getElementById('runs-active-list');
  const historyEl = document.getElementById('runs-history-list');
  if (!activeEl || !historyEl) return;

  // Merge sim + real sessions then split by running state. Sort each bucket
  // by launch date DESC so the most-recently-started run is at the top.
  const all = [
    ..._runs.map(s => ({ ...s, _kind: 'sim' })),
    ..._realRuns.map(s => ({ ...s, _kind: 'real' })),
  ];
  const _ts = (s) => s.created_at || s.start_ts || '';
  all.sort((a, b) => _ts(b).localeCompare(_ts(a)));

  const renderBucket = (el, items, filters, emptyMsg) => {
    const visible = items.filter(s => filters[s._kind]);
    if (visible.length === 0) {
      const noneOn = !filters.real && !filters.sim;
      el.innerHTML = `<p class="text-xs text-slate-500 italic px-2">${
        noneOn ? 'Aucun filtre activé.' : emptyMsg
      }</p>`;
      return;
    }
    el.innerHTML = visible.map(_runCardHtml).join('');
  };

  renderBucket(
    activeEl,
    all.filter(_isRunning),
    _runFilters.active,
    'Aucun run en cours.',
  );
  renderBucket(
    historyEl,
    all.filter(s => !_isRunning(s)),
    _runFilters.history,
    'Aucun run enregistré.',
  );

  // Keep the Runs tab in sync (it shows the same _realRuns source) so
  // navigating between tabs doesn't show stale rows.
  renderRunsTab();
}

// Runs tab content: a denser per-run summary card listing every real run
// (active first, then past, DESC by start). Only the sidebar already has
// the basic cards — this tab is for a richer overview when the user lands
// on the Activité réelle catch-all.
function renderRunsTab() {
  const list  = document.getElementById('runs-tab-list');
  const empty = document.getElementById('runs-tab-empty');
  const count = document.getElementById('runs-tab-count');
  if (!list) return;

  const all = _realRuns.slice();
  // Active session always first, others sorted by trade-window start DESC.
  all.sort((a, b) => {
    if (a.id === _activeRealSessionId) return -1;
    if (b.id === _activeRealSessionId) return 1;
    return (b.start_ts || b.created_at || '').localeCompare(a.start_ts || a.created_at || '');
  });

  if (count) count.textContent = all.length ? `${all.length} run${all.length > 1 ? 's' : ''}` : '';
  if (empty) empty.classList.toggle('hidden', all.length > 0);
  list.innerHTML = '';
  if (!all.length) return;

  for (const s of all) {
    const id        = s.id;
    const name      = s.name || id;
    const trades    = s.trade_count ?? 0;
    const startTs   = (s.start_ts || s.created_at || '').replace('T', ' ').slice(0, 16);
    const endTs     = id === _activeRealSessionId ? 'en cours' : ((s.end_ts || '').replace('T', ' ').slice(0, 16) || '—');
    const isActive  = id === _activeRealSessionId;
    const nameJson  = JSON.stringify(name).replace(/"/g, '&quot;');
    list.innerHTML += `
      <div class="bg-slate-800/40 border border-slate-700 rounded-lg p-3 hover:border-slate-600 transition-colors cursor-pointer"
           onclick="selectRun('real', '${id}')">
        <div class="flex items-start justify-between gap-3">
          <div class="flex-1 min-w-0">
            <div class="flex items-center gap-2 mb-1">
              <span class="run-tag tag-real">RÉEL</span>
              <span class="text-sm font-semibold text-slate-100 truncate">${escHtml(name)}</span>
              ${isActive ? '<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse shrink-0"></span>' : ''}
            </div>
            <div class="grid grid-cols-3 gap-2 text-[11px] text-slate-400">
              <div><span class="text-slate-500">Trades</span> <span class="text-slate-200">${trades}</span></div>
              <div><span class="text-slate-500">Début</span> <span class="text-slate-200">${escHtml(startTs || '—')}</span></div>
              <div><span class="text-slate-500">Fin</span> <span class="text-slate-200">${escHtml(endTs)}</span></div>
            </div>
          </div>
          ${isActive ? '' :
            `<div class="run-actions shrink-0">
               <button class="danger"
                       onclick="event.stopPropagation(); deleteRealRun('${id}', ${nameJson}, ${trades})"
                       title="Supprimer (run réel)">×</button>
             </div>`}
        </div>
      </div>
    `;
  }
}

function selectRun(mode, sessionId) {
  _selectedMode    = mode;
  _selectedSession = sessionId;
  renderRunsList();
  _renderRunCtxBar();
  _showContentState();
  _updateOrdersTabVisibility();
  // Clear charts immediately to avoid lingering data from the previous run
  _destroyCharts();
  // Reset logs view so we don't mix entries across runs
  clearLogsDisplay();
  // If the selected run is currently running, render its live snapshot immediately
  // (gives instant feedback even before loadPerformance returns)
  const liveSel = _simSessions[sessionId];
  if (liveSel && liveSel.snapshot) {
    renderSimComparisons(liveSel.snapshot);
    if (liveSel.snapshot.holdings) renderHoldings('holdings-list', liveSel.snapshot.holdings, liveSel.snapshot.prices||{});
  }
  loadPerformance();
  loadRunParams();
  // Refresh the charts tab too — even if it's currently hidden, the next switch
  // will see fresh data without a stale flash.
  loadCharts();
  if (mode === 'real' && typeof loadOrdersTab === 'function') loadOrdersTab();
  pollLogs();
}

// ─── Run params tab ─────────────────────────────────────────────────────────
async function loadRunParams() {
  const grid    = document.getElementById('run-params-grid');
  const wlEl    = document.getElementById('run-params-watchlist');
  const sumEl   = document.getElementById('run-params-summary');
  const srcEl   = document.getElementById('run-params-source');
  const empty   = document.getElementById('params-empty-state');
  const content = document.getElementById('params-content');
  if (!grid || !wlEl) return;
  grid.innerHTML = '';
  wlEl.textContent = '—';
  if (sumEl) sumEl.textContent = '';
  if (srcEl) srcEl.textContent = '';

  const hasSelection = (_selectedMode === 'simulation' && _selectedSession) || _selectedMode === 'real';
  if (empty)   empty.classList.toggle('hidden', hasSelection);
  if (content) content.classList.toggle('hidden', !hasSelection);
  if (!hasSelection) return;

  let params = null;
  let label  = '';
  let source = '';
  try {
    if (_selectedMode === 'simulation' && _selectedSession) {
      const r = await fetch(`/api/simulation/sessions/${_selectedSession}/detail`);
      if (!r.ok) throw new Error('detail');
      const d = await r.json();
      params = d?.initial_state || {};
      label  = 'Simulation';
      source = 'Paramètres figés au démarrage de la session.';
    } else if (_selectedMode === 'real' && _selectedSession) {
      // Past or active real session — same session-detail endpoint works
      // (sessions table is mode-agnostic for read).
      const r = await fetch(`/api/simulation/sessions/${_selectedSession}/detail`);
      if (!r.ok) throw new Error('detail');
      const d = await r.json();
      params = d?.initial_state || {};
      label  = _selectedSession === _activeRealSessionId ? 'Réel · en cours' : 'Réel · terminé';
      source = 'Paramètres figés à l’ouverture de la session réelle.';
    } else if (_selectedMode === 'real') {
      // Catch-all real history (no specific session) — fall back to the
      // current global config so the user still sees what's running.
      const r = await fetch('/api/config');
      const c = await r.json();
      params = {
        budget:               c.budget,
        cycle_seconds:        c.cycle_seconds,
        risk_level:           c.risk_level,
        stop_loss_pct:        c.stop_loss_pct,
        trailing_stop_pct:    c.trailing_stop_pct,
        watchlist:            c.watchlist,
        decider:              c.decider || 'llm',
        llm:                  c.llm,
      };
      label  = 'Réel · historique global';
      source = 'Catch-all des trades réels sans session_id (pré-refacto).';
    }
  } catch {
    params = null;
  }
  if (!params) return;
  if (srcEl) srcEl.textContent = source;

  const cycleMin = params.cycle_seconds ? Math.round(params.cycle_seconds / 60) : null;
  const decider  = params.decider || 'llm';
  const isDet    = decider === 'deterministic';
  const items = [
    ['Mode',       label || '—'],
    ['Décideur',   isDet ? 'Déterministe' : 'LLM'],
    ['Budget',     params.budget != null ? `$${fmt(params.budget)}` : '—'],
    ['Cycle',      cycleMin != null ? `${cycleMin} min` : '—'],
    ['Stop-loss',  params.stop_loss_pct != null ? `${params.stop_loss_pct}%` : '—'],
    ['Trailing',   params.trailing_stop_pct != null ? `${params.trailing_stop_pct}%` : '—'],
    ['Risque',     params.risk_level != null ? `${params.risk_level} / 10` : '—'],
  ];
  if (isDet) {
    if (params.top_n != null)                items.push(['Panier (top-N)',     params.top_n]);
    // Only surface decide_every_cycles for legacy sessions that throttled it;
    // new runs decide each cycle (=1), so showing it would be noise.
    if (params.decide_every_cycles != null && params.decide_every_cycles > 1) {
      items.push(['Décision (cycles)', params.decide_every_cycles]);
    }
    if (params.sell_cooldown_cycles != null) items.push(['Cooldown vente',     `${params.sell_cooldown_cycles} cycles`]);
  } else {
    const prov  = params.llm?.provider || '—';
    const model = params.llm?.model    || '—';
    items.push(['LLM', `${prov} · ${model}`]);
    if (params.sell_cooldown_cycles != null) items.push(['Cooldown vente', `${params.sell_cooldown_cycles} cycles`]);
  }

  grid.innerHTML = items.map(([k, v]) => `
    <div class="bg-slate-800/40 border border-slate-700/50 rounded-lg px-3 py-2">
      <div class="text-[10px] uppercase tracking-wider text-slate-500">${k}</div>
      <div class="text-sm text-slate-200 mt-0.5">${v}</div>
    </div>
  `).join('');

  const wl = Array.isArray(params.watchlist) ? params.watchlist : [];
  wlEl.textContent = wl.length ? wl.join(' · ') : '—';

  if (sumEl) {
    const bits = [
      isDet ? 'déterministe' : (params.llm?.model || 'LLM'),
      params.budget != null ? `$${fmt(params.budget)}` : null,
      cycleMin != null ? `${cycleMin} min` : null,
      wl.length ? `${wl.length} cryptos` : null,
    ].filter(Boolean);
    sumEl.textContent = bits.length ? `— ${bits.join(' · ')}` : '';
  }
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

// Delete a finished real run. Extra-loud confirm because real runs touched
// real money — even though deletion only purges DB records, not Binance
// positions, the user should still be 100% sure about scrapping the audit
// trail.
async function deleteRealRun(id, name, tradeCount) {
  const tradesStr = `${tradeCount} trade${tradeCount > 1 ? 's' : ''}`;
  const msg =
    `⚠️ Supprimer le RUN RÉEL "${name}" ?\n\n` +
    `Ce run a exécuté ${tradesStr} sur Binance. ` +
    `La suppression efface les enregistrements en DB (trades, logs, analyses), ` +
    `mais N'ANNULE PAS les ordres déjà passés sur Binance.\n\n` +
    `Cette action est irréversible — l'historique d'audit sera perdu.`;
  if (!confirm(msg)) return;
  try {
    const r = await fetch(`/api/simulation/sessions/${id}`, { method: 'DELETE' });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || 'Erreur');
    toast('Run réel supprimé', 'ok');
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

// Per-decider default cycle (min). LLM is a fast tactical loop; the
// deterministic decider works on slower regime cycles.
const NR_CYCLE_DEFAULT = { llm: 30, deterministic: 240 };

// ─── New run modal ───────────────────────────────────────────────────────────
function openNewRunModal() {
  // Pre-fill from current config
  document.getElementById('nr-budget').value = _cfg?.budget ?? 100;
  document.getElementById('nr-risk').value   = _cfg?.risk_level ?? 5;
  document.getElementById('nr-risk-val').textContent = _cfg?.risk_level ?? 5;
  document.getElementById('nr-llm-provider').value = _cfg?.llm?.provider || 'gemini';
  _onNrLlmProviderChange();
  if (_cfg?.llm?.model) document.getElementById('nr-llm-model').value = _cfg.llm.model;
  document.getElementById('nr-name').value = '';

  const stored = _cfg?.watchlist;
  const sel = (Array.isArray(stored) && stored.length) ? new Set(stored) : new Set(COIN_UNIVERSE);
  renderCryptoDrop('nr-watchlist-drop', COIN_UNIVERSE, sel);

  // Pre-fill decider + top_n from current config so the user sees their
  // last choice when re-opening the modal.
  const initialDecider = (_cfg?.decider || 'llm').toLowerCase();
  if (_cfg?.top_n) document.getElementById('nr-det-topn').value = _cfg.top_n;
  _setNrDecider(initialDecider);   // also seeds the cycle default
  _setNrMode('simulation');
  document.getElementById('newrun-modal').classList.remove('hidden');
}

function _nudgeCycle(delta) {
  const input = document.getElementById('nr-cycle');
  if (!input) return;
  const cur  = parseInt(input.value, 10) || 0;
  const next = Math.max(5, cur + delta);
  input.value = next;
  _renderCycleHint();
}

function _renderCycleHint() {
  const input = document.getElementById('nr-cycle');
  const hint  = document.getElementById('nr-cycle-hint');
  if (!input || !hint) return;
  const m = parseInt(input.value, 10) || 0;
  if (m < 60) { hint.textContent = ''; return; }
  const h = Math.floor(m / 60);
  const r = m % 60;
  hint.textContent = `≈ ${h}h${r ? ' ' + r + ' min' : ''}`;
}

function closeNewRunModal() {
  document.getElementById('newrun-modal').classList.add('hidden');
}

function _setNrDecider(val) {
  const seg = document.getElementById('nr-decider-seg');
  const prev = seg?.dataset.val;
  document.querySelectorAll('#nr-decider-seg button').forEach(b =>
    b.classList.toggle('active', b.dataset.val === val));
  if (seg) seg.dataset.val = val;
  const det = val === 'deterministic';
  document.getElementById('nr-det-params')?.classList.toggle('hidden', !det);
  // LLM provider/model only makes sense when the LLM decider is selected.
  document.getElementById('nr-llm-row')?.classList.toggle('hidden', det);
  const hint = document.getElementById('nr-decider-hint');
  if (hint) hint.textContent = det
    ? 'Stratégie déterministe (régime + panier, validée backtest) — sans LLM, gratuit.'
    : 'Agent LLM (Claude/Gemini) — comme la prod.';

  // Apply per-decider cycle default. Switch the value when the user toggles to
  // a different decider, or seed it on first open (prev undefined). Keep the
  // user's custom value if they only re-clicked the same toggle.
  const cycleInput = document.getElementById('nr-cycle');
  if (cycleInput && prev !== val) {
    cycleInput.value = NR_CYCLE_DEFAULT[val] ?? 30;
    _renderCycleHint();
  }
}

function _setNrMode(mode) {
  document.querySelectorAll('#nr-mode-seg button').forEach(b =>
    b.classList.toggle('active', b.dataset.val === mode));
  document.getElementById('nr-mode-seg').dataset.val = mode;

  const isSim = mode === 'simulation';
  document.getElementById('nr-name-row').classList.toggle('hidden', !isSim);
  document.getElementById('nr-init-row').classList.toggle('hidden', !isSim);
  // Decider toggle is available in both modes — re-apply current selection.
  const cur = document.getElementById('nr-decider-seg')?.dataset.val || 'llm';
  _setNrDecider(cur);

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
  const runWatchlist = getCryptoSelection('nr-watchlist-drop');
  // Global config update: only user-level preferences (LLM, risk, budget…).
  // Watchlist is per-run and shipped separately to /api/simulation/start so
  // selecting a subset for one run never shrinks the Marché page's universe.
  const decider = document.getElementById('nr-decider-seg')?.dataset.val || 'llm';
  // Stop-loss / trailing are not asked here — they inherit from the saved
  // config (edited elsewhere) so a run always matches the live config.
  const stopLoss = _cfg?.stop_loss_pct ?? 10;
  const trailing = _cfg?.trailing_stop_pct ?? 5;
  const cfgBody = {
    budget:            +document.getElementById('nr-budget').value,
    cycle_seconds:     Math.max(5, +document.getElementById('nr-cycle').value) * 60,
    risk_level:        +document.getElementById('nr-risk').value,
    llm: {
      provider: document.getElementById('nr-llm-provider').value,
      model:    document.getElementById('nr-llm-model').value,
    },
    mode,
    decider,
  };
  // Deterministic decider tuning persisted to global config so the real-mode
  // agent picks them up via cfg.get(...) on every cron tick. Sim mode uses
  // the same keys, shipped per-session below.
  if (decider === 'deterministic') {
    const tn = +document.getElementById('nr-det-topn')?.value;
    if (tn) cfgBody.top_n = tn;
    cfgBody.decide_every_cycles = 1;
  }

  btn.disabled = true; btn.textContent = 'Lancement…';

  try {
    // Persist user preferences (without touching the global watchlist)
    await fetch('/api/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfgBody),
    });

    if (mode === 'simulation') {
      const initSel = document.querySelector('input[name="nr-init"]:checked')?.value || 'fresh';
      const resume       = initSel === 'resume';
      const from_binance = initSel === 'binance';
      const name         = document.getElementById('nr-name').value.trim() || null;
      const startBody = {
        budget: cfgBody.budget, cycle_seconds: cfgBody.cycle_seconds,
        stop_loss_pct: stopLoss, trailing_stop_pct: trailing,
        risk_level: cfgBody.risk_level,
        watchlist: runWatchlist,
        resume,
        from_binance,
        session_name: name,
        decider,
      };
      // Deterministic decider (approach C) tuning.
      // Cycle (min) is the single timing knob — the decider decides at every
      // cycle (decide_every_cycles=1). To slow decisions, lengthen Cycle (min).
      if (decider === 'deterministic') {
        if (cfgBody.top_n) startBody.top_n = cfgBody.top_n;
        startBody.decide_every_cycles = 1;
      }
      const r = await fetch('/api/simulation/start', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify(startBody),
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
      // Real mode: enable the runner — the backend opens a new real-session
      // record on the false→true transition (see routes/config._maybe_toggle_real_session).
      // GitHub Actions cron fires the actual cycles.
      await fetch('/api/config', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ enabled: true, mode: 'real' }),
      });
      toast('Runner réel activé (GitHub Actions)', 'ok');
      closeNewRunModal();
      _cfg.enabled = true;
      _cfg.mode = 'real';
      await loadRunsList();   // pick up the freshly-created session
      selectRun('real', _activeRealSessionId);
    }
  } catch (e) {
    toast(e.message || 'Erreur', 'err');
  } finally {
    btn.disabled = false; btn.textContent = 'Lancer';
  }
}

// ─── Stop a simulation session (independent — others keep running) ───────────
async function stopRun(sessionId, name) {
  if (!sessionId) return;
  if (!confirm(`Arrêter la session "${name || sessionId}" ?\nSes positions seront liquidées en USDC. Les autres sessions continuent.`)) return;
  try {
    await fetch('/api/simulation/stop', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ session_id: sessionId }),
    });
    toast('Session arrêtée','warn');
    delete _simSessions[sessionId];
    await _pollSimStatus();   // refresh running set + countdown
    _updateSidebarLiveRail();
    await loadRunsList();
    await _checkResume();
  } catch { toast('Erreur arrêt','err'); }
}

// ─── Real-mode runner toggle ────────────────────────────────────────────────
async function stopReal() {
  if (!confirm('Désactiver le runner réel ?\nLes positions actuelles restent ouvertes sur Binance (pas de liquidation).\nLe cron ne déclenchera plus de cycle tant que tu ne ré-actives pas.')) return;
  try {
    const r = await fetch('/api/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: false }),
    });
    if (!r.ok) throw new Error((await r.json()).error || 'Erreur');
    _cfg.enabled = false;
    toast('Runner réel désactivé', 'warn');
    // The backend cleared active_real_session_id on the true→false transition;
    // reload the list so the just-stopped session moves into the history.
    await loadRunsList();
    loadRunParams();
  } catch (e) { toast(e.message || 'Erreur', 'err'); }
}

function resumeReal() {
  // Open the new-run modal pre-set to real mode. Honors the user's existing
  // saved cycle/stops/llm config (the modal pre-fills from /api/config).
  openNewRunModal();
  setTimeout(() => _setNrMode('real'), 0);
}

// Reflect "any sim running" into the collapsed-sidebar rail indicator.
// Replaces the legacy single-run header that was tied to a now-removed card.
function _updateSidebarLiveRail() {
  if (typeof updateSidebarRail !== 'function') return;
  updateSidebarRail(_simRunning
    ? { live: true, tag: 'SIM', mode: 'simulation' }
    : { live: false });
}

// ─── Simulation polling ──────────────────────────────────────────────────────
let _simPollIv = null;
function _startSimPoll() {
  if (_simPollIv) return;
  _pollSimStatus();
  _simPollIv = setInterval(_pollSimStatus, 5000);
}
function _stopSimPoll() { clearInterval(_simPollIv); _simPollIv = null; }

async function _pollSimStatus() {
  try {
    const d = await fetch('/api/simulation/status').then(r=>r.json());
    const sessions = d.sessions || [];
    _simSessions = {};
    for (const s of sessions) _simSessions[s.session_id] = s;
    _simRunning = sessions.length > 0;

    // Drive the right-panel live view from the selected session if it's running,
    // otherwise from any running session (so the countdown/box stays meaningful).
    const sel = _simSessions[_selectedSession] || sessions[0] || null;
    _simSnap      = sel?.snapshot || null;
    _simSessionId = sel?.session_id || _simSnap?.session_id || null;
    _simCycleStartedAt = sel?.cycle_started_at || null;
    _simCycleSeconds   = sel?.cycle_seconds || null;
    _simNextCycleAt    = sel?.next_cycle_at || null;

    if (_simRunning) {
      if (!_countdownIv) _countdownIv = setInterval(_tickCountdown, 1000);
    } else {
      _stopCountdown();
      _stopSimPoll();
    }

    _updateSidebarLiveRail();
    renderRunsList(); // refresh "running" indicator dots (per session)
    _renderRunCtxBar();

    // Push live updates to the right panel only when viewing a running session.
    const viewed = _simSessions[_selectedSession];
    if (_selectedMode === 'simulation' && viewed && viewed.snapshot) {
      renderSimComparisons(viewed.snapshot);
      if (viewed.snapshot.holdings)
        renderHoldings('holdings-list', viewed.snapshot.holdings, viewed.snapshot.prices||{});
    }
  } catch {}
}

function _tickCountdown() {
  const el  = document.getElementById('sim-countdown');
  const el2 = document.getElementById('run-ctx-countdown');
  if (!el && !el2) return;
  // Prefer server-computed next_cycle_at (aligned on GH Actions 5-min boundary
  // in serverless mode). Falls back to the legacy cycle_started_at + cycle_seconds
  // calculation for local dev (threading-based loop).
  let rem;
  if (_simNextCycleAt) {
    rem = Math.max(0, Math.round((new Date(_simNextCycleAt+'Z').getTime() - Date.now())/1000));
  } else if (_simCycleStartedAt && _simCycleSeconds) {
    const elapsed = (Date.now() - new Date(_simCycleStartedAt+'Z').getTime())/1000;
    rem = Math.max(0, Math.round(_simCycleSeconds - elapsed));
  } else {
    if (el)  el.textContent  = '—';
    if (el2) el2.textContent = '—';
    return;
  }
  let txt;
  if (rem === 0) {
    txt = 'exécution…';
  } else {
    const m = Math.floor(rem/60), s = rem%60;
    txt = m > 0 ? `${m}m${String(s).padStart(2,'0')}s` : `${s}s`;
  }
  if (el)  el.textContent  = txt;
  if (el2) el2.textContent = txt;
}
function _stopCountdown() {
  clearInterval(_countdownIv); _countdownIv = null;
  const el  = document.getElementById('sim-countdown');
  const el2 = document.getElementById('run-ctx-countdown');
  if (el)  el.textContent  = '—';
  if (el2) el2.textContent = '—';
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
  document.getElementById('performance-empty')?.classList.remove('hidden');
  document.getElementById('performance-content')?.classList.add('hidden');
  document.getElementById('charts-empty-state')?.classList.remove('hidden');
  document.getElementById('charts-content')?.classList.add('hidden');
  document.getElementById('params-empty-state')?.classList.remove('hidden');
  document.getElementById('params-content')?.classList.add('hidden');
}
function _showContentState() {
  document.getElementById('performance-empty')?.classList.add('hidden');
  document.getElementById('performance-content')?.classList.remove('hidden');
  document.getElementById('charts-empty-state')?.classList.add('hidden');
  document.getElementById('charts-content')?.classList.remove('hidden');
  document.getElementById('params-empty-state')?.classList.add('hidden');
  document.getElementById('params-content')?.classList.remove('hidden');
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
  if (_selectedSession) {
    // Both sim sessions and real sessions are filtered by session_id.
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
      _lastPerf.bh_breakdown   = perfBench.bh_breakdown;
      _lastPerf.btc_breakdown  = perfBench.btc_breakdown;
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

  // Priority 1: live sim → cycle-by-cycle total_value (dense, exact). Sourced
  // from /api/performance (60s) rather than the 5s status poll — see
  // _load_state_value_series. Refreshes at cycle cadence, which is all it needs.
  if (_selectedMode === 'simulation' && _simRunning && _selectedSession === _simSessionId
      && Array.isArray(_lastPerf?.value_timeseries) && _lastPerf.value_timeseries.length) {
    series = _lastPerf.value_timeseries;
    budget = _simSnap?.budget ?? budget;
  } else {
    // Priority 2: reconstruct from trade history forward-filled at every
    // decision cycle (so a 185-cycle sim that only traded once still draws
    // 185 points, not 1). Falls back to trade-only points when the session
    // pre-dates the cycle_timestamps capture.
    series = strategyTimeseriesFromCycles(
      _lastPerf.history || [], _lastPerf.cycle_timestamps || [], budget);
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

function _renderTradesList(resetPage = false) {
  if (!_lastPerf) return;
  if (resetPage) {
    const list = document.getElementById('trades-list');
    if (list) list.dataset.page = '1';
  }
  // Client-side pagination over the history already shipped by /api/performance
  // (consumed by the charts + position synthesis): no extra DB roundtrip.
  renderTradesTable({
    containerId:      'trades-list',
    headerId:         'trades-header',
    filterId:         'trades-filters',
    symbolFilterId:   'trades-symbol-filter',
    paginationId:     'trades-pagination',
    pageSize:         100,
    history:          _lastPerf.history || [],
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

  _renderBenchTooltips(p);
}

// Per-coin breakdown of the BH/BTC benchmarks, shown on card hover. The Total
// row reproduces the card value so the user can audit the composition.
function _renderBenchTooltips(p) {
  const fmtP = (n) => (n >= 0 ? '+' : '') + n.toFixed(2);
  const cls  = (n) => n > 0 ? 'pos' : (n < 0 ? 'neg' : '');

  const bhTip  = document.getElementById('kpi-bh-tooltip');
  const btcTip = document.getElementById('kpi-btc-tooltip');
  if (!bhTip || !btcTip) return;

  // ── BH tooltip: one row per coin in the basket + total ──
  const bh = p.bh_breakdown;
  if (Array.isArray(bh) && bh.length) {
    const totalPnl = bh.reduce((s, r) => s + (r.pnl || 0), 0);
    const totalW   = bh.reduce((s, r) => s + (r.weight || 0), 0);
    const totalPct = totalW > 0 ? (totalPnl / totalW * 100) : 0;
    bhTip.innerHTML = `
      <table>
        <thead><tr>
          <th>Coin</th><th class="num">Poids</th><th class="num">PnL $</th><th class="num">PnL %</th>
        </tr></thead>
        <tbody>
          ${bh.map(r => `
            <tr>
              <td class="sym">${shortSym(r.symbol)}</td>
              <td class="num">$${r.weight.toFixed(2)}</td>
              <td class="num ${cls(r.pnl)}">${fmtP(r.pnl)}</td>
              <td class="num ${cls(r.pnl)}">${fmtP(r.pnl_pct)}%</td>
            </tr>
          `).join('')}
          <tr class="total-row">
            <td>Total</td>
            <td class="num">$${totalW.toFixed(2)}</td>
            <td class="num ${cls(totalPnl)}">${fmtP(totalPnl)}</td>
            <td class="num ${cls(totalPnl)}">${fmtP(totalPct)}%</td>
          </tr>
        </tbody>
      </table>`;
    bhTip.classList.add('has-content');
  } else {
    bhTip.innerHTML = '';
    bhTip.classList.remove('has-content');
  }

  // ── BTC tooltip: single line, but kept symmetric for readability ──
  const btc = p.btc_breakdown;
  if (btc && typeof btc === 'object') {
    btcTip.innerHTML = `
      <table>
        <thead><tr>
          <th>Coin</th><th class="num">Prix début</th><th class="num">Prix actuel</th><th class="num">PnL $</th>
        </tr></thead>
        <tbody>
          <tr>
            <td class="sym">${shortSym(btc.symbol)}</td>
            <td class="num">${Number(btc.initial).toLocaleString('fr-FR', {maximumFractionDigits: 4})}</td>
            <td class="num">${Number(btc.final).toLocaleString('fr-FR', {maximumFractionDigits: 4})}</td>
            <td class="num ${cls(btc.pnl)}">${fmtP(btc.pnl)} (${fmtP(btc.pnl_pct)}%)</td>
          </tr>
        </tbody>
      </table>`;
    btcTip.classList.add('has-content');
  } else {
    btcTip.innerHTML = '';
    btcTip.classList.remove('has-content');
  }
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

  // Comparison cards: always use the DB-sourced bench timeseries (same as
  // the PnL chart) for card == chart consistency. snap.btc_bh_pnl /
  // snap.alpha would otherwise lag by one cycle (computed at last sim
  // tick, can be 5-30 min stale).
  const benchP = _lastPerf && (_lastPerf.btc_timeseries || _lastPerf.bh_timeseries)
    ? _lastPerf
    : null;
  const btcVal = benchP ? _computeVsBtc(benchP, pnl)
    : ((snap.pnl != null && snap.btc_bh_pnl != null) ? snap.pnl - snap.btc_bh_pnl : null);
  _setHeroVal('kpi-btc-bh', btcVal,
              btcVal != null ? fmtPnl(btcVal) : '—',
              btcVal != null && budget > 0 ? fmtPct(btcVal/budget*100) : 'stratégie − BTC');
  _setHeroColor('hero-btc', btcVal);

  const alphaVal = benchP ? _computeVsBH(benchP, pnl) : snap.alpha;
  _setHeroVal('kpi-alpha', alphaVal,
              alphaVal != null ? fmtPnl(alphaVal) : '—',
              alphaVal != null && budget > 0 ? fmtPct(alphaVal/budget*100) : 'stratégie − hold');
  _setHeroColor('hero-alpha', alphaVal);

  if (benchP) _renderBenchTooltips(benchP);

  // Secondary
  _setKpi('kpi-budget', `$${fmt(budget)}`);
  _setKpi('kpi-total',  `$${fmt(total)}`, pnlClass(pnl));
  _setKpi('kpi-winrate',
    snap.win_rate != null ? `${fmt(snap.win_rate)}%` : '—',
    snap.win_rate >= 50 ? 'pnl-pos' : 'text-slate-300',
    `${snap.trades_count ?? 0} trades`);

  // best/worst trade come pre-aggregated (snap.best_sell/worst_sell) so the
  // live-status poll no longer ships the full history. Fall back to scanning
  // history for older snapshots that predate the aggregation.
  let best = snap.best_sell ?? null;
  let worst = snap.worst_sell ?? null;
  if (best == null && worst == null && Array.isArray(snap.history)) {
    const sells = snap.history.filter(t => t.pnl != null);
    if (sells.length) {
      best  = Math.max(...sells.map(t=>t.pnl));
      worst = Math.min(...sells.map(t=>t.pnl));
    }
  }
  _setKpi('kpi-best', best != null ? fmtPnl(best) : '—', pnlClass(best),
          worst != null ? fmtPnl(worst) : '—', pnlClass(worst));
}

// ─── Charts tab ──────────────────────────────────────────────────────────────
let _chartsFetchToken = 0;
// renderContextBadges / symbolContextFromCtx are shared helpers in analytics.js.

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
  if (_selectedSession) params.set('session_id', _selectedSession);

  // Live context iff the selected run is the one actively trading. Past runs
  // resolve via ?session_id so the backend looks up the last trade timestamp.
  const isLive =
    (_selectedMode === 'simulation' && _simRunning && _selectedSession === _simSessionId)
    || (_selectedMode === 'real' && _selectedSession === _activeRealSessionId)
    || !_selectedSession;
  const ctxParams = new URLSearchParams();
  if (!isLive && _selectedSession) {
    ctxParams.set('session_id', _selectedSession);
    ctxParams.set('mode', _selectedMode);
  }

  try {
  const [perf, portfolio, ctx] = await Promise.all([
    fetchJson(`/api/performance?${params}`).catch(()=>null),
    fetchJson('/api/portfolio').catch(()=>null),
    fetchJson(`/api/market/context?${ctxParams}`).catch(()=>null),
  ]);
  if (token !== _chartsFetchToken) return;
  // Wait one frame so any pending layout changes are applied before Chart.js measures canvases
  await new Promise(r => requestAnimationFrame(r));
  if (token !== _chartsFetchToken) return;

  let cash = 0, positions = [];
  if (_selectedMode === 'simulation' && _simRunning && _selectedSession === _simSessionId && _simSnap) {
    // Live sim: the backend snapshot already exposes a fully-valued positions
    // array (symbol/qty/avg_price/current_price/value) — use it as-is. The
    // legacy ``holdings`` dict has no current prices, so reconstructing from
    // it would zero everything out.
    cash = _simSnap.cash ?? 0;
    positions = (_simSnap.positions || []).filter(p => (p.value || 0) > 0);
  } else if (_selectedMode === 'simulation' && perf) {
    // Past sim (or live one whose snapshot isn't ours): reconstruct from the
    // trade history — positions valued at their last-seen trade price, cash
    // = budget + net cashflow (sell-side fees already netted into `net`).
    cash = (perf.budget ?? 0) + (perf.net ?? 0);
    positions = positionsFromHistory(perf.history || []);
  } else if (portfolio && !portfolio.error && _selectedMode === 'real') {
    cash = portfolio.cash ?? 0;
    positions = (portfolio.positions || []).map(p => ({
      symbol: p.symbol, qty: p.qty, value: p.value,
      avg_price: p.avg_price ?? p.entry_price,
      current_price: p.current_price ?? p.price,
    }));
  }

  renderMarketContextCard('market-context-card', ctx);

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

// ─── Trades CSV export (current run) ─────────────────────────────────────────
function exportCurrentRunTradesCSV() {
  const history = _lastPerf?.history || [];
  const sessName =
    _runs.find(r => r.id === _selectedSession)?.name
    || _realRuns.find(r => r.id === _selectedSession)?.name
    || _selectedSession
    || 'run';
  const safeName = String(sessName).replace(/[^a-z0-9_-]+/gi, '_').slice(0, 40);
  const ts = new Date().toISOString().slice(0, 10);
  downloadTradesCSV(history, `trades_${safeName}_${ts}.csv`);
}

// ─── Logs drawer ─────────────────────────────────────────────────────────────
let _logsOpen=false, _logFilter='all', _logsSeen=new Set(), _logPollIv=null, _logGen=0;
// Newest log timestamp seen by the current view; sent as ?since=… on each poll
// so the backend only returns deltas. Reset whenever filter/session changes.
let _logsSince=null;

function toggleLogs() {
  _logsOpen = !_logsOpen;
  document.getElementById('logs-drawer').classList.toggle('open', _logsOpen);
  document.getElementById('logs-overlay').classList.toggle('open', _logsOpen);
  if (_logsOpen && !_logPollIv) startLogPolling();
}

function setLogFilter(cat, btn) {
  _logFilter = cat; _logGen++;
  document.querySelectorAll('#logs-drawer .log-filter-btn').forEach(b=>b.classList.toggle('active',b===btn));
  _logsSeen.clear(); _logsSince = null; document.getElementById('log-container').innerHTML='';
  pollLogs();
}

function clearLogsDisplay() {
  _logsSeen.clear(); _logsSince = null;
  document.getElementById('log-container').innerHTML='';
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
    // Scope logs to the currently selected run: sim session, real session,
    // or the catch-all real flux (no session_id).
    if (_selectedSession) {
      params.set('session_id', _selectedSession);
      params.set('mode', _selectedMode);
    } else if (_selectedMode === 'real') {
      params.set('mode', 'real');
    }
    // Incremental fetch: after the initial backlog (no _logsSince), each poll
    // only asks for rows strictly newer than the latest one already rendered.
    // Cuts /api/logs egress by ~50× when the dashboard sits idle.
    if (_logsSince) params.set('since', _logsSince);
    const logs = await fetch(`/api/logs?${params}`).then(r=>r.json());
    if (_logGen !== gen) return;
    const container = document.getElementById('log-container');
    let newestTs = _logsSince;
    for (const e of [...logs].reverse()) {
      const key = e.timestamp+e.message;
      if (e.timestamp && (!newestTs || e.timestamp > newestTs)) newestTs = e.timestamp;
      if (_logsSeen.has(key)) continue;
      _logsSeen.add(key);
      const div = document.createElement('div');
      div.className = `log-line ${_classifyLine(e.message)}`;
      const ts = e.timestamp ? `<span class="text-slate-500 mr-2">${e.timestamp.slice(11,19)}</span>` : '';
      div.innerHTML = ts + escHtml(e.message);
      container.prepend(div);
    }
    _logsSince = newestTs;
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
    onChange: () => _renderTradesList(true),
  });

  // No default selection — show empty state until user picks a run
  _selectedMode    = null;
  _selectedSession = null;
  _showEmptyState();

  await loadRunsList();

  // If any simulation is already running, hook into it (poll keeps boxes live)
  const simStatus = await fetch('/api/simulation/status').then(r=>r.json()).catch(()=>null);
  const running = simStatus?.sessions || [];
  if (running.length) {
    for (const s of running) _simSessions[s.session_id] = s;
    _simRunning = true;
    const sel = running[0];
    _simSnap = sel.snapshot || null;
    _simSessionId = sel.session_id || _simSnap?.session_id || null;
    _updateSidebarLiveRail();
    _startSimPoll();
  }

  startLogPolling();
  _startDashboardPolling();
  _wireVisibilityPause();
}

// ─── Dashboard-wide polling (perf + runs list) ───────────────────────────────
// Captured into named refs so _wireVisibilityPause can suspend them when the
// tab is hidden — otherwise a forgotten browser tab keeps burning Supabase
// egress 24/7 (the dominant cause of our quota overshoot).
let _perfPollIv = null;
let _runsPollIv = null;
function _startDashboardPolling() {
  if (!_perfPollIv) {
    // /api/performance bouge à chaque trade : 60s capte les nouveaux trades
    // sans saturer (avant : 30s = 2× plus d'appels backend → 2× plus de
    // load_history + _compute_benchmarks dans certains cas).
    _perfPollIv = setInterval(() => { if (_selectedMode !== null) loadPerformance(); }, 60000);
  }
  if (!_runsPollIv) {
    // La liste des sessions ne change que sur création/rename/delete (et ces
    // actions appellent loadRunsList directement). 5 min suffit largement
    // pour rattraper une éventuelle dérive d'état après un cron tick.
    _runsPollIv = setInterval(loadRunsList, 300000);
  }
}
function _stopDashboardPolling() {
  if (_perfPollIv) { clearInterval(_perfPollIv); _perfPollIv = null; }
  if (_runsPollIv) { clearInterval(_runsPollIv); _runsPollIv = null; }
}

function _wireVisibilityPause() {
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      // Tab backgrounded: drop every recurring fetch. The DOM keeps its
      // last-rendered state; the next visible flip refreshes everything.
      _stopDashboardPolling();
      if (_logPollIv) { clearInterval(_logPollIv); _logPollIv = null; }
      _stopSimPoll();
    } else {
      // Tab visible again: refetch once immediately, then restart the
      // intervals (only re-arm sim polling if there's an active sim).
      _startDashboardPolling();
      if (_selectedMode !== null) loadPerformance();
      loadRunsList();
      if (_logsOpen) startLogPolling();
      if (_simRunning) _startSimPoll();
    }
  });
}

boot();
