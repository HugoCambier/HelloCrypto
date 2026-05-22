// HelloCrypto — Order placement tab (real mode)
// Uses utilities from analytics.js (fmt, shortSym, escHtml, pnlClass, etc.)

let _ordersData = null;   // {portfolio, enriched, recent}
let _ordersFetchToken = 0;

// State for the order modal
let _orderCtx = {
  side:    null,   // 'buy' | 'sell'
  symbol:  null,
  price:   0,
  cashAvailable: 0,
  qtyHeld: 0,
  avgPrice: 0,
};

// ─── Sort scores ─────────────────────────────────────────────────────────────
// Buy score: higher = more bullish (just the signal score).
function _buyScore(item) {
  return item.score != null ? item.score : 5;
}
// Sell urgency: 0-10+, higher = sell sooner. Combines weak technical score,
// RSI overbought, and locked-in unrealized gains.
function _sellScore(item, pos) {
  if (!pos) return -1;
  let s = 10 - (item.score != null ? item.score : 5);
  if (item.rsi14 != null && item.rsi14 > 70) s += (item.rsi14 - 70) / 5;
  const unrealPct = pos.avg_price > 0 ? (item.price - pos.avg_price) / pos.avg_price * 100 : 0;
  if (unrealPct > 10) s += unrealPct / 20;
  return s;
}

// ─── Load orders tab data ────────────────────────────────────────────────────
// Pass {force: true} to bypass the 60s TTL cache (e.g. for the manual Refresh button).
async function loadOrdersTab(opts = {}) {
  if (typeof _selectedMode === 'undefined' || _selectedMode !== 'real') return;
  const token = ++_ordersFetchToken;
  const fetchOpts = opts.force ? { force: true } : undefined;
  _setLoaders(['loading-orders', 'loading-orders-sell', 'loading-orders-buy'], true);
  try {
    const [portfolio, enriched, recent] = await Promise.all([
      fetchJson('/api/portfolio',                                          undefined, fetchOpts).catch(() => null),
      fetchJson('/api/watchlist/enriched',                                 undefined, fetchOpts).catch(() => ({items:[]})),
      fetchJson('/api/performance?mode=real&period=all&with_benchmarks=0', undefined, fetchOpts).catch(() => null),
    ]);
    if (token !== _ordersFetchToken) return;
    _ordersData = { portfolio, enriched, recent };
    _renderOrdersTab();
  } finally {
    if (token === _ordersFetchToken) {
      _setLoaders(['loading-orders', 'loading-orders-sell', 'loading-orders-buy'], false);
    }
  }
}

function refreshOrdersTab() { return loadOrdersTab({ force: true }); }

