"""Named setups — boolean predicates over a snapshot row.

A pattern is a recognisable trading situation that a human trader would
name and react to (e.g. "RSI<30 + MACD turning up" = oversold reversal).
Patterns are NOT mutually exclusive — a snapshot can match several
(useful: overlap reveals which combinations actually pay).

Each predicate accepts a snapshot dict (from db.snapshots.load_snapshots)
and returns True when the situation is present. None-tolerant: missing
indicators make the pattern silently False rather than raising.
"""
from __future__ import annotations

from collections.abc import Callable

Pattern = Callable[[dict], bool]


def _rsi(s: dict) -> float | None:
    return s.get("rsi14")


def _macd(s: dict) -> float | None:
    return s.get("macd_hist")


# ── Reversal setups ───────────────────────────────────────────────────────────

def oversold_reversal(s: dict) -> bool:
    """RSI deeply oversold AND MACD histogram turning positive — classic bounce setup."""
    rsi  = _rsi(s)
    macd = _macd(s)
    return rsi is not None and rsi < 30 and macd is not None and macd > 0


def overbought_top(s: dict) -> bool:
    """RSI overbought AND MACD turning negative — classic local top."""
    rsi  = _rsi(s)
    macd = _macd(s)
    return rsi is not None and rsi > 70 and macd is not None and macd < 0


def bb_lower_bounce(s: dict) -> bool:
    """Price tagging lower Bollinger band with RSI below 40 — mean-reversion long."""
    rsi = _rsi(s)
    return s.get("bb_pos") == "↓lo" and rsi is not None and rsi < 40


def bb_upper_fade(s: dict) -> bool:
    """Price tagging upper Bollinger band with RSI above 60 — mean-reversion short/exit."""
    rsi = _rsi(s)
    return s.get("bb_pos") == "↑hi" and rsi is not None and rsi > 60


# ── Confluence setups (multi-signal alignment) ────────────────────────────────

def confluence_strong_buy(s: dict) -> bool:
    """Score≥8 + RSI<50 + trend haussier 1h + MACD positif — high-conviction long."""
    rsi  = _rsi(s)
    macd = _macd(s)
    return (
        (s.get("score") or 0) >= 8
        and rsi is not None and rsi < 50
        and s.get("trend") == "haussier"
        and macd is not None and macd > 0
    )


def confluence_strong_sell(s: dict) -> bool:
    """Score≤3 + RSI>50 + trend baissier 1h + MACD négatif — high-conviction short/exit."""
    rsi  = _rsi(s)
    macd = _macd(s)
    return (
        (s.get("score") or 10) <= 3
        and rsi is not None and rsi > 50
        and s.get("trend") == "baissier"
        and macd is not None and macd < 0
    )


# ── Trend continuation ────────────────────────────────────────────────────────

def momentum_continuation_up(s: dict) -> bool:
    """Daily AND 1h trend haussier + MACD+ + RSI in 50-70 zone — pullback buy in uptrend."""
    rsi  = _rsi(s)
    macd = _macd(s)
    return (
        s.get("trend") == "haussier"
        and s.get("trend_1d") == "haussier"
        and macd is not None and macd > 0
        and rsi is not None and 50 <= rsi <= 70
    )


def momentum_continuation_down(s: dict) -> bool:
    """Daily AND 1h trend baissier + MACD- + RSI in 30-50 zone — bounce sell in downtrend."""
    rsi  = _rsi(s)
    macd = _macd(s)
    return (
        s.get("trend") == "baissier"
        and s.get("trend_1d") == "baissier"
        and macd is not None and macd < 0
        and rsi is not None and 30 <= rsi <= 50
    )


# ── Trap setups (often look attractive but lose) ──────────────────────────────

def falling_knife(s: dict) -> bool:
    """RSI<25 + daily trend baissier + BB↓lo — the "cheap" buy that gets cheaper."""
    rsi = _rsi(s)
    return (
        rsi is not None and rsi < 25
        and s.get("trend_1d") == "baissier"
        and s.get("bb_pos") == "↓lo"
    )


def euphoria_top(s: dict) -> bool:
    """RSI>75 + BB↑hi + greed regime — buying the top with everyone else."""
    rsi = _rsi(s)
    return (
        rsi is not None and rsi > 75
        and s.get("bb_pos") == "↑hi"
        and s.get("regime_fng") == "greed"
    )


# ── Registry ──────────────────────────────────────────────────────────────────

PATTERNS: dict[str, Pattern] = {
    "oversold_reversal":         oversold_reversal,
    "overbought_top":            overbought_top,
    "bb_lower_bounce":           bb_lower_bounce,
    "bb_upper_fade":             bb_upper_fade,
    "confluence_strong_buy":     confluence_strong_buy,
    "confluence_strong_sell":    confluence_strong_sell,
    "momentum_continuation_up":  momentum_continuation_up,
    "momentum_continuation_down": momentum_continuation_down,
    "falling_knife":             falling_knife,
    "euphoria_top":              euphoria_top,
}


# Side of each pattern — informs how to interpret returns:
#   "long":  positive return at horizon = pattern worked
#   "short": negative return at horizon = pattern worked
PATTERN_SIDES: dict[str, str] = {
    "oversold_reversal":         "long",
    "overbought_top":            "short",
    "bb_lower_bounce":           "long",
    "bb_upper_fade":             "short",
    "confluence_strong_buy":     "long",
    "confluence_strong_sell":    "short",
    "momentum_continuation_up":  "long",
    "momentum_continuation_down": "short",
    "falling_knife":             "long",   # the trade you'd be tempted to take
    "euphoria_top":              "long",   # idem (buying the top)
}
