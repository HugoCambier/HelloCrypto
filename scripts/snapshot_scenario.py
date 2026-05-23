#!/usr/bin/env python3
"""Build a frozen Scenario from historical Binance klines + Fear&Greed.

Walks back from `--end` by `--days` days at intervals of `--cycle-seconds`,
fetching each symbol's klines + indicators at that point in time. Stores
each cycle's snapshot under data/scenarios/<name>.json.

BTC dominance: historical data isn't free, so we hold it constant at a
midpoint value (configurable via --btc-dominance). The strategy uses it
as a directional hint only, so a constant is acceptable for backtests.

Usage:
  python scripts/snapshot_scenario.py --name btc_crash_2025_03 \
      --end 2025-03-08T00:00:00 --days 7 --cycle-seconds 1800 \
      --watchlist BTCUSDC,ETHUSDC,SOLUSDC
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from hellocrypto.api import (  # noqa: E402
    _compute_atr,
    _compute_bollinger,
    _compute_macd,
    _compute_rsi,
    _compute_sma,
    _cycle_to_interval,
)
from hellocrypto.eval import scenario  # noqa: E402

_BINANCE = "https://api.binance.com"


def _fetch_klines(symbol: str, interval: str, end_ms: int, limit: int = 50) -> list[list]:
    r = requests.get(
        f"{_BINANCE}/api/v3/klines",
        params={"symbol": symbol, "interval": interval,
                "limit": limit, "endTime": end_ms},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def _fetch_24h(symbol: str) -> dict:
    """24h stats only available 'now' — approximated from klines later."""
    r = requests.get(f"{_BINANCE}/api/v3/ticker/24hr",
                     params={"symbol": symbol}, timeout=10)
    r.raise_for_status()
    return r.json()


def _historical_fng(start: datetime, end: datetime) -> dict[str, dict]:
    """Build a map date_str (YYYY-MM-DD) → {value, label} from alternative.me."""
    span_days = max(1, (end.date() - start.date()).days + 5)
    r = requests.get(f"https://api.alternative.me/fng/?limit={span_days}", timeout=10)
    r.raise_for_status()
    out: dict[str, dict] = {}
    for d in r.json().get("data", []):
        ts = datetime.fromtimestamp(int(d["timestamp"]), tz=UTC)
        out[ts.strftime("%Y-%m-%d")] = {
            "value": int(d["value"]), "label": d["value_classification"],
        }
    return out


def _build_market_for_cycle(symbol: str, interval: str, ts_ms: int) -> dict | None:
    try:
        kl = _fetch_klines(symbol, interval, ts_ms, limit=50)
    except Exception as exc:
        print(f"[snapshot] {symbol} {ts_ms}: {exc}", file=sys.stderr)
        return None
    if not kl:
        return None
    closes = [float(k[4]) for k in kl]
    highs  = [float(k[2]) for k in kl]
    lows   = [float(k[3]) for k in kl]
    price  = closes[-1]
    rsi14  = _compute_rsi(closes)
    sma7   = _compute_sma(closes, 7)
    sma25  = _compute_sma(closes, 25)
    if sma7 and sma25:
        trend = "haussier" if sma7 > sma25 else "baissier"
    else:
        trend = "neutre"
    change_1h  = ((closes[-1] - closes[-2]) / closes[-2] * 100) if len(closes) >= 2 else 0.0
    high_24h   = max(highs[-24:]) if len(highs) >= 24 else max(highs)
    low_24h    = min(lows[-24:])  if len(lows)  >= 24 else min(lows)
    change_24h = ((closes[-1] - closes[-24]) / closes[-24] * 100) if len(closes) >= 24 else change_1h
    range_pct  = (high_24h - low_24h) / price * 100 if price else 0.0
    volume_usdc = sum(float(k[7]) for k in kl[-24:]) if len(kl) >= 24 else sum(float(k[7]) for k in kl)
    return {
        "price":          price,
        "change_pct_24h": round(change_24h, 4),
        "change_pct_1h":  round(change_1h, 4),
        "high_24h":       high_24h,
        "low_24h":        low_24h,
        "volume_usdc":    volume_usdc,
        "rsi14":          rsi14,
        "sma7":           round(sma7, 4) if sma7 else None,
        "sma25":          round(sma25, 4) if sma25 else None,
        "trend":          trend,
        "range_pct_24h":  round(range_pct, 2),
        "macd":           _compute_macd(closes),
        "bollinger":      _compute_bollinger(closes),
        "atr":            (round(_compute_atr(highs, lows, closes), 4)
                           if _compute_atr(highs, lows, closes) else None),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--name",       required=True)
    p.add_argument("--end",        default=None, help="ISO timestamp UTC ; défaut = now")
    p.add_argument("--days",       type=int, default=7)
    p.add_argument("--cycle-seconds", type=int, default=1800)
    p.add_argument("--watchlist",  default="BTCUSDC,ETHUSDC,SOLUSDC,BNBUSDC,XRPUSDC")
    p.add_argument("--btc-dominance", type=float, default=55.0,
                   help="Constante utilisée pour BTC.D (historique gratuit indispo)")
    p.add_argument("--note",       default="")
    args = p.parse_args()

    end_dt = datetime.fromisoformat(args.end) if args.end else datetime.utcnow()
    start_dt = end_dt - timedelta(days=args.days)
    watchlist = [s.strip().upper() for s in args.watchlist.split(",") if s.strip()]
    interval  = _cycle_to_interval(args.cycle_seconds)

    print(f"[snapshot] {args.name} : {start_dt.isoformat()} → {end_dt.isoformat()} "
          f"({args.days}j, cycle {args.cycle_seconds}s, kline {interval})")
    print(f"[snapshot] watchlist : {watchlist}")
    fng_map = _historical_fng(start_dt, end_dt)
    print(f"[snapshot] F&G : {len(fng_map)} jours")

    cycles: list[scenario.Cycle] = []
    step = timedelta(seconds=args.cycle_seconds)
    cur = start_dt
    n_total = int((end_dt - start_dt).total_seconds() // args.cycle_seconds)
    while cur <= end_dt:
        ts_ms = int(cur.timestamp() * 1000)
        market: dict[str, dict] = {}
        for sym in watchlist:
            d = _build_market_for_cycle(sym, interval, ts_ms)
            if d:
                market[sym] = d
        if market:
            cycles.append(scenario.Cycle(
                timestamp=cur.isoformat(),
                market=market,
                fear_greed=fng_map.get(cur.strftime("%Y-%m-%d")),
                btc_dominance=args.btc_dominance,
            ))
        if len(cycles) % 10 == 0:
            print(f"[snapshot]   {len(cycles)}/{n_total} cycles ...", flush=True)
        cur += step
        time.sleep(0.05)  # avoid rate limit

    scn = scenario.Scenario(
        name=args.name,
        start=start_dt.isoformat(),
        end=end_dt.isoformat(),
        watchlist=watchlist,
        cycle_seconds=args.cycle_seconds,
        cycles=cycles,
        note=args.note,
    )
    path = scenario.save(scn)
    print(f"[snapshot] OK — {len(cycles)} cycles → {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