function _renderOrdersTab() {
  const { portfolio, enriched, recent } = _ordersData || {};
  const sellCardsEl = document.getElementById('orders-sell-cards');
  const buyCardsEl  = document.getElementById('orders-buy-cards');

  // Header bar
  const cash   = portfolio?.cash ?? 0;
  const posVal = (portfolio?.positions || []).reduce((s, x) => s + (x.value || 0), 0);
  const total  = cash + posVal;
  const budget = portfolio?.budget ?? 0;
  const pnl    = total - budget;
  document.getElementById('orders-cash').textContent  = `$${fmt(cash)}`;
  document.getElementById('orders-total').textContent = `$${fmt(total)}`;
  const pnlEl = document.getElementById('orders-pnl');
  pnlEl.textContent = budget > 0 ? `${fmtPnl(pnl)} (${fmtPct(pnl/budget*100)})` : '—';
  pnlEl.className = 'text-xs mt-0.5 ' + pnlClass(pnl);

  // Lookup positions by symbol
  const posMap = {};
  for (const p of (portfolio?.positions || [])) posMap[p.symbol] = p;

  const items = enriched?.items || [];

  // ── Sell section: only owned cryptos, sorted by sell urgency desc ──────────
  const sellList = items
    .filter(item => posMap[item.symbol] && posMap[item.symbol].qty > 0)
    .map(item => ({ item, pos: posMap[item.symbol], score: _sellScore(item, posMap[item.symbol]) }))
    .sort((a, b) => b.score - a.score);

  document.getElementById('sell-count').textContent = sellList.length ? `${sellList.length} position${sellList.length>1?'s':''}` : '';
  document.getElementById('liquidate-all-btn').classList.toggle('hidden', !sellList.length);
  if (!sellList.length) {
    sellCardsEl.innerHTML = '<p class="text-xs text-slate-500 italic col-span-full">Aucune position ouverte.</p>';
  } else {
    sellCardsEl.innerHTML = sellList.map(({item, pos}) => _orderCardHtml(item, pos, cash, 'sell')).join('');
  }

  // ── Buy section: all watchlist, sorted by buy importance desc ──────────────
  const buyList = items
    .map(item => ({ item, pos: posMap[item.symbol], score: _buyScore(item) }))
    .sort((a, b) => b.score - a.score);

  document.getElementById('buy-count').textContent = buyList.length ? `${buyList.length} crypto${buyList.length>1?'s':''}` : '';
  if (!buyList.length) {
    buyCardsEl.innerHTML = '<p class="text-xs text-slate-500 italic col-span-full">Aucune crypto dans la watchlist.</p>';
  } else {
    buyCardsEl.innerHTML = buyList.map(({item, pos}) => _orderCardHtml(item, pos, cash, 'buy')).join('');
  }

  // Recent orders
  const recentEl = document.getElementById('orders-recent');
  const trades = (recent?.history || [])
    .filter(t => t.action && t.action !== 'ANALYSE' && t.action !== 'HOLD')
    .slice(0, 15);
  if (!trades.length) {
    recentEl.innerHTML = '<p class="text-xs text-slate-500 italic">Aucun ordre récent.</p>';
  } else {
    recentEl.innerHTML = trades.map(t => {
      const action = t.action || '';
      const color = /BUY/i.test(action) ? 'text-green-400'
                  : /SELL/i.test(action) ? 'text-orange-400' : 'text-slate-400';
      const amount = t.amount != null ? `$${fmt(t.amount)}` : '—';
      return `<div class="flex items-center gap-3 py-1.5 border-b border-slate-800 text-xs">
        <span class="text-slate-500 w-28 shrink-0">${(t.timestamp||'').slice(0,16).replace('T',' ')}</span>
        <span class="${color} w-12 shrink-0 font-semibold">${escHtml(action)}</span>
        <span class="text-slate-300 w-14 shrink-0">${shortSym(t.symbol || '')}</span>
        <span class="text-slate-400">${fmtQty(t.qty)} @ $${fmt(t.price, 4)}</span>
        <span class="text-slate-500 ml-auto">${amount}</span>
      </div>`;
    }).join('');
  }
}

function _orderCardHtml(item, pos, cash, context) {
  const sym       = item.symbol;
  const price     = item.price;
  const change24h = item.change_pct_24h;
  const hasPos    = pos && pos.qty > 0;
  const rsi       = item.rsi14;
  const trend     = item.trend;
  const score     = item.score;

  const deltaCls = change24h == null ? 'flat' : change24h > 0 ? 'up' : change24h < 0 ? 'down' : 'flat';
  const deltaStr = change24h == null ? '—' : `${change24h >= 0 ? '+' : ''}${fmt(change24h)}% 24h`;

  let posBlock = '';
  if (hasPos) {
    const unrealizedPnl = (price - pos.avg_price) * pos.qty;
    const unrealizedPct = pos.avg_price > 0 ? (price - pos.avg_price) / pos.avg_price * 100 : 0;
    posBlock = `
      <div class="pos-block">
        <div class="row"><span>Quantité</span><span>${fmtQty(pos.qty)}</span></div>
        <div class="row"><span>Prix d'entrée</span><span>$${fmt(pos.avg_price, 4)}</span></div>
        <div class="row"><span>Valeur actuelle</span><span>$${fmt(pos.value ?? pos.qty * price)}</span></div>
        <div class="row"><span>PnL latent</span><span class="${pnlClass(unrealizedPnl)}">${fmtPnl(unrealizedPnl)} (${fmtPct(unrealizedPct)})</span></div>
      </div>`;
  }

  const techBits = [];
  if (rsi != null)   techBits.push(`RSI ${rsi}`);
  if (trend)         techBits.push(`Tendance ${trend}`);
  if (score != null) techBits.push(`Score ${score}/10`);
  const techRow = techBits.length
    ? `<div class="order-tech">${techBits.map(t => `<span class="tag">${escHtml(t)}</span>`).join('')}</div>`
    : '';

  const canBuy  = cash >= 10;
  const canSell = hasPos;

  // Primary action depends on which section the card is in. The "other" action
  // appears as a secondary smaller link so the user can still flip if needed.
  const primary = context === 'sell'
    ? `<button class="btn-sell" onclick="openOrderModal('sell','${sym}')" ${canSell?'':'disabled'}>Vendre</button>`
    : `<button class="btn-buy"  onclick="openOrderModal('buy', '${sym}')" ${canBuy ?'':'disabled'}>Acheter</button>`;
  const secondary = context === 'sell' && canBuy
    ? `<button class="btn-buy" onclick="openOrderModal('buy','${sym}')">Acheter +</button>`
    : context === 'buy' && canSell
    ? `<button class="btn-sell" onclick="openOrderModal('sell','${sym}')">Vendre</button>`
    : '';

  return `<div class="order-card${hasPos ? ' has-pos' : ''}">
    <div class="flex items-center justify-between mb-1">
      <span class="sym">${shortSym(sym)}</span>
      <span class="delta ${deltaCls}">${deltaStr}</span>
    </div>
    <div class="price">$${fmt(price, price < 1 ? 4 : 2)}</div>
    ${techRow}
    ${posBlock}
    <div class="actions">
      ${primary}
      ${secondary}
    </div>
  </div>`;
}

