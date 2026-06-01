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


def _load_close_series(symbol: str, start_iso: str, end_iso: str,
                       start_ms: int, end_ms: int) -> list:
    """Return a kline-shaped close series for ``symbol`` in ``[start, end]``.

    Source priority:
      1. ``price_snapshots`` rows (5m preferred at same minute, else 1h).
      2. Live Binance fetch (``_fetch_klines`` 1h) if DB returns < 2 rows.

    Output shape ``[[open_time_ms, None, None, None, close, None], ...]`` to
    stay drop-in compatible with the legacy BH/BTC logic that only reads
    ``k[0]`` and ``k[4]``. Falling back to Binance preserves behavior on
    cold DB / missing-symbol cases.
    """
    from db.snapshots import load_snapshots

    rows = load_snapshots(symbol=symbol, start_ts=start_iso,
                          end_ts=end_iso, limit=20000)
    if rows and len(rows) >= 2:
        by_ts: dict[str, dict] = {}
        for r in rows:
            ts    = r.get("timestamp")
            close = r.get("close")
            if ts is None or close is None:
                continue
            existing = by_ts.get(ts)
            if existing is None or r.get("interval") == "5m":
                by_ts[ts] = r
        out: list = []
        for ts in sorted(by_ts):
            r = by_ts[ts]
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                ts_ms = int(dt.timestamp() * 1000)
            except Exception:
                continue
            out.append([ts_ms, None, None, None, r["close"], None])
        if len(out) >= 2:
            return out
    return _fetch_klines(symbol, "1h", start_ms, end_ms)


def _compute_benchmarks(start_iso: str, watchlist: list[str], budget: float,
                        end_iso: str | None = None) -> dict:
    """Compute BH + BTC benchmark timeseries from Binance klines.

    Returns ``{"bh": [{ts, v}], "btc": [{ts, v}]}`` where v is the portfolio
    value (USDC equivalent) at each hourly candle.

    ``end_iso`` clips the benchmark series at a specific timestamp (for
    finished sessions). Without it, benchmarks extend to "now", which is
    correct for live runs but creates a mismatch between chart and KPI cards
    on finished sessions (chart aligns bench to strategy's last point, KPIs
    use the last bench point — they differ when ``now > session_end``).
    """
    cache_key = (start_iso, end_iso, tuple(watchlist), round(budget, 2))
    now = time.time()
    cached = _BENCH_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _BENCH_TTL:
        return cached[1]

    try:
        start_dt = datetime.fromisoformat(start_iso)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=UTC)
        start_ms = int(start_dt.timestamp() * 1000)
        if end_iso:
            end_dt = datetime.fromisoformat(end_iso)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=UTC)
            end_ms = int(end_dt.timestamp() * 1000)
        else:
            end_ms = int(datetime.now(UTC).timestamp() * 1000)
    except Exception:
        return {"bh": [], "btc": [], "bh_breakdown": [], "btc_breakdown": None}

    # Cap symbols to avoid storms (BTC + up to 9 watchlist coins)
    symbols = list(dict.fromkeys(["BTCUSDC"] + list(watchlist)))[:10]
    # Resolve end ISO for DB query (mirror end_ms)
    end_iso_db = (
        end_iso
        if end_iso
        else datetime.utcfromtimestamp(end_ms / 1000).isoformat()
    )
    klines: dict[str, list] = {}
    for sym in symbols:
        try:
            klines[sym] = _load_close_series(sym, start_iso, end_iso_db,
                                             start_ms, end_ms)
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
    bh_breakdown: list = []
    if bh_syms:
        initial = {s: float(klines[s][0][4]) for s in bh_syms}
        share   = budget / len(bh_syms)
        w_net   = share * (1 - _FEE_RATE)
        min_len = min(len(klines[s]) for s in bh_syms)
        for i in range(min_len):
            ts_iso = datetime.utcfromtimestamp(int(klines[bh_syms[0]][i][0]) / 1000).isoformat()
            v = sum(w_net * float(klines[s][i][4]) / initial[s] for s in bh_syms)
            bh_ts.append({"ts": ts_iso, "v": round(v, 2)})
        # Per-symbol contribution at the LAST point (what the card shows)
        for s in bh_syms:
            init_p  = initial[s]
            final_p = float(klines[s][min_len - 1][4])
            value   = w_net * final_p / init_p
            pnl     = value - share
            bh_breakdown.append({
                "symbol":   s,
                "weight":   round(share, 2),
                "initial":  init_p,
                "final":    final_p,
                "value":    round(value, 2),
                "pnl":      round(pnl, 2),
                "pnl_pct":  round((pnl / share) * 100, 2) if share else 0,
            })

    # BTC single-coin breakdown
    btc_breakdown: dict | None = None
    if btc_kl:
        init_p  = float(btc_kl[0][4])
        final_p = float(btc_kl[-1][4])
        value   = budget * (1 - _FEE_RATE) * final_p / init_p
        btc_breakdown = {
            "symbol":  "BTCUSDC",
            "weight":  round(budget, 2),
            "initial": init_p,
            "final":   final_p,
            "value":   round(value, 2),
            "pnl":     round(value - budget, 2),
            "pnl_pct": round((value - budget) / budget * 100, 2) if budget else 0,
        }

    result = {"bh": bh_ts, "btc": btc_ts,
              "bh_breakdown":  bh_breakdown,
              "btc_breakdown": btc_breakdown}
    _BENCH_CACHE[cache_key] = (now, result)
    return result


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
    #
    # End anchor: when the session is finished (not currently armed), clip the
    # bench at the session's last trade so chart and KPI cards agree. For
    # active runs we let it extend to "now" so the strategy's live value can
    # be compared against passive at the same horizon.
    bh_ts: list = []
    btc_ts: list = []
    bh_breakdown: list = []
    btc_breakdown: dict | None = None
    effective_budget    = float(sess_budget if sess_budget is not None else config.get("budget", 100))
    effective_watchlist = sess_watchlist if sess_watchlist else config.get("watchlist", [])
    if with_bench and (sorted_trades or sess_started_at):
        start_iso = sess_started_at or sorted_trades[0]["timestamp"]
        end_iso: str | None = None
        if session_id:
            try:
                from db.store import get_state as _get_state
                active_real = _get_state("active_real_session_id") or None
                active_sims_state = _get_state("active_sims") or {}
                is_active = (
                    session_id == active_real
                    or (isinstance(active_sims_state, dict)
                        and session_id in active_sims_state)
                )
            except Exception:
                is_active = False
            if not is_active and sorted_trades:
                end_iso = sorted_trades[-1]["timestamp"]
        try:
            bench = _compute_benchmarks(start_iso, effective_watchlist,
                                        effective_budget, end_iso=end_iso)
            bh_ts          = bench.get("bh", [])
            btc_ts         = bench.get("btc", [])
            bh_breakdown   = bench.get("bh_breakdown") or []
            btc_breakdown  = bench.get("btc_breakdown")
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
        "bh_breakdown":   bh_breakdown,
        "btc_breakdown":  btc_breakdown,
        "sessions":       sessions,
        "budget":         round(effective_budget, 2),
    })
