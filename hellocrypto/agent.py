"""Autonomous trading agent — main loop.

LLM call gating
---------------
Claude/Gemini is only called when BOTH conditions are met:
1. ``llm_cooldown_seconds`` have elapsed since the last LLM call.
2. At least one watched asset has moved by ``price_change_threshold_pct`` or more.

Stop-loss and trailing stop are always evaluated every cycle.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from dotenv import load_dotenv

from .api import (
    compute_scores,
    format_market_data,
    get_balance,
    get_btc_dominance,
    get_enriched_market_data,
    get_fear_and_greed,
    get_open_positions,
    get_ticker,
    load_config,
    load_history,
    market_buy,
    market_sell,
    save_trade,
)
from .llm import call as llm_call
from .prompts import SYSTEM, build_analysis
from .trading import check_stops as _trading_check_stops, compute_position_size

load_dotenv()

Path("logs").mkdir(exist_ok=True)
Path("data").mkdir(exist_ok=True)

log = logging.getLogger(__name__)

# Keys persisted in DB between Cloud Run Job invocations
_STATE_KEY = "agent_real"


# ── Config helpers ───────────────────────────────────────────────────────────

def _load_cfg() -> dict:
    return load_config()


def _load_state() -> dict:
    try:
        from db.store import get_state
        return get_state(_STATE_KEY) or {}
    except ImportError:
        return {}


def _save_state(state: dict) -> None:
    try:
        from db.store import set_state
        set_state(_STATE_KEY, state)
    except ImportError:
        pass


# ── Shared helpers ───────────────────────────────────────────────────────────

def _fetch_market_data(watchlist: list[str], cycle_sec: int) -> dict:
    data = get_enriched_market_data(watchlist, cycle_seconds=cycle_sec)
    for sym in watchlist:
        if sym not in data:
            log.warning("Données indisponibles pour %s", sym)
    return data


def _prices_from_data(data: dict) -> dict:
    return {sym: d["price"] for sym, d in data.items()}


def _max_price_change(current: dict, reference: dict) -> float:
    if not reference:
        return 100.0
    changes = [
        abs(current[s] - reference[s]) / reference[s]
        for s in current if s in reference and reference[s] > 0
    ]
    return max(changes) if changes else 0.0


def _check_stops(positions: dict, prices: dict, peak_prices: dict,
                 stop_loss: float, trail_stop: float):
    """Return stop signals, resolving missing prices via live ticker."""
    enriched_prices = {
        sym: prices.get(sym) or get_ticker(sym)
        for sym in positions
    }
    return _trading_check_stops(positions, enriched_prices, peak_prices, stop_loss, trail_stop)


def _performance_report(prices: dict, positions: dict, cash: float,
                        initial_total_value: float) -> str:
    history = load_history()
    portfolio_val = sum(
        p["qty"] * prices.get(sym, p["avg_price"]) for sym, p in positions.items()
    )
    total = cash + portfolio_val
    pnl = total - initial_total_value
    total_fees = sum(t.get("fee", 0) for t in history)
    lines = [
        "═══ RAPPORT DE PERFORMANCE ═══",
        f"Valeur initiale: ${initial_total_value:.2f}",
        f"Valeur totale  : ${total:.2f}",
        f"Cash USDC      : ${cash:.2f}",
        f"Portefeuille   : ${portfolio_val:.2f}",
        f"PnL            : {pnl:+.2f} USDC ({pnl / initial_total_value * 100:+.2f}%)"
        if initial_total_value else "",
        f"Frais cumulés  : ${total_fees:.4f} USDC",
        f"Transactions   : {len(history)}",
        "───────────────────────────────",
    ]
    for sym, p in positions.items():
        cur = prices.get(sym, p["avg_price"])
        pnl_pos = (cur - p["avg_price"]) / p["avg_price"] * 100
        lines.append(f"  {sym}: {p['qty']:.6f} qty  (PnL {pnl_pos:+.2f}%)")
    return "\n".join(lines)


# ── Core cycle logic ─────────────────────────────────────────────────────────

def _execute_cycle(
    cfg: dict,
    cycle: int,
    last_llm_call: float,
    llm_call_count: int,
    ref_prices: dict,
    recent_decisions: list,
    peak_prices: dict,
    cooldown_map: dict,
    initial_total_value: float = 0.0,
) -> dict:
    """Execute one trading cycle (shared by run_one_cycle and run_agent).

    Returns an updated state dict with all mutable fields.
    ``initial_total_value`` is captured once on the first cycle from the real
    Binance portfolio (cash + all open positions) and persisted in state.
    """
    watchlist = cfg["watchlist"]
    budget = float(cfg["budget"])
    stop_loss = float(cfg["stop_loss_pct"]) / 100
    trail_stop = float(cfg.get("trailing_stop_pct", 5)) / 100
    cycle_sec = int(cfg["cycle_seconds"])
    llm_cooldown = int(cfg.get("llm_cooldown_seconds", 300))
    price_threshold = float(cfg.get("price_change_threshold_pct", 0.5)) / 100
    risk_level = int(cfg.get("risk_level", 3))
    sell_cooldown_cyc = int(cfg.get("sell_cooldown_cycles", 3))

    positions = get_open_positions(watchlist)
    cash = get_balance("USDC")
    market_data_raw = _fetch_market_data(watchlist, cycle_sec)
    prices = _prices_from_data(market_data_raw)

    # ── Capture initial portfolio value on first cycle ────────────────────
    if initial_total_value == 0.0:
        portfolio_val = sum(
            p["qty"] * prices.get(sym, p["avg_price"]) for sym, p in positions.items()
        )
        initial_total_value = cash + portfolio_val
        log.info("Valeur initiale du run: $%.2f (USDC) + $%.2f (crypto) = $%.2f",
                 cash, portfolio_val, initial_total_value)

    log.info("Cash: $%.2f USDC | Positions: %s", cash, list(positions.keys()))

    # ── Update peak prices ────────────────────────────────────────────────
    for sym in positions:
        if sym in prices:
            peak_prices[sym] = max(peak_prices.get(sym, prices[sym]), prices[sym])

    # ── Stop-loss + trailing stop ─────────────────────────────────────────
    for sym, qty, price, reason_tag, _ in _check_stops(positions, prices, peak_prices, stop_loss, trail_stop):
        _, fee, fee_asset = market_sell(sym, qty)
        save_trade(
            f"SELL ({reason_tag})", sym, qty, price,
            f"{reason_tag.replace('-', ' ').title()} déclenché", fee, fee_asset,
        )
        peak_prices.pop(sym, None)
        cooldown_map[sym] = cycle
        del positions[sym]

    # ── LLM gating ────────────────────────────────────────────────────────
    now = time.time()
    cooldown_ok = (now - last_llm_call) >= llm_cooldown
    delta = _max_price_change(prices, ref_prices)
    price_change_ok = delta >= price_threshold

    if not cooldown_ok:
        log.info("Skip LLM — cooldown: %.0fs restants", max(0, llm_cooldown - (now - last_llm_call)))
    elif not price_change_ok:
        log.info("Skip LLM — Δmax %.2f%% < seuil %.1f%%", delta * 100, price_threshold * 100)
    else:
        fear_greed = get_fear_and_greed()
        btc_dominance = get_btc_dominance()
        scores = compute_scores(market_data_raw)
        market_data = format_market_data(market_data_raw, watchlist)

        decision = llm_call(
            prompt=build_analysis(
                market_data, positions, cash, budget, risk_level,
                recent_decisions, fear_greed, btc_dominance, scores,
                prices=prices, peak_prices=peak_prices,
                cooldown_map=cooldown_map, cycle=cycle,
            ),
            system=SYSTEM, config=cfg,
        )

        last_llm_call = time.time()
        ref_prices = dict(prices)
        llm_call_count += 1
        recent_decisions = (recent_decisions + [decision])[-3:]

        log.info("LLM #%d | Sentiment: %s | %s",
                 llm_call_count, decision["market_sentiment"], decision["summary"])

        try:
            from db.store import save_market_analysis as _db_analysis
            _db_analysis(
                sentiment=decision.get("market_sentiment", ""),
                summary=decision.get("summary", ""),
                analyses=decision.get("actions", []),
                mode="real",
                cycle=cycle,
            )
        except Exception:
            pass

        for action in decision.get("actions", []):
            atype = action.get("type", "")
            sym = action.get("symbol", "")
            if not atype or not sym:
                continue
            reason = action.get("reason", "")
            horizon = action.get("horizon", "").upper() if atype == "buy" else ""
            if horizon in ("SHORT", "MEDIUM", "LONG"):
                reason = f"[{horizon}] {reason}"

            if atype == "buy" and cash > 10:
                # Only apply cooldown if the symbol was actually sold before
                # (without this guard, .get(sym, 0) treats never-sold symbols
                # as sold at cycle 0 and blocks the first sell_cooldown_cyc
                # cycles of any fresh start).
                if sym in cooldown_map:
                    last_sell = cooldown_map[sym]
                    if cycle - last_sell < sell_cooldown_cyc:
                        log.info("COOLDOWN %s — %d cycles restants",
                                 sym, sell_cooldown_cyc - (cycle - last_sell))
                        continue

                rsi = market_data_raw.get(sym, {}).get("rsi14")
                amount = compute_position_size(action.get("usdc_amount", 0), cash, risk_level, rsi)
                if amount >= 10:
                    _, fee, fee_asset = market_buy(sym, amount)
                    price = prices.get(sym) or get_ticker(sym)
                    save_trade("BUY", sym, amount, price, reason, fee, fee_asset)
                    peak_prices[sym] = price
                    cash -= amount
                    log.info("BUY  $%.2f %s @ $%.4f (RSI=%.0f) [%s]",
                             amount, sym, price, rsi or 0, horizon or "?")

            elif atype == "sell" and sym in positions:
                qty = action.get("qty", positions[sym]["qty"])
                price = prices.get(sym) or get_ticker(sym)
                _, fee, fee_asset = market_sell(sym, qty)
                save_trade("SELL", sym, qty, price, reason, fee, fee_asset)
                peak_prices.pop(sym, None)
                cooldown_map[sym] = cycle
                log.info("SELL %.6f %s @ $%.4f", qty, sym, price)

            else:
                log.info("HOLD %s — %s", sym, reason)

    if cycle % 10 == 0:
        report = _performance_report(prices, positions, cash, initial_total_value)
        log.info("\n%s", report)

    return {
        "cycle":               cycle,
        "last_llm_call":       last_llm_call,
        "llm_call_count":      llm_call_count,
        "ref_prices":          ref_prices,
        "recent_decisions":    recent_decisions,
        "peak_prices":         peak_prices,
        "cooldown_map":        cooldown_map,
        "initial_total_value": initial_total_value,
    }


# ── Entry points ─────────────────────────────────────────────────────────────

def run_one_cycle() -> None:
    """Execute a single trading cycle — designed for Cloud Run Jobs.

    State (peak_prices, cooldown_map, etc.) is persisted in Firestore/SQLite
    so it survives across invocations triggered by Cloud Scheduler.
    """
    cfg = _load_cfg()
    state = _load_state()
    cycle = state.get("cycle", 0) + 1

    # Attach DB log handler for this cycle
    _db_handler = None
    try:
        from db.store import DBLogHandler
        _db_handler = DBLogHandler(mode="real")
        _db_handler.set_cycle(cycle)
        logging.getLogger().addHandler(_db_handler)
    except ImportError:
        pass

    provider = cfg.get("llm", {}).get("provider", "claude")
    model = cfg.get("llm", {}).get("model", "—")
    risk_level = int(cfg.get("risk_level", 3))
    budget = float(cfg["budget"])
    log.info("Cycle #%d | Budget: $%.0f USDC | LLM: %s/%s | Risque: %d/10",
             cycle, budget, provider, model, risk_level)

    try:
        new_state = _execute_cycle(
            cfg=cfg,
            cycle=cycle,
            last_llm_call=state.get("last_llm_call", 0.0),
            llm_call_count=state.get("llm_call_count", 0),
            ref_prices=state.get("ref_prices", {}),
            recent_decisions=state.get("recent_decisions", []),
            peak_prices=state.get("peak_prices", {}),
            cooldown_map=state.get("cooldown_map", {}),
            initial_total_value=state.get("initial_total_value", 0.0),
        )
        _save_state(new_state)
    except Exception as exc:
        log.error("Erreur cycle #%d: %s", cycle, exc, exc_info=True)
        _save_state({**state, "cycle": cycle})
    finally:
        if _db_handler is not None:
            logging.getLogger().removeHandler(_db_handler)


def run_agent() -> None:
    """Continuous trading loop — designed for VM / local execution."""
    cfg = _load_cfg()
    provider = cfg.get("llm", {}).get("provider", "claude")
    model = cfg.get("llm", {}).get("model", "—")
    budget = float(cfg["budget"])
    stop_loss = float(cfg["stop_loss_pct"]) / 100
    trail_stop = float(cfg.get("trailing_stop_pct", 5)) / 100
    cycle_sec = int(cfg["cycle_seconds"])
    llm_cooldown = int(cfg.get("llm_cooldown_seconds", 300))
    risk_level = int(cfg.get("risk_level", 3))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler("logs/agent.log")],
    )

    log.info(
        "Agent démarré | Budget: $%.0f USDC | Stop-loss: %.0f%% "
        "| Trailing: %.0f%% | LLM: %s/%s "
        "| cooldown: %ds | Risque: %d/10",
        budget, stop_loss * 100, trail_stop * 100,
        provider, model, llm_cooldown, risk_level,
    )

    _db_handler = None
    try:
        from db.store import DBLogHandler as _DBH
        _db_handler = _DBH(mode="real")
        logging.getLogger().addHandler(_db_handler)
    except ImportError:
        pass

    state: dict = {
        "cycle":               0,
        "last_llm_call":       0.0,
        "llm_call_count":      0,
        "ref_prices":          {},
        "recent_decisions":    [],
        "peak_prices":         {},
        "cooldown_map":        {},
        "initial_total_value": 0.0,
    }

    while True:
        state["cycle"] += 1
        cycle = state["cycle"]
        if _db_handler is not None:
            _db_handler.set_cycle(cycle)
        log.info("═══ Cycle #%d ═══", cycle)

        try:
            state = _execute_cycle(
                cfg=cfg,
                cycle=cycle,
                last_llm_call=state["last_llm_call"],
                llm_call_count=state["llm_call_count"],
                ref_prices=state["ref_prices"],
                recent_decisions=state["recent_decisions"],
                peak_prices=state["peak_prices"],
                cooldown_map=state["cooldown_map"],
                initial_total_value=state["initial_total_value"],
            )
        except Exception as exc:
            log.error("Erreur cycle #%d: %s", cycle, exc, exc_info=True)

        time.sleep(cycle_sec)


if __name__ == "__main__":
    run_agent()
