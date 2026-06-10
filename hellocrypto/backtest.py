"""Historical backtester using Binance klines.

Two modes:
- Deterministic (default, fast, free): calls the live ``regime_decision``
  decider (panier régime-gated sur trend_1d BTC) so backtest, sim et réel
  partagent la même logique de décision.
- LLM mode (realistic, throttled): same Claude/Gemini agent as production,
  called every ``llm_every_n_candles`` candles to control API cost.

Usage:
    poetry run backtest
    poetry run backtest --symbols BTCUSDC,ETHUSDC --start 2025-01-01 --budget 1000
"""

import argparse
import gzip
import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

from .api import (
    _compute_atr,
    _compute_bollinger,
    _compute_macd,
    _compute_rsi,
    _compute_sma,
    format_market_data,
    get_btc_dominance,
    get_fear_and_greed_history,
    load_config,
)
from .coin_tiers import is_allowed as _coin_allowed
from .deciders import regime_decision
from .llm import call as llm_call
from .prompts import SYSTEM, build_analysis
from .trading import FEE_RATE, paper_buy, paper_sell

HOUR_MS = 3_600_000

log = logging.getLogger(__name__)

BASE_URL    = "https://api.binance.com"
RESULT_FILE = Path("data/backtest_result.json")


# ── Kline fetcher ─────────────────────────────────────────────────────────────

DAY_MS = 86_400_000
KLINES_CACHE_DIR = Path("data/klines_cache")
KLINES_CACHE_TTL_S = 24 * 3600  # cache files older than this are re-fetched


def _fetch_klines_raw(symbol: str, interval: str, start_ms: int, end_ms: int) -> list:
    """Fetch all klines for symbol between start_ms and end_ms (paginated).

    Each page is retried up to 3× with exponential backoff (1s, 2s, 4s) on
    network errors. Without this, a single transient Binance timeout silently
    excludes the symbol from the run (via the caller's try/except), which
    causes hard-to-debug non-determinism between backtest runs.
    """
    candles = []
    while start_ms < end_ms:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                r = requests.get(
                    f"{BASE_URL}/api/v3/klines",
                    params={"symbol": symbol, "interval": interval,
                            "startTime": start_ms, "endTime": end_ms, "limit": 1000},
                    timeout=15,
                )
                r.raise_for_status()
                batch = r.json()
                last_exc = None
                break
            except (requests.RequestException, ConnectionError) as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(2 ** attempt)  # 1s, 2s
        if last_exc is not None:
            raise last_exc
        if not batch:
            break
        candles.extend(batch)
        start_ms = int(batch[-1][0]) + 1
        if len(batch) < 1000:
            break
        time.sleep(0.1)
    return candles


