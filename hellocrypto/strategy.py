"""Shared decision/execution primitives used by simulation, eval and agent.

The goal of this module is to keep the trading logic in *one* place: peak
tracking, cooldown gating, stop-loss application, and action execution all
live here. Both the paper-trading simulator and the live agent call into
these helpers — when we want to change *how* a buy is sized or *when* a
stop fires, we change it once.

Side-effecting helpers mutate the dicts they receive (holdings, peak_prices,
cooldown_map) because that matches how both callers already use them.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from .trading import check_stops, compute_position_size, paper_buy, paper_sell

log = logging.getLogger(__name__)


# ── Small pure helpers ────────────────────────────────────────────────────────

def update_peak_prices(holdings: dict, prices: dict, peak_prices: dict) -> dict:
    """Bump each open position's recorded peak if the current price is higher."""
    for sym in holdings:
        if sym in prices:
            peak_prices[sym] = max(peak_prices.get(sym, prices[sym]), prices[sym])
    return peak_prices


def in_cooldown(symbol: str, cycle: int, cooldown_map: dict, max_cycles: int) -> bool:
    """True iff `symbol` was sold within the last `max_cycles` cycles.

    Notably returns False when the symbol has never been sold — fixes the
    fresh-start cooldown bug where ``.get(sym, 0)`` blocked every buy of a
    never-sold symbol during the first ``max_cycles`` cycles.
    """
    if symbol not in cooldown_map:
        return False
    return (cycle - cooldown_map[symbol]) < max_cycles


def format_buy_reason(action: dict) -> str:
    """Prefix reason with horizon tag if action carries one."""
    horizon = (action.get("horizon") or "").upper()
    reason  = action.get("reason", "")
    return f"[{horizon}] {reason}" if horizon in ("SHORT", "MEDIUM", "LONG") else reason


# ── Paper trading: stops + actions ───────────────────────────────────────────

@dataclass
class PaperTrade:
    """Outcome of a paper-trade execution (used to build history records)."""
    cycle:     int
    action:    str          # "BUY" | "SELL" | "SELL (stop-loss)" | ...
    symbol:    str
    qty:       float
    price:     float
    fee:       float
    amount:    float | None = None
    pnl:       float | None = None
    reason:    str          = ""
    horizon:   str | None   = None
    confidence: float | None = None

    def to_history(self) -> dict:
        d = {
            "cycle":     self.cycle,
            "timestamp": datetime.utcnow().isoformat(),
            "action":    self.action,
            "symbol":    self.symbol,
            "qty":       round(self.qty, 6),
            "price":     self.price,
            "fee":       round(self.fee, 6),
            "reason":    self.reason,
        }
        if self.amount is not None:
            d["amount"] = self.amount
        if self.pnl is not None:
            d["pnl"] = round(self.pnl, 4)
        if self.confidence is not None:
            d["confidence"] = self.confidence
        return d


def apply_paper_stops(
    holdings: dict,
    prices: dict,
    peak_prices: dict,
    cooldown_map: dict,
    stop_loss: float,
    trail_stop: float,
    cycle: int,
    market_raw: dict | None = None,
) -> tuple[float, float, list[PaperTrade]]:
    """Sell every position that breaches a stop. Mutates `holdings`, `peak_prices`,
    `cooldown_map`.

    ``market_raw`` enables ATR-adaptive trailing if provided. Returns
    (cash_received_total, fees_total, executed_trades).
    """
    cash_recv = 0.0
    fees      = 0.0
    trades: list[PaperTrade] = []
    for sig in check_stops(holdings, prices, peak_prices, stop_loss, trail_stop, market_raw):
        sym   = sig.symbol
        entry = holdings[sym]["avg_price"]
        result = paper_sell(sym, sig.qty, sig.price, holdings)
        cash_recv += result.received
        fees      += result.fee
        peak_prices.pop(sym, None)
        cooldown_map[sym] = cycle
        pnl   = (sig.price - entry) * result.qty - result.fee
        reason = (f"Stop-loss fixe {stop_loss*100:.0f}% déclenché"
                  if sig.kind == "stop-loss"
                  else f"Trailing stop {trail_stop*100:.0f}% depuis pic ${peak_prices.get(sym, sig.price):,.4f}")
        trades.append(PaperTrade(
            cycle=cycle, action=f"SELL ({sig.kind})", symbol=sym,
            qty=result.qty, price=sig.price, fee=result.fee, pnl=pnl,
            reason=reason,
        ))
        log.info("[SIM] SELL (%s) %s: %.1f%%", sig.kind, sym, sig.loss_pct * 100)
    return cash_recv, fees, trades


