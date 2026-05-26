"""Active pattern detection — server-side gating before the LLM sees the playbook.

The playbook (regime → favored/avoid patterns) is a statistical truth. But
deciding whether a pattern *fires right now* on a given symbol is a
mechanical check on indicators (RSI, MACD, BB position, etc.). Letting the
LLM do that gating step is unreliable on small models — they read
"favored: oversold_reversal" and treat it as a permission to LONG
regardless of whether RSI is actually under 30.

This module computes the patterns that fire *this cycle* per symbol, then
formats only those into the prompt section. The LLM no longer has to
verify trigger conditions — it just acts on signals already gated.
"""
from __future__ import annotations

from . import patterns


def _bb_pos(symbol_market: dict) -> str | None:
    """Derive bb_pos from the live bollinger object.

    The snapshot schema stores ``bb_pos`` directly ('↓lo' | '↑hi' | None);
    the live ``market`` dict stores the bands and the price separately.
    """
    boll = symbol_market.get("bollinger")
    price = symbol_market.get("price")
    if not isinstance(boll, dict) or price is None:
        return None
    lower = boll.get("lower")
    upper = boll.get("upper")
    if lower is not None and price <= lower:
        return "↓lo"
    if upper is not None and price >= upper:
        return "↑hi"
    return None


def _macd_hist(symbol_market: dict) -> float | None:
    """Extract MACD histogram from either the nested live shape or the flat row shape."""
    direct = symbol_market.get("macd_hist")
    if direct is not None:
        return direct
    macd = symbol_market.get("macd")
    if isinstance(macd, dict):
        return macd.get("histogram")
    return None


def adapt_to_snapshot_row(
    symbol_market: dict,
    score: float | None = None,
    regime_fng: str | None = None,
) -> dict:
    """Convert the live ``market[symbol]`` shape to the snapshot-row shape that
    ``patterns.py`` predicates expect.

    The bench/agent market dict carries indicators in a nested form
    (``macd.histogram``, ``bollinger.lower/upper``) and lacks the
    pre-computed ``bb_pos``, ``score``, ``regime_fng``. Predicates were
    written against the flat snapshot row, so we adapt here.
    """
    return {
        "rsi14":      symbol_market.get("rsi14"),
        "macd_hist":  _macd_hist(symbol_market),
        "bb_pos":     _bb_pos(symbol_market),
        "trend":      symbol_market.get("trend"),
        "trend_1d":   symbol_market.get("trend_1d"),
        "score":      score,
        "regime_fng": regime_fng,
    }


def detect_active(
    symbol_market: dict,
    score: float | None = None,
    regime_fng: str | None = None,
) -> list[str]:
    """Return the list of pattern names that fire for this symbol right now."""
    row = adapt_to_snapshot_row(symbol_market, score, regime_fng)
    return [name for name, predicate in patterns.PATTERNS.items() if predicate(row)]


def format_active_section(
    playbook: dict | None,
    regime: str,
    market: dict,
    scores: dict | None = None,
    regime_fng: str | None = None,
    max_lines: int = 8,
) -> str:
    """Cycle-anchored playbook section: only show lessons for patterns
    that fire on at least one watchlist symbol *right now*.

    Returns '' when no pattern fires anywhere — the prompt stays clean and
    the LLM doesn't get permissive "favored" hints when there's no signal.
    """
    if not playbook:
        return ""
    slot = playbook.get("by_regime", {}).get(regime)
    if not slot:
        return ""

    favored = {e["pattern"]: e for e in slot.get("favored", [])}
    avoid   = {e["pattern"]: e for e in slot.get("avoid", [])}
    if not favored and not avoid:
        return ""

    scores = scores or {}
    fires: list[tuple[str, str, dict, bool]] = []  # (symbol, pattern, lesson, is_favored)
    for sym, sdata in market.items():
        if not isinstance(sdata, dict) or "price" not in sdata:
            continue
        active = detect_active(sdata, scores.get(sym), regime_fng)
        for pat in active:
            if pat in favored:
                fires.append((sym, pat, favored[pat], True))
            elif pat in avoid:
                fires.append((sym, pat, avoid[pat], False))

    if not fires:
        return ""

    fires.sort(key=lambda t: (not t[3], -abs(t[2].get("net_edge_pct", 0))))
    fires = fires[:max_lines]

    lines = [f"PATTERNS ACTIFS CE CYCLE — régime [{regime}] :"]
    for sym, pat, lesson, is_favored in fires:
        side = (lesson.get("side") or "long").upper()
        n = lesson.get("n", 0)
        win = lesson.get("win_rate", 0)
        mean = lesson.get("mean_pct", 0)
        net  = lesson.get("net_edge_pct", 0)
        mark = "✓" if is_favored else "✗"
        verdict = f"favored {side}" if is_favored else f"TRAP — éviter {side}"
        lines.append(
            f"  {mark} {sym}: {pat} → {verdict} "
            f"(n={n}, win {win:.0%}, mean {mean:+.2f}% / net {net:+.2f}%)"
        )
    return "\n".join(lines)