// ─── Order modal ─────────────────────────────────────────────────────────────
function openOrderModal(side, symbol) {
  const item = (_ordersData?.enriched?.items || []).find(i => i.symbol === symbol);
  const pos  = (_ordersData?.portfolio?.positions || []).find(p => p.symbol === symbol);
  const cash = _ordersData?.portfolio?.cash ?? 0;

  if (!item) return;
  _orderCtx = {
    side,
    symbol,
    price:         item.price || 0,
    cashAvailable: cash,
    qtyHeld:       pos?.qty || 0,
    avgPrice:      pos?.avg_price || 0,
  };

  document.getElementById('order-side-label').textContent = side === 'buy' ? 'Acheter' : 'Vendre';
  document.getElementById('order-sym-label').textContent  = shortSym(symbol);
  document.getElementById('order-current-price').textContent = `$${fmt(_orderCtx.price, _orderCtx.price < 1 ? 4 : 2)}`;

  const hasPos = _orderCtx.qtyHeld > 0;
  document.getElementById('order-pos-row').classList.toggle('hidden', !hasPos);
  document.getElementById('order-entry-row').classList.toggle('hidden', !hasPos);
  document.getElementById('order-unrealized-row').classList.toggle('hidden', !hasPos);
  if (hasPos) {
    document.getElementById('order-pos-info').textContent = `${fmtQty(_orderCtx.qtyHeld)} ${shortSym(symbol)}`;
    document.getElementById('order-entry-price').textContent = `$${fmt(_orderCtx.avgPrice, 4)}`;
    const unrealPnl = (_orderCtx.price - _orderCtx.avgPrice) * _orderCtx.qtyHeld;
    const unrealEl  = document.getElementById('order-unrealized');
    unrealEl.textContent = fmtPnl(unrealPnl);
    unrealEl.className   = 'font-semibold ' + pnlClass(unrealPnl);
  }

  document.getElementById('order-buy-input').classList.toggle('hidden', side !== 'buy');
  document.getElementById('order-sell-input').classList.toggle('hidden', side !== 'sell');
  document.getElementById('order-amount').value = '';
  document.getElementById('order-qty').value    = '';

  const confirmBtn = document.getElementById('order-confirm-btn');
  confirmBtn.textContent = side === 'buy' ? 'Acheter' : 'Vendre';
  confirmBtn.className = 'flex-1 py-2.5 rounded-lg font-semibold text-sm transition-colors '
    + (side === 'buy' ? 'btn-buy' : 'btn-sell');

  _updateOrderEstimate();
  document.getElementById('order-modal').classList.remove('hidden');

  // Wire input listeners (once per open)
  const amountInput = document.getElementById('order-amount');
  const qtyInput    = document.getElementById('order-qty');
  amountInput.oninput = _updateOrderEstimate;
  qtyInput.oninput    = _updateOrderEstimate;

  setTimeout(() => (side === 'buy' ? amountInput : qtyInput).focus(), 50);
}

function closeOrderModal() {
  document.getElementById('order-modal').classList.add('hidden');
}

function _setOrderAmount(pct) {
  const max = Math.max(0, _orderCtx.cashAvailable - 0.5);  // leave a buffer for fees
  document.getElementById('order-amount').value = (max * pct).toFixed(2);
  _updateOrderEstimate();
}
function _setOrderQty(pct) {
  document.getElementById('order-qty').value = (_orderCtx.qtyHeld * pct).toFixed(6);
  _updateOrderEstimate();
}

