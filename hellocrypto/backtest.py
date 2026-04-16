"""Historical backtester using Binance klines.

Two modes:
- Rule-based (default, fast, free): scoring on RSI + SMA + volatility.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .api import (
    _compute_rsi,
    _compute_sma,
    format_market_data,
    get_btc_dominance,
    get_fear_and_greed,
    load_config,
)
from .llm import call as llm_call
from .prompts import SYSTEM, build_analysis

log = logging.getLogger(__name__)

BASE_URL    = "https://api.binance.com"
FEE_RATE    = 0.001
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
        dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    return int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)


# ── Rule-based signal score ───────────────────────────────────────────────────

def _score_from_window(closes: list[float], volumes: list[float]) -> int:
    """Compute 0-10 signal score from a window of closes/volumes."""
    score = 5
    rsi = _compute_rsi(closes, 14)
    if rsi is not None:
        if rsi < 25:    score += 3
        elif rsi < 35:  score += 2
        elif rsi < 45:  score += 1
        elif rsi < 65:  pass
        elif rsi < 75:  score -= 2
        else:           score -= 3

    sma7  = _compute_sma(closes, 7)
    sma25 = _compute_sma(closes, 25)
    if sma7 and sma25:
        score += 1 if sma7 > sma25 else -1

    if len(closes) >= 24:
        hi  = max(closes[-24:])
        lo  = min(closes[-24:])
        rng = (hi - lo) / lo * 100 if lo else 0
        if rng < 3:   score += 1
        elif rng > 8: score -= 1

    return max(0, min(10, score))


# ── Market-data builder from kline windows (for LLM mode) ────────────────────

def _enrich_from_klines(symbols: list[str], all_klines: dict, i: int) -> dict:
    """Build enriched market-data dict (same shape as get_enriched_market_data)
    from pre-loaded klines at candle index i."""
    result = {}
    for sym in symbols:
        kl    = all_klines[sym]
        start = max(0, i - 50)
        closes  = [float(kl[j][4]) for j in range(start, i + 1)]
        volumes = [float(kl[j][5]) for j in range(start, i + 1)]
        highs   = [float(kl[j][2]) for j in range(max(0, i - 23), i + 1)]
        lows    = [float(kl[j][3]) for j in range(max(0, i - 23), i + 1)]

        price   = closes[-1]
        rsi14   = _compute_rsi(closes, 14)
        sma7    = _compute_sma(closes, 7)
        sma25   = _compute_sma(closes, 25)
        trend   = "hausse" if (sma7 and sma25 and sma7 > sma25) else "baisse"
        hi_24h  = max(highs) if highs else price
        lo_24h  = min(lows)  if lows  else price
        rng_pct = round((hi_24h - lo_24h) / lo_24h * 100, 2) if lo_24h else 0
        vol_24h = sum(volumes[-24:]) if len(volumes) >= 24 else sum(volumes)
        chg_1h  = round((closes[-1] - closes[-2]) / closes[-2] * 100, 2) if len(closes) >= 2 else 0

        result[sym] = {
            "price":          price,
            "rsi14":          rsi14,
            "change_pct_1h":  chg_1h,
            "change_pct_24h": 0.0,
            "volume_usdc":    vol_24h,
            "trend":          trend,
            "trend_1d":       trend,
            "range_pct_24h":  rng_pct,
            "spread_pct":     None,
        }
    return result


# ── Paper trade helpers ───────────────────────────────────────────────────────

def _paper_buy(sym, amount, price, holdings):
    fee     = amount * FEE_RATE
    qty_net = (amount - fee) / price
    if sym in holdings:
        prev = holdings[sym]
        new_qty = prev["qty"] + qty_net
        holdings[sym] = {
            "qty":       new_qty,
            "avg_price": (prev["avg_price"] * prev["qty"] + price * qty_net) / new_qty,
        }
    else:
        holdings[sym] = {"qty": qty_net, "avg_price": price}
    return fee


def _paper_sell(sym, qty, price, holdings):
    qty   = min(qty, holdings.get(sym, {}).get("qty", 0))
    if qty <= 0:
        return 0.0, 0.0
    gross = qty * price
    fee   = gross * FEE_RATE
    holdings[sym]["qty"] -= qty
    if holdings[sym]["qty"] <= 0.0001:
        del holdings[sym]
    return gross - fee, fee


# ── Shared snapshot builder ───────────────────────────────────────────────────

def _make_snapshot(current_step, total_steps, ts_ms, cash, budget, holdings,
                   prices, history, total_fees, initial_prices):
    portfolio_val = sum(h["qty"] * prices.get(s, h["avg_price"]) for s, h in holdings.items())
    total  = cash + portfolio_val
    pnl    = total - budget

    bh_pnl = bh_pct = alpha = None
    btc_bh_pnl = btc_bh_pct = None
    if initial_prices:
        valid  = [(s, p0) for s, p0 in initial_prices.items() if p0 and prices.get(s)]
        if valid:
            w_net  = (budget / len(valid)) * (1 - FEE_RATE)
            bh_val = sum(w_net * prices[s] / p0 for s, p0 in valid)
            bh_pnl = round(bh_val - budget, 2)
            bh_pct = round((bh_val - budget) / budget * 100, 2)
            alpha  = round(pnl - (bh_val - budget), 2)

        btc_sym = next((s for s in initial_prices if "BTC" in s and initial_prices[s] and prices.get(s)), None)
        if btc_sym:
            btc_val    = budget * (1 - FEE_RATE) * prices[btc_sym] / initial_prices[btc_sym]
            btc_bh_pnl = round(btc_val - budget, 2)
            btc_bh_pct = round((btc_val - budget) / budget * 100, 2)

    sells_only = [t for t in history if "SELL" in t.get("action","") and "stop" not in t.get("action","")]
    profitable = [t for t in sells_only if t.get("pnl", 0) > 0]

    return {
        "loading":           False,
        "cycle":             current_step,
        "total_steps":       total_steps,
        "current_ts":        datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M"),
        "cash":              round(cash, 2),
        "portfolio_value":   round(portfolio_val, 2),
        "total_value":       round(total, 2),
        "pnl":               round(pnl, 2),
        "pnl_pct":           round(pnl / budget * 100, 2),
        "total_fees":        round(total_fees, 4),
        "trades":            len([t for t in history if t.get("action") != "ANALYSE"]),
        "buys":              len([t for t in history if t.get("action") == "BUY"]),
        "sells":             len(sells_only),
        "stop_losses":       len([t for t in history if "stop" in t.get("action", "")]),
        "win_rate":          round(len(profitable) / len(sells_only) * 100, 1) if sells_only else None,
        "benchmark_pnl":     bh_pnl,
        "benchmark_pnl_pct": bh_pct,
        "alpha":             alpha,
        "btc_bh_pnl":        btc_bh_pnl,
        "btc_bh_pct":        btc_bh_pct,
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
    buy_threshold: int = 7,
    sell_threshold: int = 3,
    sell_cooldown_cycles: int = 3,
    llm_mode: bool = False,
    llm_every_n_candles: int = 4,
    on_step=None,
    stop_event=None,
    speed_ref: dict | None = None,
) -> dict:
    """Replay historical candles with speed control.

    Args:
        start_date:           ISO date string "YYYY-MM-DD". If None, uses `days` ago.
        llm_mode:             Use the production LLM agent for decisions.
        llm_every_n_candles:  In LLM mode, call the LLM every N candles (throttle).
        speed_ref:            Mutable dict {"value": float} — candles/second.
    """
    stop_loss  = stop_loss_pct  / 100
    trail_stop = trailing_stop_pct / 100
    max_pct    = (5 + risk_level * 4) / 100
    warmup     = 50   # enough for RSI-14, SMA-25, plus buffer

    end_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = _start_ms_from(start_date, days)

    cfg = load_config()

    # ── Phase 1 : fetch klines ────────────────────────────────────────────────
    all_klines: dict[str, list] = {}
    for idx, sym in enumerate(symbols):
        if stop_event and stop_event.is_set():
            return {"error": "stopped"}
        if on_step:
            on_step({"loading": True,
                     "message": f"Chargement {sym} ({idx + 1}/{len(symbols)})…"})
        all_klines[sym] = _fetch_klines(sym, "1h", start_ms, end_ms)

    min_len = min(len(v) for v in all_klines.values()) if all_klines else 0
    if min_len <= warmup:
        return {"error": "Pas assez de données historiques (minimum ~50 bougies)"}

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
                received, fee = _paper_sell(sym, qty, sell_price, holdings)
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
                    market_raw  = _enrich_from_klines(symbols, all_klines, i)
                    from .api import compute_scores
                    scores      = compute_scores(market_raw)
                    market_data = format_market_data(market_raw, symbols)
                    decision = llm_call(
                        prompt=build_analysis(
                            market_data, holdings, cash, budget, risk_level,
                            recent_decisions, fear_greed, btc_dominance, scores,
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
                            rsi = _enrich_from_klines([sym], all_klines, i)[sym].get("rsi14")
                            rsi_factor = max(0.5, min(1.5, 1.5 - (rsi - 20) / 60)) if rsi else 1.0
                            amount = min(action.get("usdc_amount", 0), cash * max_pct * rsi_factor)
                            if amount >= 10:
                                fee     = _paper_buy(sym, amount, prices[sym], holdings)
                                qty_got = (amount - fee) / prices[sym]
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
                            received, fee = _paper_sell(sym, qty, prices[sym], holdings)
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
            # ── Rule-based mode ───────────────────────────────────────────────
            for sym in symbols:
                closes  = [float(all_klines[sym][j][4]) for j in range(max(0, i - 26), i + 1)]
                volumes = [float(all_klines[sym][j][5]) for j in range(max(0, i - 26), i + 1)]
                score   = _score_from_window(closes, volumes)
                cur     = prices[sym]

                if score >= buy_threshold and sym not in holdings and cash > 10:
                    if i - cooldown_map.get(sym, 0) < sell_cooldown_cycles:
                        continue
                    amount  = min(cash * max_pct, cash)
                    fee     = _paper_buy(sym, amount, cur, holdings)
                    qty_got = (amount - fee) / cur
                    total_fees += fee
                    cash       -= amount
                    peak_prices[sym] = cur
                    history.append({
                        "cycle":     current_step,
                        "timestamp": dt_str,
                        "action":    "BUY",
                        "symbol":    sym,
                        "amount":    round(amount, 2),
                        "qty":       round(qty_got, 6),
                        "price":     round(cur, 4),
                        "fee":       round(fee, 6),
                        "score":     score,
                        "reason":    f"Score {score}/10",
                    })

                elif score <= sell_threshold and sym in holdings:
                    qty      = holdings[sym]["qty"]
                    entry    = holdings[sym]["avg_price"]
                    received, fee = _paper_sell(sym, qty, cur, holdings)
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
                        "price":     round(cur, 4),
                        "pnl":       round((cur - entry) * qty - fee, 4),
                        "fee":       round(fee, 6),
                        "score":     score,
                        "reason":    f"Score {score}/10",
                    })

        last_snap = _make_snapshot(
            current_step, total_steps, ts,
            cash, budget, holdings, prices, history, total_fees, initial_prices,
        )
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
        dt_str   = datetime.fromtimestamp(final_ts / 1000, tz=timezone.utc).replace(tzinfo=None).isoformat()
        for sym in list(holdings):
            if sym not in prices:
                continue
            qty   = holdings[sym]["qty"]
            entry = holdings[sym]["avg_price"]
            cur   = prices[sym]
            received, fee = _paper_sell(sym, qty, cur, holdings)
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
    parser.add_argument("--buy-thr",   type=int,   default=7)
    parser.add_argument("--sell-thr",  type=int,   default=3)
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
        sell_threshold       = args.sell_thr,
        llm_mode             = args.llm,
        llm_every_n_candles  = args.llm_every,
    )

    if "error" in result:
        print(f"Erreur: {result['error']}")
        return

    print(f"""
═══ RÉSULTATS DU BACKTEST ═══
Mode         : {'LLM' if args.llm else 'Règles (rule-based)'}
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
