// ═══════════════════════════════════════════════════════════════════════════════
// HelloCrypto Dashboard v2 — main.js
// ═══════════════════════════════════════════════════════════════════════════════

// ─── State ───────────────────────────────────────────────────────────────────
let currentTab    = 'dashboard';
let currentPeriod = 'all';
let _runnerMode   = 'simulation';
let _autoEnabled  = false;
let _maxCycles    = null;
let _cycleStartedAt = null;
let _cycleSeconds   = null;
let _countdownIv    = null;

// ─── Formatters ──────────────────────────────────────────────────────────────
function fmt(n)  { return n == null ? '—' : Number(n).toLocaleString('fr-FR', {minimumFractionDigits:2, maximumFractionDigits:2}); }
function fmt4(n) { return n == null ? '—' : Number(n).toLocaleString('fr-FR', {minimumFractionDigits:4, maximumFractionDigits:4}); }
function escHtml(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
function kpiCard(label, value, sub = '', color = 'text-slate-100') {
  return `<div class="bg-slate-800 border border-slate-700 rounded-xl p-4">
    <div class="text-xs text-slate-500 mb-1">${label}</div>
    <div class="text-xl font-bold ${color}">${value}</div>
    ${sub ? `<div class="text-xs text-slate-500 mt-0.5">${sub}</div>` : ''}
  </div>`;
}

// ─── Toast ───────────────────────────────────────────────────────────────────
let _toastTimer = null;
function toast(msg, type = 'ok') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'fixed bottom-5 right-5 px-4 py-3 rounded-xl text-sm font-medium shadow-xl z-50 '
    + (type === 'ok' ? 'bg-green-800 text-green-100' : type === 'warn' ? 'bg-amber-800 text-amber-100' : 'bg-red-800 text-red-100');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.add('hidden'), 3500);
}

// ─── Tab switching ───────────────────────────────────────────────────────────
function switchTab(tab) {
  ['dashboard','markets','performance','backtest'].forEach(t => {
    const pane = document.getElementById('pane-' + t);
    if (pane) pane.classList.toggle('hidden', t !== tab);
  });
  document.querySelectorAll('.tab-btn').forEach(btn => {
    const active = btn.dataset.tab === tab;
    btn.classList.toggle('tab-active', active);
    btn.classList.toggle('tab-inactive', !active);
  });
  currentTab = tab;
  if (tab === 'dashboard')   { loadDashboard(); _loadSimList(); }
  if (tab === 'markets')     loadMarkets();
  if (tab === 'performance') _initPerformanceTab();
  if (tab === 'backtest' && !document.querySelector('#bt-symbols-list input')) _loadBtWatchlist();
}

// ─── Logs drawer ─────────────────────────────────────────────────────────────
let _logsOpen    = false;
let _logFilter   = 'all';
let _logsSeen    = new Set();
let _logPollIv   = null;
let _logGen      = 0;

function toggleLogs() {
  _logsOpen = !_logsOpen;
  document.getElementById('logs-drawer').classList.toggle('open', _logsOpen);
  document.getElementById('logs-overlay').classList.toggle('open', _logsOpen);
  if (_logsOpen && !_logPollIv) startLogPolling();
}

function setLogFilter(cat, btn) {
  _logFilter = cat;
  _logGen++;
  document.querySelectorAll('#logs-drawer .log-filter-btn').forEach(b => b.classList.toggle('active', b === btn));
  _logsSeen.clear();
  document.getElementById('log-container').innerHTML = '';
  pollLogs();
}

