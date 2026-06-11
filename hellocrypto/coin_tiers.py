"""Per-coin risk tiers used to filter and modulate decisions.

Two-layer source:

1. **DB-backed monthly tiers** (``db.coin_tiers``) — computed from 30d
   vol / drawdown / beta in ``scripts/compute_coin_tiers.py``. Looked up
   at the decision-cycle's date so backtests use point-in-time info.

2. **Hardcoded baseline** (``COIN_RISK_TIERS_BASELINE``) — fallback for
   the period before DB tier history starts, or for symbols not yet
   profiled. Calibrated on 600d backtest (2024-10 → 2026-06).

The ``risk_level`` param (1-10, user-set) controls *which coins* are
candidates for entry, via ``risk_tier_cap``: tier ≤ cap(risk_level).
The cap is monotone and pivots on risk 7 (cap == 7) so that level — the
production reference — keeps the legacy ``tier ≤ risk_level`` behavior
exactly. Below 7 the universe narrows toward blue chips; above 7 it
opens up. Held positions are never filtered — exits always apply on the
full watchlist.
"""
from __future__ import annotations

from datetime import date
from functools import lru_cache

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

# Hard kill-switch flipped on the first DB failure (table missing, DB down…).
# Once tripped, subsequent calls go straight to the baseline — without it,
# a backtest with ~10 symbols × 24 cycles/day × 600 days = 144 k SQL queries
# all hitting the same "table missing" path, which dominates the run time.
_TIER_DB_FAILED = False


@lru_cache(maxsize=4096)
def _cached_tier_at(symbol: str, year: int, month: int) -> int | None:
    """DB tier lookup cached by month.

    ``compute_coin_tiers.py`` writes one row per (symbol, 1st-of-month), so
    within the same calendar month every ``as_of`` resolves to the same row.
    Bucketing by (year, month) lets a backtest amortize the DB cost over
    ~720 cycles per month instead of paying for each one.
    """
    global _TIER_DB_FAILED
    if _TIER_DB_FAILED:
        return None
    try:
        from db.coin_tiers import get_tier_at
        return get_tier_at(symbol, as_of=date(year, month, 1))
    except Exception:
        _TIER_DB_FAILED = True
        return None


def coin_tier(symbol: str, at: date | None = None) -> int:
    """Return the risk tier for *symbol*.

    Order of precedence:
      1. DB row with max(computed_at) ≤ *at* (or absolute latest if at is None)
      2. ``COIN_RISK_TIERS_BASELINE`` hardcoded baseline
      3. ``DEFAULT_TIER``

    DB lookup failures (table missing, no DATABASE_URL) silently fall back
    to baseline — the decider must never crash because tier history is empty.
    """
    db_tier: int | None = None
    if at is not None:
        db_tier = _cached_tier_at(symbol, at.year, at.month)
    elif not _TIER_DB_FAILED:
        # Live path (``at=None`` = "latest known"): not cached so a freshly
        # written tier is visible to the next decision cycle.
        try:
            from db.coin_tiers import get_tier_at
            db_tier = get_tier_at(symbol, as_of=None)
        except Exception:
            pass
    if db_tier is not None:
        return db_tier
    return COIN_RISK_TIERS_BASELINE.get(symbol, DEFAULT_TIER)


# risk_level → max coin tier eligible for NEW entries. Identity
# (``tier ≤ risk_level``) with a blue-chip floor so the lowest levels still
# trade BTC (tier 2) / ETH (tier 3) instead of an empty universe — a backtest
# sweep showed widening the low end churns volatile alts and regresses both PnL
# and drawdown there. risk 7 → 7 keeps the production reference exact.
_MIN_TIER_CAP = 3


def risk_tier_cap(risk_level: int) -> int:
    """Return the highest coin tier eligible for entry at *risk_level*."""
    return max(_MIN_TIER_CAP, min(10, int(risk_level)))


def is_allowed(symbol: str, risk_level: int, at: date | None = None) -> bool:
    """True if *symbol* is allowed at the given user *risk_level*."""
    return coin_tier(symbol, at=at) <= risk_tier_cap(risk_level)
