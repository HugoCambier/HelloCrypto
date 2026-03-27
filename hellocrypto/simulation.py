"""Paper-trading simulation engine.

Runs N cycles of agent logic against live Binance prices without
placing any real orders. Fees are simulated at 0.1% per trade
(Binance standard spot rate).
"""

import logging
import threading
from collections.abc import Callable
from datetime import datetime

from .api import format_market_data, get_enriched_market_data, load_config
from .llm import call as llm_call
from .prompts import SYSTEM, build_analysis

log = logging.getLogger(__name__)

SIM_FEE_RATE = 0.001  # 0.1 %


# ── Paper order helpers ───────────────────────────────────────────────────────

def _paper_buy(symbol: str, usdc_amount: float, price: float, holdings: dict) -> float:
    """Simulate a market buy. Returns fee paid (USDC)."""
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
    """Simulate a market sell. Returns (usdc_received, fee)."""
    qty = min(qty, holdings.get(symbol, {}).get("qty", 0))
    if qty <= 0:
        return 0.0, 0.0
    gross = qty * price
    fee   = gross * SIM_FEE_RATE
    holdings[symbol]["qty"] -= qty
    if holdings[symbol]["qty"] <= 0.0001:
        del holdings[symbol]
    return gross - fee, fee


# ── Snapshot builder ──────────────────────────────────────────────────────────

def _snapshot(cycle: int, cash: float, budget: float,
               holdings: dict, prices: dict, history: list, total_fees: float,
               initial_prices: dict | None = None, cycle_sec: int = 60) -> dict:
    """Build a serialisable state snapshot for the current cycle."""
    portfolio_val = sum(
        h["qty"] * prices.get(sym, h["avg_price"]) for sym, h in holdings.items()
    )
    total   = cash + portfolio_val
    pnl     = total - budget
    sells   = [t for t in history if "SELL" in t["action"] and "stop" not in t["action"]]
    profitable = [t for t in sells if t.get("pnl", 0) > 0]

    # ── Buy-and-hold benchmark ────────────────────────────────────────────────
    benchmark_pnl = benchmark_pnl_pct = alpha = None
    if initial_prices:
        valid = [(sym, p0) for sym, p0 in initial_prices.items() if p0 and prices.get(sym)]
        if valid:
            weight     = budget / len(valid)
            weight_net = weight * (1 - SIM_FEE_RATE)   # déduction frais d'achat initiaux 0.1%
            bh_value   = sum(weight_net * prices[sym] / p0 for sym, p0 in valid)
            benchmark_pnl     = round(bh_value - budget, 2)
            benchmark_pnl_pct = round((bh_value - budget) / budget * 100, 2)
            alpha             = round(pnl - (bh_value - budget), 2)

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
        "stop_losses":     len([t for t in trades_only if "stop-loss" in t["action"]]),
        "win_rate":        round(len(profitable) / len(sells_only) * 100, 1) if sells_only else None,
        "benchmark_pnl":      benchmark_pnl,
        "benchmark_pnl_pct":  benchmark_pnl_pct,
        "alpha":              alpha,
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
        "cycle_sec":       cycle_sec,
        "history": list(reversed(history)),
    }


# ── Simulation runner ─────────────────────────────────────────────────────────

