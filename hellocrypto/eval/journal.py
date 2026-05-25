"""Journal — derives realised outcomes from historical price snapshots.

Two layers:

1. **Horizon enrichment** (per-snapshot)
   For each (symbol, timestamp), compute the realised price return at
   fixed lookaheads (default 6h, 24h, 72h) and the max favorable / max
   adverse excursion (MFE/MAE) inside the first 24h. These are facts
   about the market, independent of any decision-maker.

2. **Pattern evaluation** (per pattern × regime)
   For each named setup in ``patterns.py``, filter matching snapshots,
   group by regime (F&G × BTC daily trend), and aggregate the horizon
   returns. Produces the per-regime cheat-sheet that the playbook step
   condenses into prompt-ready lessons.

Pure in-memory after loading. Designed to run nightly.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from statistics import mean, median
from typing import Any

from db.snapshots import load_snapshots

from .patterns import PATTERN_SIDES, PATTERNS

log = logging.getLogger(__name__)

DEFAULT_HORIZONS_H = (6, 24, 72)
DEFAULT_EXCURSION_LOOKAHEAD_H = 24


# ── Horizon enrichment ────────────────────────────────────────────────────────

def enrich_with_horizons(
    snapshots: list[dict],
    horizons_h: tuple[int, ...] = DEFAULT_HORIZONS_H,
    excursion_lookahead_h: int = DEFAULT_EXCURSION_LOOKAHEAD_H,
) -> list[dict]:
    """Attach ``return_h{N}``, ``mae_24h``, ``mfe_24h`` to each snapshot.

    Snapshots must be sorted by timestamp ASC for a single symbol (hourly
    candles, so index offset == hours offset). Snapshots near the end of
    the series get NULL for horizons they can't reach.
    """
    n = len(snapshots)
    out = [dict(s) for s in snapshots]

    closes = [s.get("close") for s in snapshots]
    highs  = [s.get("high")  for s in snapshots]
    lows   = [s.get("low")   for s in snapshots]

    for i in range(n):
        c0 = closes[i]
        if not c0:
            continue

        for h in horizons_h:
            j = i + h
            if j < n and closes[j]:
                out[i][f"return_h{h}"] = round((closes[j] - c0) / c0 * 100, 4)
            else:
                out[i][f"return_h{h}"] = None

        # MFE/MAE inside the next excursion_lookahead_h candles
        end = min(n, i + 1 + excursion_lookahead_h)
        if end > i + 1:
            window_highs = [highs[k] for k in range(i + 1, end) if highs[k]]
            window_lows  = [lows[k]  for k in range(i + 1, end) if lows[k]]
            if window_highs:
                out[i]["mfe_24h"] = round((max(window_highs) - c0) / c0 * 100, 4)
            if window_lows:
                out[i]["mae_24h"] = round((min(window_lows) - c0) / c0 * 100, 4)

    return out


# ── Aggregation helpers ───────────────────────────────────────────────────────

def _percentile(values: list[float], p: float) -> float | None:
    """Linear-interpolated percentile (p in [0, 1])."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = p * (len(s) - 1)
    lo  = int(pos)
    hi  = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def _aggregate(values: list[float], side: str) -> dict:
    """Summary stats. ``side`` decides what counts as a "win"."""
    if not values:
        return {"n": 0}
    wins = sum(1 for v in values if (v > 0 if side == "long" else v < 0))
    return {
        "n":        len(values),
        "mean_pct": round(mean(values), 3),
        "median":   round(median(values), 3),
        "p25":      round(_percentile(values, 0.25), 3),
        "p75":      round(_percentile(values, 0.75), 3),
        "win_rate": round(wins / len(values), 3),
    }


def regime_key(s: dict) -> str:
    """Compact regime label: 'fear+bear', 'neutral+bull', etc."""
    fng = s.get("regime_fng") or "na"
    btc = s.get("regime_btc_trend") or "na"
    return f"{fng}+{btc}"


# ── Pattern evaluation ────────────────────────────────────────────────────────