function classifyLine(text) {
  if (/\[ERROR\]/.test(text))     return 'log-error';
  if (/\[WARNING\]/.test(text))   return 'log-warn';
  if (/BUY|Acheté/.test(text))    return 'log-buy';
  if (/SELL|Vendu|stop-loss/.test(text)) return 'log-sell';
  if (/HOLD/.test(text))          return 'log-hold';
  if (/Cycle #|═══/.test(text))   return 'log-cycle';
  if (/RAPPORT|PnL|Budget/.test(text)) return 'log-report';
  return 'log-info';
}

async function pollLogs() {
  const gen = _logGen;
  try {
    const cat = _logFilter === 'all' ? '' : `&category=${_logFilter}`;
    const r = await fetch(`/api/logs?limit=200${cat}`);
    if (_logGen !== gen) return;
    const logs = await r.json();
    if (_logGen !== gen) return;
    const container = document.getElementById('log-container');
    let added = false;
    for (const entry of [...logs].reverse()) {
      const key = entry.timestamp + entry.message;
      if (_logsSeen.has(key)) continue;
      _logsSeen.add(key);
      const div = document.createElement('div');
      div.className = `log-line text-xs font-mono leading-5 ${classifyLine(entry.message)}`;
      const ts = entry.timestamp ? `<span class="text-slate-500 mr-2 select-none">${entry.timestamp.replace('T',' ').substring(0,19)}</span>` : '';
      const badge = entry.category ? `<span class="inline-block text-[10px] px-1 rounded mr-1 opacity-60 ${
        entry.category === 'trade' ? 'bg-green-900 text-green-300' :
        entry.category === 'market' ? 'bg-blue-900 text-blue-300' : 'bg-slate-700 text-slate-400'
      }">${entry.category}</span>` : '';
      div.innerHTML = ts + badge + escHtml(entry.message);
      container.prepend(div);
      added = true;
    }
    document.getElementById('log-count').textContent = _logsSeen.size;
    if (added) container.scrollTop = 0;
  } catch {}
}

function clearLogs() {
  _logsSeen.clear();
  document.getElementById('log-container').innerHTML = '';
  document.getElementById('log-count').textContent = '0';
}

function startLogPolling() {
  pollLogs();
  if (_logPollIv) clearInterval(_logPollIv);
  _logPollIv = setInterval(pollLogs, 8000);
}

// ─── Mode & Power ────────────────────────────────────────────────────────────
function setMode(mode) {
  _runnerMode = mode;
  document.getElementById('mode-sim-btn').className = mode === 'simulation'
    ? 'px-3 py-1.5 bg-blue-700 text-white transition-colors'
    : 'px-3 py-1.5 text-slate-400 hover:text-white transition-colors';
  document.getElementById('mode-real-btn').className = mode === 'real'
    ? 'px-3 py-1.5 bg-blue-700 text-white border-l border-slate-600 transition-colors'
    : 'px-3 py-1.5 text-slate-400 hover:text-white border-l border-slate-600 transition-colors';
  // Sync performance session selector visibility
  const sel = document.getElementById('perf-session-sel');
  const actions = document.getElementById('perf-session-actions');
  if (sel && actions) {
    if (mode === 'simulation') { sel.classList.remove('hidden'); actions.classList.remove('hidden'); _loadPerfSessions(); }
    else { sel.classList.add('hidden'); actions.classList.add('hidden'); }
  }
  // Refresh current tab to reflect mode change
  if (currentTab === 'dashboard') loadDashboard();
  else if (currentTab === 'performance') loadPerformance();
}

function _setPowerUI(on) {
  const label = document.getElementById('power-label');
  const btn   = document.getElementById('power-btn');
  label.textContent = on ? 'ON' : 'OFF';
  btn.className = on
    ? 'px-3 md:px-4 py-1.5 rounded-lg text-sm font-medium border-2 border-green-500 text-green-400 transition-colors'
    : 'px-3 md:px-4 py-1.5 rounded-lg text-sm font-medium border-2 border-slate-600 text-slate-400 hover:border-slate-400 transition-colors';
  document.getElementById('stop-btn').classList.toggle('hidden', !on);
  document.getElementById('cycle-badge').classList.toggle('hidden', !on);
  document.getElementById('mode-sim-btn').disabled  = on;
  document.getElementById('mode-real-btn').disabled = on;
}

async function togglePower() {
  const isOn = document.getElementById('power-label').textContent.trim() === 'ON';
  if (isOn) { await stopAll(); }
  else if (_runnerMode === 'simulation') { await _openSimModal(); }
  else { await _openRealModal(); }
}

async function stopAll() {
  try { await fetch('/api/simulation/stop', { method: 'POST' }); } catch {}
  try { await fetch('/api/runner/stop', { method: 'POST' }); } catch {}
  try { await fetch('/api/runner/schedule/disable', { method: 'POST' }); } catch {}
  toast('Arrêté', 'warn');
  await fetchAgentStatus();
}

// ─── Status polling ──────────────────────────────────────────────────────────
async function fetchAgentStatus() {
  const dot   = document.getElementById('status-dot');
  const label = document.getElementById('status-label');
  let running = false, statusText = 'Inactif', cycleBadgeText = '';

  try {
    const d = await fetch('/api/simulation/status').then(r => r.json());
    if (d.running) {
      running = true;
      const cyc = d.snapshot?.cycle ?? '…';
      cycleBadgeText = _maxCycles ? `Cycle ${cyc}/${_maxCycles}` : `Cycle ${cyc}`;
      statusText = `Simulation — ${cycleBadgeText}`;
      _cycleStartedAt = d.cycle_started_at || null;
      _cycleSeconds   = d.cycle_seconds || null;
    }
  } catch {}

  if (!running) {
    try {
      const d = await fetch('/api/runner/status').then(r => r.json());
      if (d.running) { running = true; cycleBadgeText = 'Cycle réel…'; statusText = 'Cycle réel en cours…'; }
      else if (d.cloud_run) {
        try {
          const ds = await fetch('/api/runner/schedule').then(r => r.json());
          if (ds.enabled) { running = true; cycleBadgeText = `Auto (${ds.schedule||'…'})`; statusText = 'Auto réel actif'; }
        } catch {}
      }
    } catch {}
  }

  dot.className = running ? 'w-2 h-2 rounded-full bg-green-400 animate-pulse-dot' : 'w-2 h-2 rounded-full bg-slate-500';
  label.textContent = statusText;
  const cbText = document.getElementById('cycle-badge-text');
  if (running && cycleBadgeText && cbText) cbText.textContent = cycleBadgeText;
  if (!running) {
    _cycleStartedAt = null; _cycleSeconds = null;
    const ct = document.getElementById('countdown-text');
    if (ct) ct.textContent = '';
    if (_countdownIv) { clearInterval(_countdownIv); _countdownIv = null; }
  } else if (!_countdownIv) {
    _countdownIv = setInterval(_tickCountdown, 1000);
  }
  _setPowerUI(running);
}

function _tickCountdown() {
  const ct = document.getElementById('countdown-text');
  if (!ct || !_cycleStartedAt || !_cycleSeconds) { if(ct) ct.textContent = ''; return; }
  const elapsed = (Date.now() - new Date(_cycleStartedAt + 'Z').getTime()) / 1000;
  const remaining = Math.max(0, Math.round(_cycleSeconds - elapsed));
  if (remaining <= 0) ct.textContent = 'exécution…';
  else { const m = Math.floor(remaining/60), s = remaining%60; ct.textContent = m > 0 ? `${m}m${String(s).padStart(2,'0')}s` : `${s}s`; }
}

// ─── Simulation flow ─────────────────────────────────────────────────────────
let _simPollTimer = null;
let _simLastCycle = 0;
let _simLastCycleTs = 0;
let _simCycleSec = 60;

async function _openSimModal() {
  const modal = document.getElementById('sim-init-modal');
  const holdingsList = document.getElementById('sim-holdings-list');
  const holdingsHint = document.getElementById('sim-holdings-hint');
  const budgetHint   = document.getElementById('sim-budget-hint');
  budgetHint.textContent = '';
  holdingsHint.textContent = 'Chargement…';
  holdingsList.innerHTML = '';
  document.getElementById('sim-name-input').value = new Date().toLocaleString('fr-FR', {day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'}).replace(',','');
  _updateSimModalRiskLabel();
  modal.classList.remove('hidden');
  try {
    const d = await fetch('/api/binance/balance').then(r => r.json());
    if (d.usdc != null) {
      document.getElementById('sim-budget-input').value = d.usdc.toFixed(6);
      budgetHint.textContent = `Solde Binance : ${d.usdc.toFixed(6)} USDC`;
    }
    holdingsHint.textContent = '';
    const coins = d.coins || {};
    if (!Object.keys(coins).length) { holdingsList.innerHTML = '<p class="text-xs text-slate-500">Watchlist vide</p>'; return; }
    for (const [sym, info] of Object.entries(coins)) {
      const row = document.createElement('div');
      row.className = 'flex items-center gap-3';
      row.innerHTML = `<span class="text-xs text-slate-300 w-20 shrink-0 font-mono">${info.coin}</span>
        <input type="number" min="0" step="any" data-sym="${sym}" value="${info.qty > 0 ? info.qty : ''}" placeholder="0"
          class="sim-holding-input flex-1 bg-slate-700 border border-slate-600 rounded-lg px-3 py-1.5 text-sm text-slate-200 focus:outline-none" />
        <span class="text-xs text-slate-500 w-32 text-right shrink-0">Binance: ${info.qty > 0 ? info.qty.toFixed(6) : '0'}</span>`;
      holdingsList.appendChild(row);
    }
  } catch { holdingsHint.textContent = 'Impossible de charger'; }
}

function cancelSimStart() { document.getElementById('sim-init-modal').classList.add('hidden'); }

async function confirmSimStart() {
  const budget = parseFloat(document.getElementById('sim-budget-input').value) || 0;
  const resume = document.getElementById('sim-resume').checked;
  _autoEnabled = document.getElementById('sim-auto').checked;
  const maxRaw = document.getElementById('sim-maxcycles-input').value.trim();
  const maxCyc = (_autoEnabled || maxRaw === '') ? null : parseInt(maxRaw);
  _maxCycles = maxCyc;
  const risk_level = parseInt(document.getElementById('sim-modal-risk').value) || 5;
  const cycle_seconds = Math.max(5, parseInt(document.getElementById('sim-modal-cycle').value) || 60);
  const stop_loss_pct = parseFloat(document.getElementById('sim-modal-sl').value) || 10;
  const trailing_stop_pct = parseFloat(document.getElementById('sim-modal-tr').value) || 5;
  const sell_cooldown_cycles = parseInt(document.getElementById('sim-modal-cool').value) || 3;
  const liquidate_at_end = document.getElementById('sim-liquidate').checked;
  const initial_holdings = {};
  document.querySelectorAll('.sim-holding-input').forEach(inp => {
    const qty = parseFloat(inp.value);
    if (qty > 0) initial_holdings[inp.dataset.sym] = qty;
  });
  const sessionName = document.getElementById('sim-name-input').value.trim() || new Date().toLocaleString('fr-FR');
  cancelSimStart();
  _simLastCycle = 0; _simLastCycleTs = Date.now(); _simCycleSec = cycle_seconds;
  try {
    const body = { budget, resume, initial_holdings, session_name: sessionName,
                   risk_level, cycle_seconds, stop_loss_pct, trailing_stop_pct, sell_cooldown_cycles };
    if (maxCyc !== null) body.max_cycles = maxCyc;
    if (liquidate_at_end && maxCyc !== null) body.liquidate_at_end = true;
    const d = await fetch('/api/simulation/start', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) }).then(r => r.json());
    if (d.ok || d.budget != null) {
      if (d.resume_failed) toast('Aucun état sauvegardé — démarrage frais', 'warn');
      else toast(`Simulation démarrée — ${budget} USDC`, 'ok');
      _setPowerUI(true);
      _simPollTimer = setInterval(_pollSimulation, 1000);
      await fetchAgentStatus();
    } else { toast(d.error || 'Erreur', 'error'); }
  } catch (e) { toast('Erreur : ' + e.message, 'error'); }
}

function _updateSimModalRiskLabel() {
  const v = parseInt(document.getElementById('sim-modal-risk')?.value || 5);
  const el = document.getElementById('sim-modal-risk-label');
  if (el) el.textContent = v <= 3 ? 'Prudent' : v <= 6 ? 'Modéré' : 'Agressif';
}

async function stopSimulation() {
  await fetch('/api/simulation/stop', { method: 'POST' });
}

async function _pollSimulation() {
  try {
    const d = await fetch('/api/simulation/status').then(r => r.json());
    const s = d.snapshot || {};
    if ((s.cycle || 0) !== _simLastCycle) { _simLastCycle = s.cycle || 0; _simLastCycleTs = Date.now(); }
    // Update dashboard live display
    if (s && !d.error && !s.error) _updateDashFromSim(s);
    // Error badge
    const errBadge = document.getElementById('sim-error-badge');
    if (errBadge) { errBadge.classList.toggle('hidden', !d.error); if (d.error) errBadge.title = d.error; }
    if (!d.running) {
      clearInterval(_simPollTimer);
      _setPowerUI(false);
      if (d.error) toast('Crash : ' + d.error, 'error');
      else { const sign = (s.pnl||0)>=0?'+':''; toast(`Terminée — PnL: ${sign}$${fmt(s.pnl)}`, (s.pnl||0)>=0?'ok':'warn'); }
    }
  } catch {}
}

// ─── Real mode flow ──────────────────────────────────────────────────────────
async function _openRealModal() {
  const modal = document.getElementById('real-init-modal');
  const list  = document.getElementById('real-holdings-list');
  list.innerHTML = '<p class="text-xs text-slate-400">Chargement…</p>';
  modal.classList.remove('hidden');
  try {
    const d = await fetch('/api/binance/balance').then(r => r.json());
    list.innerHTML = '';
    const usdcRow = document.createElement('div');
    usdcRow.className = 'flex items-center justify-between py-1.5 border-b border-slate-700';
    usdcRow.innerHTML = `<span class="text-xs font-mono text-slate-300">USDC</span><span class="text-xs font-medium text-slate-100">${d.usdc != null ? d.usdc.toFixed(6) : '—'} USDC</span>`;
    list.appendChild(usdcRow);
    for (const [sym, info] of Object.entries(d.coins || {})) {
      const row = document.createElement('div');
      row.className = 'flex items-center justify-between py-1.5 border-b border-slate-700/40';
      row.innerHTML = `<span class="text-xs font-mono text-slate-300">${info.coin}</span><span class="text-xs text-slate-200">${info.qty > 0 ? info.qty.toFixed(6) : '0'}</span>`;
      list.appendChild(row);
    }
  } catch { list.innerHTML = '<p class="text-xs text-red-400">Erreur Binance</p>'; }
}

function cancelRealStart() { document.getElementById('real-init-modal').classList.add('hidden'); }

async function confirmRealStart() {
  _autoEnabled = document.getElementById('real-auto').checked;
  cancelRealStart();
  try {
    if (_autoEnabled) {
      const d = await fetch('/api/runner/schedule/enable', { method: 'POST' }).then(r => r.json());
      if (d.ok) { toast('Auto réel activé', 'ok'); _setPowerUI(true); await fetchAgentStatus(); }
      else toast(d.error || 'Erreur', 'error');
    } else {
      const d = await fetch('/api/runner/start', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({mode:'real'}) }).then(r => r.json());
      if (d.ok) { toast('Cycle réel lancé', 'ok'); _setPowerUI(true); await fetchAgentStatus(); }
      else toast(d.error || 'Erreur', 'error');
    }
  } catch (e) { toast('Erreur : ' + e.message, 'error'); }
}

// ═══════════════════════════════════════════════════════════════════════════════
// DASHBOARD TAB
// ═══════════════════════════════════════════════════════════════════════════════
let _dashPortfolioData = null;