def run(
    budget: float,
    config: dict | None = None,
    on_cycle: Callable | None = None,
    stop_event: threading.Event | None = None,
) -> dict:
    """Run a paper-trading simulation and return the final snapshot dict.

    Mirrors the real agent loop: runs indefinitely until ``stop_event`` is set.
    Sleeps ``cycle_seconds`` between cycles (same as the live agent).

    Args:
        budget:      Starting USDC balance.
        config:      App config (defaults to ``load_config()``).
        on_cycle:    ``fn(cycle, snapshot_dict)`` called at end of each cycle.
        stop_event:  ``threading.Event`` — set it to stop the simulation.
    """
    import time

    cfg        = config or load_config()
    watchlist  = cfg["watchlist"]
    stop_loss  = float(cfg["stop_loss_pct"]) / 100
    cycle_sec  = int(cfg.get("cycle_seconds", 60))
    risk_level = int(cfg.get("risk_level", 3))

    cash: float          = budget
    holdings: dict       = {}
    history: list        = []
    recent_decisions: list = []
    total_fees: float    = 0.0
    prices: dict         = {}
    initial_prices: dict = {}
    cycle: int           = 0

    while True:
        if stop_event and stop_event.is_set():
            log.info("[SIM] Arrêtée par l'utilisateur au cycle %d", cycle)
            break

        cycle += 1

        # ── Fetch enriched market data ────────────────────────────────────────
        market_raw = get_enriched_market_data(watchlist)
        prices     = {sym: d["price"] for sym, d in market_raw.items()}

        if not prices:
            if stop_event:
                stop_event.wait(timeout=cycle_sec)
            else:
                time.sleep(cycle_sec)
            continue

        # ── Record initial prices (once, for benchmark) ───────────────────────
        if not initial_prices:
            initial_prices = dict(prices)
            log.info("[SIM] Prix initiaux (benchmark): %s",
                     ", ".join(f"{s}=${p:,.4f}" for s, p in initial_prices.items()))

        # ── Stop-loss ─────────────────────────────────────────────────────────
        for sym in list(holdings):
            if sym not in prices:
                continue
            entry = holdings[sym]["avg_price"]
            cur   = prices[sym]
            loss  = (cur - entry) / entry
            if loss < -stop_loss:
                qty           = holdings[sym]["qty"]
                received, fee = _paper_sell(sym, qty, cur, holdings)
                cash         += received
                total_fees   += fee
                history.append({
                    "cycle":     cycle,
                    "timestamp": datetime.utcnow().isoformat(),
                    "action":    "SELL (stop-loss)",
                    "symbol":    sym,
                    "qty":       qty,
                    "price":     cur,
                    "pnl":       round((cur - entry) * qty - fee, 4),
                    "fee":       round(fee, 6),
                    "reason":    f"Stop-loss {stop_loss*100:.0f}% déclenché",
                })
                log.info("[SIM] STOP-LOSS %s: %.1f%%", sym, loss * 100)

        # ── LLM decision ──────────────────────────────────────────────────────
        market_data = format_market_data(market_raw, watchlist)
        try:
            decision = llm_call(
                prompt=build_analysis(market_data, holdings, cash, budget, risk_level, recent_decisions),
                system=SYSTEM,
                config=cfg,
            )
        except Exception as exc:
            log.error("[SIM] Erreur LLM cycle %d: %s", cycle, exc)
            if on_cycle:
                on_cycle(cycle, _snapshot(cycle, cash, budget,
                                          holdings, prices, history, total_fees, initial_prices, cycle_sec))
            if stop_event:
                stop_event.wait(timeout=cycle_sec)
            else:
                time.sleep(cycle_sec)
            continue

        sentiment = decision.get("market_sentiment", "—")
        summary   = decision.get("summary", "")
        log.info("[SIM] Cycle %d | %s | %s", cycle, sentiment, summary)
        recent_decisions = (recent_decisions + [decision])[-3:]

        # ── Log LLM analysis as an activity entry ─────────────────────────────
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
            atype  = action["type"]
            sym    = action["symbol"]
            reason = action.get("reason", "")

            if atype == "buy" and cash > 10 and sym in prices:
                amount = min(action.get("usdc_amount", 0), cash * max_pct)
                if amount >= 10:
                    fee         = _paper_buy(sym, amount, prices[sym], holdings)
                    total_fees += fee
                    cash       -= amount
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
                    log.info("[SIM] BUY  $%.2f %s @ $%.4f", amount, sym, prices[sym])

            elif atype == "sell" and sym in holdings and sym in prices:
                qty           = min(action.get("qty", holdings[sym]["qty"]), holdings[sym]["qty"])
                entry         = holdings[sym]["avg_price"]
                received, fee = _paper_sell(sym, qty, prices[sym], holdings)
                total_fees   += fee
                cash         += received
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
                log.info("[SIM] SELL %.6f %s @ $%.4f", qty, sym, prices[sym])

        # ── Emit snapshot at end of cycle ─────────────────────────────────────
        if on_cycle:
            on_cycle(cycle, _snapshot(cycle, cash, budget,
                                      holdings, prices, history, total_fees, initial_prices, cycle_sec))

        # Attendre cycle_sec en vérifiant stop_event toutes les secondes
        if stop_event:
            stop_event.wait(timeout=cycle_sec)
        else:
            time.sleep(cycle_sec)

    return _snapshot(cycle, cash, budget, holdings, prices, history, total_fees, initial_prices, cycle_sec)
