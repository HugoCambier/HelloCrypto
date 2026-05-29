"""Pluggable decision strategies for the paper simulation.

Two deciders are exposed to a simulation session:

- ``llm``           — the production Claude/Gemini agent (handled inline in
  ``simulation.run``; this module only provides the deterministic one).
- ``deterministic`` — the validated regime-gated basket (approach C): on a slow
  clock, hold the top-N highest-scoring symbols while BTC's daily trend is
  bullish, otherwise sit in cash. Hysteresis keeps held names through minor
  rank changes so the basket doesn't churn.

``regime_decision`` is pure: it reads the live enriched market snapshot and
returns an LLM-shaped decision dict plus updated per-session strategy state.
The caller executes the actions and persists the state across cycles.
"""

from __future__ import annotations

from typing import Any

from .api import compute_score_rules

# Defaults mirror the values validated in the backtest (see backtest.run_live):
# decide every 48 cycles, basket of 3, strict entry bar 8, looser hold bar 6.
DEFAULTS = {
    "decide_every_cycles": 48,
    "top_n":               3,
    "buy_threshold":       8,
    "hold_threshold":      6,
}


def _params(params: dict | None) -> dict:
    p = dict(DEFAULTS)
    if params:
        p.update({k: v for k, v in params.items() if v is not None})
    return p


def regime_decision(
    *,
    market_raw: dict[str, dict],
    holdings: dict[str, dict],
    cash: float,
    cycle: int,
    strat_state: dict[str, Any] | None = None,
    params: dict | None = None,
) -> tuple[dict, dict]:
    """Return (decision, new_strat_state) for the deterministic regime decider.

    The decision mirrors the LLM output shape: ``{market_sentiment, summary,
    actions}``. Actions are ``{"type": "sell", "symbol", "qty", "reason"}`` and
    ``{"type": "buy", "symbol", "reason"}`` — BUYs carry no amount; the executor
    equal-weights the post-sell cash across them (faithful to the backtest).
    """
    p = _params(params)
    st = dict(strat_state or {})

    # Slow clock: only rebalance every `decide_every_cycles` cycles.
    last = st.get("last_decision_cycle")
    if last is not None and cycle - last < p["decide_every_cycles"]:
        return {"market_sentiment": "hold",
                "summary": "régime: hors cadence (hold)",
                "actions": []}, st
    st["last_decision_cycle"] = cycle

    scores = {sym: compute_score_rules(d) for sym, d in market_raw.items()}

    # Market regime from BTC's daily trend (live trend_1d is a real daily MA).
    btc = market_raw.get("BTCUSDC") or {}
    regime_bull = btc.get("trend_1d") == "haussier"

    if regime_bull:
        ranked = sorted(market_raw, key=lambda s: -scores.get(s, 0))
        rank = {s: i for i, s in enumerate(ranked)}
        keep_top = p["top_n"] + 2
        # Hysteresis: keep a held name while still "good enough" and in the wide
        # band, so a #N↔#N+1 rank swap doesn't churn the basket.
        kept = [s for s in holdings
                if scores.get(s, 0) >= p["hold_threshold"] and rank.get(s, 1e9) < keep_top]
        target = list(kept)
        for s in ranked:
            if len(target) >= p["top_n"]:
                break
            if s not in target and scores.get(s, 0) >= p["buy_threshold"]:
                target.append(s)
    else:
        target = []

    actions: list[dict] = []
    for sym in list(holdings):
        if sym not in target:
            actions.append({
                "type":   "sell",
                "symbol": sym,
                "qty":    holdings[sym]["qty"],
                "reason": "Régime bear → cash" if not regime_bull else "Rotation hors panier",
            })
    for sym in target:
        if sym not in holdings:
            actions.append({
                "type":   "buy",
                "symbol": sym,
                "reason": f"Panier top-{p['top_n']} (score {scores.get(sym)}/10)",
            })

    summary = (f"Régime {'BULL' if regime_bull else 'BEAR→cash'} | "
               f"panier cible: {target or '—'}")
    return {
        "market_sentiment": "bullish" if regime_bull else "bearish",
        "summary":          summary,
        "actions":          actions,
        "scores":           scores,
    }, st
