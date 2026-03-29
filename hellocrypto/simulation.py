"""Paper-trading simulation engine."""

import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .api import (
    compute_scores,
    format_market_data,
    get_btc_dominance,
    get_enriched_market_data,
    get_fear_and_greed,
    load_config,
)
from .llm import call as llm_call
from .prompts import SYSTEM, build_analysis
log = logging.getLogger(__name__)

SIM_FEE_RATE   = 0.001  # 0.1 %
SIM_STATE_FILE = Path("data/simulation_state.json")


# ── Paper order helpers ────────────────────────────────────────────────────────

def _paper_buy(symbol: str, usdc_amount: float, price: float, holdings: dict) -> float:
    fee     = usdc_amount * SIM_FEE_RATE
    qty_net = (usdc_amount - fee) / price
    if symbol in holdings:
        prev    = holdings[symbol]
        new_qty = prev["qty"] + qty_net
        holdings[symbol] = {
            "qty":       new_qty,
            "avg_price": (prev["avg_price"] * prev["qty"] + price * qty_net) / new_qty,
        }
    else:
        holdings[symbol] = {"qty": qty_net, "avg_price": price}
    return fee


def _paper_sell(symbol: str, qty: float, price: float, holdings: dict) -> tuple[float, float]:
    qty   = min(qty, holdings.get(symbol, {}).get("qty", 0))
    if qty <= 0:
        return 0.0, 0.0
    gross = qty * price
    fee   = gross * SIM_FEE_RATE
    holdings[symbol]["qty"] -= qty
    if holdings[symbol]["qty"] <= 0.0001:
        del holdings[symbol]
    return gross - fee, fee


# ── Persistence helpers ────────────────────────────────────────────────────────

def _save_state(state: dict) -> None:
    try:
        SIM_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SIM_STATE_FILE.write_text(json.dumps({**state, "saved_at": datetime.utcnow().isoformat()}, indent=2))
    except Exception as exc:
        log.warning("[SIM] Impossible de sauvegarder l'état: %s", exc)


def _load_state() -> dict | None:
    try:
        data = json.loads(SIM_STATE_FILE.read_text())
        if data.get("schema_version", 1) != 1:
            log.warning("[SIM] Version de schéma incompatible — démarrage propre")
            return None
        return data
    except Exception:
        return None


# ── Snapshot builder ───────────────────────────────────────────────────────────

