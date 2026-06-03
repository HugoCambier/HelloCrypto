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
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

from .api import (
    _compute_bollinger,
    _compute_macd,
    _compute_rsi,
    _compute_sma,
    format_market_data,
    get_btc_dominance,
    get_fear_and_greed,
    load_config,
)
from .deciders import regime_decision
from .llm import call as llm_call
from .prompts import SYSTEM, build_analysis
from .trading import FEE_RATE, paper_buy, paper_sell

log = logging.getLogger(__name__)

BASE_URL    = "https://api.binance.com"
RESULT_FILE = Path("data/backtest_result.json")


# ── Kline fetcher ─────────────────────────────────────────────────────────────

def _fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> list:
    """Fetch all klines for symbol between start_ms and end_ms (paginated)."""
    candles = []
    while start_ms < end_ms:
        r = requests.get(
            f"{BASE_URL}/api/v3/klines",
            params={"symbol": symbol, "interval": interval,
                    "startTime": start_ms, "endTime": end_ms, "limit": 1000},
            timeout=15,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        candles.extend(batch)
        start_ms = int(batch[-1][0]) + 1
        if len(batch) < 1000:
            break
        time.sleep(0.1)
    return candles


def _start_ms_from(start_date: str | None, days: int) -> int:
    """Return epoch-ms for start of backtest window."""
    if start_date:
        dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
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
        kl_1d = all_klines_1d.get(sym, [])
        start = max(0, i - 49)
        closes  = [float(kl[j][4]) for j in range(start, i + 1)]
        volumes = [float(kl[j][5]) for j in range(start, i + 1)]
        highs   = [float(kl[j][2]) for j in range(max(0, i - 23), i + 1)]
        lows    = [float(kl[j][3]) for j in range(max(0, i - 23), i + 1)]
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
    candle_low = float(all_klines[sym][i][3])
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
    speed_ref: dict | None = None,
) -> dict:
    """Replay historical candles with speed control.

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
        speed_ref:              Mutable dict {"value": float} — candles/second.
    """
    stop_loss  = stop_loss_pct  / 100
    trail_stop = trailing_stop_pct / 100
    max_pct    = (5 + risk_level * 4) / 100
    warmup     = 50   # enough for RSI-14, SMA-25, plus buffer

    end_ms   = int(datetime.now(UTC).timestamp() * 1000)
    start_ms = _start_ms_from(start_date, days)
    # 1d klines need 25+ finalized candles before the run starts so the daily
    # SMA25 is warm on the first decision; pull 30 extra days to be safe.
    start_ms_1d = start_ms - 30 * 86_400_000

    cfg = load_config()

    # ── Phase 1 : fetch klines (1h pour la replay, 1d pour le trend daily) ───
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

    # Drop symbols with insufficient history so one bad pair doesn't abort the run
    skipped = [s for s, k in all_klines.items() if len(k) <= warmup]
    for s in skipped:
        log.warning("[BACKTEST] %s: %d bougies (< %d), exclu du run", s, len(all_klines[s]), warmup)
        del all_klines[s]
    symbols = [s for s in symbols if s in all_klines]

    if not symbols:
        return {"error": "Aucun symbole avec suffisamment de données (min ~50 bougies)"}

    min_len = min(len(v) for v in all_klines.values())
    max_len = max(len(v) for v in all_klines.values())
    # When one symbol stops trading (low-volume gaps) before the others, min_len
    # truncates everyone to the shortest series — silently cutting hours off
    # the end of the run. Identify the bottleneck so the user sees why.
    tail_truncated_hours = max(0, max_len - min_len)
    tail_bottleneck = [
        s for s, k in all_klines.items() if len(k) == min_len
    ] if tail_truncated_hours > 0 else []
    skipped_msg = f" — {len(skipped)} crypto(s) exclue(s): {', '.join(skipped)}" if skipped else ""
    if skipped_msg:
        log.info("[BACKTEST] Run sur %d symbole(s)%s", len(symbols), skipped_msg)
    if tail_truncated_hours > 0:
        log.warning(
            "[BACKTEST] Période tronquée de %dh à la fin par %s (min_len=%d vs max_len=%d)",
            tail_truncated_hours, ",".join(tail_bottleneck), min_len, max_len,
        )

    total_steps   = min_len - warmup
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
        int(all_klines[symbols[0]][warmup][0]) / 1000
    ).strftime("%Y-%m-%d %H:%M") if all_klines else None

    # Pre-fetch global context once (cached, doesn't change during replay)
    fear_greed    = get_fear_and_greed()
    btc_dominance = get_btc_dominance()

    llm_call_count = 0
    llm_last_error = ""

    for i in range(warmup, min_len):
        if stop_event and stop_event.is_set():
            break

        ts           = int(all_klines[symbols[0]][i][0])
        prices       = {sym: float(all_klines[sym][i][4]) for sym in symbols}
        current_step = i - warmup + 1

        if not initial_prices:
            initial_prices = dict(prices)

        # Update peak prices
        for sym in list(holdings):
            if sym in prices:
                peak_prices[sym] = max(peak_prices.get(sym, prices[sym]), prices[sym])

        # Stop-loss (hard + trailing)
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
                            recent_decisions, fear_greed, btc_dominance, scores,
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
            )
            actions = decision.get("actions", [])
            scores  = decision.get("scores", {}) or {}

            # Sells first — frees cash for the buys below.
            for a in actions:
                sym = a.get("symbol")
                if a.get("type") != "sell" or sym not in holdings or sym not in prices:
                    continue
                cur   = prices[sym]
                qty   = holdings[sym]["qty"]
                entry = holdings[sym]["avg_price"]
                sr    = paper_sell(sym, qty, cur, holdings)
                total_fees += sr.fee
                cash       += sr.received
                peak_prices.pop(sym, None)
                cooldown_map[sym] = i
                history.append({
                    "cycle":     current_step,
                    "timestamp": dt_str,
                    "action":    "SELL",
                    "symbol":    sym,
                    "qty":       round(qty, 6),
                    "amount":    round(sr.received, 2),
                    "price":     round(cur, 4),
                    "pnl":       round((cur - entry) * qty - sr.fee, 4),
                    "fee":       round(sr.fee, 6),
                    "score":     scores.get(sym),
                    "reason":    a.get("reason", ""),
                })

            # Buys: decider already computed risk-aware usdc_amount per action.
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
        if tail_truncated_hours > 0:
            last_snap["tail_truncated_hours"] = tail_truncated_hours
            last_snap["tail_bottleneck"]     = tail_bottleneck
        if llm_mode:
            last_snap["llm_calls"] = llm_call_count
            if llm_last_error:
                last_snap["llm_last_error"] = llm_last_error

        if on_step:
            on_step(last_snap)

        # In LLM mode the LLM call already took real time — no extra sleep needed
        if not llm_mode:
            speed     = speed_ref["value"] if speed_ref else 10.0
            sleep_sec = max(0.005, 1.0 / speed)
            if stop_event:
                stop_event.wait(timeout=sleep_sec)
            else:
                time.sleep(sleep_sec)

    # ── Final liquidation: sell all remaining positions at last price ─────────
    if holdings and prices:
        final_ts = int(all_klines[symbols[0]][min_len - 1][0])
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
        if tail_truncated_hours > 0:
            last_snap["tail_truncated_hours"] = tail_truncated_hours
            last_snap["tail_bottleneck"]     = tail_bottleneck
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
