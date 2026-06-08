"""Measure path-dependence by running the same backtest at N start hours.

A 1-hour shift in start time can change the final PnL by more than 50% (observed
$81 vs $210 on the same commit). This script quantifies that by running the
backtest at multiple start-hour offsets and reporting median + IQR + spread.

Use it BEFORE judging whether a strategy tweak is signal or noise.

Example:
    poetry run python -m scripts.bench_start_variance \\
        --start 2023-09-14 --days 1000 --offsets 0,4,8,12,16,20

Outputs to stdout (table per run + summary). With --csv, also writes a row
per run to the given file.
"""
from __future__ import annotations

import argparse
import csv
import os
import statistics
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Enable the disk-cache layer in _fetch_klines BEFORE importing run_live.
# Without it, each offset re-fetches the full 240k-bar × 10-symbol history
# from Binance — turns a 5 min run into 15-20 min.
os.environ.setdefault("HELLOCRYPTO_KLINES_CACHE", "1")

from hellocrypto.api import load_config  # noqa: E402
from hellocrypto.backtest import run_live  # noqa: E402


def _resolve_start_date(start: str | None, days: int) -> str:
    """Always return a midnight-UTC start_date so cache keys are stable across
    offsets. Without this, ``_start_ms_from(None, N)`` returns a non-midnight
    timestamp and offsets +12h can cross day boundaries → cache misses."""
    if start:
        return start
    midnight_n_days_ago = (datetime.now(UTC) - timedelta(days=days)).date()
    return midnight_n_days_ago.isoformat()


def _max_drawdown_pct(timeseries: list[dict]) -> float | None:
    """Peak-to-trough % drawdown from a list of {'v': total_value} samples."""
    if not timeseries:
        return None
    peak = 0.0
    max_dd = 0.0
    for point in timeseries:
        v = point.get("v")
        if v is None:
            continue
        peak = max(peak, v)
        if peak > 0:
            dd = (v - peak) / peak * 100
            max_dd = min(max_dd, dd)
    return round(max_dd, 2)


def _run_one(symbols: list[str], offset: int, args: argparse.Namespace) -> dict:
    started = time.time()
    snap = run_live(
        symbols              = symbols,
        start_date           = args.start,
        days                 = args.days,
        budget               = args.budget,
        stop_loss_pct        = args.stop,
        trailing_stop_pct    = args.trailing,
        risk_level           = args.risk,
        decide_every_n_candles = args.decide_every_n,
        top_n                = args.top_n,
        buy_threshold        = args.buy_thr,
        trend_confirm_hours  = args.trend_confirm_hours,
        min_hold_hours       = args.min_hold_hours,
        rebuy_cooldown_hours = args.rebuy_cooldown_hours,
        start_hour_offset    = offset,
        # Bypass the dashboard's per-candle sleep (defaults to 10 candles/s →
        # 40 min of pure sleep on 1000d × 10 symbols). The bench has no UI to
        # animate.
        speed_ref            = {"value": 1e9},
    )
    elapsed = time.time() - started
    if "error" in snap:
        return {"offset": offset, "error": snap["error"], "elapsed_s": round(elapsed, 1)}
    return {
        "offset":     offset,
        "pnl":        snap.get("pnl"),
        "pnl_pct":    snap.get("pnl_pct"),
        "alpha":      snap.get("alpha"),
        "vs_btc":     snap.get("btc_bh_pnl"),
        "win_rate":   snap.get("win_rate"),
        "trades":     snap.get("trades_count"),
        "max_dd_pct": _max_drawdown_pct(snap.get("timeseries") or []),
        "elapsed_s":  round(elapsed, 1),
    }


def _summarize(rows: list[dict]) -> dict:
    """Compute median / IQR / spread on numeric fields across successful runs."""
    ok = [r for r in rows if "error" not in r]
    if len(ok) < 2:
        return {}
    out: dict[str, dict] = {}
    for field in ("pnl", "alpha", "vs_btc", "win_rate", "max_dd_pct", "trades"):
        vals = [r[field] for r in ok if r.get(field) is not None]
        if not vals:
            continue
        vals_sorted = sorted(vals)
        out[field] = {
            "median": statistics.median(vals),
            "min":    vals_sorted[0],
            "max":    vals_sorted[-1],
            "spread": round(vals_sorted[-1] - vals_sorted[0], 2),
            "stdev":  round(statistics.stdev(vals), 2) if len(vals) >= 2 else 0,
        }
    return out