function _updateOrderEstimate() {
  const est = document.getElementById('order-estimate');
  if (!est) return;
  const { side, price, avgPrice } = _orderCtx;
  const FEE_RATE = 0.001;
  if (side === 'buy') {
    const amount = Number(document.getElementById('order-amount').value) || 0;
    if (amount <= 0) { est.textContent = ''; return; }
    const fee = amount * FEE_RATE;
    const qty = (amount - fee) / price;
    est.innerHTML = `
      <div>Quantité estimée : <span class="text-slate-200 font-semibold">${fmtQty(qty)} ${shortSym(_orderCtx.symbol)}</span></div>
      <div>Frais (~0.1%) : <span class="text-slate-300">$${fmt(fee, 4)}</span></div>`;
  } else {
    const qty = Number(document.getElementById('order-qty').value) || 0;
    if (qty <= 0) { est.textContent = ''; return; }
    if (qty > _orderCtx.qtyHeld) {
      est.innerHTML = `<div class="text-red-400">Quantité supérieure à la position détenue (${fmtQty(_orderCtx.qtyHeld)}).</div>`;
      return;
    }
    const gross    = qty * price;
    const fee      = gross * FEE_RATE;
    const proceeds = gross - fee;
    const realized = avgPrice > 0 ? (price - avgPrice) * qty - fee : null;
    est.innerHTML = `
      <div>USDC reçus (net) : <span class="text-slate-200 font-semibold">$${fmt(proceeds)}</span></div>
      <div>Frais (~0.1%) : <span class="text-slate-300">$${fmt(fee, 4)}</span></div>
      ${realized != null ? `<div>PnL réalisé : <span class="${pnlClass(realized)} font-semibold">${fmtPnl(realized)}</span></div>` : ''}`;
  }
}

async function confirmOrder() {
  const btn = document.getElementById('order-confirm-btn');
  const { side, symbol } = _orderCtx;
  let body, url;
  if (side === 'buy') {
    const amount = Number(document.getElementById('order-amount').value) || 0;
    if (amount < 10) { toast('Montant minimum : $10', 'warn'); return; }
    if (amount > _orderCtx.cashAvailable) { toast('Solde USDC insuffisant', 'err'); return; }
    body = { symbol, amount };
    url  = '/api/trade/buy';
  } else {
    const qty = Number(document.getElementById('order-qty').value) || 0;
    if (qty <= 0) { toast('Quantité invalide', 'warn'); return; }
    if (qty > _orderCtx.qtyHeld) { toast('Quantité supérieure à la position détenue', 'err'); return; }
    body = { symbol, qty };
    url  = '/api/trade/sell';
  }

  if (!confirm(`Confirmer ${side === 'buy' ? "l'achat" : "la vente"} de ${shortSym(symbol)} ?`)) return;

  btn.disabled = true; btn.textContent = 'Exécution…';
  try {
    const r = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "Erreur d'exécution");
    toast(`Ordre exécuté @ $${fmt(d.price, 4)}`, 'ok');
    // Invalidate everything that depends on portfolio/trades state
    invalidateCache('/api/portfolio');
    invalidateCache('/api/performance');
    closeOrderModal();
    await loadOrdersTab();
    if (typeof loadPerformance === 'function') loadPerformance();
  } catch (e) {
    toast(e.message || 'Erreur', 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = side === 'buy' ? 'Acheter' : 'Vendre';
  }
}

// ── Liquidate all positions ────────────────────────────────────────────────────
async function confirmLiquidateAll() {
  const positions = _ordersData?.portfolio?.positions || [];
  const owned = positions.filter(p => (p.qty || 0) > 0);
  if (!owned.length) {
    toast('Aucune position à vendre', 'warn');
    return;
  }
  const total = owned.reduce((s, p) => s + (p.value || 0), 0);
  const lines = owned.map(p => `  • ${shortSym(p.symbol)} : ${fmtQty(p.qty)} (~$${fmt(p.value || 0)})`).join('\n');
  const msg = `Vendre TOUTES les positions au prix de marché Binance ?\n\n${lines}\n\nTotal estimé : ~$${fmt(total)} en USDC\n\nCette action est IRRÉVERSIBLE.`;
  if (!confirm(msg)) return;

  const btn = document.getElementById('liquidate-all-btn');
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = 'Vente en cours…';
  try {
    const r = await fetch('/api/trade/liquidate', { method: 'POST' });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "Erreur de liquidation");
    const sold = d.sold_count || 0;
    const failed = d.error_count || 0;
    if (failed > 0) {
      toast(`${sold} vendue(s), ${failed} erreur(s) — voir logs`, 'warn');
      console.error('Liquidation errors:', d.errors);
    } else {
      toast(`${sold} position(s) liquidée(s) en USDC`, 'ok');
    }
    invalidateCache('/api/portfolio');
    invalidateCache('/api/performance');
    await loadOrdersTab({ force: true });
    if (typeof loadPerformance === 'function') loadPerformance();
  } catch (e) {
    toast(e.message || 'Erreur', 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}