async function loadDashboard() {
  if (_runnerMode === 'real') {
    // Real mode — fetch Binance portfolio
    document.getElementById('dash-sim-live').classList.add('hidden');
    try {
      const d = await fetch('/api/portfolio').then(r => r.json());
      if (!d.error) {
        _dashPortfolioData = d;
        _renderDashKPIs(d);
        _renderDashPositions(d);
        _populateTradeDropdowns(d);
      }
    } catch {}
    try {
      const d = await fetch('/api/performance?period=7j&mode=real').then(r => r.json());
      _renderDashTrades(d.history || []);
    } catch {}
  } else {
    // Simulation mode — show sim snapshot
    try {
      const d = await fetch('/api/simulation/status').then(r => r.json());
      if (d.snapshot && (d.snapshot.cycle || 0) > 0) {
        _updateDashFromSim(d.snapshot);
      } else {
        // Empty state
        document.getElementById('dash-total').textContent = '—';
        document.getElementById('dash-cash').textContent = '—';
        document.getElementById('dash-pnl').textContent = '—';
        document.getElementById('dash-pnl-sub').textContent = '';
        document.getElementById('dash-trades').textContent = '—';
        document.getElementById('dash-fees').textContent = '—';
        document.getElementById('dash-positions-body').innerHTML = '';
        document.getElementById('dash-sim-live').classList.add('hidden');
      }
    } catch {}
    try {
      const d = await fetch('/api/performance?period=7j&mode=simulation').then(r => r.json());
      _renderDashTrades(d.history || []);
    } catch {}
  }
  _loadSimList();
}

function _renderDashKPIs(d) {
  const gainColor = d.gain >= 0 ? 'pnl-pos' : 'pnl-neg';
  const gainSign  = d.gain >= 0 ? '+' : '';
  document.getElementById('dash-total').textContent = '$' + fmt(d.total);
  document.getElementById('dash-total').className = 'text-xl font-bold text-slate-100';
  document.getElementById('dash-total-sub').textContent = `Budget: $${fmt(d.budget)}`;
  document.getElementById('dash-cash').textContent = '$' + fmt(d.cash);
  document.getElementById('dash-cash').className = 'text-xl font-bold text-slate-100';
  document.getElementById('dash-pnl').textContent = gainSign + '$' + fmt(d.gain);
  document.getElementById('dash-pnl').className = `text-xl font-bold ${gainColor}`;
  document.getElementById('dash-pnl-sub').textContent = gainSign + fmt(d.gain_pct) + '%';
  document.getElementById('dash-trades').textContent = d.positions.length + ' positions';
  document.getElementById('dash-trades').className = 'text-xl font-bold text-slate-100';
  document.getElementById('dash-fees').textContent = '$' + fmt(d.total_fees);
}

function _renderDashPositions(d) {
  const pb = document.getElementById('dash-positions-body');
  document.getElementById('dash-positions-mode').textContent = _runnerMode === 'simulation' ? 'Simulation' : 'Réel';
  const cashRow = `<tr class="border-t border-slate-600/40"><td class="px-4 py-2 text-slate-400">USDC</td><td class="px-4 py-2 text-right text-slate-400">${fmt(d.cash)}</td><td class="px-4 py-2 text-right text-slate-600">$1.00</td><td class="px-4 py-2 text-right text-slate-600">$1.00</td><td class="px-4 py-2 text-right text-slate-400">$${fmt(d.cash)}</td><td class="px-4 py-2 text-right text-slate-600">—</td></tr>`;
  if (!d.positions.length) { pb.innerHTML = cashRow; return; }
  pb.innerHTML = d.positions.map(p => {
    const pc = p.pnl_pct >= 0 ? 'pnl-pos' : 'pnl-neg';
    return `<tr class="hover:bg-slate-700/30"><td class="px-4 py-2 font-medium">${p.symbol.replace('USDC','')}</td><td class="px-4 py-2 text-right text-slate-300">${fmt4(p.qty)}</td><td class="px-4 py-2 text-right text-slate-300">$${fmt(p.avg_price)}</td><td class="px-4 py-2 text-right text-slate-300">${p.current_price?'$'+fmt(p.current_price):'—'}</td><td class="px-4 py-2 text-right text-slate-300">${p.value?'$'+fmt(p.value):'—'}</td><td class="px-4 py-2 text-right font-medium ${pc}">${p.pnl_pct>=0?'+':''}${fmt(p.pnl_pct)}%</td></tr>`;
  }).join('') + cashRow;
}

function _renderDashTrades(history) {
  const recent = history.slice(0, 20);
  document.getElementById('dash-trades-count').textContent = recent.length + ' derniers';
  const tb = document.getElementById('dash-trades-body');
  if (!recent.length) { tb.innerHTML = '<tr><td colspan="6" class="px-4 py-6 text-center text-slate-600">Aucun trade</td></tr>'; return; }
  tb.innerHTML = recent.map(t => {
    const badgeCls = t.action === 'BUY' ? 'badge-buy' : t.action.includes('stop') ? 'badge-sl' : 'badge-sell';
    const dt = new Date(t.timestamp+'Z').toLocaleString('fr-FR', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
    const pnl = t.pnl != null ? `<span class="${t.pnl>=0?'pnl-pos':'pnl-neg'}">${t.pnl>=0?'+':''}$${fmt(t.pnl)}</span>` : '—';
    return `<tr class="hover:bg-slate-700/30"><td class="px-3 py-2 text-slate-400">${dt}</td><td class="px-3 py-2"><span class="px-2 py-0.5 rounded-full text-xs font-medium ${badgeCls}">${t.action}</span></td><td class="px-3 py-2 font-medium">${t.symbol||'—'}</td><td class="px-3 py-2 text-right text-slate-300">${t.action==='BUY'?'$'+fmt(t.amount):fmt4(t.qty)}</td><td class="px-3 py-2 text-right text-slate-300">$${fmt(t.price)}</td><td class="px-3 py-2 text-right">${pnl}</td></tr>`;
  }).join('');
}

function _updateDashFromSim(s) {
  // Show live sim data on dashboard
  const liveEl = document.getElementById('dash-sim-live');
  liveEl.classList.remove('hidden');
  const pnlColor = (s.pnl||0)>=0?'pnl-pos':'pnl-neg';
  const sign = (s.pnl||0)>=0?'+':'';
  const wr = s.win_rate!=null?s.win_rate+'%':'—';
  const bhPnl = s.benchmark_pnl;
  const bhSign = bhPnl!=null?(bhPnl>=0?'+':''):'';
  const bhColor = bhPnl!=null?(bhPnl>=0?'pnl-pos':'pnl-neg'):'';
  const bhVal = bhPnl!=null?bhSign+'$'+fmt(bhPnl):'—';
  const alpha = s.alpha;
  const alphaSign = alpha!=null?(alpha>=0?'+':''):'';
  const alphaColor = alpha!=null?(alpha>=0?'pnl-pos':'pnl-neg'):'';
  const alphaVal = alpha!=null?alphaSign+'$'+fmt(alpha):'—';

  document.getElementById('dash-sim-kpis').innerHTML =
    kpiCard('Valeur totale', '$'+fmt(s.total_value)) +
    kpiCard('PnL net', sign+'$'+fmt(s.pnl), sign+fmt(s.pnl_pct)+'%', pnlColor) +
    kpiCard('Buy & Hold', bhVal, '', bhColor) +
    kpiCard('Alpha', alphaVal, '', alphaColor) +
    kpiCard('Frais', '$'+fmt(s.total_fees), '@ 0.1%', 'text-amber-400') +
    kpiCard('Trades', s.trades||0, `${s.buys||0} achats / ${s.sells||0} ventes`) +
    kpiCard('Stop-loss', s.stop_losses||0, '', (s.stop_losses||0)>0?'text-red-400':'') +
    kpiCard('Win rate', wr);

  // Update positions table from sim data
  const pb = document.getElementById('dash-positions-body');
  document.getElementById('dash-positions-mode').textContent = 'Simulation live';
  const cashRow = `<tr class="border-t border-slate-600/40"><td class="px-4 py-2 text-slate-400">USDC</td><td class="px-4 py-2 text-right text-slate-400">${fmt(s.cash)}</td><td class="px-4 py-2 text-right text-slate-600">$1.00</td><td class="px-4 py-2 text-right text-slate-600">$1.00</td><td class="px-4 py-2 text-right text-slate-400">$${fmt(s.cash)}</td><td class="px-4 py-2 text-right text-slate-600">—</td></tr>`;
  if (!(s.positions||[]).length) { pb.innerHTML = cashRow; }
  else {
    pb.innerHTML = (s.positions||[]).map(p => {
      const pc = p.pnl_pct>=0?'pnl-pos':'pnl-neg';
      return `<tr class="hover:bg-slate-700/30"><td class="px-4 py-2 font-medium">${p.symbol.replace('USDC','')}</td><td class="px-4 py-2 text-right text-slate-300">${fmt4(p.qty)}</td><td class="px-4 py-2 text-right text-slate-300">$${fmt(p.avg_price)}</td><td class="px-4 py-2 text-right text-slate-300">${p.current_price?'$'+fmt(p.current_price):'—'}</td><td class="px-4 py-2 text-right text-slate-300">$${fmt(p.value)}</td><td class="px-4 py-2 text-right font-medium ${pc}">${p.pnl_pct>=0?'+':''}${fmt(p.pnl_pct)}%</td></tr>`;
    }).join('') + cashRow;
  }

  // Update KPI header cards too
  document.getElementById('dash-total').textContent = '$'+fmt(s.total_value);
  document.getElementById('dash-cash').textContent = '$'+fmt(s.cash);
  document.getElementById('dash-pnl').textContent = sign+'$'+fmt(s.pnl);
  document.getElementById('dash-pnl').className = `text-xl font-bold ${pnlColor}`;
  document.getElementById('dash-pnl-sub').textContent = sign+fmt(s.pnl_pct)+'%';
  document.getElementById('dash-trades').textContent = (s.trades||0)+' trades';
  document.getElementById('dash-winrate').textContent = wr;
  document.getElementById('dash-winrate').className = `text-xl font-bold ${(s.win_rate||0)>=50?'pnl-pos':'pnl-neg'}`;
  document.getElementById('dash-fees').textContent = '$'+fmt(s.total_fees);

  // Recent trades from sim
  _renderDashTrades(s.history || []);
}

// ─── Sessions management ─────────────────────────────────────────────────────
async function _loadSimList() {
  const body = document.getElementById('dash-sessions-body');
  if (!body) return;
  try {
    const sessions = await fetch('/api/simulation/sessions').then(r => r.json());
    if (!Array.isArray(sessions) || !sessions.length) {
      body.innerHTML = '<div class="px-4 py-4 text-center text-slate-600">Aucune session</div>';
      return;
    }
    body.innerHTML = sessions.map(s => {
      const sid = s.id||s.session_id;
      const name = escHtml(s.name||s.session_name||sid);
      const dateTs = s.created_at||s.start_ts;
      const date = dateTs ? new Date(dateTs+(dateTs.includes('Z')?'':'Z')).toLocaleString('fr-FR',{dateStyle:'short',timeStyle:'short'}) : '—';
      const trades = s.trade_count!=null?s.trade_count:'?';
      return `<div class="flex items-center justify-between px-4 py-2.5 hover:bg-slate-700/30">
        <div class="flex items-center gap-3 min-w-0"><span class="font-medium text-slate-200 truncate">${name}</span><span class="text-slate-500 shrink-0">${date}</span><span class="text-slate-600 shrink-0">${trades} trades</span></div>
        <div class="flex items-center gap-1 shrink-0 ml-2">
          <button data-action="rename" data-sid="${sid}" data-name="${name}" class="px-2 py-1 text-slate-500 hover:text-blue-400" title="Renommer">✏</button>
          <button data-action="delete" data-sid="${sid}" data-name="${name}" class="px-2 py-1 text-slate-500 hover:text-red-400" title="Supprimer">✕</button>
        </div></div>`;
    }).join('');
    // Event delegation for rename/delete buttons
    body.onclick = e => {
      const btn = e.target.closest('button[data-action]');
      if (!btn) return;
      const {action, sid, name} = btn.dataset;
      if (action === 'rename') _simListRename(sid, name);
      else if (action === 'delete') _simListDelete(sid, name);
    };
  } catch { body.innerHTML = '<div class="px-4 py-4 text-center text-red-400">Erreur</div>'; }
}

async function _simListRename(sid, currentName) {
  const n = prompt('Nouveau nom :', currentName);
  if (!n || n.trim() === currentName) return;
  await fetch(`/api/simulation/sessions/${sid}`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:n.trim()}) });
  _loadSimList(); if (currentTab==='performance') _loadPerfSessions();
}