def _snapshot(cycle, cash, budget, holdings, prices, history, total_fees,
              initial_prices=None, cycle_sec=60):
    portfolio_val = sum(
        h["qty"] * prices.get(sym, h["avg_price"]) for sym, h in holdings.items()
    )
    total = cash + portfolio_val
    pnl   = total - budget

    benchmark_pnl = benchmark_pnl_pct = alpha = None
    btc_bh_pnl = btc_bh_pct = None
    if initial_prices:
        valid = [(sym, p0) for sym, p0 in initial_prices.items() if p0 and prices.get(sym)]
        if valid:
            weight     = budget / len(valid)
            weight_net = weight * (1 - SIM_FEE_RATE)
            bh_value   = sum(weight_net * prices[sym] / p0 for sym, p0 in valid)
            benchmark_pnl     = round(bh_value - budget, 2)
            benchmark_pnl_pct = round((bh_value - budget) / budget * 100, 2)
            alpha             = round(pnl - (bh_value - budget), 2)

        btc_sym = next((s for s in initial_prices if "BTC" in s and initial_prices[s] and prices.get(s)), None)
        if btc_sym:
            btc_val    = budget * (1 - SIM_FEE_RATE) * prices[btc_sym] / initial_prices[btc_sym]
            btc_bh_pnl = round(btc_val - budget, 2)
            btc_bh_pct = round((btc_val - budget) / budget * 100, 2)

    trades_only = [t for t in history if t["action"] != "ANALYSE"]
    sells_only  = [t for t in trades_only if "SELL" in t["action"] and "stop" not in t["action"]]
    profitable  = [t for t in sells_only if t.get("pnl", 0) > 0]

    return {
        "cycle":           cycle,
        "cash":            round(cash, 2),
        "portfolio_value": round(portfolio_val, 2),
        "total_value":     round(total, 2),
        "pnl":             round(pnl, 2),
        "pnl_pct":         round(pnl / budget * 100, 2),
        "total_fees":      round(total_fees, 4),
        "trades":          len(trades_only),
        "buys":            len([t for t in trades_only if t["action"] == "BUY"]),
        "sells":           len(sells_only),
        "stop_losses":     len([t for t in trades_only if "stop" in t["action"]]),
        "win_rate":        round(len(profitable) / len(sells_only) * 100, 1) if sells_only else None,
        "benchmark_pnl":      benchmark_pnl,
        "benchmark_pnl_pct":  benchmark_pnl_pct,
        "alpha":              alpha,
        "btc_bh_pnl":         btc_bh_pnl,
        "btc_bh_pct":         btc_bh_pct,
        "positions": [
            {
                "symbol":        sym,
                "qty":           round(h["qty"], 6),
                "avg_price":     round(h["avg_price"], 4),
                "current_price": prices.get(sym),
                "value":         round(h["qty"] * prices.get(sym, h["avg_price"]), 2),
                "pnl_pct":       round((prices[sym] - h["avg_price"]) / h["avg_price"] * 100, 2)
                                 if prices.get(sym) else 0,
            }
            for sym, h in holdings.items()
        ],
        "cycle_sec": cycle_sec,
        "history":   list(reversed(history)),
    }


# ── Simulation runner ──────────────────────────────────────────────────────────