def apply_paper_actions(
    actions: list[dict],
    holdings: dict,
    cash: float,
    prices: dict,
    peak_prices: dict,
    cooldown_map: dict,
    market_raw: dict,
    cycle: int,
    risk_level: int,
    sell_cooldown_cycles: int,
    *,
    min_confidence: float = 0.0,
    confidence_calibration: dict | None = None,
    cash_floor_pct: float = 0.0,
    now_ts: float | None = None,
) -> tuple[float, float, list[PaperTrade]]:
    """Apply a list of LLM-emitted actions in paper-trading mode.

    Returns (new_cash, fees_total, executed_trades). Mutates holdings,
    peak_prices, cooldown_map.

    `min_confidence` is enforced when the action carries a `confidence` field
    (Phase D+ schema). Actions without a confidence field bypass the gate
    (backwards-compatible with the legacy schema).

    `confidence_calibration` is the ``confidence_calibration`` slice of the
    behavior report (passed through from the cycle). When provided, BUY
    confidences are shrunken toward the realized win-rate of the matching
    bucket before the ``min_confidence`` gate — bayesian shrinkage means
    thin data barely moves the value, abundant data corrects systematic
    bias.
    """
    fees_total = 0.0
    trades: list[PaperTrade] = []

    # Regime cash floor: keep at least cash_floor_pct of total portfolio value
    # in cash. A BUY is clamped (or skipped) so it never breaches the floor.
    # Computed once from total value (cash + mark-to-market holdings) — a buy
    # converts cash to asset, leaving total ~unchanged, so a single snapshot
    # is enough. 0.0 (default) = no floor.
    cash_floor_usd = 0.0
    if cash_floor_pct > 0:
        holdings_val = sum(h["qty"] * prices.get(s, h["avg_price"]) for s, h in holdings.items())
        cash_floor_usd = (cash + holdings_val) * cash_floor_pct / 100.0

    for action in actions:
        atype = action.get("type", "")
        sym   = action.get("symbol", "")
        if not atype or not sym:
            continue

        # Calibrate the LLM-emitted confidence against the agent's own history.
        # Pass-through when calibration is absent or sample-thin (see behavior.py).
        raw_conf = action.get("confidence")
        if raw_conf is not None and confidence_calibration is not None:
            from .eval.behavior import calibrate_confidence
            calibrated = calibrate_confidence(atype, float(raw_conf), confidence_calibration)
            if abs(calibrated - float(raw_conf)) > 0.01:
                log.info("[STRAT] calibrate %s %s: %.2f → %.2f",
                         atype.upper(), sym, float(raw_conf), calibrated)
            conf = calibrated
        else:
            conf = raw_conf

        # Phase E gate: skip low-confidence actions when the model emits a confidence.
        if conf is not None and atype != "hold" and float(conf) < min_confidence:
            log.info("[STRAT] skip %s %s — confidence %.2f < %.2f",
                     atype.upper(), sym, float(conf), min_confidence)
            continue

        if atype == "buy" and cash > 10 and sym in prices:
            if in_cooldown(sym, cycle, cooldown_map, sell_cooldown_cycles):
                log.info("[STRAT] COOLDOWN %s (%d cycles restants)",
                         sym, sell_cooldown_cycles - (cycle - cooldown_map[sym]))
                continue
            rsi      = market_raw.get(sym, {}).get("rsi14")
            base_amt = float(action.get("usdc_amount") or 0)
            # Phase E: confidence scales position size when present (range 0.5–1.0).
            if conf is not None:
                base_amt *= max(0.5, min(1.0, float(conf)))
            amount = compute_position_size(base_amt, cash, risk_level, rsi)
            # Clamp to the regime cash floor — never spend below the reserve.
            if cash_floor_usd > 0:
                spendable = cash - cash_floor_usd
                if amount > spendable:
                    if spendable < 10:
                        log.info("[STRAT] skip BUY %s — cash floor %.0f%% atteint (réserve $%.2f)",
                                 sym, cash_floor_pct, cash_floor_usd)
                        continue
                    log.info("[STRAT] clamp BUY %s $%.2f → $%.2f (cash floor %.0f%%)",
                             sym, amount, spendable, cash_floor_pct)
                    amount = spendable
            if amount >= 10:
                res = paper_buy(sym, amount, prices[sym], holdings, entry_ts=now_ts)
                cash -= amount
                fees_total += res.fee
                peak_prices[sym] = prices[sym]
                trades.append(PaperTrade(
                    cycle=cycle, action="BUY", symbol=sym, amount=amount,
                    price=prices[sym], qty=res.qty, fee=res.fee,
                    reason=format_buy_reason(action),
                    horizon=action.get("horizon"),
                    confidence=conf,
                ))
                rsi_factor = (max(0.5, min(1.5, 1.5 - (rsi - 20) / 60))
                              if rsi is not None else 1.0)
                log.info("[STRAT] BUY  $%.2f %s @ $%.4f (RSI=%.0f ×%.2f) [%s]",
                         amount, sym, prices[sym], rsi or 0, rsi_factor,
                         (action.get("horizon") or "?"))

        elif atype == "sell" and sym in holdings and sym in prices:
            qty   = min(action.get("qty") or holdings[sym]["qty"], holdings[sym]["qty"])
            entry = holdings[sym]["avg_price"]
            res   = paper_sell(sym, qty, prices[sym], holdings)
            cash       += res.received
            fees_total += res.fee
            peak_prices.pop(sym, None)
            cooldown_map[sym] = cycle
            pnl = (prices[sym] - entry) * res.qty - res.fee
            trades.append(PaperTrade(
                cycle=cycle, action="SELL", symbol=sym, qty=res.qty,
                price=prices[sym], fee=res.fee, pnl=pnl,
                reason=action.get("reason", ""), confidence=conf,
            ))
            log.info("[STRAT] SELL %.6f %s @ $%.4f", res.qty, sym, prices[sym])

    return cash, fees_total, trades
