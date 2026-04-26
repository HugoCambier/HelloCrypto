"""Paper-trading simulation engine."""

from __future__ import annotations

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
from .trading import FEE_RATE as SIM_FEE_RATE, check_stops, compute_position_size, paper_buy, paper_sell
log = logging.getLogger(__name__)

SIM_STATE_FILE = Path("data/simulation_state.json")


# ── Persistence helpers ────────────────────────────────────────────────────────

def _save_state(state: dict) -> None:
    try:
        from db.store import set_state
        set_state("simulation", {**state, "saved_at": datetime.utcnow().isoformat(), "schema_version": 1})
        return
    except ImportError:
        pass
    # JSON fallback
    try:
        SIM_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SIM_STATE_FILE.write_text(json.dumps({**state, "saved_at": datetime.utcnow().isoformat()}, indent=2))
    except Exception as exc:
        log.warning("[SIM] Impossible de sauvegarder l'état: %s", exc)


def _load_state() -> dict | None:
    try:
        from db.store import get_state
        data = get_state("simulation")
        if data and data.get("schema_version", 1) != 1:
            log.warning("[SIM] Version de schéma incompatible — démarrage propre")
            return None
        return data
    except ImportError:
        pass
    # JSON fallback
    try:
        data = json.loads(SIM_STATE_FILE.read_text())
        if data.get("schema_version", 1) != 1:
            log.warning("[SIM] Version de schéma incompatible — démarrage propre")
            return None
        return data
    except Exception:
        return None


# ── Snapshot builder ───────────────────────────────────────────────────────────