def _fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> list:
    """Fetch klines with optional disk cache (opt-in via env var).

    Set ``HELLOCRYPTO_KLINES_CACHE=1`` to enable. Used by
    ``scripts/bench_start_variance`` to avoid re-fetching the same 240k-bar
    1000d × 10-symbol history across N start-hour offsets — saves ~10 min/run.

    The cache key bins start/end to day boundaries so runs that differ only by
    hour-of-start share the same cached file. Stale cache (>TTL) is re-fetched.
    Production code paths (cron, dashboard, single backtest) leave the env var
    unset and hit Binance fresh, so live data is never served stale.
    """
    if os.environ.get("HELLOCRYPTO_KLINES_CACHE") != "1":
        return _fetch_klines_raw(symbol, interval, start_ms, end_ms)

    start_day = (start_ms // DAY_MS) * DAY_MS
    end_day = ((end_ms + DAY_MS - 1) // DAY_MS) * DAY_MS
    cache_file = KLINES_CACHE_DIR / f"{symbol}_{interval}_{start_day}_{end_day}.json.gz"

    if (cache_file.exists()
            and (time.time() - cache_file.stat().st_mtime) < KLINES_CACHE_TTL_S):
        with gzip.open(cache_file, "rt") as f:
            candles = json.load(f)
    else:
        candles = _fetch_klines_raw(symbol, interval, start_day, end_day)
        KLINES_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with gzip.open(cache_file, "wt") as f:
            json.dump(candles, f)

    return [k for k in candles if start_ms <= int(k[0]) < end_ms]


def _start_ms_from(start_date: str | None, days: int) -> int:
    """Return epoch-ms for start of backtest window.

    Accepts ``YYYY-MM-DD`` (midnight UTC), ``YYYY-MM-DDTHH:MM``,
    ``YYYY-MM-DDTHH:MM:SS``, or ``YYYY-MM-DD HH:MM`` — all interpreted as UTC.
    The intra-hour precision matters: ``decide_every_n_candles`` shifts the
    decision calendar by the start's minute-of-hour, which can swing PnL by
    tens of dollars over a 1000-day backtest.
    """
    if start_date:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                    "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(start_date, fmt).replace(tzinfo=UTC)
                return int(dt.timestamp() * 1000)
            except ValueError:
                continue
        raise ValueError(f"Invalid start_date format: {start_date!r}")
    return int((datetime.now(UTC) - timedelta(days=days)).timestamp() * 1000)


# ── Market-data builder from kline windows ──────────────────────────────────

def _daily_closes_up_to(klines_1d: list, ts_ms: int, running_close: float) -> list[float]:
    """Return finalized daily closes whose open_time ≤ ts_ms, plus the ongoing
    day's running close on top — mirrors how ``get_enriched_market_data``
    consumes Binance's daily klines (includes the current unclosed candle).
    """
    closes = []
    for k in klines_1d:
        if int(k[0]) > ts_ms:
            break
        closes.append(float(k[4]))
    if not closes:
        return []
    closes[-1] = running_close
    return closes


def _enrich_from_klines(symbols: list[str], all_klines: dict,
                        all_klines_1d: dict, i: int) -> dict:
    """Build enriched market-data dict for the *live* decider at candle index ``i``.

    Same shape as ``get_enriched_market_data``: includes 1h-derived indicators
    (RSI, SMA7/25, MACD, Bollinger) and a real *daily* ``trend_1d`` computed
    from pre-fetched 1d klines. This is the only enricher used by the backtest
    decision path so backtest, sim et réel partagent une vue marché identique.
    """
    result = {}
    for sym in symbols:
        kl    = all_klines[sym]
        if i >= len(kl) or kl[i] is None:
            continue  # symbol has no data at this hour
        kl_1d = all_klines_1d.get(sym, [])
        # Skip None entries in the lookback window (gaps in low-liquidity pairs).
        start = max(0, i - 49)
        closes  = [float(kl[j][4]) for j in range(start, i + 1) if kl[j] is not None]
        volumes = [float(kl[j][5]) for j in range(start, i + 1) if kl[j] is not None]
        highs   = [float(kl[j][2]) for j in range(max(0, i - 23), i + 1) if kl[j] is not None]
        lows    = [float(kl[j][3]) for j in range(max(0, i - 23), i + 1) if kl[j] is not None]
        if len(closes) < 2:
            continue  # not enough data for any indicator
        ts_ms   = int(kl[i][0])

        price   = closes[-1]
        rsi14   = _compute_rsi(closes, 14)
        sma7    = _compute_sma(closes, 7)
        sma25   = _compute_sma(closes, 25)
        trend = "haussier" if (sma7 and sma25 and sma7 > sma25) \
                else "baissier" if (sma7 and sma25) else "neutre"

        # Daily trend: real SMA7 vs SMA25 on daily closes — matches the
        # live ``get_enriched_market_data`` exactly. Falls back to None when
        # we don't have 25 finalized daily candles yet.
        daily_closes = _daily_closes_up_to(kl_1d, ts_ms, price)
        sma7_1d  = _compute_sma(daily_closes, 7)
        sma25_1d = _compute_sma(daily_closes, 25)
        if sma7_1d and sma25_1d:
            trend_1d = "haussier" if sma7_1d > sma25_1d else "baissier"
        else:
            trend_1d = None

        hi_24h  = max(highs) if highs else price
        lo_24h  = min(lows)  if lows  else price
        rng_pct = round((hi_24h - lo_24h) / lo_24h * 100, 2) if lo_24h else 0
        vol_24h = sum(volumes[-24:]) if len(volumes) >= 24 else sum(volumes)
        chg_1h  = round((closes[-1] - closes[-2]) / closes[-2] * 100, 2) if len(closes) >= 2 else 0
        chg_24h = round((closes[-1] - closes[-25]) / closes[-25] * 100, 2) \
                  if len(closes) >= 25 else 0.0

        # ATR needs matching-length highs/lows/closes from a dedicated 15-bar
        # window. Aligned klines may contain None entries (gaps in the source
        # data) — filter them out and only compute ATR if enough bars remain.
        atr_val = None
        if i >= 14:
            atr_window = [kl[j] for j in range(i - 14, i + 1) if kl[j] is not None]
            if len(atr_window) >= 15:
                atr_h = [float(k[2]) for k in atr_window]
                atr_l = [float(k[3]) for k in atr_window]
                atr_c = [float(k[4]) for k in atr_window]
                atr_val = _compute_atr(atr_h, atr_l, atr_c, period=14)
        result[sym] = {
            "price":          price,
            "rsi14":          rsi14,
            "sma7":            round(sma7, 4) if sma7 else None,
            "sma25":           round(sma25, 4) if sma25 else None,
            "change_pct_1h":  chg_1h,
            "change_pct_24h": chg_24h,
            "volume_usdc":    vol_24h,
            "trend":          trend,
            "trend_1d":       trend_1d,
            "range_pct_24h":  rng_pct,
            "spread_pct":     None,
            "macd":           _compute_macd(closes),
            "bollinger":      _compute_bollinger(closes, 20, 2.0),
            "atr":            round(atr_val, 4) if atr_val else None,
        }
    return result


# ── Shared snapshot builder ───────────────────────────────────────────────────

def _make_snapshot(current_step, total_steps, ts_ms, cash, budget, holdings,
                   prices, history, total_fees, initial_prices):
    portfolio_val = sum(h["qty"] * prices.get(s, h["avg_price"]) for s, h in holdings.items())
    total  = cash + portfolio_val
    pnl    = total - budget

    bh_pnl = bh_pct = alpha = bh_total = None
    btc_bh_pnl = btc_bh_pct = btc_total = None
    if initial_prices:
        valid  = [(s, p0) for s, p0 in initial_prices.items() if p0 and prices.get(s)]
        if valid:
            w_net    = (budget / len(valid)) * (1 - FEE_RATE)
            bh_total = sum(w_net * prices[s] / p0 for s, p0 in valid)
            bh_pnl   = round(bh_total - budget, 2)
            bh_pct   = round((bh_total - budget) / budget * 100, 2)
            alpha    = round(pnl - (bh_total - budget), 2)

        btc_sym = next((s for s in initial_prices if "BTC" in s and initial_prices[s] and prices.get(s)), None)
        if btc_sym:
            btc_total  = budget * (1 - FEE_RATE) * prices[btc_sym] / initial_prices[btc_sym]
            btc_bh_pnl = round(btc_total - budget, 2)
            btc_bh_pct = round((btc_total - budget) / budget * 100, 2)

    sells_only = [t for t in history if "SELL" in t.get("action","") and "stop" not in t.get("action","")]
    profitable = [t for t in sells_only if t.get("pnl", 0) > 0]

    trades_count = len([t for t in history if t.get("action") != "ANALYSE"])

    return {
        "loading":           False,
        "cycle":             current_step,
        "current_step":      current_step,
        "total_steps":       total_steps,
        "current_ts":        datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M"),
        "cash":              round(cash, 2),
        "budget":            round(budget, 2),
        "portfolio_value":   round(portfolio_val, 2),
        "total_value":       round(total, 2),
        "total":             round(total, 2),
        "pnl":               round(pnl, 2),
        "pnl_pct":           round(pnl / budget * 100, 2),
        "total_fees":        round(total_fees, 4),
        "trades":            trades_count,
        "trades_count":      trades_count,
        "buys":              len([t for t in history if t.get("action") == "BUY"]),
        "sells":             len(sells_only),
        "stop_losses":       len([t for t in history if "stop" in t.get("action", "")]),
        "win_rate":          round(len(profitable) / len(sells_only) * 100, 1) if sells_only else None,
        "benchmark_pnl":     bh_pnl,
        "benchmark_pnl_pct": bh_pct,
        "bh_total":          round(bh_total, 2) if bh_total is not None else None,
        "alpha":             alpha,
        "btc_bh_pnl":        btc_bh_pnl,
        "btc_bh_pct":        btc_bh_pct,
        "btc_total":         round(btc_total, 2) if btc_total is not None else None,
        "positions": [
            {
                "symbol":        sym,
                "qty":           round(h["qty"], 6),
                "avg_price":     round(h["avg_price"], 4),
                "current_price": prices.get(sym),
                "value":         round(h["qty"] * prices.get(sym, h["avg_price"]), 2),
                "pnl_pct":       round((prices[sym] - h["avg_price"]) / h["avg_price"] * 100, 2)
                                 if prices.get(sym) else 0,
            }
            for sym, h in holdings.items()
        ],
        "history": list(reversed(history)),
        "prices":  dict(prices),
    }


# ── Stop-loss check ───────────────────────────────────────────────────────────

def _check_stops(sym, all_klines, i, holdings, prices, peak_prices,
                 stop_loss, trail_stop):
    """Return (triggered, action_label, sell_price) for a symbol."""
    kl_now = all_klines[sym][i] if i < len(all_klines[sym]) else None
    if kl_now is None or sym not in prices:
        return False, "", 0.0  # no data for this symbol this hour — skip
    candle_low = float(kl_now[3])
    entry      = holdings[sym]["avg_price"]
    peak       = peak_prices.get(sym, entry)
    cur        = prices[sym]
    hard_loss  = (candle_low - entry) / entry
    trail_loss = (cur - peak) / peak

    if hard_loss < -stop_loss:
        return True, "SELL (stop-loss)", entry * (1 - stop_loss)
    if trail_loss < -trail_stop and peak > entry and cur >= entry:
        return True, "SELL (trailing-stop)", cur
    return False, "", cur


# ── live replay (dashboard) ───────────────────────────────────────────────────

def run_live(
    symbols: list[str],
    start_date: str | None = None,
    days: int = 30,
    budget: float = 1000.0,
    stop_loss_pct: float = 10.0,
    trailing_stop_pct: float = 5.0,
    risk_level: int = 3,
    sell_cooldown_cycles: int = 3,
    decide_every_n_candles: int = 4,
    top_n: int = 3,
    buy_threshold: int = 8,
    trend_confirm_hours: float = 24.0,
    min_hold_hours: float = 12.0,
    rebuy_cooldown_hours: float = 0.0,
    enable_regime_stance: bool = True,
    llm_mode: bool = False,
    llm_every_n_candles: int = 4,
    on_step=None,
    stop_event=None,
    start_hour_offset: int = 0,
) -> dict:
    """Replay historical candles as fast as possible.

    Args:
        start_date:             ISO date string "YYYY-MM-DD". If None, uses `days` ago.
        decide_every_n_candles: Cadence du décideur déterministe en bougies 1h
                                (4 = décision toutes les 4h, défaut). Les stops
                                fire à chaque bougie peu importe la cadence.
        top_n / buy_threshold:  Params de ``regime_decision`` — taille du
                                panier (positions simultanées max) et seuil
                                de score requis pour entrer.
        trend_confirm_hours:    Heures de tendance baissière confirmée pour exit.
        min_hold_hours:         Période min de détention avant tout exit.
        rebuy_cooldown_hours:   Anti-whipsaw — pas de rachat avant N heures
                                après un SELL.
        llm_mode:               Use the production LLM agent for decisions.
        llm_every_n_candles:    In LLM mode, call the LLM every N candles (throttle).
    """
    stop_loss  = stop_loss_pct  / 100
    trail_stop = trailing_stop_pct / 100
    max_pct    = (5 + risk_level * 4) / 100
    warmup     = 50   # enough for RSI-14, SMA-25, plus buffer

    end_ms   = int(datetime.now(UTC).timestamp() * 1000)
    start_ms = _start_ms_from(start_date, days)
    # Offset by N hours — used by bench_start_variance to measure path-dependence.
    start_ms += int(start_hour_offset) * HOUR_MS
    # Align start_ms / end_ms to UTC hourly boundaries so lookup keys match
    # exactly the open-time timestamps Binance returns for 1h klines.
    start_ms = ((start_ms + HOUR_MS - 1) // HOUR_MS) * HOUR_MS
    end_ms   = (end_ms // HOUR_MS) * HOUR_MS
    # 1d klines need 25+ finalized candles before the run starts so the daily
    # SMA25 is warm on the first decision; pull 30 extra days to be safe.
    start_ms_1d = start_ms - 30 * 86_400_000

    cfg = load_config()

    # ── Phase 1a : pré-filtre par tier (don't fetch what we won't trade) ─────
    excluded_by_tier = [s for s in symbols if not _coin_allowed(s, risk_level)]
    if excluded_by_tier:
        log.info("[BACKTEST] Tier > risk_level %d, exclu du fetch: %s",
                 risk_level, ", ".join(excluded_by_tier))
    symbols = [s for s in symbols if s not in excluded_by_tier]

    if not symbols:
        return {"error": "Aucun symbole compatible avec le risk_level demandé"}

    # ── Phase 1b : fetch klines (1h pour la replay, 1d pour le trend daily) ──
    all_klines: dict[str, list]    = {}
    all_klines_1d: dict[str, list] = {}
    for idx, sym in enumerate(symbols):
        if stop_event and stop_event.is_set():
            return {"error": "stopped"}
        if on_step:
            on_step({"loading": True,
                     "message": f"Chargement {sym} ({idx + 1}/{len(symbols)})…"})
        try:
            all_klines[sym] = _fetch_klines(sym, "1h", start_ms, end_ms)
        except Exception as exc:
            log.warning("[BACKTEST] %s: échec fetch (%s) — exclu", sym, exc)
            all_klines[sym] = []
        try:
            all_klines_1d[sym] = _fetch_klines(sym, "1d", start_ms_1d, end_ms)
        except Exception as exc:
            log.warning("[BACKTEST] %s: échec fetch 1d (%s) — trend_1d sera None", sym, exc)
            all_klines_1d[sym] = []

    # ── Phase 1c : drop symbols with insufficient history ────────────────────
    skipped = [s for s, k in all_klines.items() if len(k) <= warmup]
    for s in skipped:
        log.warning("[BACKTEST] %s: %d bougies (< %d), exclu du run", s, len(all_klines[s]), warmup)
        del all_klines[s]
    symbols = [s for s in symbols if s in all_klines]

    if not symbols:
        return {"error": "Aucun symbole avec suffisamment de données (min ~50 bougies)"}

    # ── Phase 2 : timestamp alignment ────────────────────────────────────────
    # Iterate by GLOBAL HOUR, not per-symbol index. This fixes two bugs:
    # (a) Late-launch symbols (e.g. POLUSDC born 2024-09-13) used to truncate
    #     the entire run via ``min_len`` ; now they simply don't participate
    #     for the cycles before their first kline.
    # (b) Symbols with intra-period gaps (low-liquidity USDC pairs) used to
    #     index-shift relative to BTC, so prices['SOL'] at iteration i was a
    #     different DATE than prices['BTC'] at the same i. Now both keys at
    #     index i resolve to the exact same hourly timestamp.
    total_hours = (end_ms - start_ms) // HOUR_MS
    aligned_klines:    dict[str, list] = {}
    aligned_klines_1d: dict[str, list] = {}
    for sym in symbols:
        ts_to_k = {int(k[0]): k for k in all_klines[sym]}
        aligned_klines[sym] = [ts_to_k.get(start_ms + i * HOUR_MS) for i in range(total_hours)]
        # Daily klines stay timestamp-queryable (sparse, ~30/year) — keep as-is.
        aligned_klines_1d[sym] = all_klines_1d.get(sym, [])

    # ── Phase 3 : exclude late-launch symbols (<50% coverage of requested range) ─
    # Forward-fill *within* the run is acceptable (gaps of a few hours), but
    # a symbol that doesn't exist for half the period would skew breadth and
    # confuse the user. Drop with explicit warning.
    late_launch: list[tuple[str, float]] = []
    for sym in list(aligned_klines.keys()):
        valid = sum(1 for k in aligned_klines[sym] if k is not None)
        coverage = valid / total_hours if total_hours else 0
        if coverage < 0.5:
            late_launch.append((sym, coverage))
            del aligned_klines[sym]
            aligned_klines_1d.pop(sym, None)
    for sym, cov in late_launch:
        log.warning("[BACKTEST] %s: %.0f%% de coverage seulement (lance trop tard / délisté) — exclu",
                    sym, cov * 100)
    symbols = list(aligned_klines.keys())

    if not symbols:
        return {"error": "Aucun symbole couvre suffisamment la période demandée"}

    skipped_msg = ""
    if skipped or excluded_by_tier or late_launch:
        parts = []
        if excluded_by_tier:
            parts.append(f"tier: {', '.join(excluded_by_tier)}")
        if skipped:
            parts.append(f"data<warmup: {', '.join(skipped)}")
        if late_launch:
            parts.append(f"coverage<50%: {', '.join(s for s, _ in late_launch)}")
        skipped_msg = " — exclus (" + " ; ".join(parts) + ")"
    log.info("[BACKTEST] Run sur %d symbole(s)%s", len(symbols), skipped_msg)

    # The remaining code iterates from warmup up to total_hours.  Replace the
    # legacy ``min_len``-based view with the aligned one.
    min_len      = total_hours
    all_klines   = aligned_klines     # downstream code reads this name
    all_klines_1d = aligned_klines_1d
    total_steps  = min_len - warmup
    cash          = budget
    holdings: dict = {}
    peak_prices: dict = {}
    cooldown_map: dict = {}
    history: list = []
    total_fees    = 0.0
    initial_prices: dict = {}
    prices: dict  = {}
    last_snap: dict = {}
    recent_decisions: list = []
    timeseries: list = []
    strat_state: dict = {}  # last_decision_cycle for regime_decision cadence
    start_ts_iso = datetime.utcfromtimestamp(
        (start_ms + warmup * HOUR_MS) / 1000
    ).strftime("%Y-%m-%d %H:%M")

    # Historical FNG indexed by date so each simulated day sees the value
    # that actually held then — not the live value of the moment the backtest
    # was launched (which would shift PnL by tens of $ when FNG crosses 25/75).
    # On fetch failure, ``fng_history`` is None → per-cycle lookups yield None
    # → decider skips the FNG modifier (safe, neutral fallback).
    fng_history   = get_fear_and_greed_history(days + 60)
    btc_dominance = get_btc_dominance()

    llm_call_count = 0
    llm_last_error = ""

    for i in range(warmup, min_len):
        if stop_event and stop_event.is_set():
            break

        # ts is deterministic from the global hour grid — independent of any
        # symbol's index, so all symbols at i refer to the same wall-clock hour.
        ts = start_ms + i * HOUR_MS
        cycle_date = datetime.fromtimestamp(ts / 1000, tz=UTC).date()
        fear_greed_today = (fng_history or {}).get(cycle_date.isoformat())
        # Only include symbols that have actual data at this hour (no
        # forward-fill — we want a price decision based on real market data).
        prices = {sym: float(kl[4])
                  for sym in symbols
                  if (kl := all_klines[sym][i]) is not None}
        if not prices:
            continue  # global gap; advance the clock
        current_step = i - warmup + 1

        if not initial_prices:
            initial_prices = dict(prices)

        # Update peak prices
        for sym in list(holdings):
            if sym in prices:
                peak_prices[sym] = max(peak_prices.get(sym, prices[sym]), prices[sym])

        # Stop-loss (hard + trailing).
        for sym in list(holdings):
            triggered, action_label, sell_price = _check_stops(
                sym, all_klines, i, holdings, prices, peak_prices, stop_loss, trail_stop)
            if triggered:
                qty   = holdings[sym]["qty"]
                entry = holdings[sym]["avg_price"]
                sr            = paper_sell(sym, qty, sell_price, holdings)
                received, fee = sr.received, sr.fee
                cash        += received
                total_fees  += fee
                peak_prices.pop(sym, None)
                cooldown_map[sym] = i
                dt_str = datetime.utcfromtimestamp(ts / 1000).isoformat()
                history.append({
                    "cycle":     current_step,
                    "timestamp": dt_str,
                    "action":    action_label,
                    "symbol":    sym,
                    "qty":       round(qty, 6),
                    "amount":    round(received, 2),
                    "price":     round(sell_price, 4),
                    "pnl":       round((sell_price - entry) * qty - fee, 4),
                    "fee":       round(fee, 6),
                    "reason":    action_label,
                })

        dt_str = datetime.utcfromtimestamp(ts / 1000).isoformat()

        # ── Decision: LLM or rule-based ───────────────────────────────────────
        if llm_mode:
            # Call LLM every N candles (throttle to limit API cost)
            if current_step % llm_every_n_candles == 1 or current_step == 1:
                try:
                    market_raw  = _enrich_from_klines(symbols, all_klines, all_klines_1d, i)
                    from .api import compute_scores
                    scores      = compute_scores(market_raw)
                    market_data = format_market_data(market_raw, symbols)
                    decision = llm_call(
                        prompt=build_analysis(
                            market_data, holdings, cash, budget, risk_level,
                            recent_decisions, fear_greed_today, btc_dominance, scores,
                            prices=prices, peak_prices=peak_prices,
                            cooldown_map=cooldown_map, total_fees=total_fees,
                            cycle=current_step,
                        ),
                        system=SYSTEM,
                        config=cfg,
                    )
                    llm_call_count += 1
                    recent_decisions = (recent_decisions + [decision])[-3:]

                    history.append({
                        "cycle":     current_step,
                        "timestamp": dt_str,
                        "action":    "ANALYSE",
                        "sentiment": decision.get("market_sentiment", "—"),
                        "reason":    decision.get("summary", ""),
                        "symbol":    "", "qty": None, "amount": None,
                        "price":     None, "fee": None, "pnl": None,
                    })

                    for action in decision.get("actions", []):
                        atype  = action.get("type", "")
                        sym    = action.get("symbol", "")
                        if not atype or not sym:
                            continue
                        reason = action.get("reason", "")

                        if atype == "buy" and cash > 10 and sym in prices:
                            last_sell = cooldown_map.get(sym, 0)
                            if i - last_sell < sell_cooldown_cycles:
                                continue
                            rsi = _enrich_from_klines([sym], all_klines, all_klines_1d, i)[sym].get("rsi14")
                            rsi_factor = max(0.5, min(1.5, 1.5 - (rsi - 20) / 60)) if rsi else 1.0
                            amount = min(action.get("usdc_amount", 0), cash * max_pct * rsi_factor)
                            if amount >= 10:
                                br      = paper_buy(sym, amount, prices[sym], holdings)
                                fee     = br.fee
                                qty_got = br.qty
                                total_fees += fee
                                cash       -= amount
                                peak_prices[sym] = prices[sym]
                                history.append({
                                    "cycle":     current_step,
                                    "timestamp": dt_str,
                                    "action":    "BUY",
                                    "symbol":    sym,
                                    "amount":    round(amount, 2),
                                    "qty":       round(qty_got, 6),
                                    "price":     round(prices[sym], 4),
                                    "fee":       round(fee, 6),
                                    "reason":    reason,
                                })

                        elif atype == "sell" and sym in holdings:
                            qty      = min(action.get("qty", holdings[sym]["qty"]), holdings[sym]["qty"])
                            entry    = holdings[sym]["avg_price"]
                            sr = paper_sell(sym, qty, prices[sym], holdings)
                            received, fee = sr.received, sr.fee
                            total_fees += fee
                            cash       += received
                            peak_prices.pop(sym, None)
                            cooldown_map[sym] = i
                            history.append({
                                "cycle":     current_step,
                                "timestamp": dt_str,
                                "action":    "SELL",
                                "symbol":    sym,
                                "qty":       round(qty, 6),
                                "amount":    round(received, 2),
                                "price":     round(prices[sym], 4),
                                "pnl":       round((prices[sym] - entry) * qty - fee, 4),
                                "fee":       round(fee, 6),
                                "reason":    reason,
                            })

                except Exception as exc:
                    llm_last_error = f"Cycle {current_step}: {exc}"
                    log.error("[BT-LLM] %s", llm_last_error, exc_info=True)

        else:
            # ── Deterministic decider — appel direct du même ``regime_decision``
            # que la simulation et le run réel. La cadence (decide_every_cycles)
            # est gérée par le décideur lui-même via ``strat_state``.
            market_raw = _enrich_from_klines(symbols, all_klines, all_klines_1d, i)
            fng_v = (fear_greed_today or {}).get("value") if fear_greed_today else None
            decision, strat_state = regime_decision(
                market_raw=market_raw, holdings=holdings, cash=cash,
                cycle=current_step, now_ts=ts / 1000.0,
                risk_level=risk_level, strat_state=strat_state,
                params={
                    "decide_every_cycles":  decide_every_n_candles,
                    # When stance is on, buy_threshold + top_n are dynamically
                    # derived per-cycle by _derive_stance; don't pin them so
                    # STANCE_PARAMS can override.  When off, honour the explicit
                    # values passed by the caller (backtest UI / propose script).
                    **({"top_n": top_n, "buy_threshold": buy_threshold}
                       if not enable_regime_stance else {}),
                    "trend_confirm_hours":  trend_confirm_hours,
                    "min_hold_hours":       min_hold_hours,
                    "rebuy_cooldown_hours": rebuy_cooldown_hours,
                    "enable_regime_stance": enable_regime_stance,
                },
                fng_value=fng_v,
                as_of_date=cycle_date,
            )
            actions = decision.get("actions", [])
            scores  = decision.get("scores", {}) or {}

            # Sells first — frees cash for the buys below. ``scale_out`` is
            # un sell partiel (qty fraction de la position courante) qui ne
            # libère pas le peak_prices ni n'arme le rebuy cooldown — la
            # position reste ouverte avec un reliquat qui continue à courir.
            for a in actions:
                sym   = a.get("symbol")
                atype = a.get("type")
                if atype not in ("sell", "scale_out") or sym not in holdings or sym not in prices:
                    continue
                cur            = prices[sym]
                requested_qty  = float(a.get("qty") or 0)
                available_qty  = holdings[sym]["qty"]
                qty            = (min(requested_qty, available_qty)
                                  if requested_qty > 0 else available_qty)
                if qty <= 0:
                    continue
                entry = holdings[sym]["avg_price"]
                sr    = paper_sell(sym, qty, cur, holdings)
                total_fees += sr.fee
                cash       += sr.received
                # paper_sell supprime holdings[sym] uniquement quand qty atteint 0.
                fully_closed = sym not in holdings
                if fully_closed:
                    peak_prices.pop(sym, None)
                    cooldown_map[sym] = i
                if atype == "scale_out":
                    action_label = "SELL (scale-out)"
                elif a.get("exit_kind") == "early":
                    action_label = "SELL (early-exit)"
                else:
                    action_label = "SELL"
                history.append({
                    "cycle":     current_step,
                    "timestamp": dt_str,
                    "action":    action_label,
                    "symbol":    sym,
                    "qty":       round(sr.qty, 6),
                    "amount":    round(sr.received, 2),
                    "price":     round(cur, 4),
                    "pnl":       round((cur - entry) * sr.qty - sr.fee, 4),
                    "fee":       round(sr.fee, 6),
                    "score":     scores.get(sym),
                    "reason":    a.get("reason", ""),
                })

            # Buys: decider already computed risk-aware usdc_amount per action.
            # Capture BTC context au moment de l'entrée — sert au diagnostic
            # "qui sont les trades qui sortent en signal-bear avec perte" :
            # on veut savoir si BTC était déjà fragile (loin sous son SMA25,
            # stance dégradée) au moment où on a engagé la position.
            btc_d     = market_raw.get("BTCUSDC") or {}
            btc_price = btc_d.get("price")
            btc_sma25 = btc_d.get("sma25")
            btc_ext   = ((btc_price / btc_sma25 - 1.0) * 100
                         if btc_price and btc_sma25 else None)
            entry_btc_ctx = {
                "stance":       decision.get("stance"),
                "ext_sma25":    round(btc_ext, 2) if btc_ext is not None else None,
                "chg_24h":      btc_d.get("change_pct_24h"),
                "trend_1d":     btc_d.get("trend_1d"),
            }
            for a in actions:
                if a.get("type") != "buy" or a.get("symbol") not in prices:
                    continue
                amount = float(a.get("usdc_amount", 0) or 0)
                if amount < 10 or amount > cash:
                    amount = min(amount, cash)
                    if amount < 10:
                        continue
                sym = a["symbol"]
                cur = prices[sym]
                br  = paper_buy(sym, amount, cur, holdings)
                total_fees += br.fee
                cash       -= amount
                peak_prices[sym] = cur
                history.append({
                    "cycle":     current_step,
                    "timestamp": dt_str,
                    "action":    "BUY",
                    "symbol":    sym,
                    "amount":    round(amount, 2),
                    "qty":       round(br.qty, 6),
                    "price":     round(cur, 4),
                    "fee":       round(br.fee, 6),
                    "score":     scores.get(sym),
                    "reason":    a.get("reason", ""),
                    "btc_ctx":   entry_btc_ctx,
                })

        last_snap = _make_snapshot(
            current_step, total_steps, ts,
            cash, budget, holdings, prices, history, total_fees, initial_prices,
        )
        timeseries.append({
            "ts":  last_snap["current_ts"],
            "v":   last_snap["total_value"],
            "bh":  last_snap.get("bh_total"),
            "btc": last_snap.get("btc_total"),
        })
        # Downsample to keep snapshot lightweight while preserving shape
        if len(timeseries) > 250:
            step = max(1, len(timeseries) // 200)
            last_snap["timeseries"] = timeseries[::step] + [timeseries[-1]]
        else:
            last_snap["timeseries"] = list(timeseries)
        last_snap["start_ts"] = start_ts_iso
        if skipped:
            last_snap["skipped_symbols"] = skipped
        if excluded_by_tier:
            last_snap["excluded_by_tier"] = excluded_by_tier
        if late_launch:
            last_snap["excluded_late_launch"] = [s for s, _ in late_launch]
        if llm_mode:
            last_snap["llm_calls"] = llm_call_count
            if llm_last_error:
                last_snap["llm_last_error"] = llm_last_error

        if on_step:
            on_step(last_snap)

    # ── Final liquidation: sell all remaining positions at last price ─────────
    if holdings and prices:
        final_ts = start_ms + (min_len - 1) * HOUR_MS
        dt_str   = datetime.fromtimestamp(final_ts / 1000, tz=UTC).replace(tzinfo=None).isoformat()
        for sym in list(holdings):
            if sym not in prices:
                continue
            qty   = holdings[sym]["qty"]
            entry = holdings[sym]["avg_price"]
            cur   = prices[sym]
            sr = paper_sell(sym, qty, cur, holdings)
            received, fee = sr.received, sr.fee
            cash       += received
            total_fees += fee
            history.append({
                "cycle":     total_steps,
                "timestamp": dt_str,
                "action":    "SELL (liquidation)",
                "symbol":    sym,
                "qty":       round(qty, 6),
                "amount":    round(received, 2),
                "price":     round(cur, 4),
                "pnl":       round((cur - entry) * qty - fee, 4),
                "fee":       round(fee, 6),
                "reason":    "Liquidation finale du backtest",
            })
        last_snap = _make_snapshot(
            total_steps, total_steps, final_ts,
            cash, budget, holdings, prices, history, total_fees, initial_prices,
        )
        timeseries.append({
            "ts":  last_snap["current_ts"],
            "v":   last_snap["total_value"],
            "bh":  last_snap.get("bh_total"),
            "btc": last_snap.get("btc_total"),
        })
        if len(timeseries) > 250:
            step = max(1, len(timeseries) // 200)
            last_snap["timeseries"] = timeseries[::step] + [timeseries[-1]]
        else:
            last_snap["timeseries"] = list(timeseries)
        last_snap["start_ts"] = start_ts_iso
        if skipped:
            last_snap["skipped_symbols"] = skipped
        if excluded_by_tier:
            last_snap["excluded_by_tier"] = excluded_by_tier
        if late_launch:
            last_snap["excluded_late_launch"] = [s for s, _ in late_launch]
        if on_step:
            on_step(last_snap)

    return last_snap or {"error": "Aucune étape traitée"}


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    cfg = load_config()
    parser = argparse.ArgumentParser(description="HelloCrypto backtester")
    parser.add_argument("--symbols",   default=",".join(cfg.get("watchlist", ["BTCUSDC", "ETHUSDC"])))
    parser.add_argument("--start",     default=None, help="Date de début YYYY-MM-DD (défaut: --days ago)")
    parser.add_argument("--days",      type=int,   default=30)
    parser.add_argument("--budget",    type=float, default=float(cfg.get("budget", 1000)))
    parser.add_argument("--stop",      type=float, default=float(cfg.get("stop_loss_pct", 10)))
    parser.add_argument("--trailing",  type=float, default=float(cfg.get("trailing_stop_pct", 5)))
    parser.add_argument("--risk",      type=int,   default=int(cfg.get("risk_level", 3)))
    parser.add_argument("--buy-thr",   type=int,   default=8,
                        help="Score requis pour entrer (sur 10)")
    parser.add_argument("--top-n",     type=int,   default=3,
                        help="Nombre max de positions simultanées")
    parser.add_argument("--decide-every-n", type=int, default=4,
                        help="Cadence du décideur déterministe en bougies 1h "
                             "(4 = décision toutes les 4h, défaut)")
    parser.add_argument("--trend-confirm-hours", type=float, default=24.0,
                        help="Heures de tendance baissière confirmée requises pour exit")
    parser.add_argument("--min-hold-hours", type=float, default=12.0,
                        help="Période min de détention (h) avant tout exit hors stop")
    parser.add_argument("--rebuy-cooldown-hours", type=float, default=0.0,
                        help="Anti-whipsaw : interdiction de racheter pendant N heures "
                             "après un SELL. 0 = désactivé (défaut)")
    parser.add_argument("--llm",       action="store_true", help="Utiliser l'agent LLM (réaliste)")
    parser.add_argument("--llm-every", type=int,   default=4, help="Appel LLM toutes les N bougies")
    args = parser.parse_args()

    syms = [s.strip().upper() for s in args.symbols.split(",")]
    result = run_live(
        symbols              = syms,
        start_date           = args.start,
        days                 = args.days,
        budget               = args.budget,
        stop_loss_pct        = args.stop,
        trailing_stop_pct    = args.trailing,
        risk_level           = args.risk,
        buy_threshold        = args.buy_thr,
        top_n                = args.top_n,
        decide_every_n_candles = args.decide_every_n,
        trend_confirm_hours  = args.trend_confirm_hours,
        min_hold_hours       = args.min_hold_hours,
        rebuy_cooldown_hours = args.rebuy_cooldown_hours,
        llm_mode             = args.llm,
        llm_every_n_candles  = args.llm_every,
    )

    if "error" in result:
        print(f"Erreur: {result['error']}")
        return

    print(f"""
═══ RÉSULTATS DU BACKTEST ═══
Mode         : {'LLM' if args.llm else 'Déterministe (regime_decision)'}
Symboles     : {', '.join(syms)}
Budget       : ${result['total_value'] - result['pnl'] + result.get('pnl',0):,.2f}
Valeur finale: ${result['total_value']:,.2f}
PnL          : {result['pnl']:+.2f} USDC ({result['pnl_pct']:+.2f}%)
Buy & Hold   : {(result.get('benchmark_pnl') or 0):+.2f} USDC
Alpha        : {(result.get('alpha') or 0):+.2f} USDC
Frais        : ${result['total_fees']:.4f}
─────────────────────────────
Trades       : {result['trades']} ({result['buys']} achats / {result['sells']} ventes)
Stop-loss    : {result['stop_losses']}
Win rate     : {result.get('win_rate') or '—'}%
""")

    RESULT_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESULT_FILE.write_text(json.dumps(result, indent=2))
    print(f"Résultat → {RESULT_FILE}")


if __name__ == "__main__":
    main()
