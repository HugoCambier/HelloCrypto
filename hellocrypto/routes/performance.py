"""Performance & watchlist API."""
from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from flask import Blueprint, jsonify, request

from ..api import compute_scores, get_enriched_market_data, load_config, load_history
from ..backtest import _fetch_klines
from .shared import PERIODS

log = logging.getLogger(__name__)

bp = Blueprint("performance", __name__)

_FEE_RATE = 0.001  # 0.1% per trade
_BENCH_CACHE: dict = {}
_BENCH_TTL  = 600  # seconds (benchmarks change slowly; aggressive cache)


def _compute_benchmarks(start_iso: str, watchlist: list[str], budget: float) -> dict:
    """Compute BH + BTC benchmark timeseries from Binance klines.

    Returns ``{"bh": [{ts, v}], "btc": [{ts, v}]}`` where v is the portfolio
    value (USDC equivalent) at each hourly candle.
    """
    cache_key = (start_iso, tuple(watchlist), round(budget, 2))
    now = time.time()
    cached = _BENCH_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _BENCH_TTL:
        return cached[1]

    try:
        start_dt = datetime.fromisoformat(start_iso)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=UTC)
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms   = int(datetime.now(UTC).timestamp() * 1000)
    except Exception:
        return {"bh": [], "btc": []}

    # Cap symbols to avoid storms (BTC + up to 9 watchlist coins)
    symbols = list(dict.fromkeys(["BTCUSDC"] + list(watchlist)))[:10]
    klines: dict[str, list] = {}
    for sym in symbols:
        try:
            klines[sym] = _fetch_klines(sym, "1h", start_ms, end_ms)
        except Exception:
            klines[sym] = []

    btc_kl = klines.get("BTCUSDC", [])
    bh_syms = [s for s in watchlist if klines.get(s)]
    if not btc_kl and not bh_syms:
        result = {"bh": [], "btc": []}
        _BENCH_CACHE[cache_key] = (now, result)
        return result

    # BTC benchmark
    btc_ts = []
    if btc_kl:
        p0 = float(btc_kl[0][4])
        for k in btc_kl:
            ts_iso = datetime.utcfromtimestamp(int(k[0]) / 1000).isoformat()
            v = budget * (1 - _FEE_RATE) * float(k[4]) / p0
            btc_ts.append({"ts": ts_iso, "v": round(v, 2)})

    # Buy & Hold benchmark — equal-weight split across watchlist
    bh_ts = []
    if bh_syms:
        initial = {s: float(klines[s][0][4]) for s in bh_syms}
        w_net = (budget / len(bh_syms)) * (1 - _FEE_RATE)
        # Align on shortest series
        min_len = min(len(klines[s]) for s in bh_syms)
        for i in range(min_len):
            ts_iso = datetime.utcfromtimestamp(int(klines[bh_syms[0]][i][0]) / 1000).isoformat()
            v = sum(w_net * float(klines[s][i][4]) / initial[s] for s in bh_syms)
            bh_ts.append({"ts": ts_iso, "v": round(v, 2)})

    result = {"bh": bh_ts, "btc": btc_ts}
    _BENCH_CACHE[cache_key] = (now, result)
    return result


@bp.get("/api/watchlist")
def api_watchlist():
    cfg = load_config()
    return jsonify({
        "watchlist":             cfg.get("watchlist", []),
        "stop_loss_pct":         float(cfg.get("stop_loss_pct", 10)),
        "trailing_stop_pct":     float(cfg.get("trailing_stop_pct", 5)),
        "budget":                float(cfg.get("budget", 1000)),
        "risk_level":            int(cfg.get("risk_level", 5)),
        "sell_cooldown_cycles":  int(cfg.get("sell_cooldown_cycles", 3)),
    })


@bp.get("/api/watchlist/enriched")
def api_watchlist_enriched():
    """Return watchlist symbols with inline market indicators."""
    try:
        cfg = load_config()
        watchlist = cfg.get("watchlist", [])
        data = get_enriched_market_data(watchlist, cycle_seconds=300)
        scores = compute_scores(data)
        items = []
        for sym in watchlist:
            d = data.get(sym)
            if not d:
                continue
            items.append({
                "symbol":          sym,
                "price":           d.get("price"),
                "change_pct_24h":  d.get("change_pct_24h"),
                "rsi14":           d.get("rsi14"),
                "sma7":            d.get("sma7"),
                "sma25":           d.get("sma25"),
                "trend":           d.get("trend"),
                "volume_usdc":     d.get("volume_usdc"),
                "score":           scores.get(sym) if scores else None,
            })
        return jsonify({"items": items})
    except Exception as exc:
        log.warning("Enriched watchlist error: %s", exc)
        return jsonify({"items": [], "error": str(exc)}), 200