def _print_table(rows: list[dict]) -> None:
    print(f"\n{'offset':>7} {'PnL':>9} {'PnL%':>7} {'α':>8} {'vs BTC':>8} "
          f"{'win%':>6} {'trades':>7} {'DD%':>7} {'time':>7}")
    print("-" * 72)
    for r in rows:
        if "error" in r:
            print(f"{r['offset']:>+7d}  ERROR: {r['error']}")
            continue
        print(f"{r['offset']:>+7d} "
              f"{r['pnl']:>9.2f} "
              f"{r['pnl_pct']:>7.2f} "
              f"{r.get('alpha') or 0:>+8.2f} "
              f"{r.get('vs_btc') or 0:>+8.2f} "
              f"{r.get('win_rate') or 0:>6.1f} "
              f"{r['trades']:>7d} "
              f"{r.get('max_dd_pct') or 0:>+7.2f} "
              f"{r['elapsed_s']:>6.1f}s")


def _print_summary(summary: dict) -> None:
    if not summary:
        print("\n(not enough successful runs to summarize)")
        return
    print(f"\n{'metric':<12} {'median':>10} {'min':>10} {'max':>10} "
          f"{'spread':>10} {'stdev':>10}")
    print("-" * 65)
    for field, stats in summary.items():
        print(f"{field:<12} "
              f"{stats['median']:>10.2f} "
              f"{stats['min']:>10.2f} "
              f"{stats['max']:>10.2f} "
              f"{stats['spread']:>10.2f} "
              f"{stats['stdev']:>10.2f}")


def main() -> None:
    cfg = load_config()
    parser = argparse.ArgumentParser(
        description="Run backtest at N start-hour offsets, report variance.",
    )
    parser.add_argument("--symbols", default=",".join(cfg.get("watchlist", ["BTCUSDC"])))
    parser.add_argument("--start",   default=None, help="YYYY-MM-DD (default: --days ago)")
    parser.add_argument("--days",    type=int,   default=1000)
    parser.add_argument("--budget",  type=float, default=float(cfg.get("budget", 100)))
    parser.add_argument("--stop",    type=float, default=float(cfg.get("stop_loss_pct", 21)))
    parser.add_argument("--trailing",type=float, default=float(cfg.get("trailing_stop_pct", 10)))
    parser.add_argument("--risk",    type=int,   default=int(cfg.get("risk_level", 7)))
    parser.add_argument("--buy-thr", type=int,   default=8)
    parser.add_argument("--top-n",   type=int,   default=3)
    parser.add_argument("--decide-every-n",      type=int,   default=4)
    parser.add_argument("--trend-confirm-hours", type=float, default=24.0)
    parser.add_argument("--min-hold-hours",      type=float, default=12.0)
    parser.add_argument("--rebuy-cooldown-hours",type=float, default=0.0)
    parser.add_argument("--offsets", default="0,4,8,12,16,20",
                        help="Comma-separated hour offsets to test (default: 6 starts)")
    parser.add_argument("--csv", default=None,
                        help="Optional path to write a CSV row per run")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    offsets = [int(x) for x in args.offsets.split(",")]
    args.start = _resolve_start_date(args.start, args.days)
    print(f"Bench {len(offsets)} starts × {args.days}d on {len(symbols)} symbols "
          f"(risk={args.risk}, top_n={args.top_n}, buy_thr={args.buy_thr})")
    print(f"Start={args.start} (midnight UTC) — Offsets: {offsets}")

    t0 = time.time()
    rows: list[dict] = []
    for i, off in enumerate(offsets, 1):
        print(f"\n[{i}/{len(offsets)}] offset=+{off}h …", flush=True)
        rows.append(_run_one(symbols, off, args))

    _print_table(rows)
    _print_summary(_summarize(rows))

    print(f"\nTotal wall-clock: {time.time() - t0:.1f}s")

    if args.csv:
        out_path = Path(args.csv)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="") as f:
            fieldnames = ["offset", "pnl", "pnl_pct", "alpha", "vs_btc",
                          "win_rate", "trades", "max_dd_pct", "elapsed_s", "error"]
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k) for k in fieldnames})
        print(f"CSV written: {out_path}")


if __name__ == "__main__":
    main()
