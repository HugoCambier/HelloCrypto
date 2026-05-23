"""Strategy evaluation metrics — pure functions, no I/O."""
from __future__ import annotations

import math
from collections.abc import Iterable


def total_return_pct(initial: float, final: float) -> float:
    if initial <= 0:
        return 0.0
    return (final - initial) / initial * 100


def btc_buy_and_hold_pct(btc_initial: float, btc_final: float) -> float:
    """Return of a 100% BTC position over the same window (no fees, no slippage)."""
    return total_return_pct(btc_initial, btc_final)


def alpha_vs_btc(strategy_pct: float, btc_pct: float) -> float:
    """Excess return vs BTC buy-and-hold."""
    return strategy_pct - btc_pct


def max_drawdown_pct(value_series: Iterable[float]) -> float:
    """Largest peak-to-trough drop, as a negative percentage.

    A series of [100, 110, 90, 120] yields -18.18 (peak 110 → trough 90).
    """
    peak = -math.inf
    worst = 0.0
    for v in value_series:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (v - peak) / peak * 100
            if dd < worst:
                worst = dd
    return round(worst, 4)


def sharpe(value_series: list[float], cycles_per_year: int = 365 * 24) -> float | None:
    """Annualised Sharpe ratio from a value series.

    Uses log-returns between consecutive snapshots. ``cycles_per_year`` lets
    you annualise correctly given the cycle frequency (default assumes hourly
    cycles ≈ 8760/year).
    """
    if len(value_series) < 2:
        return None
    rets = []
    for prev, cur in zip(value_series, value_series[1:], strict=False):
        if prev > 0 and cur > 0:
            rets.append(math.log(cur / prev))
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var  = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std  = math.sqrt(var)
    if std == 0:
        return None
    return round(mean / std * math.sqrt(cycles_per_year), 4)


def win_rate_pct(sells: list[dict]) -> float | None:
    """Fraction of sell trades that closed in profit."""
    closed = [s for s in sells if s.get("pnl") is not None]
    if not closed:
        return None
    winners = sum(1 for s in closed if s["pnl"] > 0)
    return round(winners / len(closed) * 100, 2)


def summarize(
    initial_value: float,
    final_value: float,
    btc_initial: float,
    btc_final: float,
    value_series: list[float],
    sells: list[dict],
    total_fees: float,
    num_trades: int,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cycle_seconds: int = 3600,
) -> dict:
    strat_pct = total_return_pct(initial_value, final_value)
    btc_pct   = btc_buy_and_hold_pct(btc_initial, btc_final) if btc_initial > 0 else 0.0
    cycles_per_year = max(1, int(365 * 24 * 3600 / cycle_seconds))
    return {
        "initial_value":     round(initial_value, 2),
        "final_value":       round(final_value, 2),
        "return_pct":        round(strat_pct, 4),
        "btc_return_pct":    round(btc_pct, 4),
        "alpha_vs_btc_pct":  round(alpha_vs_btc(strat_pct, btc_pct), 4),
        "max_drawdown_pct":  max_drawdown_pct(value_series),
        "sharpe":            sharpe(value_series, cycles_per_year),
        "win_rate_pct":      win_rate_pct(sells),
        "num_trades":        num_trades,
        "total_fees":        round(total_fees, 4),
        "tokens_in":         tokens_in,
        "tokens_out":        tokens_out,
        "tokens_total":      tokens_in + tokens_out,
    }
