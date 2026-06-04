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
    ['Top-N panier',   fmtVal(p.top_n)],
    ['Confirm bear',   p.trend_confirm_hours != null ? `${p.trend_confirm_hours}h` : '—'],
    ['Min-hold',       p.min_hold_hours != null ? `${p.min_hold_hours}h` : '—'],
    ['Cooldown rebuy', p.rebuy_cooldown_hours ? `${p.rebuy_cooldown_hours}h` : 'off'],
    ['Décision /N bougies', p.decide_every_n_candles ? `toutes les ${p.decide_every_n_candles}h` : 'chaque heure'],
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
// server roundtrip. Also emits an auto-critique (strengths / weaknesses /
// improvement axes) pointing at the decider's actual config knobs so the
// next iteration starts from observed signals, not gut feel.
function _buildRecapMarkdown() {
  const p    = _btRunParams || {};
  const snap = _latestSnap || {};
  const history = snap.history || [];
  const syms = (p.symbols || '').split(',').filter(Boolean);

  // ── Aggregates over trades ────────────────────────────────────────────────
  const buys      = history.filter(t => t.action === 'BUY');
  const sellsAll  = history.filter(t => /SELL/.test(t.action || ''));
  const sellsHard  = sellsAll.filter(t => /stop-loss/i.test(t.action || ''));
  const sellsTrail = sellsAll.filter(t => /trailing-stop/i.test(t.action || ''));
  const sellsLiq   = sellsAll.filter(t => /liquidation/i.test(t.action || ''));
  const sellsScale = sellsAll.filter(t => /scale-out/i.test(t.action || ''));
  // Signal-driven exits = ce qui n'est ni stop / trailing / liquidation / scale-out :
  // en rule mode c'est la sortie "trend break", en LLM mode le SELL du modèle.
  const sellsSig   = sellsAll.filter(t => !/stop-loss|trailing-stop|liquidation|scale-out/i.test(t.action || ''));
  const winners   = sellsAll.filter(t => (t.pnl ?? 0) > 0);
  const losers    = sellsAll.filter(t => (t.pnl ?? 0) < 0);
  const grossW    = winners.reduce((s, t) => s + (t.pnl || 0), 0);
  const grossL    = Math.abs(losers.reduce((s, t) => s + (t.pnl || 0), 0));
  const pf        = grossL > 0 ? grossW / grossL : (grossW > 0 ? Infinity : null);
  const expectancy= sellsAll.length ? (grossW - grossL) / sellsAll.length : null;
  const avgWin    = winners.length ? grossW / winners.length : 0;
  const avgLoss   = losers.length  ? grossL / losers.length  : 0;
  const payoff    = avgLoss > 0 ? avgWin / avgLoss : null;
  const isLLM     = history.some(t => t.action === 'ANALYSE');

  // ── Max drawdown from the equity curve (peak-to-trough %) ─────────────────
  let mdd = null;
  if ((snap.timeseries || []).length) {
    let peak = -Infinity, low = 0;
    for (const pt of snap.timeseries) {
      if (pt?.v == null) continue;
      if (pt.v > peak) peak = pt.v;
      const dd = peak > 0 ? (pt.v - peak) / peak * 100 : 0;
      if (dd < low) low = dd;
    }
    mdd = low;
  }

  // ── Holding period (h) + entry-score outcome, FIFO match BUY → SELL ───────
  // We pair each closing sell with its oldest open buy on the same symbol so
  // a partial-then-full sell still produces reasonable hold/score samples.
  // The snapshot serves `history` newest-first for table rendering — FIFO
  // matching needs chronological order, so iterate over a cycle-sorted copy.
  const cycleStack = {};
  const scoreStack = {};
  const holdHours = [];
  const scoredOutcomes = [];
  const chronological = history.slice().sort((a, b) => (a.cycle ?? 0) - (b.cycle ?? 0));
  for (const t of chronological) {
    const sym = t.symbol;
    if (!sym) continue;
    cycleStack[sym] = cycleStack[sym] || [];
    scoreStack[sym] = scoreStack[sym] || [];
    if (t.action === 'BUY') {
      cycleStack[sym].push(t.cycle);
      scoreStack[sym].push(t.score);
    } else if (/SELL/.test(t.action || '') && !/scale-out/i.test(t.action || '')) {
      // Scale-out est une vente partielle : la position reste ouverte, on
      // ne consomme pas l'entrée BUY de la pile FIFO. Hold-time et score
      // discriminant ne sont mesurés qu'à la fermeture complète.
      if (cycleStack[sym].length) holdHours.push(t.cycle - cycleStack[sym].shift());
      if (scoreStack[sym].length) {
        const s = scoreStack[sym].shift();
        if (typeof s === 'number') scoredOutcomes.push({ s, pnl: t.pnl });
      }
    }
  }
  const sortedH = holdHours.slice().sort((a, b) => a - b);
  const medianH = sortedH.length ? sortedH[Math.floor(sortedH.length / 2)] : null;
  const avgH    = sortedH.length ? sortedH.reduce((s, x) => s + x, 0) / sortedH.length : null;
  const maxH    = sortedH.length ? sortedH[sortedH.length - 1] : null;

  const winScores  = scoredOutcomes.filter(o => (o.pnl || 0) > 0).map(o => o.s);
  const lossScores = scoredOutcomes.filter(o => (o.pnl || 0) < 0).map(o => o.s);
  const mean = a => a.length ? a.reduce((s, x) => s + x, 0) / a.length : null;
  const avgScoreBuy  = mean(buys.map(b => b.score).filter(s => typeof s === 'number'));
  const avgScoreWin  = mean(winScores);
  const avgScoreLoss = mean(lossScores);

  // ── Per-crypto breakdown (PnL, trade count, win rate) ─────────────────────
  const perCrypto = {};
  for (const t of history) {
    const sym = (t.symbol || '').toUpperCase();
    if (!sym) continue;
    perCrypto[sym] = perCrypto[sym] || { trades: 0, pnl: 0, wins: 0, losses: 0 };
    if (t.action !== 'ANALYSE') perCrypto[sym].trades += 1;
    if (t.pnl != null) {
      perCrypto[sym].pnl += t.pnl;
      if (t.pnl > 0) perCrypto[sym].wins += 1;
      else if (t.pnl < 0) perCrypto[sym].losses += 1;
    }
  }
  const cryptoEntries = Object.entries(perCrypto).sort((a, b) => b[1].pnl - a[1].pnl);
  const noTradeCryptos = syms.filter(s => !perCrypto[s.toUpperCase()]);
  const totalAbsPnL = cryptoEntries.reduce((s, [, v]) => s + Math.abs(v.pnl), 0);
  const topConc = cryptoEntries.length && totalAbsPnL > 0
    ? Math.abs(cryptoEntries[0][1].pnl) / totalAbsPnL : 0;

  // ── Headline numbers (from snapshot) ──────────────────────────────────────
  const budget = snap.budget ?? p.budget ?? 0;
  const total  = snap.total ?? snap.total_value ?? budget;
  const pnl    = snap.pnl ?? (total - budget);
  const pnlPct = snap.pnl_pct ?? (budget > 0 ? pnl / budget * 100 : 0);
  const wr     = snap.win_rate;
  const tn     = snap.trades_count ?? snap.trades ?? history.filter(t => t.action !== 'ANALYSE').length;
  const alpha  = snap.alpha;
  const btcDiff = (snap.pnl != null && snap.btc_bh_pnl != null) ? snap.pnl - snap.btc_bh_pnl : null;
  const best   = sellsAll.length ? Math.max(...sellsAll.map(t => t.pnl || 0)) : null;
  const worst  = sellsAll.length ? Math.min(...sellsAll.map(t => t.pnl || 0)) : null;
  const sumPnL = arr => arr.reduce((s, t) => s + (t.pnl || 0), 0);

  // ── Formatters ────────────────────────────────────────────────────────────
  const fmtNum    = (v, d = 2) => (v == null || isNaN(v)) ? '—' : Number(v).toFixed(d);
  const fmtDollar = v => v == null ? '—' : (v >= 0 ? '+$' : '-$') + Math.abs(v).toFixed(2);
  const fmtPct    = (v, d = 1) => v == null ? '—' : `${v >= 0 ? '+' : ''}${Number(v).toFixed(d)}%`;
  const fmtH      = h => h == null ? '—' : `${h}h`;
  const pctOf     = (n, d) => d > 0 ? `${(n / d * 100).toFixed(0)}%` : '—';

  // ── Heuristic critique ────────────────────────────────────────────────────
  // Each bullet is an observation grounded in a number. The closing note
  // reminds the reader these are heuristics, not verdicts.
  const strengths  = [];
  const weaknesses = [];
  const axes       = [];

  if (alpha != null && alpha > 0)      strengths.push(`Alpha positif vs buy & hold (${fmtDollar(alpha)})`);
  if (btcDiff != null && btcDiff > 0)  strengths.push(`Surperforme BTC seul (${fmtDollar(btcDiff)})`);
  if (typeof wr === 'number' && wr >= 55) strengths.push(`Win rate solide (${wr.toFixed(1)}%)`);
  if (pf != null && pf !== Infinity && pf >= 1.5) strengths.push(`Profit factor sain (${pf.toFixed(2)})`);
  if (payoff != null && payoff >= 1.5) strengths.push(`Asymétrie favorable : gains moyens ${payoff.toFixed(2)}× les pertes`);
  if (mdd != null && mdd > -10)        strengths.push(`Drawdown maîtrisé (${mdd.toFixed(1)}%)`);

  if (alpha != null && alpha < 0)      weaknesses.push(`Sous-performance vs buy & hold (${fmtDollar(alpha)})`);
  if (btcDiff != null && btcDiff < 0)  weaknesses.push(`Sous BTC seul (${fmtDollar(btcDiff)})`);
  if (typeof wr === 'number' && wr < 40) weaknesses.push(`Win rate faible (${wr.toFixed(1)}%)`);
  if (pf != null && pf < 1)            weaknesses.push(`Profit factor < 1 (${pf.toFixed(2)}) : on perd plus qu'on ne gagne`);
  if (payoff != null && payoff < 1)    weaknesses.push(`Pertes moyennes > gains moyens (payoff ${payoff.toFixed(2)})`);
  if (mdd != null && mdd < -20)        weaknesses.push(`Drawdown élevé (${mdd.toFixed(1)}%)`);

  if (sellsAll.length >= 5) {
    const ratioHard  = sellsHard.length  / sellsAll.length;
    const ratioTrail = sellsTrail.length / sellsAll.length;
    if (ratioHard > 0.4)
      weaknesses.push(`${pctOf(sellsHard.length, sellsAll.length)} de sorties sur stop-loss dur (${sellsHard.length}/${sellsAll.length}) — entrée probablement trop tôt ou stop trop serré`);
    if (ratioTrail > 0.4)
      weaknesses.push(`${pctOf(sellsTrail.length, sellsAll.length)} de sorties sur trailing (${sellsTrail.length}/${sellsAll.length}) — peut churner sur le bruit`);
    if (sellsLiq.length && sumPnL(sellsLiq) < 0)
      weaknesses.push(`Liquidation finale négative (${fmtDollar(sumPnL(sellsLiq))}) sur ${sellsLiq.length} position(s) — la sortie de tendance arrive trop tard`);
  }

  if (noTradeCryptos.length > 0)
    weaknesses.push(`${noTradeCryptos.length}/${syms.length} cryptos sans aucun trade (${noTradeCryptos.join(', ')}) — filtre d'entrée trop strict ou symboles peu volatils`);

  if (avgScoreWin != null && avgScoreLoss != null) {
    const gap = avgScoreWin - avgScoreLoss;
    if (gap >= 0.8)
      strengths.push(`Score discrimine : entrées gagnantes ${avgScoreWin.toFixed(1)}/10 vs perdantes ${avgScoreLoss.toFixed(1)}/10`);
    else if (gap <= 0.2)
      weaknesses.push(`Score ne discrimine pas gains/pertes (${avgScoreWin.toFixed(1)} vs ${avgScoreLoss.toFixed(1)}) — la recette de scoring est à revoir`);
  }

  if (cryptoEntries.length >= 3 && topConc > 0.7)
    weaknesses.push(`Concentration : ${cryptoEntries[0][0]} porte ${(topConc * 100).toFixed(0)}% du PnL absolu — manque de robustesse cross-actifs`);

  if (!sellsAll.length)
    weaknesses.push(`Aucun trade exécuté : décideur trop conservateur ou période sans setup valide`);

  // ── Improvement axes — each one references the actual knob to turn ────────
  if (!isLLM) {
    if (sellsAll.length >= 5 && sellsHard.length / sellsAll.length > 0.4 && p.stop_loss_pct != null)
      axes.push(`**Stop-loss** : actuel ${p.stop_loss_pct}%. Soit l'élargir (laisser respirer), soit durcir l'entrée (relever \`buy_threshold\` de ${p.buy_threshold ?? '?'} à ${(p.buy_threshold ?? 0) + 1}).`);
    if (sellsAll.length >= 5 && sellsTrail.length / sellsAll.length > 0.4 && p.trailing_stop_pct != null)
      axes.push(`**Trailing-stop** : actuel ${p.trailing_stop_pct}%. Tester ${(Number(p.trailing_stop_pct) + 2).toFixed(0)}%+ pour laisser courir les gagnants.`);
    if (sellsLiq.length > 0 && sumPnL(sellsLiq) < 0)
      axes.push(`**Sortie de tendance** : liquidation négative. Réduire \`trend_confirm_candles\` (défaut 6 → essayer 3-4) pour sortir plus vite quand la tendance casse.`);
    if (avgScoreWin != null && avgScoreLoss != null && (avgScoreWin - avgScoreLoss) >= 0.8 && p.buy_threshold != null)
      axes.push(`**Seuil d'achat** : le score corrèle au gain (Δ ${(avgScoreWin - avgScoreLoss).toFixed(1)}). Tester \`buy_threshold\` ${p.buy_threshold + 1} pour ne garder que les setups les plus francs.`);
    if (noTradeCryptos.length >= Math.max(2, syms.length / 2) && p.buy_threshold != null)
      axes.push(`**Seuil d'achat trop strict** : ${noTradeCryptos.length}/${syms.length} symboles muets. Abaisser \`buy_threshold\` à ${Math.max(1, p.buy_threshold - 1)} ou revoir la watchlist.`);
    if (mdd != null && mdd < -20 && p.risk_level != null)
      axes.push(`**Risque par trade** : drawdown ${mdd.toFixed(1)}%. Réduire \`risk_level\` (${p.risk_level} → ${Math.max(1, p.risk_level - 1)}) pour des tailles de position plus petites.`);
    if (cryptoEntries.length >= 3) {
      const repeatLosers = cryptoEntries
        .filter(([, v]) => v.pnl < 0 && v.trades >= 2)
        .map(([s]) => s);
      if (repeatLosers.length)
        axes.push(`**Watchlist** : ${repeatLosers.join(', ')} génèrent des pertes répétées. Tester un run sans ces symboles.`);
    }
    if (medianH != null && medianH <= 3 && sellsAll.length >= 5 && sellsTrail.length / sellsAll.length > 0.3)
      axes.push(`**Min-hold trop court** : médiane de détention ${medianH}h. Augmenter \`min_hold_candles\` (défaut 12) pour réduire le churn intra-tendance.`);
  } else {
    axes.push(`Mode LLM détecté — la critique ci-dessus vise le décideur déterministe. Pour ajuster le LLM, voir \`hellocrypto/prompts.py\` puis lancer \`make bench\`.`);
  }

  // ── Markdown assembly ─────────────────────────────────────────────────────
  const startTs = (snap.start_ts || '').replace('T', ' ').slice(0, 16) || (p.start_date || 'auto');
  const periodLine = p.days ? `${p.days} jour${p.days > 1 ? 's' : ''}${startTs ? ' (à partir du ' + startTs + ')' : ''}` : '—';
  const modeLabel = isLLM ? 'LLM (Claude/Gemini)' : 'Règles (déterministe)';

  const cryptoRows = cryptoEntries.map(([sym, s]) => {
    const closed = s.wins + s.losses;
    const wrSym  = closed > 0 ? `${(s.wins / closed * 100).toFixed(0)}% wr (${s.wins}/${closed})` : '—';
    return `- ${sym} : ${s.trades} trade${s.trades > 1 ? 's' : ''}, PnL ${s.pnl >= 0 ? '+' : ''}$${s.pnl.toFixed(2)} · ${wrSym}`;
  });

  const exitRows = sellsAll.length ? [
    `- Stop-loss dur : ${sellsHard.length}/${sellsAll.length} (${pctOf(sellsHard.length, sellsAll.length)}, PnL ${fmtDollar(sumPnL(sellsHard))})`,
    `- Trailing-stop : ${sellsTrail.length}/${sellsAll.length} (${pctOf(sellsTrail.length, sellsAll.length)}, PnL ${fmtDollar(sumPnL(sellsTrail))})`,
    ...(sellsScale.length ? [`- Scale-out (prise de profit) : ${sellsScale.length}/${sellsAll.length} (${pctOf(sellsScale.length, sellsAll.length)}, PnL ${fmtDollar(sumPnL(sellsScale))})`] : []),
    `- Signal (tendance/LLM) : ${sellsSig.length}/${sellsAll.length} (${pctOf(sellsSig.length, sellsAll.length)}, PnL ${fmtDollar(sumPnL(sellsSig))})`,
    `- Liquidation finale : ${sellsLiq.length}/${sellsAll.length} (${pctOf(sellsLiq.length, sellsAll.length)}, PnL ${fmtDollar(sumPnL(sellsLiq))})`,
  ] : ['- Aucune sortie enregistrée'];

  const scoreSection = isLLM ? [] : [
    '',
    '## Qualité du scoring (rule-based)',
    `- Score moyen à l'entrée : ${avgScoreBuy != null ? avgScoreBuy.toFixed(2) + '/10' : '—'} (n=${buys.length})`,
    `- Sur trades gagnants : ${avgScoreWin  != null ? avgScoreWin.toFixed(2)  + '/10' : '—'} (n=${winScores.length})`,
    `- Sur trades perdants : ${avgScoreLoss != null ? avgScoreLoss.toFixed(2) + '/10' : '—'} (n=${lossScores.length})`,
  ];

  const lines = [
    '# Backtest — récap',
    '',
    '## Paramètres',
    `- Mode : ${modeLabel}`,
    `- Cryptos : ${syms.length ? syms.join(' · ') : '—'} (${syms.length})`,
    `- Période : ${periodLine}`,
    `- Budget initial : $${fmtNum(p.budget)}`,
    `- Stop-loss : ${fmtNum(p.stop_loss_pct, 1)}% · Trailing : ${fmtNum(p.trailing_stop_pct, 1)}%`,
    `- Risque : ${p.risk_level ?? '—'}/10`,
    `- Seuil achat ${p.buy_threshold ?? '—'} · top-N ${p.top_n ?? '—'} · confirm-bear ${p.trend_confirm_hours ?? '—'}h · min-hold ${p.min_hold_hours ?? '—'}h · cooldown ${p.rebuy_cooldown_hours ?? 0}h`,
    ...(p.enable_regime_stance ? [`- Stance régime : DEPLOY 1.4× sizing / SELECTIVE 1.0× / PRESERVE 0.7× / CASH 0.5× — seuil + top-N pilotés par stance`] : []),
    `- Vitesse de simulation : ${p.speed ?? '—'}x`,
    '',
    '## Performance globale',
    `- Total final : $${fmtNum(total)} (${fmtDollar(pnl)}, ${fmtPct(pnlPct, 2)})`,
    `- vs Buy & Hold (alpha) : ${alpha != null ? fmtDollar(alpha) : '—'}`,
    `- vs BTC seul : ${btcDiff != null ? fmtDollar(btcDiff) : '—'}`,
    `- Max drawdown : ${mdd != null ? fmtPct(mdd, 1) : '—'}`,
    `- Win rate : ${wr != null ? fmtNum(wr, 1) + '%' : '—'} sur ${tn} trade${tn > 1 ? 's' : ''}`,
    `- Profit factor : ${pf == null ? '—' : (pf === Infinity ? '∞ (aucune perte)' : pf.toFixed(2))}`,
    `- Expectancy / trade : ${expectancy != null ? fmtDollar(expectancy) : '—'} · Payoff : ${payoff != null ? payoff.toFixed(2) : '—'}`,
    `- Best : ${best != null ? fmtDollar(best) : '—'} · Worst : ${worst != null ? fmtDollar(worst) : '—'}`,
    `- Détention (h) : médiane ${fmtH(medianH)} · moy ${avgH != null ? fmtH(Math.round(avgH)) : '—'} · max ${fmtH(maxH)}`,
    '',
    '## Sorties par motif',
    exitRows.join('\n'),
    '',
    '## Trades par crypto',
    cryptoRows.length ? cryptoRows.join('\n') : '- Aucun trade enregistré',
    ...(noTradeCryptos.length ? [`- _Sans trade :_ ${noTradeCryptos.join(', ')}`] : []),
    ...scoreSection,
    '',
    '## Points forts (auto-détectés)',
    strengths.length ? strengths.map(s => `- ${s}`).join('\n') : '- _Rien de marquant détecté._',
    '',
    '## Points faibles (auto-détectés)',
    weaknesses.length ? weaknesses.map(s => `- ${s}`).join('\n') : '- _Rien de marquant détecté._',
    '',
    "## Axes d'amélioration suggérés",
    axes.length ? axes.map(s => `- ${s}`).join('\n') : '- _Pas de signal clair pour ajuster le décideur sur ce run._',
    '',
    "> Critique auto-générée à partir des trades exécutés — heuristiques, pas verdicts. À recouper avec ton intuition et le contexte marché avant d'itérer sur `hellocrypto/strategy.py` ou `config.json`.",
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

// ── Stance toggle helper ──────────────────────────────────────────────────────
function btStanceToggle() {
  const on = document.getElementById('bt-stance').checked;
  const manual = document.getElementById('bt-stance-manual');
  if (manual) manual.style.opacity = on ? '0.35' : '1';
  ['bt-buy-thr', 'bt-top-n'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.disabled = on;
  });
}
// Apply initial state on load.
document.addEventListener('DOMContentLoaded', btStanceToggle);

// ── Backtest control ─────────────────────────────────────────────────────────
async function startBacktest() {
  const syms = getCryptoSelection('bt-cryptos-drop');
  if (!syms.length) { toast('Sélectionne au moins une crypto', 'warn'); return; }
  const stanceOn = document.getElementById('bt-stance')?.checked ?? true;
  const body = {
    symbols:        syms.join(','),
    days:           Number(document.getElementById('bt-days').value),
    budget:         Number(document.getElementById('bt-budget').value),
    stop_loss_pct:  Number(document.getElementById('bt-sl').value),
    trailing_stop_pct: Number(document.getElementById('bt-ts').value),
    enable_regime_stance: stanceOn,
    buy_threshold:  Number(document.getElementById('bt-buy-thr').value),
    top_n:          Math.max(1, Number(document.getElementById('bt-top-n').value) || 3),
    trend_confirm_hours: Math.max(0, Number(document.getElementById('bt-trend-confirm').value) || 0),
    min_hold_hours:      Math.max(0, Number(document.getElementById('bt-min-hold').value) || 0),
    rebuy_cooldown_hours: Math.max(0, Number(document.getElementById('bt-rebuy-cd').value) || 0),
    decide_every_n_candles: Math.max(1, Number(document.getElementById('bt-decide-every').value) || 4),
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
  document.getElementById('bt-end-ts').textContent   = snap.current_ts || '—';

  const skippedEl = document.getElementById('bt-skipped');
  const msgs = [];
  if (Array.isArray(snap.skipped_symbols) && snap.skipped_symbols.length) {
    msgs.push(`Exclu(s) (données insuffisantes) : ${snap.skipped_symbols.map(shortSym).join(', ')}`);
  }
  if (snap.tail_truncated_hours && Array.isArray(snap.tail_bottleneck) && snap.tail_bottleneck.length) {
    msgs.push(`Période tronquée de ${snap.tail_truncated_hours}h en fin de run (bridé par ${snap.tail_bottleneck.map(shortSym).join(', ')})`);
  }
  if (msgs.length) {
    skippedEl.textContent = msgs.join(' • ');
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

  // Backtest trades live only in the in-memory snapshot (never persisted to
  // the DB), so we paginate client-side over snap.history.
  renderTradesTable({
    containerId:    'bt-trades-list',
    headerId:       'bt-trades-header',
    filterId:       'trades-filters',
    symbolFilterId: 'bt-trades-symbol-filter',
    paginationId:   'bt-trades-pagination',
    pageSize:       100,
    history:        snap.history || [],
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

  // Total sans fees : ce qu'aurait été le total si on n'avait pas payé de
  // frais. Approximation (n'inclut pas la compounding du surplus de qty),
  // utile pour mesurer le coût brut du churn de la stratégie.
  const fees        = snap.total_fees ?? 0;
  const totalNoFee  = total + fees;
  const pnlNoFee    = totalNoFee - budget;
  const pnlNoFeePct = budget > 0 ? pnlNoFee / budget * 100 : null;
  _setKpi('kpi-total-no-fees',
          `$${fmt(totalNoFee)}`,
          pnlClass(pnlNoFee),
          `${fmtPnl(pnlNoFee)} · ${fmtPct(pnlNoFeePct)}`,
          pnlClass(pnlNoFee));

  const alphaEl    = document.getElementById('kpi-alpha');
  const alphaSubEl = document.getElementById('kpi-alpha-pct');
  if (alphaEl) {
    alphaEl.textContent = snap.alpha != null ? fmtPnl(snap.alpha) : '—';
    alphaEl.className   = 'kpi-val ' + pnlClass(snap.alpha);
  }
  if (alphaSubEl) {
    const alphaPct = (snap.pnl_pct != null && snap.benchmark_pnl_pct != null)
      ? snap.pnl_pct - snap.benchmark_pnl_pct : null;
    alphaSubEl.textContent = alphaPct != null ? fmtPct(alphaPct) : 'stratégie − hold';
    alphaSubEl.className   = 'kpi-sub ' + (alphaPct != null ? pnlClass(alphaPct) : 'text-slate-500');
  }

  const btcEl    = document.getElementById('kpi-btc-bh');
  const btcSubEl = document.getElementById('kpi-btc-bh-pct');
  const btcDiff  = (snap.pnl != null && snap.btc_bh_pnl != null) ? snap.pnl - snap.btc_bh_pnl : null;
  if (btcEl) {
    btcEl.textContent = btcDiff != null ? fmtPnl(btcDiff) : '—';
    btcEl.className   = 'kpi-val ' + pnlClass(btcDiff);
  }
  if (btcSubEl) {
    const btcDiffPct = (snap.pnl_pct != null && snap.btc_bh_pct != null)
      ? snap.pnl_pct - snap.btc_bh_pct : null;
    btcSubEl.textContent = btcDiffPct != null ? fmtPct(btcDiffPct) : 'si tout en BTC';
    btcSubEl.className   = 'kpi-sub ' + (btcDiffPct != null ? pnlClass(btcDiffPct) : 'text-slate-500');
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
    onChange: () => {
      // Period change resets the trades table to page 1.
      const list = document.getElementById('bt-trades-list');
      if (list) list.dataset.page = '1';
      if (_latestSnap) renderFromSnapshot(_latestSnap);
    },
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
