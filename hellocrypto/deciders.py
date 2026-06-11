"""Pluggable decision strategies for the paper simulation.

Two deciders are exposed to a simulation session:

- ``llm``           — the production Claude/Gemini agent (handled inline in
  ``simulation.run``; this module only provides the deterministic one).
- ``deterministic`` — per-symbol entry/exit with a top-N cap, risk-aware
  sizing, bear-trend confirmation before exit, and an anti-rebuy cooldown.

``regime_decision`` is pure: it reads the live enriched market snapshot and
returns an LLM-shaped decision dict plus updated per-session strategy state.
The caller executes the actions and persists the state across cycles.

All delays are expressed in **wall-clock hours** so behavior is identical
regardless of decision cadence (backtest 1h candle cycles, live 30 min
cycles, etc.).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .api import compute_score_rules
from .coin_tiers import coin_tier, risk_tier_cap

DEFAULTS = {
    "decide_every_cycles":   48,         # cadence gate (units = caller cycle)
    "top_n":                 3,          # max simultaneously held positions
    "buy_threshold":         8,          # score required to enter
    "trend_confirm_hours":   24.0,       # bearish trend duration required to exit
    "min_hold_hours":        12.0,       # minimum holding period before any exit
    "rebuy_cooldown_hours":  0.0,        # anti-whipsaw: hours before re-entering a sold sym
    "enable_regime_stance":  True,       # modulate threshold+top_n via market stance
    "exit_signal":           "trend_1d", # which signal triggers bear-confirm exits
    "score_exit_threshold":  5,          # anti-whipsaw: block exit while score >= this
    "max_portfolio_dd_pct":  25.0,       # circuit-breaker: liquidate all if portfolio drops this much
    "dd_cooldown_days":      3.0,        # no new entries for N days after the breaker fires
    "size_multiplier":       1.0,        # per-stance allocation multiplier on top of risk-level/tier sizing
    "dd_scale_out_paliers": (0.10, 0.15, 0.20),  # fractions: progressive de-risking thresholds when portfolio drops below peak
    "dd_scale_out_frac":     1.0 / 3.0,  # fraction du qty détenu vendue à chaque palier DD franchi
    "early_exit_loss_pct":     5.0,      # loss% from entry needed to consider an early exit
    "early_exit_score_thr":    4,        # score below which we cut on loss (stricter than score_exit_threshold)
    "topup_max_loss_pct":      2.0,      # block top-up if position is below entry by more than this % (prevents averaging down into bagholders)
}

# Per-stance overrides. ``exit_signal`` switches the source of the bearish-trend
# timer between the daily SMA cross (slow, ~weeks lag) and the 1h SMA cross
# (~25h lag). In defensive stances we want faster exits, so we read from the
# faster signal. ``top_n=0`` in CASH effectively blocks all new entries.
STANCE_PARAMS: dict[str, dict] = {
    # Per-stance behavior:
    # - ``buy_threshold`` / ``top_n``: how selective/aggressive on entries
    # - ``exit_signal``: which trend signal gates exits (trend_1d = slow daily,
    #   trend = fast 1h SMA cross)
    # - ``score_exit_threshold``: anti-whipsaw gate (require score < N to exit).
    #   ON in bull (5) to let winners run, OFF in defensive (99) to exit fast.
    # - ``trend_confirm_hours``: how long the bear signal must persist before
    #   exit fires. Long in bull (48h: filter transient trend_1d flips) ;
    #   moderate in PRESERVE (24h) ; fast in CASH (12h: capitulation mode).
    # - ``size_multiplier``: scales per-buy allocation. Asymmetric exposure:
    #   bigger in bull, smaller in defensive stances.
    "DEPLOY":    {"buy_threshold": 7,  "top_n": 4, "exit_signal": "trend_1d", "score_exit_threshold": 5,  "trend_confirm_hours": 36.0, "size_multiplier": 1.4},
    "SELECTIVE": {"buy_threshold": 8,  "top_n": 3, "exit_signal": "trend_1d", "score_exit_threshold": 5,  "trend_confirm_hours": 36.0, "size_multiplier": 1.0},
    # PRESERVE = BTC trend_1d baissier confirmé. Diagnostic 1000j a montré
    # un wr de 28% (vs 64% en SELECTIVE, 40% en DEPLOY) — entrer dans cette
    # stance dilue les gains des autres. On bloque toute nouvelle entrée
    # (top_n=0) ; les positions existantes restent gérées via exit_signal=trend
    # (sortie rapide via 1h SMA) et le score_exit_threshold=99 (toujours exit
    # quand bear timer expire). Identique à CASH côté entrées.
    "PRESERVE":  {"buy_threshold": 9,  "top_n": 0, "exit_signal": "trend",    "score_exit_threshold": 99, "trend_confirm_hours": 24.0, "size_multiplier": 0.7},
    "CASH":      {"buy_threshold": 11, "top_n": 0, "exit_signal": "trend",    "score_exit_threshold": 99, "trend_confirm_hours": 24.0, "size_multiplier": 0.5},
}

# CASH triggers — leading signals so we don't lag the lagging trend_1d.
CASH_BTC_DRAWDOWN_PCT     = 7.0  # BTC down this much from its 7d high
CASH_BEAR_BREADTH_INTRA   = 0.7  # ratio of watchlist with intraday `trend` == baissier

# Fear & Greed contrarian thresholds — raise the buy bar when crowd is greedy,
# lower it when crowd capitulates. Classic mean-reversion on sentiment.
FNG_EXTREME_GREED         = 75   # ≥ → harder to enter (crowd at the top)
FNG_EXTREME_FEAR          = 25   # ≤ → easier to enter (crowd at the bottom)


def _derive_stance(market_raw: dict) -> str:
    """Derive DEPLOY / SELECTIVE / PRESERVE / CASH from market signals.

    CASH (no new entries, fast exits via intraday signal) requires *both*
    leading conditions — BTC drawdown ≥7% from its 7d high AND intraday bear
    breadth ≥70%. The AND gate avoids false positives on transient hourly
    breadth flips during normal bull pullbacks (which would otherwise force
    us out of bull-market positions).
    """
    btc = market_raw.get("BTCUSDC") or {}
    btc_trend    = btc.get("trend_1d")
    btc_drawdown = btc.get("drawdown_pct_7d")

    # Leading: both BTC drawdown AND intraday breadth confirm a real downturn.
    if market_raw:
        bear_intra = sum(1 for d in market_raw.values() if d.get("trend") == "baissier")
        breadth_ratio = bear_intra / len(market_raw)
        if (btc_drawdown is not None
                and btc_drawdown >= CASH_BTC_DRAWDOWN_PCT
                and breadth_ratio >= CASH_BEAR_BREADTH_INTRA):
            return "CASH"

    # Lagging: daily breadth — used only when leading signals are clean.
    bull = sum(1 for d in market_raw.values() if d.get("trend_1d") == "haussier")
    bear = sum(1 for d in market_raw.values() if d.get("trend_1d") == "baissier")
    if btc_trend == "haussier" and bull >= bear:
        return "DEPLOY"
    if btc_trend == "baissier":
        return "PRESERVE"
    return "SELECTIVE"


def _params(params: dict | None) -> dict:
    p = dict(DEFAULTS)
    if params:
        p.update({k: v for k, v in params.items() if v is not None})
    return p


def _max_pct(risk_level: int) -> float:
    """Per-buy allocation as a fraction of available cash (matches A's formula)."""
    return (5 + max(1, min(10, int(risk_level))) * 4) / 100


# risk_level → (buy_threshold delta, hold/confirm-horizon multiplier). Profile
# overlay applied on top of the resolved stance/default knobs. Pinned so risk 7
# = (0, 1.0) leaves the production reference byte-identical. Lower risk = stricter
# entry bar + longer horizon (measured, long-term) ; higher risk = looser bar +
# shorter horizon (bullish, scalp). The universe axis lives in coin_tiers.
_RISK_ENTRY = {
    1: (+3, 1.6), 2: (+3, 1.5), 3: (+2, 1.4), 4: (+2, 1.3), 5: (+1, 1.2),
    6: (+1, 1.1), 7: (0, 1.0), 8: (0, 0.9), 9: (-1, 0.8), 10: (-2, 0.7),
}


def _risk_entry(risk_level: int) -> tuple[int, float]:
    """Return (buy_threshold delta, horizon multiplier) for *risk_level*."""
    return _RISK_ENTRY[max(1, min(10, int(risk_level)))]


def _per_coin_threshold(base_threshold: int, tier: int) -> int:
    """Coins with tier > 6 require a higher score to clear the entry bar.

    Net rule: +1 to threshold per tier above 6 (so tier 7→+1, 8→+2, 9→+3).
    Tiers ≤ 6 use the base threshold unmodified. Combined with the
    `tier > risk_tier_cap(risk_level)` universe filter, this gives a graded
    discouragement rather than a binary cliff.
    """
    return base_threshold + max(0, tier - 6)


def _per_coin_size_factor(tier: int) -> float:
    """Reduce position size on higher-risk coins (10% smaller per tier > 5).

    Capped at 50% so even the riskiest allowed coin still gets a meaningful
    position. Tier ≤ 5 (blue chips) get full size.
    """
    return max(0.5, 1.0 - max(0, tier - 5) * 0.10)


# Hard cap on a single position as a fraction of remaining cash at the moment
# of the buy. Prevents the BTC conviction boost (below) from eating the whole
# pool when BTC happens to be the first candidate in the queue.
_MAX_POSITION_PCT_OF_CASH = 0.65


def build_decider_context(
    *,
    market_raw: dict,
    holdings: dict,
    cash: float,
    strat_state: dict | None = None,
    params: dict | None = None,
    now_ts: float | None = None,
) -> dict:
    """Snapshot the deterministic decider's state + active rules so the LLM
    sees the same context. Returned dict is rendered by ``prompts.build_analysis``
    into a compact "ÉTAT MACHINE & RÈGLES" section.

    Without this, the LLM is handicapped vs the deterministic decider — it
    misses 7 inputs the deterministic uses: coin tiers, hold-hours per
    position, bear-duration counters, portfolio peak/DD, stance params,
    BTC conviction rule, strong-DEPLOY breadth rule.
    """
    p = _params(params)
    stance: str = "OFF"
    bull_breadth: float | None = None
    strong_deploy = False
    if p.get("enable_regime_stance") and market_raw:
        stance = _derive_stance(market_raw)
        for k, v in STANCE_PARAMS[stance].items():
            p[k] = v
        bull_breadth = sum(
            1 for d in market_raw.values() if d.get("trend_1d") == "haussier"
        ) / len(market_raw)
        if stance == "DEPLOY" and bull_breadth >= 0.70:
            p["top_n"] = 2
            strong_deploy = True

    hold_hours: dict[str, float] = {}
    if now_ts is not None:
        for sym, pos in (holdings or {}).items():
            et = pos.get("entry_ts")
            if et:
                hold_hours[sym] = round((now_ts - et) / 3600, 1)

    coin_tiers = {sym: coin_tier(sym) for sym in (market_raw or {})}

    st = strat_state or {}
    bear_since_1d = st.get("bear_since_1d")
    bear_since_1h = st.get("bear_since_1h")
    bear_hours_1d = round((now_ts - bear_since_1d) / 3600, 1) if (
        bear_since_1d and now_ts is not None) else None
    bear_hours_1h = round((now_ts - bear_since_1h) / 3600, 1) if (
        bear_since_1h and now_ts is not None) else None

    portfolio_now = cash + sum(
        (h.get("qty") or 0) * float((market_raw.get(s) or {}).get("price") or 0)
        for s, h in (holdings or {}).items()
    )
    portfolio_peak = float(st.get("portfolio_peak") or portfolio_now)
    dd_pct = ((portfolio_peak - portfolio_now) / portfolio_peak * 100
              if portfolio_peak > 0 else 0.0)

    return {
        "stance":             stance,
        "stance_params": {
            k: p.get(k) for k in (
                "buy_threshold", "top_n", "size_multiplier",
                "trend_confirm_hours", "min_hold_hours",
                "score_exit_threshold", "exit_signal", "rebuy_cooldown_hours",
            )
        },
        "strong_deploy":      strong_deploy,
        "bull_breadth":       round(bull_breadth, 2) if bull_breadth is not None else None,
        "btc_conviction":     {"DEPLOY_mult": 2.0, "SELECTIVE_mult": 1.5,
                               "cap_pct": int(_MAX_POSITION_PCT_OF_CASH * 100)},
        "coin_tiers":         coin_tiers,
        "hold_hours":         hold_hours,
        "bear_hours_1d":      bear_hours_1d,
        "bear_hours_1h":      bear_hours_1h,
        "portfolio_peak":     round(portfolio_peak, 2),
        "dd_pct_from_peak":   round(dd_pct, 2),
        "circuit_breaker_dd": float(p.get("max_portfolio_dd_pct") or 0),
    }


def _btc_conviction_mult(symbol: str, stance: str) -> float:
    """Asymmetric sizing: BTC gets 2× weight in DEPLOY, 1.5× in SELECTIVE.

    The honest 1000-day bench-path shows we underperform BTC by ~75 pts of
    return because we split capital across the watchlist while BTC alone
    rides the trend. In confirmed bull regimes (DEPLOY/SELECTIVE) we lean
    harder into BTC to capture more of that trend. PRESERVE/CASH stay
    symmetric — defensive stances shouldn't concentrate on a single asset.
    """
    if symbol != "BTCUSDC":
        return 1.0
    return {"DEPLOY": 2.0, "SELECTIVE": 1.5}.get(stance, 1.0)


def regime_decision(
    *,
    market_raw: dict[str, dict],
    holdings: dict[str, dict],
    cash: float,
    cycle: int,
    now_ts: float | None = None,
    risk_level: int = 7,
    strat_state: dict[str, Any] | None = None,
    params: dict | None = None,
    fng_value: int | None = None,
    as_of_date: date | None = None,
) -> tuple[dict, dict]:
    """Per-symbol deterministic decider with top-N cap and risk-aware sizing.

    Entry per symbol:
      score >= buy_threshold
      AND trend_1d != baissier
      AND len(holdings) < top_n
      AND now - last_sell_ts[sym] >= rebuy_cooldown_hours

    Exit per symbol (only after min_hold_hours since entry):
      trend_1d has been baissier continuously for trend_confirm_hours

    Sizing per BUY: cash * max_pct  where max_pct = (5 + risk*4)/100.
    Returns actions with ``usdc_amount`` populated so the caller just executes.
    """
    p = _params(params)
    user_pinned = {k for k, v in (params or {}).items() if v is not None}
    stance = "OFF"
    fng_adj = 0
    if p.get("enable_regime_stance"):
        stance = _derive_stance(market_raw)
        for k, v in STANCE_PARAMS[stance].items():
            if k not in user_pinned:
                p[k] = v
        # Strong-DEPLOY concentration: when DEPLOY fires AND ≥70% of the
        # watchlist is in daily uptrend, cut top_n from 4 to 2 so capital
        # concentrates on BTC (boosted ×2 by ``_btc_conviction_mult``) + the
        # single best-scoring alt. The tail positions in normal DEPLOY get
        # crap-sized allocations anyway after BTC's 65% cap, so dropping them
        # mostly removes noise — and frees us from carrying weak picks during
        # the cleanest bull windows.
        if stance == "DEPLOY" and "top_n" not in user_pinned and market_raw:
            bull_breadth = sum(
                1 for d in market_raw.values() if d.get("trend_1d") == "haussier"
            ) / len(market_raw)
            if bull_breadth >= 0.70:
                p["top_n"] = 2
        # Contrarian sentiment modulation on top of stance: when the crowd
        # is at extremes, the next move tends to mean-revert. Raise the bar
        # in extreme greed, lower it in extreme fear. Only nudges by ±1, so
        # it's a tiebreaker on borderline setups rather than a regime change.
        if fng_value is not None and "buy_threshold" not in user_pinned:
            if fng_value >= FNG_EXTREME_GREED:
                fng_adj = +1
            elif fng_value <= FNG_EXTREME_FEAR:
                fng_adj = -1
            p["buy_threshold"] = max(1, p["buy_threshold"] + fng_adj)

    # ── Risk-level profile overlay (always applies; risk 7 = identity) ───────
    # risk_level reshapes the *resolved* knobs into a coherent profile: lower
    # risk raises the entry bar and stretches the hold/bear-confirm horizon
    # (measured, long-term) ; higher risk lowers the bar and shortens the
    # horizon (bullish, scalp). The (0, 1.0) entry at risk 7 leaves the pinned
    # reference exact. The universe axis is applied at the tier filter below.
    thr_delta, horizon_mult = _risk_entry(risk_level)
    p["buy_threshold"]       = max(1, int(p["buy_threshold"]) + thr_delta)
    p["trend_confirm_hours"] = float(p["trend_confirm_hours"]) * horizon_mult
    p["min_hold_hours"]      = float(p["min_hold_hours"]) * horizon_mult

    st = dict(strat_state or {})

    # ── Portfolio-level drawdown circuit-breaker ────────────────────────────
    # Catastrophic-loss filet: if the *whole portfolio* is down N% from its
    # all-time high, liquidate everything and freeze new entries for K days.
    # This overrides cadence (we want to react NOW, not on the next decision
    # window) and stance (CASH won't sell existing positions, this will).
    holdings_value = sum(
        (holdings.get(s, {}).get("qty") or 0)
        * float((market_raw.get(s) or {}).get("price") or 0)
        for s in holdings
    )
    portfolio_now = cash + holdings_value
    prev_peak     = float(st.get("portfolio_peak") or 0.0)
    peak          = max(prev_peak, portfolio_now)
    st["portfolio_peak"] = peak

    # Reset DD paliers when on a new high — chaque drawdown a son propre cycle
    # de paliers, on ne re-déclenche pas en boucle sur des allers-retours.
    dd_paliers_taken = list(st.get("dd_paliers_taken") or [])
    if peak > prev_peak:
        dd_paliers_taken = []

    dd_cooldown_until = float(st.get("dd_cooldown_until") or 0.0)
    in_dd_cooldown = (now_ts is not None and now_ts < dd_cooldown_until)

    if peak > 0 and not in_dd_cooldown:
        dd_frac = (peak - portfolio_now) / peak  # fraction (0.10 = 10%)
        dd_pct  = dd_frac * 100
        if dd_pct >= float(p["max_portfolio_dd_pct"]) and holdings:
            actions = [{
                "type":   "sell",
                "symbol": sym,
                "qty":    holdings[sym]["qty"],
                "reason": f"Circuit-breaker DD -{dd_pct:.1f}% (peak ${peak:,.0f})",
            } for sym in holdings]
            if now_ts is not None:
                st["dd_cooldown_until"] = now_ts + float(p["dd_cooldown_days"]) * 86400
                st["last_sell_ts"] = {sym: now_ts for sym in holdings}
            # Reset peak to current value so we don't re-trigger on the new base.
            st["portfolio_peak"]    = portfolio_now
            st["dd_paliers_taken"]  = []
            st["last_decision_cycle"] = cycle
            return {
                "market_sentiment": "circuit-breaker",
                "summary":          f"DD circuit-breaker -{dd_pct:.1f}% → liquidation + {p['dd_cooldown_days']:g}d cooldown",
                "actions":          actions,
                "scores":           {},
                "stance":           "FROZEN",
            }, st

        # Progressive de-risking : avant le seuil dur, on vend 1/3 par palier
        # franchi (-10% / -15% / -20% par défaut). Gate strict — actif
        # uniquement en CASH (capitulation confirmée : BTC drawdown 7d ≥7%
        # ET breadth bear intraday ≥70%). PRESERVE alone n'est pas suffisant
        # car BTC daily peut basculer baissier sur des corrections modérées
        # qui se résorbent ensuite — on a observé que la version étendue à
        # PRESERVE crystallisait des pertes sur des dips de bull qui
        # rebondissaient. CASH représente une vraie capitulation, c'est là
        # que la protection compense le coût d'opportunité de la sortie.
        dd_paliers      = tuple(p.get("dd_scale_out_paliers") or ())
        dd_scale_frac   = float(p.get("dd_scale_out_frac") or 0.0)
        if (stance == "CASH"
                and holdings and dd_paliers and dd_scale_frac > 0):
            unhit_reached = [pal for pal in dd_paliers if pal not in dd_paliers_taken and dd_frac >= pal]
            if unhit_reached:
                top_pal = max(unhit_reached)
                dd_actions = []
                for sym in list(holdings):
                    qty_to_sell = round((holdings[sym].get("qty") or 0) * dd_scale_frac, 8)
                    if qty_to_sell <= 0:
                        continue
                    dd_actions.append({
                        "type":   "scale_out",
                        "symbol": sym,
                        "qty":    qty_to_sell,
                        "reason": (
                            f"Portfolio DD scale-out: -{dd_pct:.1f}% (palier -{top_pal*100:.0f}%), "
                            f"vente {dd_scale_frac*100:.0f}% du qty (peak ${peak:,.0f})"
                        ),
                    })
                if dd_actions:
                    # Mark this palier and any lower ones as taken
                    for pal in dd_paliers:
                        if pal <= top_pal and pal not in dd_paliers_taken:
                            dd_paliers_taken.append(pal)
                    st["dd_paliers_taken"]    = dd_paliers_taken
                    st["last_decision_cycle"] = cycle
                    return {
                        "market_sentiment": "de-risking",
                        "summary":          f"DD scale-out -{dd_pct:.1f}% (palier -{top_pal*100:.0f}%) → {dd_scale_frac*100:.0f}% sell sur {len(dd_actions)} positions",
                        "actions":          dd_actions,
                        "scores":           {},
                        "stance":           "DE-RISKING",
                    }, st

    st["dd_paliers_taken"] = dd_paliers_taken

    # Cadence gate.
    last = st.get("last_decision_cycle")
    if last is not None and cycle - last < p["decide_every_cycles"]:
        return {"market_sentiment": "hold",
                "summary": "régime: hors cadence (hold)",
                "actions": []}, st
    st["last_decision_cycle"] = cycle

    scores = {sym: compute_score_rules(d) for sym, d in market_raw.items()}

    # Maintain two parallel bear-trend timers so stance can switch which signal
    # gates the exit without losing history. ``bear_since_1d`` tracks the
    # daily SMA cross (used in DEPLOY/SELECTIVE), ``bear_since_1h`` tracks the
    # 1h intraday cross (used in PRESERVE/CASH for faster exits).
    bear_since_1d = dict(st.get("bear_since_1d") or st.get("bear_since") or {})
    bear_since_1h = dict(st.get("bear_since_1h") or {})
    entry_ts      = dict(st.get("entry_ts")      or {})
    last_sell_ts  = dict(st.get("last_sell_ts")  or {})

    if now_ts is not None:
        for sym, d in market_raw.items():
            if d.get("trend_1d") == "baissier":
                bear_since_1d.setdefault(sym, now_ts)
            else:
                bear_since_1d.pop(sym, None)
            if d.get("trend") == "baissier":
                bear_since_1h.setdefault(sym, now_ts)
            else:
                bear_since_1h.pop(sym, None)

    exit_signal  = p.get("exit_signal", "trend_1d")
    bear_since   = bear_since_1d if exit_signal == "trend_1d" else bear_since_1h
    confirm_sec  = float(p["trend_confirm_hours"])  * 3600
    min_hold_sec = float(p["min_hold_hours"])       * 3600
    cooldown_sec = float(p["rebuy_cooldown_hours"]) * 3600

    # ── Exits ───────────────────────────────────────────────────────────────
    # Trend-bear timer must elapse AND the holistic score must have fallen
    # under ``score_exit_threshold`` — this anti-whipsaw guard prevents the
    # 1h trend from kicking us out of positions whose multi-signal score
    # still says the setup is sound (the -$60 net signal-exit problem we
    # measured in the 600d backtest).
    score_exit_thr = int(p["score_exit_threshold"])
    actions: list[dict] = []
    selling_now: set[str] = set()
    blocked_cooldown: list[str] = []
    blocked_tier: list[str]     = []
    max_pct    = _max_pct(risk_level)
    cash_after = cash  # mutated as we propose buys

    # ── Prise de profit progressive (scale-out) ─────────────────────────────
    # Règle math universelle (ne dépend pas du symbole) : à chaque palier de
    # gain depuis l'avg_price, on vend une fraction du qty restant. Lock-in
    # incrémental qui complète les stops/trailing/signal exits sans les
    # remplacer. La position garde un reliquat ("moonbag") qui continue à
    # capturer l'upside.
    profit_milestones = tuple(p.get("profit_milestones") or (0.30, 0.60, 1.00))
    scale_out_frac    = float(p.get("scale_out_frac") or (1.0 / 3.0))
    milestones_taken  = {
        sym: list(m)
        for sym, m in (st.get("milestones_taken") or {}).items()
        if sym in holdings  # drop entries for closed positions
    }
    if now_ts is not None:
        for sym, h in holdings.items():
            avg = float(h.get("avg_price") or 0)
            cur = float((market_raw.get(sym) or {}).get("price") or 0)
            if avg <= 0 or cur <= 0:
                continue
            gain = (cur - avg) / avg
            already = milestones_taken.get(sym, [])
            unhit_reached = [m for m in profit_milestones if m not in already and gain >= m]
            if not unhit_reached:
                continue
            top_m       = max(unhit_reached)
            qty_to_sell = round(float(h.get("qty") or 0) * scale_out_frac, 8)
            if qty_to_sell <= 0:
                continue
            actions.append({
                "type":   "scale_out",
                "symbol": sym,
                "qty":    qty_to_sell,
                "reason": (
                    f"Scale-out: gain +{gain*100:.1f}% (palier +{top_m*100:.0f}%), "
                    f"vente {scale_out_frac*100:.0f}% du qty restant (stance {stance})"
                ),
            })
            # Marque le palier (et les inférieurs s'ils ont été sautés).
            for m in profit_milestones:
                if m <= top_m and m not in already:
                    already.append(m)
            milestones_taken[sym] = already
    st["milestones_taken"] = milestones_taken

    # ── Exits: early-on-deterioration + bear-trend timer ────────────────────
    # Two distinct exit paths share this loop:
    #
    # 1. Early-exit (NEW): position en perte ≥ ``early_exit_loss_pct`` ET
    #    score actuel < ``early_exit_score_thr``. Bypasse le bear timer —
    #    quand le setup est cassé techniquement ET qu'on saigne, attendre
    #    24-36h de confirmation bear coûte trop cher. Cible les "signal-
    #    perdants à -$1.5/trade" qui bleed lentement.
    #
    # 2. Bear-timer (existant): trend baissier confirmé ``trend_confirm_hours``
    #    ET score < ``score_exit_threshold``. Anti-whipsaw : on évite de
    #    sortir d'une position dont le score multi-signal dit encore "ok".
    #
    # Both respect ``min_hold_hours`` to avoid panic exits right after entry.
    early_loss_thr  = float(p.get("early_exit_loss_pct") or 0)
    early_score_thr = int(p.get("early_exit_score_thr") or 0)
    for sym in list(holdings):
        if now_ts is None or sym in selling_now:
            continue
        bear_ts   = bear_since.get(sym)
        ent_ts    = entry_ts.get(sym, now_ts)
        sym_score = scores.get(sym, 5)
        held_for  = now_ts - ent_ts
        if held_for < min_hold_sec:
            continue

        # Path 1: early exit on deterioration (loss + weak score).
        cur_price = float((market_raw.get(sym) or {}).get("price") or 0)
        avg_price = float(holdings[sym].get("avg_price") or 0)
        if (early_loss_thr > 0 and early_score_thr > 0
                and cur_price > 0 and avg_price > 0):
            loss_pct = (avg_price - cur_price) / avg_price * 100
            if loss_pct >= early_loss_thr and sym_score < early_score_thr:
                actions.append({
                    "type":      "sell",
                    "symbol":    sym,
                    "qty":       holdings[sym]["qty"],
                    "exit_kind": "early",
                    "reason": (
                        f"Exit précoce — perte {loss_pct:.1f}% ≥ {early_loss_thr:g}% "
                        f"+ score {sym_score:.1f}/10 < {early_score_thr} "
                        f"(hold {held_for / 3600:.1f}h, stance {stance})"
                    ),
                })
                selling_now.add(sym)
                last_sell_ts[sym] = now_ts
                entry_ts.pop(sym, None)
                continue

        # Path 2: bear-trend confirmed + score gate.
        if (bear_ts is not None
                and (now_ts - bear_ts) >= confirm_sec
                and sym_score < score_exit_thr):
            bear_h = (now_ts - bear_ts) / 3600
            hold_h = held_for / 3600
            actions.append({
                "type":   "sell",
                "symbol": sym,
                "qty":    holdings[sym]["qty"],
                "reason": (
                    f"Exit {exit_signal} baissier {bear_h:.1f}h ≥ {p['trend_confirm_hours']:g}h "
                    f"+ score {sym_score}/10 < {score_exit_thr} "
                    f"(hold {hold_h:.1f}h ≥ {p['min_hold_hours']:g}h, stance {stance})"
                ),
            })
            selling_now.add(sym)
            last_sell_ts[sym] = now_ts
            entry_ts.pop(sym, None)

    # ── Entries (ranked by score desc, capped at top_n) ─────────────────────
    held_after = len(holdings) - len(selling_now)
    if in_dd_cooldown:
        candidates = []
    else:
        candidates = sorted(market_raw.items(), key=lambda kv: -scores.get(kv[0], 0))
    for sym, d in candidates:
        # Top-up = the symbol is already a current holding (not being sold this
        # cycle). Adding to it doesn't count against top_n, and uses a stricter
        # threshold so we only stack on high-conviction signals. This is what
        # lets a 1-coin watchlist reach ~100% invested instead of capping at
        # one initial buy (~46% in DEPLOY).
        is_topup = sym in holdings and sym not in selling_now
        if not is_topup and held_after >= p["top_n"]:
            break
        score = scores.get(sym, 0)
        tier = coin_tier(sym, at=as_of_date)
        sym_threshold = _per_coin_threshold(p["buy_threshold"], tier)
        # Stricter conditions on top-ups: only offensive stances, +1 over the
        # entry threshold. PRESERVE/CASH never stack — they're defensive.
        # ALSO: don't double down on a losing position. Even with great
        # technicals (high score), if cur_price < entry by more than the
        # tampon, the market is telling us our timing was off — adding fuel
        # to a bleeding position is what builds bagholders (ETH/SOL/POL on
        # the 1000d run). Creates a neutral zone with early-exit: between
        # -topup_max_loss_pct% and -early_exit_loss_pct% we neither add nor
        # cut, we just wait.
        if is_topup:
            if stance not in ("DEPLOY", "SELECTIVE"):
                continue
            topup_max_loss = float(p.get("topup_max_loss_pct") or 0)
            if topup_max_loss > 0:
                avg_price = float(holdings[sym].get("avg_price") or 0)
                cur_price = float(d.get("price") or 0)
                if avg_price > 0 and cur_price > 0:
                    loss_pct = (avg_price - cur_price) / avg_price * 100
                    if loss_pct > topup_max_loss:
                        continue
            effective_threshold = sym_threshold + 1
        else:
            effective_threshold = sym_threshold
        if score < effective_threshold:
            continue
        if d.get("trend_1d") == "baissier":
            continue
        if tier > risk_tier_cap(risk_level):
            if not is_topup:
                blocked_tier.append(sym)
            continue
        if cooldown_sec > 0 and now_ts is not None:
            sold_at = last_sell_ts.get(sym)
            if sold_at is not None and (now_ts - sold_at) < cooldown_sec:
                if not is_topup:
                    blocked_cooldown.append(sym)
                continue
        size_factor = _per_coin_size_factor(tier)
        size_mult   = float(p.get("size_multiplier", 1.0))
        btc_mult    = _btc_conviction_mult(sym, stance)
        alloc = cash_after * max_pct * size_factor * size_mult * btc_mult
        # Cap any single position so a 2× BTC boost can't drain the pool
        # before the next candidates get their share.
        alloc = min(alloc, cash_after * _MAX_POSITION_PCT_OF_CASH)
        if alloc < 10:
            break
        fng_note = ""
        if fng_adj:
            sign = "+" if fng_adj > 0 else ""
            fng_note = f", fng={fng_value} (thr {sign}{fng_adj})"
        label = "Top-up" if is_topup else "Entry"
        btc_note = f", btc-conviction×{btc_mult:.1f}" if btc_mult != 1.0 else ""
        actions.append({
            "type":        "buy",
            "symbol":      sym,
            "usdc_amount": round(alloc, 2),
            "reason": (
                f"{label} score {score}/10 ≥ {effective_threshold} (tier {tier}, stance {stance}{fng_note}{btc_note}), "
                f"trend_1d={d.get('trend_1d', '?')}, "
                f"{alloc / cash_after * 100:.0f}% du cash dispo"
            ),
        })
        # Preserve entry_ts on top-ups so min_hold / bear_confirm timers stay
        # anchored on the *original* entry, not on each refill.
        if not is_topup and now_ts is not None:
            entry_ts[sym] = now_ts
        cash_after -= alloc
        if not is_topup:
            held_after += 1

    st["bear_since_1d"] = bear_since_1d
    st["bear_since_1h"] = bear_since_1h
    st.pop("bear_since", None)  # legacy key, superseded by per-signal trackers
    st["entry_ts"]      = entry_ts
    st["last_sell_ts"]  = last_sell_ts

    bull_count = sum(1 for d in market_raw.values() if d.get("trend_1d") == "haussier")
    bear_count = sum(1 for d in market_raw.values() if d.get("trend_1d") == "baissier")
    sentiment = ("bullish" if bull_count > bear_count
                 else "bearish" if bear_count > bull_count else "neutral")

    summary_parts = [f"per-sym | held {held_after}/{p['top_n']}",
                     f"breadth bull={bull_count}/bear={bear_count}",
                     f"stance={stance}"]
    if in_dd_cooldown and now_ts is not None:
        hours_left = max(0, (dd_cooldown_until - now_ts) / 3600)
        summary_parts.append(f"DD-cooldown ({hours_left:.0f}h restantes)")
    if fng_adj:
        sign = "+" if fng_adj > 0 else ""
        summary_parts.append(f"fng={fng_value} (thr {sign}{fng_adj})")
    if blocked_tier:
        summary_parts.append(f"risk-tier bloque: {','.join(blocked_tier)}")
    if blocked_cooldown:
        summary_parts.append(f"cooldown bloque: {','.join(blocked_cooldown)}")
    return {
        "market_sentiment": sentiment,
        "summary":          " | ".join(summary_parts),
        "actions":          actions,
        "scores":           scores,
        "stance":           stance,
    }, st