@bp.get("/api/performance")
def api_performance():
    period         = request.args.get("period", "all")
    mode           = request.args.get("mode", "real")
    session_id     = request.args.get("session_id")
    with_bench     = request.args.get("with_benchmarks", "1") not in ("0", "false", "no")
    config         = load_config()

    # Resolve the actual budget / watchlist / start anchor for the selected session.
    # Falls back to the current config when no session is selected (real mode or fresh view).
    sess_budget: float | None    = None
    sess_watchlist: list | None  = None
    sess_started_at: str | None  = None
    if session_id:
        try:
            from db.store import get_session as _get_session
            sess = _get_session(session_id)
        except ImportError:
            sess = None
        if sess:
            import json as _json
            init_st = sess.get("initial_state")
            if isinstance(init_st, str):
                try:
                    init_st = _json.loads(init_st)
                except Exception:
                    init_st = None
            if isinstance(init_st, dict):
                # Prefer initial_total_value (= cash + seeded positions at entry) over raw budget,
                # since that's what the strategy PnL is measured against.
                sess_budget    = init_st.get("initial_total_value") or init_st.get("budget")
                sess_watchlist = init_st.get("watchlist")
            sess_started_at = sess.get("created_at")

    try:
        from db.store import load_history as _db_load
        history = _db_load(mode=mode, limit=2000)
    except ImportError:
        history = load_history()

    cutoff   = datetime.utcnow() - PERIODS.get(period, PERIODS["all"])
    filtered = [t for t in history if datetime.fromisoformat(t["timestamp"]) >= cutoff]
    if session_id:
        filtered = [t for t in filtered if t.get("session_id") == session_id]

    buys        = [t for t in filtered if t["action"] == "BUY"]
    sells       = [t for t in filtered if "SELL" in t["action"] and "stop" not in t["action"].lower()]
    stop_losses = [t for t in filtered if "stop" in t["action"].lower()]
    all_sells   = sells + stop_losses

    invested   = sum(t.get("amount", 0) or 0 for t in buys)
    recovered  = sum((t.get("qty", 0) or 0) * (t.get("price", 0) or 0) for t in all_sells)
    fees       = sum(t.get("fee", 0) or 0 for t in filtered)
    # Only sell fees reduce net cash: buy fees are already embedded in the reduced qty received
    sell_fees  = sum(t.get("fee", 0) or 0 for t in all_sells)
    net        = round(recovered - invested - sell_fees, 2)

    sells_pnl  = [t for t in all_sells if t.get("pnl") is not None]
    profitable = [t for t in sells_pnl if t["pnl"] > 0]
    win_rate   = round(len(profitable) / len(all_sells) * 100, 1) if all_sells else None
    best_trade  = round(max(t["pnl"] for t in sells_pnl), 2) if sells_pnl else None
    worst_trade = round(min(t["pnl"] for t in sells_pnl), 2) if sells_pnl else None

    sorted_trades = sorted(filtered, key=lambda t: t["timestamp"])
    timeseries, cum = [], 0.0
    for t in sorted_trades:
        if t["action"] == "BUY":
            # Buy fee is embedded in reduced qty — subtract only the USDC spent
            cum -= t.get("amount", 0) or 0
        elif "SELL" in t["action"].upper():
            cum += (t.get("qty", 0) or 0) * (t.get("price", 0) or 0) - (t.get("fee", 0) or 0)
        timeseries.append({"ts": t["timestamp"], "v": round(cum, 2)})

    sessions = []
    if mode == "simulation" and sorted_trades:
        session_start = sorted_trades[0]["timestamp"]
        prev_ts = session_start
        for t in sorted_trades[1:]:
            gap = datetime.fromisoformat(t["timestamp"]) - datetime.fromisoformat(prev_ts)
            if gap.total_seconds() > 7200:
                sessions.append({"start": session_start, "end": prev_ts})
                session_start = t["timestamp"]
            prev_ts = t["timestamp"]
        sessions.append({"start": session_start, "end": prev_ts})

    # Benchmark timeseries (BH + BTC) — anchor on the session start when known,
    # else on the first trade. Use the session's own budget + watchlist so the
    # curves are measured against the same capital as the strategy.
    bh_ts: list = []
    btc_ts: list = []
    effective_budget    = float(sess_budget if sess_budget is not None else config.get("budget", 100))
    effective_watchlist = sess_watchlist if sess_watchlist else config.get("watchlist", [])
    if with_bench and (sorted_trades or sess_started_at):
        start_iso = sess_started_at or sorted_trades[0]["timestamp"]
        try:
            bench = _compute_benchmarks(start_iso, effective_watchlist, effective_budget)
            bh_ts  = bench.get("bh", [])
            btc_ts = bench.get("btc", [])
        except Exception:
            log.exception("Failed to compute benchmarks")

    return jsonify({
        "period":         period,
        "mode":           mode,
        "trades":         len(filtered),
        "buys":           len(buys),
        "sells":          len(sells),
        "stop_losses":    len(stop_losses),
        "invested":       round(invested, 2),
        "recovered":      round(recovered, 2),
        "fees":           round(fees, 4),
        "net":            net,
        "win_rate":       win_rate,
        "best_trade":     best_trade,
        "worst_trade":    worst_trade,
        "history":        list(reversed(sorted_trades[-200:])),
        "timeseries":     timeseries,
        "bh_timeseries":  bh_ts,
        "btc_timeseries": btc_ts,
        "sessions":       sessions,
        "budget":         round(effective_budget, 2),
    })