async function _simListDelete(sid, name) {
  if (!confirm(`Supprimer "${name}" et toutes ses données ?`)) return;
  await fetch(`/api/simulation/sessions/${sid}`, { method:'DELETE' });
  _loadSimList(); if (_perfSessionId===sid) { _perfSessionId=''; loadPerformance(); }
  if (currentTab==='performance') _loadPerfSessions();
}

// ═══════════════════════════════════════════════════════════════════════════════
// MARKETS TAB
// ═══════════════════════════════════════════════════════════════════════════════
async function loadMarkets() {
  try {
    const d = await fetch('/api/portfolio').then(r => r.json());
    if (!d.error) _populateTradeDropdowns(d);
  } catch {}
  // Load enriched watchlist data
  try {
    const d = await fetch('/api/watchlist/enriched').then(r => r.json());
    _renderWatchlist(d);
  } catch {
    // Fallback: basic market data from portfolio
    if (_dashPortfolioData) {
      const tb = document.getElementById('mkt-watchlist-body');
      tb.innerHTML = (_dashPortfolioData.market||[]).map(m =>
        `<tr class="hover:bg-slate-700/30"><td class="px-4 py-2 font-medium">${m.symbol.replace('USDC','')}</td><td class="px-4 py-2 text-right">$${fmt(m.price)}</td><td class="px-4 py-2 text-right text-slate-500">—</td><td class="px-4 py-2 text-right text-slate-500">—</td><td class="px-4 py-2 text-center text-slate-500">—</td><td class="px-4 py-2 text-right text-slate-500">—</td><td class="px-4 py-2 text-right text-slate-500">—</td></tr>`
      ).join('');
    }
  }
}

function _renderWatchlist(data) {
  const items = data.items || data;
  if (!Array.isArray(items) || !items.length) return;
  const tb = document.getElementById('mkt-watchlist-body');
  tb.innerHTML = items.map(m => {
    const chgColor = (m.change_pct_24h||0)>=0?'pnl-pos':'pnl-neg';
    const chgSign = (m.change_pct_24h||0)>=0?'+':'';
    const rsi = m.rsi14!=null?m.rsi14.toFixed(0):'—';
    const rsiColor = m.rsi14>70?'text-red-400':m.rsi14<30?'text-green-400':'text-slate-300';
    const trendArrow = m.trend==='up'?'▲':m.trend==='down'?'▼':'●';
    const trendColor = m.trend==='up'?'trend-up':m.trend==='down'?'trend-down':'trend-flat';
    const score = m.score!=null?m.score:'—';
    const scoreColor = m.score>=7?'text-green-400':m.score<=3?'text-red-400':'text-slate-300';
    const vol = m.volume_usdc ? '$'+Number(m.volume_usdc/1e6).toFixed(1)+'M' : '—';
    return `<tr class="hover:bg-slate-700/30 border-b border-slate-700/30">
      <td class="px-4 py-3 font-medium">${(m.symbol||'').replace('USDC','')}</td>
      <td class="px-4 py-3 text-right font-mono">$${fmt(m.price)}</td>
      <td class="px-4 py-3 text-right ${chgColor}">${chgSign}${(m.change_pct_24h||0).toFixed(2)}%</td>
      <td class="px-4 py-3 text-right ${rsiColor}">${rsi}</td>
      <td class="px-4 py-3 text-center ${trendColor}">${trendArrow}</td>
      <td class="px-4 py-3 text-right font-medium ${scoreColor}">${score}/10</td>
      <td class="px-4 py-3 text-right text-slate-400">${vol}</td>
    </tr>`;
  }).join('');
}

function _populateTradeDropdowns(d) {
  const buySelect = document.getElementById('buy-symbol');
  const prev = buySelect.value;
  buySelect.innerHTML = '<option value="">— choisir —</option>' +
    (d.market||[]).map(m => `<option value="${m.symbol}">${m.symbol.replace('USDC','')} — $${fmt(m.price)}</option>`).join('');
  if (prev) buySelect.value = prev;
  const sellSelect = document.getElementById('sell-symbol');
  const prevSell = sellSelect.value;
  sellSelect.innerHTML = '<option value="">— choisir —</option>' +
    (d.positions||[]).map(p => `<option value="${p.symbol}" data-qty="${p.qty}">${p.symbol.replace('USDC','')} — ${fmt4(p.qty)}</option>`).join('');
  if (prevSell) sellSelect.value = prevSell;
  _onSellSymbolChange();
}

function _onSellSymbolChange() {
  const sel = document.getElementById('sell-symbol');
  const opt = sel.options[sel.selectedIndex];
  if (opt && opt.dataset.qty) document.getElementById('sell-qty').value = opt.dataset.qty;
}

async function executeBuy() {
  const symbol = document.getElementById('buy-symbol').value.trim().toUpperCase();
  const amount = parseFloat(document.getElementById('buy-amount').value);
  if (!symbol || !amount) { toast('Remplis le symbole et le montant', 'warn'); return; }
  try {
    const d = await fetch('/api/trade/buy', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({symbol,amount}) }).then(r => r.json());
    if (d.ok) { toast(`Achat ${symbol} à $${fmt(d.price)}`, 'ok'); loadDashboard(); }
    else toast(d.error || 'Erreur', 'error');
  } catch { toast('Erreur réseau', 'error'); }
}

async function executeSell() {
  const symbol = document.getElementById('sell-symbol').value.trim().toUpperCase();
  const qty = parseFloat(document.getElementById('sell-qty').value);
  if (!symbol || !qty) { toast('Remplis le symbole et la quantité', 'warn'); return; }
  try {
    const d = await fetch('/api/trade/sell', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({symbol,qty}) }).then(r => r.json());
    if (d.ok) { toast(`Vente ${symbol} à $${fmt(d.price)}`, 'ok'); loadDashboard(); }
    else toast(d.error || 'Erreur', 'error');
  } catch { toast('Erreur réseau', 'error'); }
}

// ─── Analysis ────────────────────────────────────────────────────────────────
let _analysisPollIv = null;

async function _startAnalysis() {
  const btn = document.getElementById('analysis-btn');
  const lbl = document.getElementById('analysis-btn-label');
  const ico = document.getElementById('analysis-btn-icon');
  btn.disabled = true; lbl.textContent = 'Analyse…'; ico.innerHTML = '<div class="spinner"></div>';
  document.getElementById('analysis-error').classList.add('hidden');
  try {
    await fetch('/api/analysis/start', { method:'POST' });
    _analysisPollIv = setInterval(_pollAnalysis, 2000);
  } catch { _analysisError('Erreur réseau'); }
}

async function _pollAnalysis() {
  try {
    const d = await fetch('/api/analysis/status').then(r => r.json());
    if (d.running) return;
    clearInterval(_analysisPollIv);
    const btn = document.getElementById('analysis-btn');
    const lbl = document.getElementById('analysis-btn-label');
    const ico = document.getElementById('analysis-btn-icon');
    btn.disabled = false; lbl.textContent = 'Analyser'; ico.textContent = '◈';
    if (d.error) { _analysisError(d.error); return; }
    if (d.result) _renderAnalysis(d.result);
  } catch {}
}

