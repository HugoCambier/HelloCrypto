"""Per-coin risk tiers used to filter the watchlist on entry.

The ``risk_level`` param (1-10, set by the user) now controls *which coins
we'll consider entering*, not just position sizing. A coin enters the
candidate list only when its tier ≤ ``risk_level``. Held positions are
never filtered — exits still apply on the full watchlist.

Tiers are hardcoded (not data-driven) to remain interpretable. Lower tier
= more conservative pick (blue chip, mature, lower historical whipsaw).
Higher tier = more speculative (alt with weaker signal-to-noise).

Baseline calibration uses 600-day backtest (2024-10 → 2026-06) PnL by
coin: winners stay tier ≤ 5, structural losers (LINK, ADA, POL) push
into tiers 7-8 since their signals proved unreliable over that window.
"""
from __future__ import annotations

COIN_RISK_TIERS: dict[str, int] = {
    "BTCUSDC":  2,  # blue chip
    "ETHUSDC":  3,
    "BNBUSDC":  4,
    "SOLUSDC":  5,
    "XRPUSDC":  5,
    "DOGEUSDC": 5,  # backtest winner despite meme status
    "AVAXUSDC": 6,
    "LINKUSDC": 8,  # consistent loser (-$21 / -$9 / -$18 / -$4 / -$8 across 5 BTs)
    "ADAUSDC":  8,  # consistent loser (-$28 over 3 successive 600d backtests)
    "POLUSDC":  8,  # worst backtest loser
}

DEFAULT_TIER = 6  # for unknown coins


def coin_tier(symbol: str) -> int:
    """Return the risk tier for *symbol*, falling back to DEFAULT_TIER."""
    return COIN_RISK_TIERS.get(symbol, DEFAULT_TIER)


def is_allowed(symbol: str, risk_level: int) -> bool:
    """True if *symbol* is allowed at the given user *risk_level*."""
    return coin_tier(symbol) <= risk_level
