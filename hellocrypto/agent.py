"""Autonomous trading agent — main loop.

LLM call gating
---------------
Claude/Gemini is only called when BOTH conditions are met:
1. ``llm_cooldown_seconds`` have elapsed since the last LLM call.
2. At least one watched asset has moved by ``price_change_threshold_pct`` or more.

Stop-loss and trailing stop are always evaluated every cycle.
"""

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
load_dotenv()

Path("logs").mkdir(exist_ok=True)
Path("data").mkdir(exist_ok=True)

_cfg              = load_config()
BUDGET            = float(_cfg["budget"])
STOP_LOSS         = float(_cfg["stop_loss_pct"]) / 100
TRAIL_STOP        = float(_cfg.get("trailing_stop_pct", 5)) / 100
CYCLE_SEC         = int(_cfg["cycle_seconds"])
WATCHLIST         = _cfg["watchlist"]
LLM_COOLDOWN      = int(_cfg.get("llm_cooldown_seconds", 300))
PRICE_THRESHOLD   = float(_cfg.get("price_change_threshold_pct", 0.5)) / 100
RISK_LEVEL        = int(_cfg.get("risk_level", 3))
SELL_COOLDOWN_CYC = int(_cfg.get("sell_cooldown_cycles", 3))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("logs/agent.log")],
)
log = logging.getLogger(__name__)

_stop_requested: bool = False

# Keys persisted in DB between Cloud Run Job invocations
_STATE_KEY = "agent_real"


def _load_state() -> dict:
    try:
        from db.store import get_state
        saved = get_state(_STATE_KEY)
        if saved:
            return saved
    except ImportError:
        pass
    return {}


def _save_state(state: dict) -> None:
    try:
        from db.store import set_state
        set_state(_STATE_KEY, state)
        return
    except ImportError:
        pass


def _fetch_market_data() -> dict:
    data = get_enriched_market_data(WATCHLIST, cycle_seconds=CYCLE_SEC)
    for sym in WATCHLIST:
        if sym not in data:
            log.warning(f"Données indisponibles pour {sym}")
    return data


def _prices_from_data(data: dict) -> dict:
    return {sym: d["price"] for sym, d in data.items()}


def _max_price_change(current: dict, reference: dict) -> float:
    if not reference:
        return 100.0
    changes = [abs(current[s] - reference[s]) / reference[s]
               for s in current if s in reference and reference[s] > 0]
    return max(changes) if changes else 0.0


def _check_stops(positions: dict, prices: dict, peak_prices: dict) -> list[tuple]:
    """Return list of (sym, qty, price, reason) for positions that triggered a stop."""
    triggered = []
    for sym, pos in positions.items():
        cur   = prices.get(sym) or get_ticker(sym)
        entry = pos["avg_price"]
        peak  = peak_prices.get(sym, entry)

        hard_loss  = (cur - entry) / entry
        trail_loss = (cur - peak)  / peak

        if hard_loss < -STOP_LOSS:
            log.warning(f"STOP-LOSS {sym}: {hard_loss*100:.1f}%")
            triggered.append((sym, pos["qty"], cur, "stop-loss", hard_loss))
        elif trail_loss < -TRAIL_STOP and peak > entry and cur >= entry:
            log.warning(f"TRAILING STOP {sym}: chute {trail_loss*100:.1f}% depuis pic ${peak:,.4f}")
            triggered.append((sym, pos["qty"], cur, "trailing-stop", trail_loss))
    return triggered


def _performance_report(prices: dict, positions: dict, cash: float) -> str:
    history       = load_history()
    portfolio_val = sum(p["qty"] * prices.get(sym, p["avg_price"]) for sym, p in positions.items())
    total         = cash + portfolio_val
    pnl           = total - BUDGET
    total_fees    = sum(t.get("fee", 0) for t in history)
    lines = [
        "═══ RAPPORT DE PERFORMANCE ═══",
        f"Budget initial : ${BUDGET:.2f}",
        f"Valeur totale  : ${total:.2f}",
        f"Cash USDC      : ${cash:.2f}",
        f"Portefeuille   : ${portfolio_val:.2f}",
        f"PnL            : {pnl:+.2f} USDC ({pnl / BUDGET * 100:+.2f}%)",
        f"Frais cumulés  : ${total_fees:.4f} USDC",
        f"Transactions   : {len(history)}",
        "───────────────────────────────",
    ]
    for sym, p in positions.items():
        cur     = prices.get(sym, p["avg_price"])
        pnl_pos = (cur - p["avg_price"]) / p["avg_price"] * 100
        lines.append(f"  {sym}: {p['qty']:.6f} qty  (PnL {pnl_pos:+.2f}%)")
    return "\n".join(lines)