function _analysisError(msg) {
  document.getElementById('analysis-error').textContent = msg;
  document.getElementById('analysis-error').classList.remove('hidden');
  const btn = document.getElementById('analysis-btn');
  btn.disabled = false;
  document.getElementById('analysis-btn-label').textContent = 'Analyser';
  document.getElementById('analysis-btn-icon').textContent = '◈';
}

function _renderAnalysis(r) {
  // Global sentiment
  const gEl = document.getElementById('analysis-global');
  gEl.classList.remove('hidden');
  const sentColor = r.global_sentiment==='bullish'?'text-green-400':r.global_sentiment==='bearish'?'text-red-400':'text-slate-300';
  document.getElementById('analysis-global-sentiment').className = `text-lg font-bold ${sentColor}`;
  document.getElementById('analysis-global-sentiment').textContent = (r.global_sentiment||'neutral').toUpperCase();
  document.getElementById('analysis-global-summary').textContent = r.market_summary || '';
  document.getElementById('analysis-ts').textContent = r.generated_at ? new Date(r.generated_at+'Z').toLocaleString('fr-FR') : '';

  // Per-symbol cards
  const cards = document.getElementById('analysis-cards');
  cards.innerHTML = (r.analyses||[]).map(a => {
    const sColor = a.sentiment==='bullish'?'text-green-400 bg-green-900/30':a.sentiment==='bearish'?'text-red-400 bg-red-900/30':'text-slate-300 bg-slate-700';
    const scenarios = (a.scenarios||[]).map(sc => {
      const name = sc.name || sc.type || '?';
      const col = name==='bull'?'text-green-400':name==='bear'?'text-red-400':'text-slate-300';
      const p24 = sc.price_24h ? `$${fmt(sc.price_24h)}` : '—';
      const p7  = sc.price_7j  ? `$${fmt(sc.price_7j)}`  : '—';
      const prob = sc.probability != null ? `${sc.probability}%` : '—';
      return `<div class="flex justify-between items-center py-1 border-b border-slate-700/30 gap-2">
        <span class="${col} font-medium w-10">${name}</span>
        <span class="text-slate-400 text-xs">24h: ${p24}</span>
        <span class="text-slate-400 text-xs">7j: ${p7}</span>
        <span class="text-slate-500 text-xs">${prob}</span>
      </div>`;
    }).join('');
    const actionScore = a.action==='buy'?+1:a.action==='sell'?-1:0;
    const actionLabel = a.action==='buy'?'ACHAT':a.action==='sell'?'VENTE':'NEUTRE';
    const actionColor = a.action==='buy'?'text-green-400 bg-green-900/30 border-green-700/50'
      :a.action==='sell'?'text-red-400 bg-red-900/30 border-red-700/50'
      :'text-slate-400 bg-slate-700/50 border-slate-600/50';
    const scoreSign = actionScore>0?'+':'';
    const actionHtml = a.action ? `
      <div class="flex items-center gap-2 mb-2 p-2 rounded-lg border ${actionColor}">
        <span class="text-lg font-bold shrink-0">${scoreSign}${actionScore}</span>
        <div class="min-w-0">
          <span class="text-xs font-medium">${actionLabel}</span>
          ${a.action_reason?`<p class="text-xs opacity-80 mt-0.5">${escHtml(a.action_reason)}</p>`:''}
        </div>
      </div>` : '';
    return `<div class="bg-slate-800 border border-slate-700 rounded-xl p-4">
      <div class="flex items-center justify-between mb-2">
        <span class="font-medium text-slate-200">${(a.symbol||'').replace('USDC','')}</span>
        <span class="px-2 py-0.5 rounded-full text-xs font-medium ${sColor}">${(a.sentiment||'').toUpperCase()}</span>
      </div>
      ${actionHtml}
      ${a.current_price?`<div class="text-xs text-slate-400 mb-2">Prix: $${fmt(a.current_price)}</div>`:''}
      <p class="text-xs text-slate-300 mb-3">${a.summary||''}</p>
      ${scenarios?`<div class="text-xs">${scenarios}</div>`:''}
    </div>`;
  }).join('');
}

// ═══════════════════════════════════════════════════════════════════════════════
// PERFORMANCE TAB
// ═══════════════════════════════════════════════════════════════════════════════
let _perfSessionId = '';
let _perfChart     = null;
let _perfPnlChart  = null;

async function _initPerformanceTab() {
  const sel = document.getElementById('perf-session-sel');
  const actions = document.getElementById('perf-session-actions');
  if (_runnerMode === 'simulation') {
    sel.classList.remove('hidden'); actions.classList.remove('hidden');
    await _loadPerfSessions();
  } else {
    sel.classList.add('hidden'); actions.classList.add('hidden');
  }
  loadPerformance();
}

async function _loadPerfSessions() {
  try {
    const sessions = await fetch('/api/simulation/sessions').then(r => r.json());
    const sel = document.getElementById('perf-session-sel');
    if (!Array.isArray(sessions) || !sessions.length) { sel.innerHTML = '<option value="">Aucune session</option>'; return; }
    sel.innerHTML = sessions.map(s => {
      const sid = s.id||s.session_id;
      const name = s.name||s.session_name||sid;
      return `<option value="${sid}">${name}</option>`;
    }).join('');
    if (!_perfSessionId) _perfSessionId = sessions[0].id||sessions[0].session_id;
    sel.value = _perfSessionId;
    sel.onchange = () => { _perfSessionId = sel.value; loadPerformance(); };
  } catch {}
}

async function _renameSession() {
  if (!_perfSessionId) return;
  const n = prompt('Nouveau nom :');
  if (!n) return;
  await fetch(`/api/simulation/sessions/${_perfSessionId}`, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:n.trim()}) });
  await _loadPerfSessions();
}

async function _deleteSession() {
  if (!_perfSessionId) return;
  if (!confirm('Supprimer cette session et toutes ses données ?')) return;
  await fetch(`/api/simulation/sessions/${_perfSessionId}`, { method:'DELETE' });
  _perfSessionId = '';
  await _loadPerfSessions();
  loadPerformance();
}

function setPeriod(btn, period) {
  document.querySelectorAll('.perf-period-btn').forEach(b => {
    b.classList.remove('bg-slate-600','text-white','font-medium');
    b.classList.add('text-slate-400');
  });
  btn.classList.add('bg-slate-600','text-white','font-medium');
  btn.classList.remove('text-slate-400');
  currentPeriod = period;
  loadPerformance();
}

async function loadPerformance() {
  const sessionParam = _runnerMode==='simulation'&&_perfSessionId ? `&session_id=${_perfSessionId}` : '';
  try {
    const d = await fetch(`/api/performance?period=${currentPeriod}&mode=${_runnerMode}${sessionParam}`).then(r => r.json());
    _renderPerfKPIs(d);
    _renderPerfChart(d);
    _renderPerfPnlBySymbol(d);
    _renderPerfHistory(d);
    // Load session details if applicable
    if (_runnerMode==='simulation' && _perfSessionId) { _loadSessionDetail(_perfSessionId); _loadRunLogs(_perfSessionId); }
    else { document.getElementById('perf-session-detail').classList.add('hidden'); document.getElementById('perf-run-logs').classList.add('hidden'); }
  } catch {}
}

function _renderPerfKPIs(d) {
  const netColor = d.net>=0?'pnl-pos':'pnl-neg';
  const netSign = d.net>=0?'+':'';
  document.getElementById('perf-kpi-cards').innerHTML =
    kpiCard('Transactions', d.trades) +
    kpiCard('Achats / Ventes', `${d.buys} / ${d.sells}${d.stop_losses?` (+${d.stop_losses} SL)`:''}`, '', 'text-slate-100') +
    kpiCard('Win rate', d.win_rate!=null?d.win_rate+'%':'—', '', d.win_rate>=50?'text-green-400':'text-red-400') +
    kpiCard('Frais', '$'+fmt(d.fees), '', 'text-amber-400') +
    kpiCard('Net P&L', netSign+'$'+fmt(d.net), 'ventes-achats-frais', netColor);
  document.getElementById('perf-trade-count').textContent = d.history.length+' trades';
}

function _renderPerfChart(d) {
  const ctx = document.getElementById('perf-chart').getContext('2d');
  if (_perfChart) { _perfChart.destroy(); _perfChart = null; }
  if (!d.timeseries?.length || d.timeseries.length < 2) return;
  _perfChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: d.timeseries.map(p => new Date(p.ts+'Z').toLocaleString('fr-FR', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})),
      datasets: [{ label: 'PnL cumulé', data: d.timeseries.map(p => p.v), borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,0.08)', borderWidth: 1.5, pointRadius: 0, fill: true, tension: 0.3 }],
    },
    options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } },
      scales: { x: { ticks: { color: '#64748b', maxTicksLimit: 8, font: { size: 10 } }, grid: { color: '#1e293b' } },
                y: { ticks: { color: '#64748b', font: { size: 10 }, callback: v => (v>=0?'+':'')+v.toFixed(2) }, grid: { color: '#1e293b' } } } },
  });
}

function _renderPerfPnlBySymbol(d) {
  const ctx = document.getElementById('perf-pnl-chart').getContext('2d');
  if (_perfPnlChart) { _perfPnlChart.destroy(); _perfPnlChart = null; }
  // Compute P&L per symbol from history
  const pnlMap = {};
  (d.history||[]).forEach(t => {
    if (t.pnl != null && t.symbol) {
      const sym = t.symbol.replace('USDC','');
      pnlMap[sym] = (pnlMap[sym]||0) + t.pnl;
    }
  });
  const symbols = Object.keys(pnlMap);
  if (!symbols.length) return;
  const values = symbols.map(s => pnlMap[s]);
  const colors = values.map(v => v >= 0 ? '#34d399' : '#f87171');
  _perfPnlChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: symbols,
      datasets: [{ data: values.map(v => +v.toFixed(2)), backgroundColor: colors, borderRadius: 4 }],
    },
    options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } },
      scales: { x: { ticks: { color: '#64748b', font: { size: 10 } }, grid: { display: false } },
                y: { ticks: { color: '#64748b', font: { size: 10 }, callback: v => '$'+v }, grid: { color: '#1e293b' } } } },
  });
}