def _snapshot(cycle, cash, holdings, prices, history, total_fees,
              initial_total_value, initial_prices, cycle_sec=60):
    portfolio_val = sum(
        h["qty"] * prices.get(sym, h["avg_price"]) for sym, h in holdings.items()
    )
    total = cash + portfolio_val
    base  = initial_total_value or 0
    pnl   = total - base

    benchmark_pnl = benchmark_pnl_pct = alpha = None
    btc_bh_pnl = btc_bh_pct = None
    if initial_prices and base > 0:
        valid = [(sym, p0) for sym, p0 in initial_prices.items() if p0 and prices.get(sym)]
        if valid:
            weight     = base / len(valid)
            weight_net = weight * (1 - SIM_FEE_RATE)
            bh_value   = sum(weight_net * prices[sym] / p0 for sym, p0 in valid)
            benchmark_pnl     = round(bh_value - base, 2)
            benchmark_pnl_pct = round((bh_value - base) / base * 100, 2)
            alpha             = round(pnl - (bh_value - base), 2)

        btc_sym = next((s for s in initial_prices if "BTC" in s and initial_prices[s] and prices.get(s)), None)
        if btc_sym:
            btc_val    = base * (1 - SIM_FEE_RATE) * prices[btc_sym] / initial_prices[btc_sym]
            btc_bh_pnl = round(btc_val - base, 2)
            btc_bh_pct = round((btc_val - base) / base * 100, 2)

    trades_only = [t for t in history if t["action"] != "ANALYSE"]
    sells_only  = [t for t in trades_only if "SELL" in t["action"] and "stop" not in t["action"]]
    profitable  = [t for t in sells_only if t.get("pnl", 0) > 0]

    return {
        "cycle":           cycle,
        "cash":            round(cash, 2),
        "portfolio_value": round(portfolio_val, 2),
        "total_value":     round(total, 2),
        "pnl":             round(pnl, 2),
        "pnl_pct":         round(pnl / base * 100, 2) if base > 0 else 0,
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
    max_cycles: int | None = None,
    initial_holdings: dict[str, float] | None = None,
    session_id: str | None = None,
    session_name: str | None = None,
    liquidate_at_end: bool = False,
) -> dict:
    """Run the paper-trading simulation.

    ``initial_holdings`` is an optional dict ``{symbol: qty}`` used to seed
    the portfolio on a fresh start (ignored when ``resume=True`` and a saved
    state is found).  avg_price is set to the first-cycle market price.
    """
    import time
    import uuid as _uuid
    if not session_id:
        session_id = _uuid.uuid4().hex[:8]
    if not session_name:
        session_name = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    log.info("[SIM] Session: %s - %s", session_id, session_name)

    cfg                  = config or load_config()
    watchlist            = cfg["watchlist"]
    stop_loss            = float(cfg["stop_loss_pct"]) / 100
    trail_stop           = float(cfg.get("trailing_stop_pct", 5)) / 100
    cycle_sec            = int(cfg.get("cycle_seconds", 60))
    risk_level           = int(cfg.get("risk_level", 3))
    sell_cooldown_cycles = int(cfg.get("sell_cooldown_cycles", 3))

    try:
        from db.store import DBLogHandler as _DBH
        _db_handler = _DBH(mode="simulation", session_id=session_id)
        logging.getLogger().addHandler(_db_handler)
    except ImportError:
        _db_handler = None

    # ── State initialisation (fresh or resumed) ────────────────────────────────
    cash: float          = budget
    holdings: dict       = {}
    history: list        = []
    recent_decisions: list = []
    total_fees: float    = 0.0
    prices: dict         = {}
    initial_prices: dict = {}
    initial_total_value: float = 0.0
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
            initial_total_value = saved.get("initial_total_value", 0.0) or 0.0
            peak_prices      = saved.get("peak_prices", {})
            cooldown_map     = {k: int(v) for k, v in saved.get("cooldown_map", {}).items()}
            budget           = saved.get("budget", budget)
            log.info("[SIM] Reprise depuis cycle %d — cash $%.2f", cycle, cash)
        else:
            log.info("[SIM] Aucun état sauvegardé — démarrage propre")

    effective_max = max_cycles
    if liquidate_at_end and max_cycles is not None:
        effective_max = max_cycles + 1

    while True:
        if stop_event and stop_event.is_set():
            log.info("[SIM] Arrêtée par l'utilisateur au cycle %d", cycle)
            break
        if effective_max is not None and cycle >= effective_max:
            log.info("[SIM] max_cycles=%d atteint — arrêt", max_cycles)
            break

        cycle += 1
        is_liquidation_cycle = (liquidate_at_end and max_cycles is not None and cycle > max_cycles)
        if _db_handler is not None:
            _db_handler.set_cycle(cycle)

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
            # Seed holdings from initial_holdings on first cycle (fresh start only)
            if initial_holdings and not holdings:
                for sym, qty in initial_holdings.items():
                    if qty > 0 and sym in prices:
                        holdings[sym] = {"qty": qty, "avg_price": prices[sym]}
                        peak_prices[sym] = prices[sym]
                        log.info("[SIM] Avoir initial: %s qty=%.6f @ $%.4f", sym, qty, prices[sym])
            
            initial_portfolio_val = sum(
                h["qty"] * prices.get(sym, h["avg_price"]) for sym, h in holdings.items()
            )
            initial_total_value = cash + initial_portfolio_val
            log.info("[SIM] Valeur initiale du portefeuille: $%.2f (cash) + $%.2f (actifs) = $%.2f",
                     cash, initial_portfolio_val, initial_total_value)

            # Persist initial state to sessions table
            try:
                from db.store import upsert_session as _upsert
                _upsert(
                    session_id=session_id,
                    name=session_name,
                    mode="simulation",
                    initial_state={
                        "budget":           budget,
                        "initial_prices":   initial_prices,
                        "initial_holdings": {
                            sym: {"qty": h["qty"], "avg_price": h["avg_price"]}
                            for sym, h in holdings.items()
                        },
                        "initial_total_value": initial_total_value,
                        "watchlist": watchlist,
                    },
                )
            except Exception:
                pass

        # ── Update peak prices ─────────────────────────────────────────────────
        for sym in holdings:
            if sym in prices:
                peak_prices[sym] = max(peak_prices.get(sym, prices[sym]), prices[sym])

        # ── Stop-loss (hard + trailing) ────────────────────────────────────────
        for sig in check_stops(holdings, prices, peak_prices, stop_loss, trail_stop):
            sym          = sig.symbol
            entry        = holdings[sym]["avg_price"] if sym in holdings else sig.price
            action_label = f"SELL ({sig.kind})"
            reason_str   = (
                f"Stop-loss fixe {stop_loss*100:.0f}% déclenché"
                if sig.kind == "stop-loss"
                else f"Trailing stop {trail_stop*100:.0f}% depuis pic ${peak_prices.get(sym, sig.price):,.4f}"
            )
            result = paper_sell(sym, sig.qty, sig.price, holdings)
            cash         += result.received
            total_fees   += result.fee
            peak_prices.pop(sym, None)
            cooldown_map[sym] = cycle
            history.append({
                "cycle":     cycle,
                "timestamp": datetime.utcnow().isoformat(),
                "action":    action_label,
                "symbol":    sym,
                "qty":       result.qty,
                "price":     sig.price,
                "pnl":       round((sig.price - entry) * result.qty - result.fee, 4),
                "fee":       round(result.fee, 6),
                "reason":    reason_str,
            })
            try:
                from db.store import save_trade as _db_save
                _db_save(
                    action=action_label, symbol=sym, amount=None, price=sig.price,
                    reason=reason_str, fee=result.fee, qty=result.qty,
                    pnl=round((sig.price - entry) * result.qty - result.fee, 4),
                    mode="simulation", session_id=session_id, session_name=session_name,
                )
            except Exception:
                pass
            log.info("[SIM] %s %s: %.1f%%", action_label, sym, sig.loss_pct * 100)

        # ── Liquidation cycle: force-sell everything ─────────────────────────
        if is_liquidation_cycle:
            log.info("[SIM] Cycle de liquidation — vente de toutes les positions")
            for sym in list(holdings.keys()):
                if sym not in prices:
                    continue
                entry  = holdings[sym]["avg_price"]
                qty    = holdings[sym]["qty"]
                result = paper_sell(sym, qty, prices[sym], holdings)
                cash       += result.received
                total_fees += result.fee
                peak_prices.pop(sym, None)
                pnl = round((prices[sym] - entry) * result.qty - result.fee, 4)
                history.append({
                    "cycle":     cycle,
                    "timestamp": datetime.utcnow().isoformat(),
                    "action":    "SELL (liquidation)",
                    "symbol":    sym,
                    "qty":       result.qty,
                    "price":     prices[sym],
                    "pnl":       pnl,
                    "fee":       round(result.fee, 6),
                    "reason":    "Liquidation finale — conversion en USDC",
                })
                try:
                    from db.store import save_trade as _db_save
                    _db_save(
                        action="SELL (liquidation)", symbol=sym, amount=None,
                        price=prices[sym], reason="Liquidation finale — conversion en USDC",
                        fee=result.fee, qty=result.qty, pnl=pnl,
                        mode="simulation", session_id=session_id, session_name=session_name,
                    )
                except Exception:
                    pass
                log.info("[SIM] LIQUIDATION %s: %.6f @ $%.4f → PnL %+.2f", sym, result.qty, prices[sym], pnl)
            snap = _snapshot(cycle, cash, holdings, prices, history, total_fees, initial_total_value, initial_prices, cycle_sec)
            if on_cycle:
                on_cycle(cycle, snap)
            break

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
                    prices=prices, peak_prices=peak_prices,
                    cooldown_map=cooldown_map, total_fees=total_fees,
                    cycle=cycle,
                ),
                system=SYSTEM,
                config=cfg,
            )
        except Exception as exc:
            log.error("[SIM] Erreur LLM cycle %d: %s", cycle, exc)
            snap = _snapshot(cycle, cash, holdings, prices, history, total_fees, initial_total_value, initial_prices, cycle_sec)
            if on_cycle:
                on_cycle(cycle, snap)
            _save_state({"schema_version": 1, "budget": budget, "cycle": cycle, "cash": cash,
                         "holdings": holdings, "history": history, "total_fees": total_fees,
                         "initial_prices": initial_prices, "peak_prices": peak_prices,
                         "cooldown_map": cooldown_map, "recent_decisions": recent_decisions,
                         "initial_total_value": initial_total_value,
                         "session_id": session_id, "session_name": session_name})
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

        # Save market analysis to DB
        try:
            from db.store import save_market_analysis as _db_analysis
            _db_analysis(
                sentiment=sentiment,
                summary=summary,
                analyses=decision.get("actions", []),
                mode="simulation",
                cycle=cycle,
                session_id=session_id,
            )
        except Exception:
            pass

        # ── Execute paper trades ───────────────────────────────────────────────
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

                rsi         = market_raw.get(sym, {}).get("rsi14")
                horizon     = action.get("horizon", "").upper()
                full_reason = f"[{horizon}] {reason}" if horizon in ("SHORT", "MEDIUM", "LONG") else reason
                amount      = compute_position_size(action.get("usdc_amount", 0), cash, risk_level, rsi)

                if amount >= 10:
                    result = paper_buy(sym, amount, prices[sym], holdings)
                    total_fees += result.fee
                    cash       -= amount
                    peak_prices[sym] = prices[sym]
                    history.append({
                        "cycle":     cycle,
                        "timestamp": datetime.utcnow().isoformat(),
                        "action":    "BUY",
                        "symbol":    sym,
                        "amount":    amount,
                        "price":     prices[sym],
                        "fee":       round(result.fee, 6),
                        "reason":    full_reason,
                    })
                    try:
                        from db.store import save_trade as _db_save
                        _db_save(
                            action="BUY", symbol=sym, amount=amount, price=prices[sym],
                            reason=full_reason, fee=result.fee, qty=None, pnl=None,
                            mode="simulation", session_id=session_id, session_name=session_name,
                        )
                    except Exception:
                        pass
                    rsi_factor = max(0.5, min(1.5, 1.5 - (rsi - 20) / 60)) if rsi is not None else 1.0
                    log.info("[SIM] BUY  $%.2f %s @ $%.4f (RSI=%.0f ×%.2f) [%s]",
                             amount, sym, prices[sym], rsi or 0, rsi_factor, horizon or "?")

            elif atype == "sell" and sym in holdings and sym in prices:
                qty    = min(action.get("qty", holdings[sym]["qty"]), holdings[sym]["qty"])
                entry  = holdings[sym]["avg_price"]
                result = paper_sell(sym, qty, prices[sym], holdings)
                total_fees += result.fee
                cash       += result.received
                peak_prices.pop(sym, None)
                cooldown_map[sym] = cycle
                history.append({
                    "cycle":     cycle,
                    "timestamp": datetime.utcnow().isoformat(),
                    "action":    "SELL",
                    "symbol":    sym,
                    "qty":       result.qty,
                    "price":     prices[sym],
                    "pnl":       round((prices[sym] - entry) * result.qty - result.fee, 4),
                    "fee":       round(result.fee, 6),
                    "reason":    reason,
                })
                try:
                    from db.store import save_trade as _db_save
                    _db_save(
                        action="SELL", symbol=sym, amount=None, price=prices[sym],
                        reason=reason, fee=result.fee, qty=result.qty,
                        pnl=round((prices[sym] - entry) * result.qty - result.fee, 4),
                        mode="simulation", session_id=session_id, session_name=session_name,
                    )
                except Exception:
                    pass

        # ── Emit snapshot & persist state ─────────────────────────────────────
        snap = _snapshot(cycle, cash, holdings, prices, history, total_fees, initial_total_value, initial_prices, cycle_sec)
        if on_cycle:
            on_cycle(cycle, snap)

        _save_state({"schema_version": 1, "budget": budget, "cycle": cycle, "cash": cash,
                     "holdings": holdings, "history": history, "total_fees": total_fees,
                     "initial_prices": initial_prices, "peak_prices": peak_prices,
                     "cooldown_map": cooldown_map, "recent_decisions": recent_decisions,
                     "initial_total_value": initial_total_value,
                     "session_id": session_id, "session_name": session_name,
                     "running": True})

        if stop_event:
            stop_event.wait(timeout=cycle_sec)
        else:
            time.sleep(cycle_sec)

    # Mark simulation as stopped in persisted state
    _save_state({"schema_version": 1, "budget": budget, "cycle": cycle, "cash": cash,
                 "holdings": holdings, "history": history, "total_fees": total_fees,
                 "initial_prices": initial_prices, "peak_prices": peak_prices,
                 "cooldown_map": cooldown_map, "recent_decisions": recent_decisions,
                 "initial_total_value": initial_total_value,
                 "session_id": session_id, "session_name": session_name,
                 "running": False})

    if _db_handler is not None:
        logging.getLogger().removeHandler(_db_handler)

    return _snapshot(cycle, cash, holdings, prices, history, total_fees, initial_total_value, initial_prices, cycle_sec)