def evaluate_patterns(
    snapshots: list[dict],
    horizons_h: tuple[int, ...] = DEFAULT_HORIZONS_H,
    min_samples: int = 20,
) -> dict:
    """Return ``{pattern_name: {regime: stats, '_all': stats}}``.

    Snapshots must already be enriched via ``enrich_with_horizons``. A
    pattern × regime cell with fewer than ``min_samples`` matches is
    aggregated into a ``_thin`` bucket alongside the named regimes.
    """
    results: dict[str, Any] = {}

    for name, predicate in PATTERNS.items():
        side = PATTERN_SIDES[name]
        matches = [s for s in snapshots if predicate(s)]

        # Group by regime
        by_regime: dict[str, list[dict]] = defaultdict(list)
        for s in matches:
            by_regime[regime_key(s)].append(s)

        regime_stats: dict[str, Any] = {}
        thin_pool: list[dict] = []
        for reg, items in by_regime.items():
            if len(items) < min_samples:
                thin_pool.extend(items)
                continue
            regime_stats[reg] = _stats_for_group(items, horizons_h, side)

        if thin_pool:
            regime_stats["_thin"] = _stats_for_group(thin_pool, horizons_h, side)

        # Overall (all regimes combined) — useful sanity check
        regime_stats["_all"] = _stats_for_group(matches, horizons_h, side)

        results[name] = {
            "side":         side,
            "n_matches":    len(matches),
            "by_regime":    regime_stats,
        }

    return results


def _stats_for_group(
    items: list[dict],
    horizons_h: tuple[int, ...],
    side: str,
) -> dict:
    """Aggregate horizon returns + MFE/MAE for a list of matched snapshots."""
    out: dict[str, Any] = {"n": len(items)}
    for h in horizons_h:
        vals = [s[f"return_h{h}"] for s in items if s.get(f"return_h{h}") is not None]
        out[f"h{h}"] = _aggregate(vals, side)
    maes = [s["mae_24h"] for s in items if s.get("mae_24h") is not None]
    mfes = [s["mfe_24h"] for s in items if s.get("mfe_24h") is not None]
    if maes:
        out["mae_24h_mean"]   = round(mean(maes), 3)
        out["mae_24h_median"] = round(median(maes), 3)
    if mfes:
        out["mfe_24h_mean"]   = round(mean(mfes), 3)
        out["mfe_24h_median"] = round(median(mfes), 3)
    return out


# ── Convenience: full pipeline from DB ────────────────────────────────────────

def run_full_analysis(
    symbols: list[str] | None = None,
    source: str | None = "backfill",
    horizons_h: tuple[int, ...] = DEFAULT_HORIZONS_H,
    min_samples: int = 20,
) -> dict:
    """End-to-end: load → enrich → evaluate, returning the pattern report.

    ``symbols=None`` means evaluate every symbol present in DB with the
    matching ``source``. ``source=None`` pools backfill + live together —
    used by the nightly cron rebuild once live data has accumulated.
    """
    # Discover symbols if not provided
    if symbols is None:
        from db.snapshots import _USE_POSTGRES
        if _USE_POSTGRES:
            from db.store import _postgres
            with _postgres() as c:
                if source:
                    c.execute("SELECT DISTINCT symbol FROM price_snapshots WHERE source=%s ORDER BY symbol", (source,))
                else:
                    c.execute("SELECT DISTINCT symbol FROM price_snapshots ORDER BY symbol")
                symbols = [r[0] for r in c.fetchall()]
        else:
            from db.store import _sqlite
            with _sqlite() as c:
                if source:
                    rows = c.execute(
                        "SELECT DISTINCT symbol FROM price_snapshots WHERE source=? ORDER BY symbol",
                        (source,),
                    ).fetchall()
                else:
                    rows = c.execute(
                        "SELECT DISTINCT symbol FROM price_snapshots ORDER BY symbol"
                    ).fetchall()
            symbols = [r[0] for r in rows]
        log.info("Discovered %d symbols (source=%s): %s", len(symbols), source or "all", symbols)

    # Per-symbol enrichment (horizons must not cross symbol boundaries),
    # then pool for pattern matching across the universe.
    all_enriched: list[dict] = []
    for sym in symbols:
        snaps = load_snapshots(symbol=sym, source=source, limit=20_000)
        log.info("[%s] %d snapshots loaded", sym, len(snaps))
        if not snaps:
            continue
        enriched = enrich_with_horizons(snaps, horizons_h=horizons_h)
        all_enriched.extend(enriched)

    log.info("Evaluating %d patterns over %d total snapshots",
             len(PATTERNS), len(all_enriched))
    return evaluate_patterns(all_enriched, horizons_h=horizons_h,
                             min_samples=min_samples)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _main() -> int:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description="Run the journal analysis on backfilled snapshots.")
    parser.add_argument("--symbols", default="", help="Comma-separated subset; default = all symbols in DB")
    parser.add_argument("--source",  default="backfill", choices=("backfill", "live"))
    parser.add_argument("--min-samples", type=int, default=20)
    parser.add_argument("--output",  default="", help="Write JSON report to this path; default = stdout")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or None
    report  = run_full_analysis(symbols=symbols, source=args.source,
                                min_samples=args.min_samples)

    payload = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    if args.output:
        from pathlib import Path
        Path(args.output).write_text(payload)
        log.info("Report written to %s", args.output)
    else:
        sys.stdout.write(payload + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