function _renderPerfHistory(d) {
  const tb = document.getElementById('perf-history-body');
  if (!d.history.length) { tb.innerHTML = '<tr><td colspan="8" class="px-4 py-8 text-center text-slate-500">Aucune transaction</td></tr>'; return; }
  tb.innerHTML = d.history.map(t => {
    const badgeCls = t.action==='BUY'?'badge-buy':t.action.includes('stop')?'badge-sl':'badge-sell';
    const dt = new Date(t.timestamp+'Z').toLocaleString('fr-FR', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
    const pnl = t.pnl!=null?`<span class="${t.pnl>=0?'pnl-pos':'pnl-neg'}">${t.pnl>=0?'+':''}$${fmt(t.pnl)}</span>`:'—';
    const reason = t.reason || '—';
    return `<tr class="hover:bg-slate-700/30"><td class="px-4 py-2.5 text-slate-400 whitespace-nowrap">${dt}</td><td class="px-4 py-2.5"><span class="px-2 py-0.5 rounded-full text-xs font-medium ${badgeCls}">${t.action}</span></td><td class="px-4 py-2.5 font-medium">${t.symbol||'—'}</td><td class="px-4 py-2.5 text-right text-slate-300">${fmt4(t.amount)}</td><td class="px-4 py-2.5 text-right text-slate-300">$${fmt(t.price)}</td><td class="px-4 py-2.5 text-right text-amber-400/80">${t.fee?'$'+fmt(t.fee):'—'}</td><td class="px-4 py-2.5 text-right">${pnl}</td><td class="px-4 py-2.5 text-slate-400 max-w-xs truncate cursor-pointer select-none reason-cell" title="Cliquer pour développer">${reason}</td></tr>`;
  }).join('');
}

async function _loadSessionDetail(sessionId) {
  const card = document.getElementById('perf-session-detail');
  if (!sessionId) { card.classList.add('hidden'); return; }
  card.classList.remove('hidden');
  try {
    const s = await fetch(`/api/simulation/sessions/${sessionId}/detail`).then(r => r.json());
    const st = s.initial_state || {};
    card.innerHTML = `<h3 class="text-sm font-medium text-slate-200">Session</h3>
      <div class="text-xs text-slate-400">Budget: $${fmt(st.budget)} | ID: ${sessionId} | ${s.created_at ? new Date(s.created_at+'Z').toLocaleString('fr-FR') : '—'}</div>`;
  } catch { card.classList.add('hidden'); }
}

async function _loadRunLogs(sessionId) {
  const wrap = document.getElementById('perf-run-logs');
  const body = document.getElementById('perf-run-logs-body');
  if (!sessionId) { wrap.classList.add('hidden'); return; }
  wrap.classList.remove('hidden');
  try {
    const logs = await fetch(`/api/logs?session_id=${sessionId}&limit=300`).then(r => r.json());
    if (!logs.length) { body.innerHTML = '<div class="text-slate-500 py-4 text-center">Aucun log</div>'; return; }
    body.innerHTML = [...logs].reverse().map(l => {
      const ts = l.timestamp ? new Date(l.timestamp+'Z').toLocaleTimeString('fr-FR') : '';
      const cls = classifyLine(l.message);
      return `<div class="log-line ${cls}"><span class="text-slate-600 mr-1">${ts}</span>${escHtml(l.message)}</div>`;
    }).join('');
  } catch {}
}

function _setRunLogFilter(btn) {
  document.querySelectorAll('.perf-log-filter').forEach(b => b.classList.toggle('active', b===btn));
  // Re-load with filter (simplified - reload all)
  if (_perfSessionId) _loadRunLogs(_perfSessionId);
}

// ═══════════════════════════════════════════════════════════════════════════════
// BACKTEST TAB (preserved from v1)
// ═══════════════════════════════════════════════════════════════════════════════
let _btPollTimer = null;
let _btHistLen = -1;
let _btHistFilterSig = '';
let _lastBtHist = [];
let _btLlmMode = false;
let _btChart = null;
let _btChartInitP = {};
let _btChartLastTs = null;
let _btChartSymbols = [];
let _btChartRaw = [];
let _btChartHours = 24;
const _BT_PALETTE = ['#f59e0b','#6366f1','#8b5cf6','#06b6d4','#eab308','#ec4899','#10b981','#f87171'];
const _BT_KNOWN = { BTC:'#f59e0b', ETH:'#6366f1', SOL:'#8b5cf6', XRP:'#06b6d4', BNB:'#eab308' };

function _updateBtSpeedLabel() { document.getElementById('bt-speed-label').textContent = document.getElementById('bt-speed').value + 'x'; }
async function _sendBtSpeed() { try { await fetch('/api/backtest/speed', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({speed:parseInt(document.getElementById('bt-speed').value)})}); } catch {} }
function _btSpeedChange() { _sendBtSpeed(); }

function _toggleBtLlm() {
  _btLlmMode = !_btLlmMode;
  const btn = document.getElementById('bt-llm-btn');
  const hint = document.getElementById('bt-llm-hint');
  if (_btLlmMode) {
    btn.textContent = 'LLM On'; btn.className = 'px-3 py-1.5 rounded-lg text-xs font-semibold border transition-colors border-purple-500 bg-purple-900/50 text-purple-300';
    hint.textContent = 'Agent LLM (réaliste)';
    document.getElementById('bt-rule-params').classList.add('hidden');
    document.getElementById('bt-llm-params').classList.remove('hidden');
  } else {
    btn.textContent = 'LLM Off'; btn.className = 'px-3 py-1.5 rounded-lg text-xs font-semibold border transition-colors border-slate-600 bg-slate-700 text-slate-400';
    hint.textContent = 'Rule-based';
    document.getElementById('bt-rule-params').classList.remove('hidden');
    document.getElementById('bt-llm-params').classList.add('hidden');
  }
}

function _updateLlmEveryLabel() {
  const v = parseInt(document.getElementById('bt-llm-every').value);
  document.getElementById('bt-llm-every-label').textContent = v===1?'chaque bougie':`toutes les ${v} bougies`;
}

function _filterBtChart(btn) {
  document.querySelectorAll('.bt-chart-filter').forEach(b => { b.classList.remove('bg-slate-600','text-slate-200'); b.classList.add('text-slate-400'); });
  btn.classList.add('bg-slate-600','text-slate-200'); btn.classList.remove('text-slate-400');
  _btChartHours = parseInt(btn.dataset.hours);
  _redrawBtChart();
}

function _initBtChart(symbols) {
  _btChartSymbols=symbols; _btChartInitP={}; _btChartLastTs=null; _btChartRaw=[];
  if (_btChart) { _btChart.destroy(); _btChart=null; }
  const cryptoDs = symbols.map((sym,i) => {
    const short = sym.replace('USDC',''); const color = _BT_KNOWN[short]||_BT_PALETTE[i%_BT_PALETTE.length];
    return {label:short, symbolKey:sym, data:[], borderColor:color, backgroundColor:color+'18', borderWidth:1.5, pointRadius:0, tension:0.2};
  });
  _btChart = new Chart(document.getElementById('bt-chart').getContext('2d'), {
    type:'line', data:{labels:[],datasets:[
      {label:'Portfolio',data:[],borderColor:'#3b82f6',backgroundColor:'#3b82f618',borderWidth:2.5,pointRadius:0,tension:0.2},
      {label:'Buy & Hold',data:[],borderColor:'#64748b',backgroundColor:'transparent',borderWidth:1.5,borderDash:[5,4],pointRadius:0,tension:0.2},
      ...cryptoDs]},
    options:{animation:false,responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},
      plugins:{legend:{labels:{color:'#94a3b8',font:{size:11},boxWidth:16,padding:16}},tooltip:{backgroundColor:'#1e293b',borderColor:'#334155',borderWidth:1,titleColor:'#94a3b8',bodyColor:'#e2e8f0',callbacks:{label:c=>` ${c.dataset.label}: ${c.parsed.y>=0?'+':''}${c.parsed.y.toFixed(2)}%`}}},
      scales:{x:{ticks:{color:'#475569',font:{size:10},maxTicksLimit:10},grid:{color:'#1e293b'}},y:{ticks:{color:'#475569',font:{size:10},callback:v=>(v>=0?'+':'')+v.toFixed(1)+'%'},grid:{color:'#334155'}}}}
  });
}

function _redrawBtChart() {
  if (!_btChart) return;
  const raw = _btChartHours===0?_btChartRaw:_btChartRaw.slice(-Math.max(1,_btChartHours));
  _btChart.data.labels = raw.map(d=>d.label);
  _btChart.data.datasets[0].data = raw.map(d=>d.portfolio);
  _btChart.data.datasets[1].data = raw.map(d=>d.bh);
  _btChart.data.datasets.slice(2).forEach(ds=>{ds.data=raw.map(d=>d.cryptos[ds.symbolKey]??null);});
  _btChart.update('none');
}

