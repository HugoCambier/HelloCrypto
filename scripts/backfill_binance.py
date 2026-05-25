"""Backfill historical market snapshots from Binance.

Pulls N days of hourly klines per watchlist symbol, computes indicators
(RSI/MACD/Bollinger/ATR/SMA/trend) and regime tags (F&G bucket, BTC daily
trend), then writes everything to the ``price_snapshots`` table. Idempotent:
re-running for the same window updates existing rows in place.

Usage:
    poetry run python -m scripts.backfill_binance --days 365
    poetry run python -m scripts.backfill_binance --symbols BTCUSDC --days 7
    poetry run python -m scripts.backfill_binance --symbols BTCUSDC,ETHUSDC --days 30

BTC dominance is NOT backfilled — free APIs don't expose historical dominance
without a paid tier. Live snapshots will carry it; backfilled rows have NULL.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow `python scripts/backfill_binance.py` from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from db.snapshots import count_snapshots, save_snapshots_batch
from db.store import init_db
from hellocrypto.api import (
    _bb_position,
    _compute_atr,
    _compute_bollinger,
    _compute_macd,
    _compute_rsi,
    _compute_sma,
    api_get,
    compute_score,
)

log = logging.getLogger("backfill")

KLINE_BATCH       = 1000          # Binance max candles per /klines call
INDICATOR_WARMUP  = 50            # candles to look back when computing indicators
DB_BATCH          = 500           # snapshots per DB executemany
MS_PER_HOUR       = 3_600_000


# ── Regime bucketing ──────────────────────────────────────────────────────────

def regime_fng(value: int | None) -> str | None:
    """Bucket the F&G index into 3 regimes (keeps sample size workable)."""
    if value is None:
        return None
    if value < 35:
        return "fear"
    if value > 65:
        return "greed"
    return "neutral"


def regime_btc_trend(trend_1d: str | None) -> str | None:
    """Map daily trend label → 3-bucket regime."""
    if trend_1d == "haussier":
        return "bull"
    if trend_1d == "baissier":
        return "bear"
    if trend_1d == "neutre":
        return "range"
    return None


# ── F&G historical fetch ──────────────────────────────────────────────────────

def fetch_fng_history(days: int) -> dict[str, dict]:
    """Return ``{date_str_YYYYMMDD: {value, label}}`` for the last *days* days."""
    log.info("Fetching F&G history for %d days…", days)
    r = requests.get(f"https://api.alternative.me/fng/?limit={days}&format=json", timeout=15)
    r.raise_for_status()
    out: dict[str, dict] = {}
    for d in r.json().get("data", []):
        ts = datetime.fromtimestamp(int(d["timestamp"]), tz=timezone.utc)
        out[ts.strftime("%Y-%m-%d")] = {
            "value": int(d["value"]),
            "label": d["value_classification"],
        }
    log.info("  → %d days of F&G data", len(out))
    return out


# ── Kline pagination ──────────────────────────────────────────────────────────

def fetch_klines_paginated(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> list[list]:
    """Walk forward from ``start_ms`` in 1000-candle batches up to ``end_ms``."""
    all_kl: list[list] = []
    cursor = start_ms
    while cursor < end_ms:
        batch = api_get("/api/v3/klines", {
            "symbol":    symbol,
            "interval":  interval,
            "startTime": cursor,
            "endTime":   end_ms,
            "limit":     KLINE_BATCH,
        })
        if not batch:
            break
        all_kl.extend(batch)
        last_open = int(batch[-1][0])
        if last_open <= cursor:
            break  # safety: no progress
        cursor = last_open + 1
        time.sleep(0.05)  # gentle on the public ratelimit
    return all_kl


# ── Per-symbol processing ─────────────────────────────────────────────────────

def build_snapshots_for_symbol(
    symbol: str,
    days: int,
    fng_by_date: dict[str, dict],
) -> list[dict]:
    """Return the list of snapshot rows to write for *symbol* over *days* days.

    Strategy:
    1. Fetch hourly klines for the window, plus a warmup buffer for indicators.
    2. Fetch daily klines for the same window (+ warmup) → daily trend lookup.
    3. For each hourly candle within the requested window, compute indicators
       from the rolling window and produce one snapshot row.
    """
    now_ms     = int(time.time() * 1000)
    window_ms  = days * 24 * MS_PER_HOUR
    start_ms   = now_ms - window_ms
    warmup_ms  = INDICATOR_WARMUP * MS_PER_HOUR
    fetch_from = start_ms - warmup_ms

    log.info("[%s] Fetching hourly klines (≈%d candles)…", symbol, days * 24)
    klines_1h = fetch_klines_paginated(symbol, "1h", fetch_from, now_ms)
    if not klines_1h:
        log.warning("[%s] No klines returned, skipping.", symbol)
        return []
    log.info("[%s]   → %d hourly candles fetched", symbol, len(klines_1h))

    log.info("[%s] Fetching daily klines for trend_1d…", symbol)
    klines_1d = fetch_klines_paginated(symbol, "1d", fetch_from, now_ms)

    # Build daily trend lookup: date → "haussier"|"baissier"|"neutre"
    daily_trend: dict[str, str] = {}
    daily_closes: list[float] = []
    for kl in klines_1d:
        daily_closes.append(float(kl[4]))
        sma7  = _compute_sma(daily_closes, 7)
        sma25 = _compute_sma(daily_closes, 25)
        if sma7 and sma25:
            trend = "haussier" if sma7 > sma25 else "baissier"
        else:
            trend = "neutre"
        date_str = datetime.fromtimestamp(int(kl[0]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        daily_trend[date_str] = trend

    # Walk the hourly candles forward; only emit snapshots within the requested
    # window (skip the warmup prefix).
    rows: list[dict] = []
    closes: list[float] = []
    highs: list[float]  = []
    lows: list[float]   = []
    for kl in klines_1h:
        open_ms  = int(kl[0])
        o, h, l, c, v = float(kl[1]), float(kl[2]), float(kl[3]), float(kl[4]), float(kl[5])
        closes.append(c)
        highs.append(h)
        lows.append(l)

        if open_ms < start_ms:
            continue  # in warmup zone, accumulate but don't emit

        # Rolling indicators (use last ~50 closes max — plenty for MACD warmup)
        window      = closes[-50:]
        window_h    = highs[-50:]
        window_l    = lows[-50:]

        rsi14   = _compute_rsi(window)
        sma7    = _compute_sma(window, 7)
        sma25   = _compute_sma(window, 25)
        macd    = _compute_macd(window)
        boll    = _compute_bollinger(window)
        atr14   = _compute_atr(window_h, window_l, window)

        if sma7 and sma25:
            trend = "haussier" if sma7 > sma25 else "baissier"
        else:
            trend = "neutre"

        ts_dt = datetime.fromtimestamp(open_ms / 1000, tz=timezone.utc)
        ts    = ts_dt.isoformat()
        date_str = ts_dt.strftime("%Y-%m-%d")
        trend_1d = daily_trend.get(date_str)

        # 24h range from the last 24 hourly candles — fallback to current candle
        # if we don't yet have 24h of data (early backfill window).
        last_24 = closes[-24:] if len(closes) >= 24 else closes
        if len(last_24) >= 2:
            hi24 = max(highs[-24:])
            lo24 = min(lows[-24:])
            range_pct = (hi24 - lo24) / c * 100 if c else 0.0
        else:
            range_pct = (h - l) / c * 100 if c else 0.0

        # Score: reuse compute_score() with a synthesized dict
        score = compute_score({
            "rsi14":         rsi14,
            "trend":         trend,
            "trend_1d":      trend_1d,
            "range_pct_24h": range_pct,
        })

        # F&G for the candle's date (daily granularity is fine — F&G is daily)
        fng = fng_by_date.get(date_str, {})
        fng_value = fng.get("value")
        fng_label = fng.get("label")

        rows.append({
            "timestamp":        ts,
            "symbol":           symbol,
            "interval":         "1h",
            "open":             o,
            "high":             h,
            "low":              l,
            "close":            c,
            "volume":           v,
            "rsi14":            rsi14,
            "macd_hist":        macd["histogram"] if macd else None,
            "bb_lower":         boll["lower"] if boll else None,
            "bb_middle":        boll["middle"] if boll else None,
            "bb_upper":         boll["upper"] if boll else None,
            "bb_pos":           _bb_position(c, boll) if boll else None,
            "atr14":            round(atr14, 6) if atr14 else None,
            "sma7":             round(sma7, 6) if sma7 else None,
            "sma25":            round(sma25, 6) if sma25 else None,
            "trend":            trend,
            "trend_1d":         trend_1d,
            "score":            score,
            "fng_value":        fng_value,
            "fng_label":        fng_label,
            "btc_dominance":    None,   # not backfillable on free APIs
            "regime_fng":       regime_fng(fng_value),
            "regime_btc_trend": regime_btc_trend(trend_1d),
            "regime_dom":       None,
            "source":           "backfill",
            "session_id":       None,
            "cycle":            None,
        })

    return rows


# ── Main ──────────────────────────────────────────────────────────────────────

def _resolve_symbols(arg: str) -> list[str]:
    if arg == "config" or arg == "all":
        cfg = json.loads(Path("config.json").read_text())
        return list(cfg.get("watchlist", []))
    return [s.strip().upper() for s in arg.split(",") if s.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="config",
                        help="'config' (from config.json), 'all' (alias), or comma-list (BTCUSDC,ETHUSDC)")
    parser.add_argument("--days",    type=int, default=365)
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + compute but don't write to DB")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    symbols = _resolve_symbols(args.symbols)
    log.info("Backfill: %d symbols × %d days, dry_run=%s", len(symbols), args.days, args.dry_run)

    if not args.dry_run:
        init_db()

    # F&G is symbol-agnostic, fetch once.
    try:
        fng_by_date = fetch_fng_history(args.days + 2)  # tiny buffer
    except Exception:
        log.exception("F&G fetch failed — proceeding without F&G context")
        fng_by_date = {}

    total_written = 0
    t0 = time.time()
    for i, symbol in enumerate(symbols, 1):
        log.info("─── [%d/%d] %s ───", i, len(symbols), symbol)
        try:
            rows = build_snapshots_for_symbol(symbol, args.days, fng_by_date)
        except Exception:
            log.exception("[%s] Failed, skipping", symbol)
            continue

        if args.dry_run:
            log.info("[%s] DRY: %d snapshots ready (first ts=%s, last ts=%s)",
                     symbol, len(rows),
                     rows[0]["timestamp"] if rows else "—",
                     rows[-1]["timestamp"] if rows else "—")
            total_written += len(rows)
            continue

        # Chunked DB write
        written = 0
        for start in range(0, len(rows), DB_BATCH):
            chunk = rows[start:start + DB_BATCH]
            written += save_snapshots_batch(chunk)
        total_written += written
        log.info("[%s] %d rows written (total %d so far)", symbol, written, total_written)

    elapsed = time.time() - t0
    log.info("Done in %.1fs — %d snapshots %s",
             elapsed, total_written,
             "ready (dry-run)" if args.dry_run else "persisted")
    if not args.dry_run:
        for sym in symbols:
            n = count_snapshots(symbol=sym, source="backfill")
            log.info("  %s: %d backfilled rows in DB", sym, n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