def run(
    budget: float,
    config: dict | None = None,
    on_cycle: Callable | None = None,
    stop_event: threading.Event | None = None,
    resume: bool = False,
) -> dict:
    import time

    cfg                  = config or load_config()
    watchlist            = cfg["watchlist"]
    stop_loss            = float(cfg["stop_loss_pct"]) / 100
    trail_stop           = float(cfg.get("trailing_stop_pct", 5)) / 100
    cycle_sec            = int(cfg.get("cycle_seconds", 60))
    risk_level           = int(cfg.get("risk_level", 3))
    sell_cooldown_cycles = int(cfg.get("sell_cooldown_cycles", 3))

    # ── State initialisation (fresh or resumed) ────────────────────────────────
    cash: float          = budget
    holdings: dict       = {}
    history: list        = []
    recent_decisions: list = []
    total_fees: float    = 0.0
    prices: dict         = {}
    initial_prices: dict = {}
    peak_prices: dict    = {}   # sym → highest price seen since entry
    cooldown_map: dict   = {}   # sym → last sell cycle
    cycle: int           = 0

    if resume:
        saved = _load_state()
        if saved:
            cycle            = saved.get("cycle", 0)
            cash             = saved.get("cash", budget)
            holdings         = saved.get("holdings", {})
            history          = saved.get("history", [])
            recent_decisions = saved.get("recent_decisions", [])
            total_fees       = saved.get("total_fees", 0.0)
            initial_prices   = saved.get("initial_prices", {})
            peak_prices      = saved.get("peak_prices", {})
            cooldown_map     = {k: int(v) for k, v in saved.get("cooldown_map", {}).items()}
            budget           = saved.get("budget", budget)
            log.info("[SIM] Reprise depuis cycle %d — cash $%.2f", cycle, cash)
        else:
            log.info("[SIM] Aucun état sauvegardé — démarrage propre")

    while True:
        if stop_event and stop_event.is_set():
            log.info("[SIM] Arrêtée par l'utilisateur au cycle %d", cycle)
            break

        cycle += 1

        # ── Fetch enriched market data ─────────────────────────────────────────
        try:
            market_raw = get_enriched_market_data(watchlist, cycle_seconds=cycle_sec)
            prices     = {sym: d["price"] for sym, d in market_raw.items()}
        except Exception as exc:
            log.error("[SIM] Erreur fetch données cycle %d: %s", cycle, exc, exc_info=True)
            prices = {}
            market_raw = {}

        if not prices:
            if stop_event:
                stop_event.wait(timeout=cycle_sec)
            else:
                time.sleep(cycle_sec)
            continue

        if not initial_prices:
            initial_prices = dict(prices)
            log.info("[SIM] Prix initiaux (benchmark): %s",
                     ", ".join(f"{s}=${p:,.4f}" for s, p in initial_prices.items()))

        # ── Update peak prices ─────────────────────────────────────────────────
        for sym in holdings:
            if sym in prices:
                peak_prices[sym] = max(peak_prices.get(sym, prices[sym]), prices[sym])

        # ── Stop-loss (hard + trailing) ────────────────────────────────────────
        for sym in list(holdings):
            if sym not in prices:
                continue
            entry      = holdings[sym]["avg_price"]
            cur        = prices[sym]
            peak       = peak_prices.get(sym, entry)
            hard_loss  = (cur - entry) / entry
            trail_loss = (cur - peak)  / peak

            triggered = False
            action_label = ""
            reason_str   = ""

            if hard_loss < -stop_loss:
                triggered    = True
                action_label = "SELL (stop-loss)"
                reason_str   = f"Stop-loss fixe {stop_loss*100:.0f}% déclenché"
            elif trail_loss < -trail_stop and peak > entry and cur >= entry:
                triggered    = True
                action_label = "SELL (trailing-stop)"
                reason_str   = f"Trailing stop {trail_stop*100:.0f}% depuis pic ${peak:,.4f}"

            if triggered:
                qty           = holdings[sym]["qty"]
                received, fee = _paper_sell(sym, qty, cur, holdings)
                cash         += received
                total_fees   += fee
                peak_prices.pop(sym, None)
                cooldown_map[sym] = cycle
                history.append({
                    "cycle":     cycle,
                    "timestamp": datetime.utcnow().isoformat(),
                    "action":    action_label,
                    "symbol":    sym,
                    "qty":       qty,
                    "price":     cur,
                    "pnl":       round((cur - entry) * qty - fee, 4),
                    "fee":       round(fee, 6),
                    "reason":    reason_str,
                })
                log.info("[SIM] %s %s: hard=%.1f%% trail=%.1f%%",
                         action_label, sym, hard_loss * 100, trail_loss * 100)

        # ── Fetch global market context ────────────────────────────────────────
        fear_greed    = get_fear_and_greed()
        btc_dominance = get_btc_dominance()
        scores        = compute_scores(market_raw)

        # ── LLM decision ──────────────────────────────────────────────────────
        market_data = format_market_data(market_raw, watchlist)
        try:
            decision = llm_call(
                prompt=build_analysis(
                    market_data, holdings, cash, budget, risk_level,
                    recent_decisions, fear_greed, btc_dominance, scores,
                ),
                system=SYSTEM,
                config=cfg,
            )
        except Exception as exc:
            log.error("[SIM] Erreur LLM cycle %d: %s", cycle, exc)
            snap = _snapshot(cycle, cash, budget, holdings, prices, history, total_fees, initial_prices, cycle_sec)
            if on_cycle:
                on_cycle(cycle, snap)
            _save_state({"schema_version": 1, "budget": budget, "cycle": cycle, "cash": cash,
                         "holdings": holdings, "history": history, "total_fees": total_fees,
                         "initial_prices": initial_prices, "peak_prices": peak_prices,
                         "cooldown_map": cooldown_map, "recent_decisions": recent_decisions})
            if stop_event:
                stop_event.wait(timeout=cycle_sec)
            else:
                time.sleep(cycle_sec)
            continue

        sentiment = decision.get("market_sentiment", "—")
        summary   = decision.get("summary", "")
        log.info("[SIM] Cycle %d | %s | %s", cycle, sentiment, summary)
        recent_decisions = (recent_decisions + [decision])[-3:]

        history.append({
            "cycle":     cycle,
            "timestamp": datetime.utcnow().isoformat(),
            "action":    "ANALYSE",
            "sentiment": sentiment,
            "reason":    summary,
            "symbol":    "",
            "qty":       None,
            "amount":    None,
            "price":     None,
            "fee":       None,
            "pnl":       None,
        })

        # ── Execute paper trades ───────────────────────────────────────────────
        max_pct = (5 + risk_level * 4) / 100
        for action in decision.get("actions", []):
            atype  = action.get("type", "")
            sym    = action.get("symbol", "")
            if not atype or not sym:
                continue
            reason = action.get("reason", "")

            if atype == "buy" and cash > 10 and sym in prices:
                # Cooldown check
                last_sell = cooldown_map.get(sym, 0)
                if cycle - last_sell < sell_cooldown_cycles:
                    log.info("[SIM] COOLDOWN %s (%d cycles restants)", sym,
                             sell_cooldown_cycles - (cycle - last_sell))
                    continue

                # Dynamic sizing based on RSI
                rsi = market_raw.get(sym, {}).get("rsi14")
                rsi_factor = max(0.5, min(1.5, 1.5 - (rsi - 20) / 60)) if rsi is not None else 1.0

                amount = min(action.get("usdc_amount", 0), cash * max_pct * rsi_factor)
                if amount >= 10:
                    fee         = _paper_buy(sym, amount, prices[sym], holdings)
                    total_fees += fee
                    cash       -= amount
                    peak_prices[sym] = prices[sym]
                    history.append({
                        "cycle":     cycle,
                        "timestamp": datetime.utcnow().isoformat(),
                        "action":    "BUY",
                        "symbol":    sym,
                        "amount":    amount,
                        "price":     prices[sym],
                        "fee":       round(fee, 6),
                        "reason":    reason,
                    })
                    log.info("[SIM] BUY  $%.2f %s @ $%.4f (RSI=%.0f factor=%.2f)",
                             amount, sym, prices[sym], rsi or 0, rsi_factor)

            elif atype == "sell" and sym in holdings and sym in prices:
                qty           = min(action.get("qty", holdings[sym]["qty"]), holdings[sym]["qty"])
                entry         = holdings[sym]["avg_price"]
                received, fee = _paper_sell(sym, qty, prices[sym], holdings)
                total_fees   += fee
                cash         += received
                peak_prices.pop(sym, None)
                cooldown_map[sym] = cycle
                history.append({
                    "cycle":     cycle,
                    "timestamp": datetime.utcnow().isoformat(),
                    "action":    "SELL",
                    "symbol":    sym,
                    "qty":       qty,
                    "price":     prices[sym],
                    "pnl":       round((prices[sym] - entry) * qty - fee, 4),
                    "fee":       round(fee, 6),
                    "reason":    reason,
                })

        # ── Emit snapshot & persist state ─────────────────────────────────────
        snap = _snapshot(cycle, cash, budget, holdings, prices, history, total_fees, initial_prices, cycle_sec)
        if on_cycle:
            on_cycle(cycle, snap)

        _save_state({
            "schema_version": 1,
            "budget":         budget,
            "cycle":          cycle,
            "cash":           cash,
            "holdings":       holdings,
            "history":        history,
            "total_fees":     total_fees,
            "initial_prices": initial_prices,
            "peak_prices":    peak_prices,
            "cooldown_map":   cooldown_map,
            "recent_decisions": recent_decisions,
        })

        if stop_event:
            stop_event.wait(timeout=cycle_sec)
        else:
            time.sleep(cycle_sec)

    return _snapshot(cycle, cash, budget, holdings, prices, history, total_fees, initial_prices, cycle_sec)