function _updateBtChart(snap) {
  if (!_btChart||snap.loading||!snap.current_ts) return;
  if (snap.current_ts===_btChartLastTs) return;
  _btChartLastTs=snap.current_ts;
  const prices=snap.prices||{};
  if(!Object.keys(_btChartInitP).length&&Object.keys(prices).length) _btChartInitP={...prices};
  const dt=new Date(snap.current_ts.replace(' ','T')+'Z');
  const label=dt.toLocaleString('fr-FR',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
  const cryptos={};
  _btChartSymbols.forEach(sym=>{const p=prices[sym],p0=_btChartInitP[sym]; cryptos[sym]=(p&&p0)?+((p/p0-1)*100).toFixed(2):null;});
  _btChartRaw.push({label,portfolio:+(snap.pnl_pct||0).toFixed(2),bh:+(snap.benchmark_pnl_pct||0).toFixed(2),cryptos});
  _redrawBtChart();
}

async function startBacktest() {
  const checked = [...document.querySelectorAll('#bt-symbols-list input[type=checkbox]:checked')];
  const symbols = checked.map(cb=>cb.value).join(',');
  if (!symbols) { toast('Sélectionne au moins un symbole','warn'); return; }
  const body = {
    symbols, start_date: document.getElementById('bt-start-date').value||null,
    budget: parseFloat(document.getElementById('bt-budget').value),
    stop_loss_pct: parseFloat(document.getElementById('bt-stop-loss').value),
    trailing_stop_pct: parseFloat(document.getElementById('bt-trailing-stop').value),
    risk_level: parseInt(document.getElementById('bt-risk').value),
    buy_threshold: parseInt(document.getElementById('bt-buy-thr').value),
    sell_threshold: parseInt(document.getElementById('bt-sell-thr').value),
    sell_cooldown_cycles: parseInt(document.getElementById('bt-sell-cooldown').value),
    speed: parseInt(document.getElementById('bt-speed').value),
    llm_mode: _btLlmMode, llm_every_n_candles: parseInt(document.getElementById('bt-llm-every').value),
  };
  document.getElementById('bt-start-btn').disabled=true;
  document.getElementById('bt-stop-btn').disabled=false;
  _btHistLen=-1; _btHistFilterSig='';
  _initBtChart(symbols.split(',').map(s=>s.trim()).filter(Boolean));
  document.getElementById('bt-status-label').textContent='Chargement…';
  document.getElementById('bt-progress-bar').style.width='0%';
  try {
    const d = await fetch('/api/backtest/start', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
    if (d.error) { toast(d.error,'error'); _btDone(); return; }
    _btPollTimer = setInterval(_pollBacktest, 300);
  } catch { toast('Erreur','error'); _btDone(); }
}

async function stopBacktest() { document.getElementById('bt-stop-btn').disabled=true; await fetch('/api/backtest/stop',{method:'POST'}); }

function _btDone() { clearInterval(_btPollTimer); document.getElementById('bt-start-btn').disabled=false; document.getElementById('bt-stop-btn').disabled=true; }

async function _pollBacktest() {
  try {
    const d = await fetch('/api/backtest/status').then(r=>r.json());
    const s = d.snapshot||{};
    _setBtUI(s, d.running);
    if (s&&!s.error&&!s.loading) { _updateBtChart(s); _updateBtDisplay(s); }
    if (!d.running) { _btDone(); if(s&&!s.error&&s.pnl!==undefined){const sign=(s.pnl||0)>=0?'+':''; toast(`Backtest terminé — PnL: ${sign}$${fmt(s.pnl)}`,(s.pnl||0)>=0?'ok':'warn');} }
  } catch {}
}

function _setBtUI(snap, running) {
  if (!snap) return;
  const bar=document.getElementById('bt-progress-bar'), label=document.getElementById('bt-status-label'), prog=document.getElementById('bt-progress-label'), tsEl=document.getElementById('bt-ts-label');
  if (snap.loading) { label.textContent=snap.message||'Chargement…'; bar.style.width='5%'; prog.textContent=''; tsEl.textContent=''; return; }
  const step=snap.cycle||0, total=snap.total_steps||0, pct=total>0?Math.round(step/total*100):0;
  bar.style.width = (!running&&step>0?'100':pct)+'%';
  label.textContent = running?`Étape ${step}/${total}`:(step>0?'Terminé':'—');
  prog.textContent = total>0?pct+'%':'';
  tsEl.textContent = snap.current_ts ? new Date(snap.current_ts).toLocaleString('fr-FR',{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'}) : '';
}

function _updateBtDisplay(r) {
  if (!r||r.error||r.loading) return;
  const pnlColor=(r.pnl||0)>=0?'pnl-pos':'pnl-neg', sign=(r.pnl||0)>=0?'+':'';
  const bhPnl=r.benchmark_pnl, bhSign=bhPnl!=null?(bhPnl>=0?'+':''):'', bhColor=bhPnl!=null?(bhPnl>=0?'pnl-pos':'pnl-neg'):'';
  const alpha=r.alpha, aSign=alpha!=null?(alpha>=0?'+':''):'', aColor=alpha!=null?(alpha>=0?'pnl-pos':'pnl-neg'):'';
  document.getElementById('bt-kpi-cards').innerHTML =
    kpiCard('Valeur totale','$'+fmt(r.total_value)) +
    kpiCard('PnL net',sign+'$'+fmt(r.pnl),sign+fmt(r.pnl_pct)+'%',pnlColor) +
    kpiCard('Buy & Hold',bhPnl!=null?bhSign+'$'+fmt(bhPnl):'—','',bhColor) +
    kpiCard('Alpha',alpha!=null?aSign+'$'+fmt(alpha):'—','',aColor) +
    kpiCard('Frais','$'+fmt(r.total_fees),'','text-amber-400') +
    kpiCard('Trades',r.trades||0,`${r.buys||0}/${r.sells||0}`) +
    kpiCard('Stop-loss',r.stop_losses||0,'',(r.stop_losses||0)>0?'text-red-400':'') +
    kpiCard('Win rate',r.win_rate!=null?r.win_rate+'%':'—');

  // Positions
  const pb=document.getElementById('bt-positions-body');
  const cash=`<tr class="border-t border-slate-600/40"><td class="px-4 py-2 text-slate-400">USDC</td><td class="px-4 py-2 text-right">${fmt(r.cash)}</td><td class="px-4 py-2 text-right text-slate-600">$1.00</td><td class="px-4 py-2 text-right text-slate-600">$1.00</td><td class="px-4 py-2 text-right">${'$'+fmt(r.cash)}</td><td class="px-4 py-2 text-right text-slate-600">—</td></tr>`;
  pb.innerHTML = (r.positions||[]).map(p=>{const u=p.current_price!=null?(p.current_price-p.avg_price)*p.qty:null; const c=u!=null?(u>=0?'pnl-pos':'pnl-neg'):''; return `<tr class="hover:bg-slate-700/30"><td class="px-4 py-2 font-medium">${p.symbol.replace('USDC','')}</td><td class="px-4 py-2 text-right">${fmt4(p.qty)}</td><td class="px-4 py-2 text-right">$${fmt(p.avg_price)}</td><td class="px-4 py-2 text-right">${p.current_price?'$'+fmt(p.current_price):'—'}</td><td class="px-4 py-2 text-right">$${fmt(p.value)}</td><td class="px-4 py-2 text-right ${c}">${u!=null?(u>=0?'+':'')+'$'+fmt(u):'—'}</td></tr>`;}).join('') + cash;

  // History
  const hist = (r.history||[]).filter(t=>t.action!=='ANALYSE');
  _lastBtHist = hist;
  document.getElementById('bt-history-count').textContent = hist.length+' trades';
  const sig = [...document.querySelectorAll('#bt-hist-filter-list input:checked')].map(b=>b.value).join(',');
  if (hist.length!==_btHistLen||sig!==_btHistFilterSig) { _btHistLen=hist.length; _btHistFilterSig=sig; _redrawBtHistory(hist); }
}

function _redrawBtHistory(hist) {
  const checked=[...document.querySelectorAll('#bt-hist-filter-list input:checked')].map(b=>b.value);
  const all=[...document.querySelectorAll('#bt-hist-filter-list input')].map(b=>b.value);
  const filterOn=checked.length>0&&checked.length<all.length;
  const rows=filterOn?hist.filter(t=>checked.includes(t.symbol)):hist;
  const hb=document.getElementById('bt-history-body');
  if (!rows.length) { hb.innerHTML='<tr><td colspan="11" class="px-4 py-6 text-center text-slate-600">Aucun trade</td></tr>'; return; }
  hb.innerHTML = rows.map(t=>{
    const bc=t.action==='BUY'?'badge-buy':t.action.includes('stop')?'badge-sl':'badge-sell';
    const pnl=t.pnl!=null?`<span class="${t.pnl>=0?'pnl-pos':'pnl-neg'}">${t.pnl>=0?'+':''}$${fmt(t.pnl)}</span>`:'—';
    const dt=t.timestamp?new Date(t.timestamp+(t.timestamp.endsWith('Z')?'':'Z')).toLocaleString('fr-FR',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';
    const sc=t.score!=null?`<span class="px-1 py-0.5 rounded text-xs ${t.score>=7?'bg-green-900/60 text-green-400':t.score<=3?'bg-red-900/60 text-red-400':'bg-slate-700 text-slate-300'}">${t.score}/10</span>`:'—';
    return `<tr class="hover:bg-slate-700/30"><td class="px-3 py-2 text-slate-400">${t.cycle||'—'}</td><td class="px-3 py-2 text-slate-400">${dt}</td><td class="px-3 py-2"><span class="px-2 py-0.5 rounded-full text-xs font-medium ${bc}">${t.action}</span></td><td class="px-3 py-2 font-medium">${t.symbol}</td><td class="px-3 py-2 text-right">${t.amount!=null?'$'+fmt(t.amount):'—'}</td><td class="px-3 py-2 text-right">${t.qty!=null?fmt4(t.qty):'—'}</td><td class="px-3 py-2 text-right">$${fmt(t.price)}</td><td class="px-3 py-2 text-right text-amber-400/80">$${fmt(t.fee)}</td><td class="px-3 py-2 text-right">${pnl}</td><td class="px-3 py-2 text-right">${sc}</td><td class="px-3 py-2 text-slate-400 max-w-xs truncate text-xs">${t.reason||'—'}</td></tr>`;
  }).join('');
}

// ─── Dropdown helpers ────────────────────────────────────────────────────────
function _toggleSymDropdown(e) { e.stopPropagation(); document.getElementById('bt-sym-dropdown').classList.toggle('hidden'); }
function _toggleBtHistFilter(e) { e.stopPropagation(); document.getElementById('bt-hist-filter-dropdown').classList.toggle('hidden'); }
document.addEventListener('click', () => {
  document.getElementById('bt-sym-dropdown')?.classList.add('hidden');
  document.getElementById('bt-hist-filter-dropdown')?.classList.add('hidden');
});

function _updateSymSummary() {
  const all=[...document.querySelectorAll('#bt-symbols-list input')], checked=all.filter(b=>b.checked);
  const el=document.getElementById('bt-sym-summary');
  if (!all.length) el.textContent='Chargement…';
  else if (!checked.length) el.textContent='Aucun';
  else if (checked.length===all.length) el.textContent=`Toutes (${all.length})`;
  else el.textContent=checked.map(b=>b.value.replace('USDC','')).join(', ');
}

function _updateBtHistFilterSummary() {
  const all=[...document.querySelectorAll('#bt-hist-filter-list input')], checked=all.filter(b=>b.checked);
  const el=document.getElementById('bt-hist-filter-summary');
  if (!checked.length) el.textContent='Aucune';
  else if (checked.length===all.length) el.textContent=`Toutes (${all.length})`;
  else el.textContent=checked.map(b=>b.value.replace('USDC','')).join(', ');
  _btHistFilterSig=''; _redrawBtHistory(_lastBtHist);
}
function _btHistToggleAll() { const b=[...document.querySelectorAll('#bt-hist-filter-list input')]; const a=b.every(x=>x.checked); b.forEach(x=>{x.checked=!a;}); _updateBtHistFilterSummary(); }
function _btToggleAll() { const b=[...document.querySelectorAll('#bt-symbols-list input')]; const a=b.every(x=>x.checked); b.forEach(x=>{x.checked=!a;}); _updateSymSummary(); }

async function _loadBtWatchlist() {
  try {
    const d = await fetch('/api/watchlist').then(r=>r.json());
    if (d.stop_loss_pct!=null) document.getElementById('bt-stop-loss').value=d.stop_loss_pct;
    if (d.trailing_stop_pct!=null) document.getElementById('bt-trailing-stop').value=d.trailing_stop_pct;
    if (d.risk_level!=null) { document.getElementById('bt-risk').value=d.risk_level; document.getElementById('bt-risk-label').textContent=d.risk_level+' / 10'; }
    if (d.sell_cooldown_cycles!=null) document.getElementById('bt-sell-cooldown').value=d.sell_cooldown_cycles;
    const syms=d.watchlist||[];
    const c=document.getElementById('bt-symbols-list');
    c.innerHTML = syms.map(sym=>`<label class="flex items-center gap-2 px-3 py-2 hover:bg-slate-700 cursor-pointer select-none"><input type="checkbox" value="${sym}" checked onchange="_updateSymSummary()" class="accent-purple-500" /><span class="text-sm text-slate-200">${sym.replace('USDC','')}</span></label>`).join('');
    _updateSymSummary();
  } catch {}
}

async function _loadBtHistWatchlist() {
  try {
    const d = await fetch('/api/watchlist').then(r=>r.json());
    const c=document.getElementById('bt-hist-filter-list');
    c.innerHTML = (d.watchlist||[]).map(sym=>`<label class="flex items-center gap-2 px-3 py-2 hover:bg-slate-700 cursor-pointer select-none"><input type="checkbox" value="${sym}" checked onchange="_updateBtHistFilterSummary()" class="accent-purple-500" /><span class="text-sm text-slate-200">${sym.replace('USDC','')}</span></label>`).join('');
    _updateBtHistFilterSummary();
  } catch {}
}

// ═══════════════════════════════════════════════════════════════════════════════
// LLM CONFIG
// ═══════════════════════════════════════════════════════════════════════════════
let _llmModels = {};

function _toggleLlmPanel(e) {
  e.stopPropagation();
  const p = document.getElementById('llm-panel');
  const btn = document.getElementById('llm-selector-btn');
  if (p.classList.contains('hidden')) {
    const r = btn.getBoundingClientRect();
    p.style.top = (r.bottom+6)+'px'; p.style.right = (window.innerWidth-r.right)+'px'; p.style.left = 'auto';
    p.classList.remove('hidden');
  } else p.classList.add('hidden');
}
document.addEventListener('click', e => {
  const btn=document.getElementById('llm-selector-btn'), p=document.getElementById('llm-panel');
  if (btn&&!btn.contains(e.target)&&p&&!p.contains(e.target)) p.classList.add('hidden');
});

async function _loadLlmConfig() {
  try {
    const d = await fetch('/api/config/llm').then(r=>r.json());
    _llmModels = d.models || {};
    const provSel = document.getElementById('llm-provider-sel');
    provSel.innerHTML = (d.providers||[]).map(p=>`<option value="${p}">${p}</option>`).join('');
    provSel.value = d.provider;
    _populateLlmModels(d.provider, d.model);
    _showOllamaUrl(d.provider==='ollama');
    if (d.base_url) document.getElementById('llm-ollama-url').value = d.base_url;
    document.getElementById('llm-temperature').value = d.temperature; document.getElementById('llm-temp-label').textContent = d.temperature.toFixed(1);
    document.getElementById('llm-max-tokens').value = d.max_tokens;
    document.getElementById('llm-selector-label').textContent = `${d.provider}/${d.model}`.substring(0,20);
  } catch {}
}

function _populateLlmModels(provider, current) {
  const models = _llmModels[provider]||[];
  const sel = document.getElementById('llm-model-sel');
  sel.innerHTML = models.map(m=>`<option value="${m}">${m}</option>`).join('') + '<option value="__custom__">Autre…</option>';
  if (models.includes(current)) sel.value = current;
  else { sel.value = '__custom__'; document.getElementById('llm-model-custom').classList.remove('hidden'); document.getElementById('llm-model-custom').value = current; }
  sel.onchange = () => { document.getElementById('llm-model-custom').classList.toggle('hidden', sel.value !== '__custom__'); };
}

function _onLlmProviderChange() {
  const p = document.getElementById('llm-provider-sel').value;
  _populateLlmModels(p, '');
  _showOllamaUrl(p==='ollama');
}

function _showOllamaUrl(show) {
  document.getElementById('llm-ollama-url-wrap').classList.toggle('hidden', !show);
  if (show) _checkOllamaStatus();
}

async function _checkOllamaStatus() {
  try {
    const d = await fetch('/api/ollama/status').then(r=>r.json());
    const badge = document.getElementById('ollama-status-badge');
    const startBtn = document.getElementById('ollama-start-btn');
    if (d.running) {
      badge.className = 'text-xs px-2 py-0.5 rounded-full bg-green-900 text-green-300'; badge.textContent = '● En ligne';
      startBtn.classList.add('hidden');
      if (d.models?.length) { document.getElementById('ollama-models-row').classList.remove('hidden'); document.getElementById('ollama-models-list').innerHTML = d.models.map(m=>`<div class="py-0.5">${m}</div>`).join(''); }
    } else {
      badge.className = 'text-xs px-2 py-0.5 rounded-full bg-red-900 text-red-300'; badge.textContent = '● Hors ligne';
      startBtn.classList.remove('hidden');
    }
  } catch {}
}

async function _startOllama() {
  await fetch('/api/ollama/start', { method:'POST' });
  toast('Démarrage Ollama…','ok');
  setTimeout(_checkOllamaStatus, 3000);
}

async function _saveLlmConfig() {
  const provider = document.getElementById('llm-provider-sel').value;
  const modelSel = document.getElementById('llm-model-sel').value;
  const model = modelSel==='__custom__' ? document.getElementById('llm-model-custom').value.trim() : modelSel;
  const body = { provider, model, temperature: parseFloat(document.getElementById('llm-temperature').value), max_tokens: parseInt(document.getElementById('llm-max-tokens').value) };
  if (provider==='ollama') body.base_url = document.getElementById('llm-ollama-url').value.trim();
  try {
    const d = await fetch('/api/config/llm', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) }).then(r=>r.json());
    if (d.ok) { toast('Configuration LLM sauvegardée','ok'); document.getElementById('llm-selector-label').textContent = `${provider}/${model}`.substring(0,20); document.getElementById('llm-panel').classList.add('hidden'); }
    else toast(d.error||'Erreur','error');
  } catch { toast('Erreur réseau','error'); }
}

// ═══════════════════════════════════════════════════════════════════════════════
// BOOT
// ═══════════════════════════════════════════════════════════════════════════════
(function() {
  // Init backtest date to 30 days ago
  const d = new Date(); d.setDate(d.getDate()-30);
  document.getElementById('bt-start-date').value = d.toISOString().split('T')[0];
})();

// Reason cell expand/collapse (delegated — fires once, survives innerHTML rewrites)
document.addEventListener('click', e => {
  const td = e.target.closest('td.reason-cell');
  if (!td) return;
  if (td.classList.contains('truncate')) {
    td.classList.remove('truncate', 'max-w-xs');
    td.classList.add('break-words', 'whitespace-normal');
    td.title = 'Cliquer pour réduire';
  } else {
    td.classList.add('truncate', 'max-w-xs');
    td.classList.remove('break-words', 'whitespace-normal');
    td.title = 'Cliquer pour développer';
  }
});

// Initial loads
fetchAgentStatus();
setInterval(fetchAgentStatus, 5000);
startLogPolling();
_loadLlmConfig();
loadDashboard();
setInterval(() => { if (currentTab==='dashboard') loadDashboard(); }, 30000);
