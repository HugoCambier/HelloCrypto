"""Autonomous trading agent — main loop.

LLM call gating
---------------
To avoid over-consuming API credits, Claude/Gemini is only called when
BOTH conditions are met:

1. ``llm_cooldown_seconds`` have elapsed since the last LLM call.
2. At least one watched asset has moved by ``price_change_threshold_pct``
   or more since the last LLM call.

Stop-loss is always evaluated every ``cycle_seconds`` regardless.
"""

import logging
import time
from pathlib import Path

from dotenv import load_dotenv

from .api import (
    format_market_data,
    get_balance,
    get_enriched_market_data,
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

# ── Config ────────────────────────────────────────────────────────────────────
_cfg       = load_config()
BUDGET     = float(_cfg["budget"])
STOP_LOSS  = float(_cfg["stop_loss_pct"]) / 100
CYCLE_SEC  = int(_cfg["cycle_seconds"])
WATCHLIST  = _cfg["watchlist"]
LLM_COOLDOWN    = int(_cfg.get("llm_cooldown_seconds", 300))
PRICE_THRESHOLD = float(_cfg.get("price_change_threshold_pct", 0.5)) / 100
RISK_LEVEL      = int(_cfg.get("risk_level", 3))

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/agent.log"),
    ],
)
log = logging.getLogger(__name__)


# ── Market data ───────────────────────────────────────────────────────────────

def _fetch_market_data() -> dict[str, dict]:
    """Fetch enriched market data (price + indicators) for all watched symbols."""
    data = get_enriched_market_data(WATCHLIST)
    for sym in WATCHLIST:
        if sym not in data:
            log.warning(f"Données indisponibles pour {sym}")
    return data


def _prices_from_data(data: dict[str, dict]) -> dict[str, float]:
    return {sym: d["price"] for sym, d in data.items()}


def _max_price_change(current: dict[str, float], reference: dict[str, float]) -> float:
    """Return the max absolute % change between *current* and *reference* prices.

    Returns 100.0 when no reference exists (first cycle → always trigger).
    """
    if not reference:
        return 100.0
    changes = [
        abs(current[s] - reference[s]) / reference[s]
        for s in current
        if s in reference and reference[s] > 0
    ]
    return max(changes) if changes else 0.0


# ── Trading helpers ───────────────────────────────────────────────────────────

def _check_stop_loss(positions: dict, prices: dict[str, float]) -> list[tuple]:
    triggered = []
    for sym, pos in positions.items():
        cur  = prices.get(sym) or get_ticker(sym)
        loss = (cur - pos["avg_price"]) / pos["avg_price"]
        if loss < -STOP_LOSS:
            log.warning(f"STOP-LOSS {sym}: {loss*100:.1f}%")
            triggered.append((sym, pos["qty"], cur))
    return triggered


def _ai_decide(market_data: str, positions: dict, cash: float, recent_decisions: list) -> dict:
    return llm_call(
        prompt=build_analysis(market_data, positions, cash, BUDGET, RISK_LEVEL, recent_decisions),
        system=SYSTEM,
        config=_cfg,
    )


def _performance_report(prices: dict[str, float], positions: dict, cash: float) -> str:
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


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_agent() -> None:
    provider = _cfg.get("llm", {}).get("provider", "claude")
    model    = _cfg.get("llm", {}).get("model", "—")
    log.info(
        f"Agent démarré | Budget: ${BUDGET} USDC | Stop-loss: {STOP_LOSS*100:.0f}% "
        f"| LLM: {provider}/{model} | cooldown: {LLM_COOLDOWN}s | seuil Δ: {PRICE_THRESHOLD*100:.1f}% "
        f"| Risque: {RISK_LEVEL}/10"
    )

    cycle              = 0
    last_llm_call      = 0.0
    llm_call_count     = 0
    ref_prices: dict[str, float] = {}
    recent_decisions: list = []   # last 3 LLM decisions for prompt context

    while True:
        cycle += 1
        log.info(f"═══ Cycle #{cycle} ═══")

        try:
            positions   = get_open_positions(WATCHLIST)
            cash        = get_balance("USDC")
            market_data_raw = _fetch_market_data()
            prices      = _prices_from_data(market_data_raw)

            log.info(f"Cash: ${cash:.2f} USDC | Positions: {list(positions.keys())}")

            # ── Stop-loss (always) ────────────────────────────────────────────
            for sym, qty, price in _check_stop_loss(positions, prices):
                _, fee, fee_asset = market_sell(sym, qty)
                save_trade(
                    "SELL (stop-loss)", sym, qty, price,
                    f"Stop-loss {STOP_LOSS*100:.0f}% déclenché", fee, fee_asset,
                )
                del positions[sym]

            # ── LLM gating ────────────────────────────────────────────────────
            now              = time.time()
            cooldown_ok      = (now - last_llm_call) >= LLM_COOLDOWN
            delta            = _max_price_change(prices, ref_prices)
            price_change_ok  = delta >= PRICE_THRESHOLD
            next_call_in     = max(0, LLM_COOLDOWN - (now - last_llm_call))

            if not cooldown_ok:
                log.info(f"Skip LLM — cooldown: {next_call_in:.0f}s restants")
            elif not price_change_ok:
                log.info(f"Skip LLM — Δmax {delta*100:.2f}% < seuil {PRICE_THRESHOLD*100:.1f}%")
            else:
                # ── AI decision ───────────────────────────────────────────────
                market_data = format_market_data(market_data_raw, WATCHLIST)
                decision    = _ai_decide(market_data, positions, cash, recent_decisions)

                last_llm_call  = time.time()
                ref_prices     = dict(prices)
                llm_call_count += 1
                recent_decisions = (recent_decisions + [decision])[-3:]

                log.info(
                    f"LLM #{llm_call_count} | Sentiment: {decision['market_sentiment']}"
                    f" | {decision['summary']}"
                )

                for action in decision.get("actions", []):
                    atype  = action["type"]
                    sym    = action["symbol"]
                    reason = action.get("reason", "")

                    if atype == "buy" and cash > 10:
                        max_pct = (5 + RISK_LEVEL * 4) / 100
                        amount = min(action.get("usdc_amount", 0), cash * max_pct)
                        if amount >= 10:
                            _, fee, fee_asset = market_buy(sym, amount)
                            price = prices.get(sym) or get_ticker(sym)
                            save_trade("BUY", sym, amount, price, reason, fee, fee_asset)
                            cash -= amount
                            log.info(f"BUY  ${amount:.2f} {sym} @ ${price:.4f} — fee {fee:.4f} {fee_asset} — {reason}")

                    elif atype == "sell" and sym in positions:
                        qty   = action.get("qty", positions[sym]["qty"])
                        price = prices.get(sym) or get_ticker(sym)
                        _, fee, fee_asset = market_sell(sym, qty)
                        save_trade("SELL", sym, qty, price, reason, fee, fee_asset)
                        log.info(f"SELL {qty:.6f} {sym} @ ${price:.4f} — fee {fee:.4f} {fee_asset} — {reason}")

                    else:
                        log.info(f"HOLD {sym} — {reason}")

            if cycle % 10 == 0:
                log.info("\n" + _performance_report(prices, positions, cash))

        except Exception as exc:
            log.error(f"Erreur cycle #{cycle}: {exc}", exc_info=True)

        time.sleep(CYCLE_SEC)


if __name__ == "__main__":
    run_agent()
