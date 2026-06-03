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

from typing import Any

from .api import compute_score_rules
from .coin_tiers import is_allowed as _coin_allowed

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
}

# Per-stance overrides. ``exit_signal`` switches the source of the bearish-trend
# timer between the daily SMA cross (slow, ~weeks lag) and the 1h SMA cross
# (~25h lag). In defensive stances we want faster exits, so we read from the
# faster signal. ``top_n=0`` in CASH effectively blocks all new entries.
STANCE_PARAMS: dict[str, dict] = {
    # ``score_exit_threshold`` gates exits by a holistic score check. We turn
    # the gate ON in bull/neutral stances (let winners run, ignore intraday
    # noise) and OFF in defensive stances (exit fast, don't second-guess).
    "DEPLOY":    {"buy_threshold": 7,  "top_n": 4, "exit_signal": "trend_1d", "score_exit_threshold": 5},
    "SELECTIVE": {"buy_threshold": 8,  "top_n": 3, "exit_signal": "trend_1d", "score_exit_threshold": 5},
    "PRESERVE":  {"buy_threshold": 9,  "top_n": 2, "exit_signal": "trend",    "score_exit_threshold": 99},
    "CASH":      {"buy_threshold": 11, "top_n": 0, "exit_signal": "trend",    "score_exit_threshold": 99},
}

# CASH triggers — leading signals so we don't lag the lagging trend_1d.
CASH_BTC_DRAWDOWN_PCT     = 7.0  # BTC down this much from its 7d high
CASH_BEAR_BREADTH_INTRA   = 0.7  # ratio of watchlist with intraday `trend` == baissier


def _derive_stance(market_raw: dict) -> str:
    """Derive DEPLOY / SELECTIVE / PRESERVE / CASH from market signals.

    CASH (no new entries, fast exits via intraday signal) is triggered by
    *leading* indicators — BTC drawdown from its 7d high, or intraday bear
    breadth — rather than the lagging daily trend, so it activates *before*
    the worst of the downturn lands.
    """
    btc = market_raw.get("BTCUSDC") or {}
    btc_trend    = btc.get("trend_1d")
    btc_drawdown = btc.get("drawdown_pct_7d")

    # Leading: BTC is in a >7% pullback from its 7d high → defense.
    if btc_drawdown is not None and btc_drawdown >= CASH_BTC_DRAWDOWN_PCT:
        return "CASH"

    # Leading: intraday breadth collapse on 1h SMA cross.
    if market_raw:
        bear_intra = sum(1 for d in market_raw.values() if d.get("trend") == "baissier")
        if bear_intra / len(market_raw) >= CASH_BEAR_BREADTH_INTRA:
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


def regime_decision(
    *,
    market_raw: dict[str, dict],
    holdings: dict[str, dict],
    cash: float,
    cycle: int,
    now_ts: float | None = None,
    risk_level: int = 5,
    strat_state: dict[str, Any] | None = None,
    params: dict | None = None,
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
    if p.get("enable_regime_stance"):
        stance = _derive_stance(market_raw)
        for k, v in STANCE_PARAMS[stance].items():
            if k not in user_pinned:
                p[k] = v
    st = dict(strat_state or {})

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
    for sym in list(holdings):
        if now_ts is None:
            continue
        bear_ts = bear_since.get(sym)
        ent_ts  = entry_ts.get(sym, now_ts)
        sym_score = scores.get(sym, 5)
        if (bear_ts is not None
                and (now_ts - bear_ts) >= confirm_sec
                and (now_ts - ent_ts) >= min_hold_sec
                and sym_score < score_exit_thr):
            actions.append({
                "type":   "sell",
                "symbol": sym,
                "qty":    holdings[sym]["qty"],
                "reason": f"{exit_signal} baissier ({p['trend_confirm_hours']:g}h) + score {sym_score}<{score_exit_thr}",
            })
            selling_now.add(sym)
            last_sell_ts[sym] = now_ts
            entry_ts.pop(sym, None)

    # ── Entries (ranked by score desc, capped at top_n) ─────────────────────
    held_after = len(holdings) - len(selling_now)
    max_pct    = _max_pct(risk_level)
    cash_after = cash  # mutated as we propose buys
    blocked_cooldown: list[str] = []
    blocked_tier: list[str]     = []
    candidates = sorted(market_raw.items(), key=lambda kv: -scores.get(kv[0], 0))
    for sym, d in candidates:
        if held_after >= p["top_n"]:
            break
        if sym in holdings and sym not in selling_now:
            continue
        score = scores.get(sym, 0)
        if score < p["buy_threshold"]:
            continue
        if d.get("trend_1d") == "baissier":
            continue
        # Risk-tier gate: skip coins above the user's risk tolerance.
        if not _coin_allowed(sym, risk_level):
            blocked_tier.append(sym)
            continue
        if cooldown_sec > 0 and now_ts is not None:
            sold_at = last_sell_ts.get(sym)
            if sold_at is not None and (now_ts - sold_at) < cooldown_sec:
                blocked_cooldown.append(sym)
                continue
        alloc = cash_after * max_pct
        if alloc < 10:
            break
        actions.append({
            "type":        "buy",
            "symbol":      sym,
            "usdc_amount": round(alloc, 2),
            "reason":      f"Score {score}/10 entry (risk {risk_level} → {max_pct*100:.0f}%)",
        })
        if now_ts is not None:
            entry_ts[sym] = now_ts
        cash_after -= alloc
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
