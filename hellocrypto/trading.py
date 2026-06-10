"""Shared trading primitives used by both simulation.py and agent.py.

All fee calculations, stop-loss checks, and position sizing live here so that
the two execution modes stay in sync without code duplication.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

FEE_RATE = 0.001  # 0.1 % — Binance maker/taker approx.


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class TradeResult:
    qty: float
    price: float
    fee: float
    received: float        # USDC received after fee (sell) or 0 (buy)
    avg_price: float       # new avg entry price after the trade


@dataclass
class StopSignal:
    symbol: str
    qty: float
    price: float
    kind: str              # "stop-loss" | "trailing-stop"
    loss_pct: float        # negative float, e.g. -0.12 for -12 %


# ── Paper order execution ──────────────────────────────────────────────────────

def paper_buy(
    symbol: str,
    usdc_amount: float,
    price: float,
    holdings: dict,
    entry_ts: float | None = None,
) -> TradeResult:
    """Execute a simulated BUY. Mutates *holdings* in-place.

    Returns a TradeResult with fee and resulting qty.

    ``entry_ts`` (unix seconds) is stamped on new positions so the LLM and
    the deterministic decider can compute hold-time consistently. On top-ups
    the original ``entry_ts`` is preserved — the min-hold timer anchors on
    the *original* entry, not on each refill.
    """
    fee     = usdc_amount * FEE_RATE
    qty_net = (usdc_amount - fee) / price

    if symbol in holdings:
        prev    = holdings[symbol]
        new_qty = prev["qty"] + qty_net
        holdings[symbol] = {
            "qty":       new_qty,
            "avg_price": (prev["avg_price"] * prev["qty"] + price * qty_net) / new_qty,
            "entry_ts":  prev.get("entry_ts"),
        }
    else:
        holdings[symbol] = {"qty": qty_net, "avg_price": price,
                            "entry_ts": entry_ts}

    return TradeResult(
        qty=qty_net,
        price=price,
        fee=fee,
        received=0.0,
        avg_price=holdings[symbol]["avg_price"],
    )


def paper_sell(
    symbol: str,
    qty: float,
    price: float,
    holdings: dict,
) -> TradeResult:
    """Execute a simulated SELL. Mutates *holdings* in-place.

    Returns a TradeResult with net USDC received and fee.
    """
    actual_qty = min(qty, holdings.get(symbol, {}).get("qty", 0))
    if actual_qty <= 0:
        return TradeResult(qty=0, price=price, fee=0, received=0, avg_price=price)

    gross    = actual_qty * price
    fee      = gross * FEE_RATE
    received = gross - fee

    holdings[symbol]["qty"] -= actual_qty
    if holdings[symbol]["qty"] <= 1e-8:
        del holdings[symbol]

    return TradeResult(qty=actual_qty, price=price, fee=fee, received=received, avg_price=price)


# ── Stop-loss checker ─────────────────────────────────────────────────────────

def check_stops(
    holdings: dict,
    prices: dict,
    peak_prices: dict,
    stop_loss: float,
    trail_stop: float,
    market_raw: dict | None = None,  # noqa: ARG001 (reserved for future use)
) -> list[StopSignal]:
    """Return stop signals for all positions that breach their thresholds.

    Args:
        holdings:    Current positions {symbol: {qty, avg_price}}.
        prices:      Current market prices {symbol: float}.
        peak_prices: Highest price seen since entry {symbol: float}.
        stop_loss:   Hard stop fraction, e.g. 0.10 for 10 %.
        trail_stop:  Trailing fraction, e.g. 0.10 for 10 %.
        market_raw:  Reserved for future per-symbol stop logic (unused).

    Returns:
        List of StopSignal for each triggered position.
    """
    signals: list[StopSignal] = []
    for sym, pos in holdings.items():
        cur   = prices.get(sym)
        if cur is None:
            continue
        entry = pos["avg_price"]
        peak  = peak_prices.get(sym, entry)

        hard_loss  = (cur - entry) / entry
        trail_loss = (cur - peak)  / peak

        if hard_loss < -stop_loss:
            log.warning("[STOP-LOSS] %s: %.1f%%", sym, hard_loss * 100)
            signals.append(StopSignal(sym, pos["qty"], cur, "stop-loss", hard_loss))
        elif trail_loss < -trail_stop and peak > entry and cur >= entry:
            log.warning("[TRAILING-STOP] %s: %.1f%% depuis pic $%.4f",
                        sym, trail_loss * 100, peak)
            signals.append(StopSignal(sym, pos["qty"], cur, "trailing-stop", trail_loss))

    return signals


# ── Take-profit checker ──────────────────────────────────────────────────────

@dataclass
class TakeProfitSignal:
    symbol: str
    qty_to_sell: float
    price: float
    gain_pct: float
    level: int            # 1, 2, or 3 (which TP tier was hit)


def check_take_profits(
    holdings: dict,
    prices: dict,
    tp_levels: list[dict] | None = None,
    tp_state: dict | None = None,
) -> list[TakeProfitSignal]:
    """Check positions against take-profit levels.

    Args:
        holdings:  Current positions {symbol: {qty, avg_price}}.
        prices:    Current market prices {symbol: float}.
        tp_levels: List of TP tiers, e.g.:
                   [{"pct": 0.10, "sell_frac": 0.50},
                    {"pct": 0.20, "sell_frac": 0.25}]
                   meaning: at +10% sell 50%, at +20% sell 25%.
        tp_state:  Mutable dict tracking which levels were already
                   triggered per symbol, e.g. {sym: {1: True}}.

    Returns:
        List of TakeProfitSignal for each newly triggered level.
    """
    if not tp_levels:
        return []
    if tp_state is None:
        tp_state = {}

    signals: list[TakeProfitSignal] = []
    for sym, pos in list(holdings.items()):
        cur = prices.get(sym)
        if cur is None:
            continue
        entry = pos["avg_price"]
        gain  = (cur - entry) / entry

        sym_state = tp_state.setdefault(sym, {})
        for i, level in enumerate(tp_levels, start=1):
            if sym_state.get(i):
                continue  # already triggered
            if gain >= level["pct"]:
                qty_sell = pos["qty"] * level.get("sell_frac", 0.5)
                if qty_sell > 1e-8:
                    log.info("[TAKE-PROFIT] %s L%d: +%.1f%% → sell %.4f",
                             sym, i, gain * 100, qty_sell)
                    sym_state[i] = True
                    signals.append(TakeProfitSignal(sym, qty_sell, cur, gain, i))

    return signals


# ── Position timeout checker ─────────────────────────────────────────────────

@dataclass
class TimeoutSignal:
    symbol: str
    qty: float
    price: float
    held_cycles: int


def check_position_timeouts(
    holdings: dict,
    prices: dict,
    entry_cycles: dict,
    current_cycle: int,
    max_hold_cycles: int = 50,
    min_gain_pct: float = 0.01,
) -> list[TimeoutSignal]:
    """Force-sell stale positions that haven't moved enough.

    Args:
        holdings:        Current positions.
        prices:          Current market prices.
        entry_cycles:    {symbol: cycle_number_at_entry} — mutable.
        current_cycle:   Current simulation cycle number.
        max_hold_cycles: Close position after this many cycles.
        min_gain_pct:    Only timeout if gain is below this threshold.

    Returns:
        List of TimeoutSignal for positions that should be closed.
    """
    if max_hold_cycles <= 0:
        return []

    signals: list[TimeoutSignal] = []
    for sym, pos in list(holdings.items()):
        cur = prices.get(sym)
        if cur is None:
            continue
        entry_cy = entry_cycles.get(sym, current_cycle)
        held = current_cycle - entry_cy
        if held < max_hold_cycles:
            continue
        gain = (cur - pos["avg_price"]) / pos["avg_price"]
        if gain < min_gain_pct:
            log.info("[TIMEOUT] %s held %d cycles, gain %.2f%% — closing",
                     sym, held, gain * 100)
            signals.append(TimeoutSignal(sym, pos["qty"], cur, held))

    return signals


# ── Position sizing ────────────────────────────────────────────────────────────

def compute_position_size(
    usdc_requested: float,
    cash: float,
    risk_level: int,
    rsi: float | None = None,
) -> float:
    """Return the USDC amount to allocate for a BUY order.

    Applies:
    - max_pct cap derived from risk_level (5 % + risk * 4 %)
    - RSI-based scaling: lower RSI (oversold) → larger size, higher RSI → smaller

    Args:
        usdc_requested: Amount suggested by the LLM.
        cash:           Available USDC balance.
        risk_level:     Integer 1–10.
        rsi:            Current RSI value (optional).

    Returns:
        Clamped USDC amount, minimum 0.
    """
    max_pct    = (5 + risk_level * 4) / 100
    rsi_factor = max(0.5, min(1.5, 1.5 - (rsi - 20) / 60)) if rsi is not None else 1.0
    return min(usdc_requested, cash * max_pct * rsi_factor)