def run_one_cycle() -> None:
    """Execute a single trading cycle — designed for Cloud Run Jobs.

    State (peak_prices, cooldown_map, etc.) is persisted in Firestore/SQLite
    so it survives across invocations triggered by Cloud Scheduler.
    """
    state          = _load_state()
    cycle          = state.get("cycle", 0) + 1
    last_llm_call  = state.get("last_llm_call", 0.0)

    # Attach DB log handler for this cycle
    _db_handler: "DBLogHandler | None" = None
    try:
        from db.store import DBLogHandler
        _db_handler = DBLogHandler(mode="real")
        _db_handler.set_cycle(cycle)
        logging.getLogger().addHandler(_db_handler)
    except ImportError:
        pass
    llm_call_count = state.get("llm_call_count", 0)
    ref_prices     = state.get("ref_prices", {})
    recent_decisions = state.get("recent_decisions", [])
    peak_prices    = state.get("peak_prices", {})
    cooldown_map   = state.get("cooldown_map", {})

    provider = _cfg.get("llm", {}).get("provider", "claude")
    model    = _cfg.get("llm", {}).get("model", "—")
    log.info(
        f"Cycle #{cycle} | Budget: ${BUDGET} USDC | LLM: {provider}/{model} | Risque: {RISK_LEVEL}/10"
    )

    try:
        positions       = get_open_positions(WATCHLIST)
        cash            = get_balance("USDC")
        market_data_raw = _fetch_market_data()
        prices          = _prices_from_data(market_data_raw)

        log.info(f"Cash: ${cash:.2f} USDC | Positions: {list(positions.keys())}")

        for sym in positions:
            if sym in prices:
                peak_prices[sym] = max(peak_prices.get(sym, prices[sym]), prices[sym])

        for sym, qty, price, reason_tag, _ in _check_stops(positions, prices, peak_prices):
            _, fee, fee_asset = market_sell(sym, qty)
            save_trade(
                f"SELL ({reason_tag})", sym, qty, price,
                f"{reason_tag.replace('-', ' ').title()} déclenché", fee, fee_asset,
            )
            peak_prices.pop(sym, None)
            cooldown_map[sym] = cycle
            del positions[sym]

        now             = time.time()
        cooldown_ok     = (now - last_llm_call) >= LLM_COOLDOWN
        delta           = _max_price_change(prices, ref_prices)
        price_change_ok = delta >= PRICE_THRESHOLD

        if not cooldown_ok:
            log.info(f"Skip LLM — cooldown: {max(0, LLM_COOLDOWN - (now - last_llm_call)):.0f}s restants")
        elif not price_change_ok:
            log.info(f"Skip LLM — Δmax {delta*100:.2f}% < seuil {PRICE_THRESHOLD*100:.1f}%")
        else:
            fear_greed    = get_fear_and_greed()
            btc_dominance = get_btc_dominance()
            scores        = compute_scores(market_data_raw)
            market_data   = format_market_data(market_data_raw, WATCHLIST)

            decision = llm_call(
                prompt=build_analysis(
                    market_data, positions, cash, BUDGET, RISK_LEVEL,
                    recent_decisions, fear_greed, btc_dominance, scores,
                ),
                system=SYSTEM, config=_cfg,
            )

            last_llm_call    = time.time()
            ref_prices       = dict(prices)
            llm_call_count  += 1
            recent_decisions = (recent_decisions + [decision])[-3:]

            log.info(f"LLM #{llm_call_count} | Sentiment: {decision['market_sentiment']} | {decision['summary']}")

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
                atype  = action.get("type", "")
                sym    = action.get("symbol", "")
                if not atype or not sym:
                    continue
                reason  = action.get("reason", "")
                horizon = action.get("horizon", "").upper() if atype == "buy" else ""
                if horizon in ("SHORT", "MEDIUM", "LONG"):
                    reason = f"[{horizon}] {reason}"

                if atype == "buy" and cash > 10:
                    last_sell = cooldown_map.get(sym, 0)
                    if cycle - last_sell < SELL_COOLDOWN_CYC:
                        log.info(f"COOLDOWN {sym} — {SELL_COOLDOWN_CYC - (cycle - last_sell)} cycles restants")
                        continue

                    max_pct    = (5 + RISK_LEVEL * 4) / 100
                    rsi        = market_data_raw.get(sym, {}).get("rsi14")
                    rsi_factor = max(0.5, min(1.5, 1.5 - (rsi - 20) / 60)) if rsi is not None else 1.0
                    amount     = min(action.get("usdc_amount", 0), cash * max_pct * rsi_factor)
                    if amount >= 10:
                        _, fee, fee_asset = market_buy(sym, amount)
                        price = prices.get(sym) or get_ticker(sym)
                        save_trade("BUY", sym, amount, price, reason, fee, fee_asset)
                        peak_prices[sym] = price
                        cash -= amount
                        log.info(f"BUY  ${amount:.2f} {sym} @ ${price:.4f} (RSI={rsi or 0:.0f} ×{rsi_factor:.2f}) [{horizon or '?'}]")

                elif atype == "sell" and sym in positions:
                    qty   = action.get("qty", positions[sym]["qty"])
                    price = prices.get(sym) or get_ticker(sym)
                    _, fee, fee_asset = market_sell(sym, qty)
                    save_trade("SELL", sym, qty, price, reason, fee, fee_asset)
                    peak_prices.pop(sym, None)
                    cooldown_map[sym] = cycle
                    log.info(f"SELL {qty:.6f} {sym} @ ${price:.4f}")

                else:
                    log.info(f"HOLD {sym} — {reason}")

        if cycle % 10 == 0:
            report = _performance_report(prices, positions, cash)
            log.info("\n" + report)

    except Exception as exc:
        log.error(f"Erreur cycle #{cycle}: {exc}", exc_info=True)
    finally:
        if _db_handler is not None:
            logging.getLogger().removeHandler(_db_handler)
        _save_state({
            "cycle": cycle,
            "last_llm_call": last_llm_call,
            "llm_call_count": llm_call_count,
            "ref_prices": ref_prices,
            "recent_decisions": recent_decisions,
            "peak_prices": peak_prices,
            "cooldown_map": cooldown_map,
        })


