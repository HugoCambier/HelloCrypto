"""Per-coin risk tiers used to filter and modulate decisions.

Two-layer source:

1. **DB-backed monthly tiers** (``db.coin_tiers``) — computed from 30d
   vol / drawdown / beta in ``scripts/compute_coin_tiers.py``. Looked up
   at the decision-cycle's date so backtests use point-in-time info.

2. **Hardcoded baseline** (``COIN_RISK_TIERS_BASELINE``) — fallback for
   the period before DB tier history starts, or for symbols not yet
   profiled. Calibrated on 600d backtest (2024-10 → 2026-06).

The ``risk_level`` param (1-10, user-set) controls *which coins* are
candidates for entry: tier ≤ risk_level. Held positions are never
filtered — exits always apply on the full watchlist.
"""
from __future__ import annotations

from datetime import date

COIN_RISK_TIERS_BASELINE: dict[str, int] = {
    "BTCUSDC":  2,  # blue chip
    "ETHUSDC":  3,
    "BNBUSDC":  4,
    "SOLUSDC":  5,
    "XRPUSDC":  5,
    "DOGEUSDC": 5,  # backtest winner despite meme status
    "AVAXUSDC": 6,
    "LINKUSDC": 8,  # consistent loser (-$21/-$9/-$18/-$4/-$8 across 5 BTs)
    "ADAUSDC":  8,  # consistent loser (-$28 over 3 successive 600d backtests)
    "POLUSDC":  8,  # worst backtest loser
}

DEFAULT_TIER = 6  # for unknown coins (when neither DB nor baseline has it)


def coin_tier(symbol: str, at: date | None = None) -> int:
    """Return the risk tier for *symbol*.

    Order of precedence:
      1. DB row with max(computed_at) ≤ *at* (or absolute latest if at is None)
      2. ``COIN_RISK_TIERS_BASELINE`` hardcoded baseline
      3. ``DEFAULT_TIER``

    DB lookup failures (table missing, no DATABASE_URL) silently fall back
    to baseline — the decider must never crash because tier history is empty.
    """
    try:
        from db.coin_tiers import get_tier_at
        db_tier = get_tier_at(symbol, as_of=at)
        if db_tier is not None:
            return db_tier
    except Exception:
        # Table may not exist yet (fresh DB) or DB unavailable — fall through.
        pass
    return COIN_RISK_TIERS_BASELINE.get(symbol, DEFAULT_TIER)


def is_allowed(symbol: str, risk_level: int, at: date | None = None) -> bool:
    """True if *symbol* is allowed at the given user *risk_level*."""
    return coin_tier(symbol, at=at) <= risk_level