def run_agent() -> None:
    provider = _cfg.get("llm", {}).get("provider", "claude")
    model    = _cfg.get("llm", {}).get("model", "—")
    log.info(
        f"Agent démarré | Budget: ${BUDGET} USDC | Stop-loss: {STOP_LOSS*100:.0f}% "
        f"| Trailing: {TRAIL_STOP*100:.0f}% | LLM: {provider}/{model} "
        f"| cooldown: {LLM_COOLDOWN}s | Risque: {RISK_LEVEL}/10"
    )

    cycle              = 0
    last_llm_call      = 0.0
    llm_call_count     = 0
    ref_prices: dict   = {}
    recent_decisions   = []
    peak_prices: dict  = {}   # sym → highest price seen since entry
    cooldown_map: dict = {}   # sym → last sell cycle

    try:
        from db.store import DBLogHandler as _DBH
        _db_handler = _DBH(mode="real")
        logging.getLogger().addHandler(_db_handler)
    except ImportError:
        _db_handler = None

    while not _stop_requested:
        cycle += 1
        if _db_handler is not None:
            _db_handler.set_cycle(cycle)
        log.info(f"═══ Cycle #{cycle} ═══")
        try:
            positions       = get_open_positions(WATCHLIST)
            cash            = get_balance("USDC")
            market_data_raw = _fetch_market_data()
            prices          = _prices_from_data(market_data_raw)

            log.info(f"Cash: ${cash:.2f} USDC | Positions: {list(positions.keys())}")

            # ── Update peak prices ─────────────────────────────────────────────
            for sym in positions:
                if sym in prices:
                    peak_prices[sym] = max(peak_prices.get(sym, prices[sym]), prices[sym])

            # ── Stop-loss + trailing stop ──────────────────────────────────────
            for sym, qty, price, reason_tag, _ in _check_stops(positions, prices, peak_prices):
                _, fee, fee_asset = market_sell(sym, qty)
                save_trade(
                    f"SELL ({reason_tag})", sym, qty, price,
                    f"{reason_tag.replace('-', ' ').title()} déclenché", fee, fee_asset,
                )
                peak_prices.pop(sym, None)
                cooldown_map[sym] = cycle
                del positions[sym]

            # ── LLM gating ────────────────────────────────────────────────────
            now             = time.time()
            cooldown_ok     = (now - last_llm_call) >= LLM_COOLDOWN
            delta           = _max_price_change(prices, ref_prices)
            price_change_ok = delta >= PRICE_THRESHOLD

            if not cooldown_ok:
                log.info(f"Skip LLM — cooldown: {max(0, LLM_COOLDOWN - (now - last_llm_call)):.0f}s restants")
            elif not price_change_ok:
                log.info(f"Skip LLM — Δmax {delta*100:.2f}% < seuil {PRICE_THRESHOLD*100:.1f}%")
            else:
                fear_greed    = get_fear_and_greed()
                btc_dominance = get_btc_dominance()
                scores        = compute_scores(market_data_raw)
                market_data   = format_market_data(market_data_raw, WATCHLIST)

                decision = llm_call(
                    prompt=build_analysis(
                        market_data, positions, cash, BUDGET, RISK_LEVEL,
                        recent_decisions, fear_greed, btc_dominance, scores,
                    ),
                    system=SYSTEM, config=_cfg,
                )

                last_llm_call    = time.time()
                ref_prices       = dict(prices)
                llm_call_count  += 1
                recent_decisions = (recent_decisions + [decision])[-3:]

                log.info(f"LLM #{llm_call_count} | Sentiment: {decision['market_sentiment']} | {decision['summary']}")

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
                    atype  = action.get("type", "")
                    sym    = action.get("symbol", "")
                    if not atype or not sym:
                        continue
                    reason  = action.get("reason", "")
                    horizon = action.get("horizon", "").upper() if atype == "buy" else ""
                    if horizon in ("SHORT", "MEDIUM", "LONG"):
                        reason = f"[{horizon}] {reason}"

                    if atype == "buy" and cash > 10:
                        # Cooldown check
                        last_sell = cooldown_map.get(sym, 0)
                        if cycle - last_sell < SELL_COOLDOWN_CYC:
                            log.info(f"COOLDOWN {sym} — {SELL_COOLDOWN_CYC - (cycle - last_sell)} cycles restants")
                            continue

                        max_pct = (5 + RISK_LEVEL * 4) / 100
                        rsi     = market_data_raw.get(sym, {}).get("rsi14")
                        rsi_factor = max(0.5, min(1.5, 1.5 - (rsi - 20) / 60)) if rsi is not None else 1.0
                        amount  = min(action.get("usdc_amount", 0), cash * max_pct * rsi_factor)
                        if amount >= 10:
                            _, fee, fee_asset = market_buy(sym, amount)
                            price = prices.get(sym) or get_ticker(sym)
                            save_trade("BUY", sym, amount, price, reason, fee, fee_asset)
                            peak_prices[sym] = price
                            cash -= amount
                            log.info(f"BUY  ${amount:.2f} {sym} @ ${price:.4f} (RSI={rsi or 0:.0f} ×{rsi_factor:.2f}) [{horizon or '?'}]")

                    elif atype == "sell" and sym in positions:
                        qty   = action.get("qty", positions[sym]["qty"])
                        price = prices.get(sym) or get_ticker(sym)
                        _, fee, fee_asset = market_sell(sym, qty)
                        save_trade("SELL", sym, qty, price, reason, fee, fee_asset)
                        peak_prices.pop(sym, None)
                        cooldown_map[sym] = cycle
                        log.info(f"SELL {qty:.6f} {sym} @ ${price:.4f}")

                    else:
                        log.info(f"HOLD {sym} — {reason}")

            if cycle % 10 == 0:
                report = _performance_report(prices, positions, cash)
                log.info("\n" + report)
        except Exception as exc:
            log.error(f"Erreur cycle #{cycle}: {exc}", exc_info=True)

        time.sleep(CYCLE_SEC)


if __name__ == "__main__":
    run_agent()
