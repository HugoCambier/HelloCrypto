"""Performance & watchlist API."""
from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from flask import Blueprint, jsonify, request

from ..api import (
    compute_scores,
    get_enriched_market_data,
    load_config,
    load_history,
)
from ..backtest import _fetch_klines
from ..deciders import _derive_stance
from .shared import PERIODS

log = logging.getLogger(__name__)

bp = Blueprint("performance", __name__)

_BENCH_CACHE: dict = {}
_BENCH_TTL  = 3600  # seconds — bumped from 600s: BH/BTC benchmarks shift on
# the hourly grid, so refreshing once an hour is enough; previous 10-min TTL
# meant ~6 unnecessary recomputes per hour, each triggering a multi-symbol
# scan on price_snapshots.

# Computed-response cache for /api/performance. The endpoint is polled every 60s
# and its payload (equity reconstruction + benchmarks + price series) is pure CPU
# to rebuild. We key on the request shape + the trades generation counter so any
# new trade busts it instantly within the process; a TTL bounds staleness across
# serverless instances. A finished run's payload never changes between trades, so
# it caches long; an active run only advances on the 5-min cycle grid, so a short
# TTL is loss-free in practice while still collapsing multi-tab / repeated polls.
_PERF_CACHE: dict = {}
_PERF_TTL_ACTIVE   = 90.0
_PERF_TTL_FINISHED = 600.0


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

    # Project only the three columns this function actually reads — drops
    # per-row egress from ~600 B (full snapshot) to ~60 B.
    rows = load_snapshots(symbol=symbol, start_ts=start_iso,
                          end_ts=end_iso, limit=20000,
                          columns=["timestamp", "close", "interval"])
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

    # Benchmarks anchor at the full budget so all three curves start at PnL 0 at
    # t0 — like the strategy, which holds only cash before its first trade. The
    # 0.1% entry fee is omitted from the passive baselines on purpose: a level
    # shift there would make the curves "start" below zero.

    # BTC benchmark
    btc_ts = []
    if btc_kl:
        p0 = float(btc_kl[0][4])
        for k in btc_kl:
            ts_iso = datetime.utcfromtimestamp(int(k[0]) / 1000).isoformat()
            v = budget * float(k[4]) / p0
            btc_ts.append({"ts": ts_iso, "v": round(v, 2)})

    # Buy & Hold benchmark — equal-weight split across watchlist
    bh_ts = []
    bh_breakdown: list = []
    if bh_syms:
        initial = {s: float(klines[s][0][4]) for s in bh_syms}
        share   = budget / len(bh_syms)
        min_len = min(len(klines[s]) for s in bh_syms)
        for i in range(min_len):
            ts_iso = datetime.utcfromtimestamp(int(klines[bh_syms[0]][i][0]) / 1000).isoformat()
            v = sum(share * float(klines[s][i][4]) / initial[s] for s in bh_syms)
            bh_ts.append({"ts": ts_iso, "v": round(v, 2)})
        # Per-symbol contribution at the LAST point (what the card shows)
        for s in bh_syms:
            init_p  = initial[s]
            final_p = float(klines[s][min_len - 1][4])
            value   = share * final_p / init_p
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
        value   = budget * final_p / init_p
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


def _iso_to_ms(iso: str) -> int:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _strategy_value_series(trades: list, cycle_timestamps: list[str],
                           budget: float) -> list:
    """Dense mark-to-market equity curve: ``cash + Σ qty × market_price`` per cycle.

    Walks the decision cycles, applies each trade as it occurs, and prices held
    positions at the market close captured for that cycle (``price_snapshots`` —
    the same source as the BH/BTC benchmarks). This is the definition the backtest
    and the live sim already use; reconstructing it here lets finished sims and
    real runs share it instead of valuing positions at their stale entry price.

    A symbol with no snapshot at a given cycle falls back to its last trade price,
    so the curve degrades gracefully rather than dropping the position to $0.
    Returns ``[]`` when the cycle grid is unavailable (e.g. Firestore / purged
    logs); the caller then falls back to the client-side trade reconstruction.
    """
    if not cycle_timestamps or not trades:
        return []

    import bisect

    sorted_trades = sorted(trades, key=lambda t: t["timestamp"])
    symbols = {t["symbol"] for t in sorted_trades if t.get("symbol")}
    start_ms = _iso_to_ms(cycle_timestamps[0])
    end_ms   = _iso_to_ms(cycle_timestamps[-1])

    # Per-symbol close series (ascending), same source/path as the benchmarks.
    grid: dict[str, tuple[list[int], list[float]]] = {}
    for sym in symbols:
        try:
            kl = _load_close_series(sym, cycle_timestamps[0], cycle_timestamps[-1],
                                    start_ms, end_ms)
        except Exception:
            kl = []
        if kl:
            grid[sym] = ([int(k[0]) for k in kl], [float(k[4]) for k in kl])

    def price_at(sym: str, ms: int, fallback: float) -> float:
        g = grid.get(sym)
        if not g:
            return fallback
        times, closes = g
        i = bisect.bisect_right(times, ms) - 1
        return closes[i] if i >= 0 else fallback

    cash = budget
    holdings: dict[str, float] = {}
    last_px: dict[str, float] = {}

    def apply(t: dict) -> None:
        nonlocal cash
        sym = t.get("symbol")
        if not sym:
            return
        if t.get("price"):
            last_px[sym] = float(t["price"])
        qty = float(t.get("qty") or 0)
        amount = float(t["amount"]) if t.get("amount") is not None else qty * float(t.get("price") or 0)
        if "BUY" in t["action"].upper():
            cash -= amount
            holdings[sym] = holdings.get(sym, 0.0) + qty
        elif "SELL" in t["action"].upper():
            cash += amount
            holdings[sym] = holdings.get(sym, 0.0) - qty
        if holdings.get(sym, 0.0) <= 1e-8:
            holdings.pop(sym, None)

    def snapshot_value(ms: int) -> float:
        return cash + sum(q * price_at(sym, ms, last_px.get(sym, 0.0))
                          for sym, q in holdings.items())

    points: list = []
    ti = 0
    for cts in cycle_timestamps:
        cms = _iso_to_ms(cts)
        while ti < len(sorted_trades) and _iso_to_ms(sorted_trades[ti]["timestamp"]) <= cms:
            apply(sorted_trades[ti])
            ti += 1
        points.append({"ts": cts, "v": round(snapshot_value(cms), 2)})
    # Trades after the last recorded cycle still move the curve.
    while ti < len(sorted_trades):
        t = sorted_trades[ti]
        apply(t)
        points.append({"ts": t["timestamp"], "v": round(snapshot_value(_iso_to_ms(t["timestamp"])), 2)})
        ti += 1
    return points


def _symbol_price_series(symbols: list[str], cycle_timestamps: list[str]) -> list:
    """Per-cycle close series for each symbol, aligned to the cycle grid.

    Returns ``[{ts, prices: {symbol: close}}, ...]`` — one frame per decision
    cycle, each carrying the market close (from ``price_snapshots``, the same
    source as the benchmarks) for every symbol that traded. Drives the
    per-crypto momentum chart; a symbol missing a snapshot at a given cycle is
    simply omitted from that frame so the curve degrades gracefully.
    """
    if not symbols or not cycle_timestamps:
        return []

    import bisect

    start_ms = _iso_to_ms(cycle_timestamps[0])
    end_ms   = _iso_to_ms(cycle_timestamps[-1])
    grid: dict[str, tuple[list[int], list[float]]] = {}
    for sym in symbols:
        try:
            kl = _load_close_series(sym, cycle_timestamps[0], cycle_timestamps[-1],
                                    start_ms, end_ms)
        except Exception:
            kl = []
        if kl:
            grid[sym] = ([int(k[0]) for k in kl], [float(k[4]) for k in kl])

    out: list = []
    for cts in cycle_timestamps:
        cms = _iso_to_ms(cts)
        prices: dict[str, float] = {}
        for sym, (times, closes) in grid.items():
            i = bisect.bisect_right(times, cms) - 1
            if i >= 0:
                prices[sym] = round(closes[i], 8)
        if prices:
            out.append({"ts": cts, "prices": prices})
    return out


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


def _latest_snapshot_ts() -> str | None:
    """Return the most recent ``price_snapshots`` timestamp (5m preferred, 1h fallback).

    Reflects the last time the live capture cycle refreshed indicators, which is
    what the cockpit shows next to the ``● live`` badge.
    """
    from db.snapshots import _USE_FIRESTORE, _USE_POSTGRES
    if _USE_FIRESTORE:
        return None
    ph = "%s" if _USE_POSTGRES else "?"
    sql = f"SELECT MAX(timestamp) AS ts FROM price_snapshots WHERE interval={ph}"
    try:
        if _USE_POSTGRES:
            from db.store import _postgres
            with _postgres() as c:
                for interval in ("5m", "1h"):
                    c.execute(sql, (interval,))
                    row = c.fetchone()
                    ts = (dict(row).get("ts") if row else None)
                    if ts:
                        return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            return None
        from db.store import _sqlite
        with _sqlite() as c:
            for interval in ("5m", "1h"):
                row = c.execute(sql, (interval,)).fetchone()
                ts = (dict(row).get("ts") if row else None)
                if ts:
                    return str(ts)
        return None
    except Exception:
        return None


def _context_live(watchlist: list[str]) -> dict:
    """Market context from the latest captured snapshots (DB), not live Binance.

    The dashboard must not drive Binance traffic that could rate-limit the
    shared IP/key and starve the decision cron — so the live context card reads
    the freshest captured row per symbol (~2 KB) instead of refetching. The
    drawdown-based CASH stance gate isn't available from a single-row read
    (no 7d window); ``_derive_stance`` degrades to its trend-breadth path.
    """
    if not watchlist:
        return {"live": False, "as_of_ts": _latest_snapshot_ts(), "stance": None,
                "btc_dominance": None, "fng_value": None, "fng_label": None,
                "symbols": []}
    from db.snapshots import latest_snapshot_rows
    rows = latest_snapshot_rows(
        watchlist,
        columns=["timestamp", "trend", "trend_1d", "score",
                 "fng_value", "fng_label", "btc_dominance"],
    )
    market_raw = {
        sym: {"trend": r.get("trend"), "trend_1d": r.get("trend_1d")}
        for sym, r in rows.items()
    }
    stance = _derive_stance(market_raw) if market_raw else None
    btc = rows.get("BTCUSDC") or {}
    return {
        "live":          True,
        "as_of_ts":      _latest_snapshot_ts() or (datetime.utcnow().isoformat() + "Z"),
        "stance":        stance,
        "btc_dominance": btc.get("btc_dominance"),
        "fng_value":     btc.get("fng_value"),
        "fng_label":     btc.get("fng_label"),
        "symbols": [
            {
                "symbol":   sym,
                "score":    rows[sym].get("score"),
                "trend":    rows[sym].get("trend"),
                "trend_1d": rows[sym].get("trend_1d"),
            }
            for sym in watchlist if sym in rows
        ],
    }


def _context_at(at_ts: str, watchlist: list[str]) -> dict:
    """Reconstruct context from price_snapshots at *at_ts* (closest snapshot ≤ ts)."""
    from datetime import timedelta

    from db.snapshots import _USE_POSTGRES

    try:
        end_dt = datetime.fromisoformat(at_ts.replace("Z", "+00:00"))
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=UTC)
    except Exception:
        return {"error": f"Invalid 'at' timestamp: {at_ts}", "symbols": []}

    start_dt = end_dt - timedelta(hours=168)
    if not watchlist:
        return {"live": False, "as_of_ts": at_ts, "stance": None,
                "btc_dominance": None, "fng_value": None, "fng_label": None,
                "symbols": []}

    ph = "%s" if _USE_POSTGRES else "?"
    in_clause = ",".join([ph] * len(watchlist))
    # Only the columns _context_at actually reads — drops payload from ~600 B
    # to ~110 B per row (8 syms × 168h ≈ 800 KB → ~140 KB per call).
    sql = (
        f"SELECT symbol, timestamp, high, close, trend, trend_1d, score, "
        f"fng_value, fng_label, btc_dominance "
        f"FROM price_snapshots "
        f"WHERE symbol IN ({in_clause}) "
        f"AND timestamp >= {ph} AND timestamp <= {ph} "
        f"ORDER BY symbol, timestamp ASC"
    )
    params = (*watchlist, start_dt.isoformat(), end_dt.isoformat())

    if _USE_POSTGRES:
        from db.store import _postgres
        with _postgres() as c:
            c.execute(sql, params)
            rows = [dict(r) for r in c.fetchall()]
    else:
        from db.store import _sqlite
        with _sqlite() as c:
            rows = [dict(r) for r in c.execute(sql, params).fetchall()]

    by_sym: dict[str, list] = {}
    for r in rows:
        by_sym.setdefault(r["symbol"], []).append(r)

    market_raw: dict[str, dict] = {}
    symbols_out: list[dict] = []
    fng_value = fng_label = btc_dom = None
    for sym in watchlist:
        srows = by_sym.get(sym) or []
        if not srows:
            continue
        last = srows[-1]
        highs = [r.get("high") for r in srows if r.get("high") is not None]
        peak  = max(highs) if highs else None
        close = last.get("close")
        dd = round((peak - close) / peak * 100, 2) if (peak and close and peak > 0) else None
        market_raw[sym] = {
            "trend":           last.get("trend"),
            "trend_1d":        last.get("trend_1d"),
            "drawdown_pct_7d": dd,
        }
        if sym == "BTCUSDC":
            fng_value = last.get("fng_value")
            fng_label = last.get("fng_label")
            btc_dom   = last.get("btc_dominance")
        symbols_out.append({
            "symbol":   sym,
            "score":    last.get("score"),
            "trend":    last.get("trend"),
            "trend_1d": last.get("trend_1d"),
        })

    stance = _derive_stance(market_raw) if market_raw else None
    return {
        "live":          False,
        "as_of_ts":      at_ts,
        "stance":        stance,
        "btc_dominance": btc_dom,
        "fng_value":     fng_value,
        "fng_label":     fng_label,
        "symbols":       symbols_out,
    }


@bp.get("/api/market/context")
def api_market_context():
    """Market context (stance + dominance + F&G + per-symbol score/trend).

    ``?at=ISO_TS`` reconstructs the context from ``price_snapshots`` at that
    timestamp; without it, live data is fetched. ``?session_id=...`` is a
    convenience: when given, the context is reconstructed at the session's
    last trade timestamp if the session is past (no live runner).
    """
    at = (request.args.get("at") or "").strip()
    session_id = (request.args.get("session_id") or "").strip()
    cfg = load_config()
    watchlist = cfg.get("watchlist", []) or []

    if not at and session_id:
        # Resolve last trade ts for that session (server doesn't know if the
        # runner is active — the client passes ?at explicitly for historical).
        try:
            from db.store import load_history as _db_load
            hist = _db_load(mode=request.args.get("mode", "real"), limit=2000)
            sess_trades = [t for t in hist if t.get("session_id") == session_id]
            if sess_trades:
                at = max(t["timestamp"] for t in sess_trades)
        except Exception:
            pass

    try:
        if at:
            return jsonify(_context_at(at, watchlist))
        return jsonify(_context_live(watchlist))
    except Exception as exc:
        log.warning("Market context error: %s", exc)
        return jsonify({"error": str(exc), "symbols": []}), 200


@bp.get("/api/performance")
def api_performance():
    period         = request.args.get("period", "all")
    mode           = request.args.get("mode", "real")
    session_id     = request.args.get("session_id")
    with_bench     = request.args.get("with_benchmarks", "1") not in ("0", "false", "no")
    with_prices    = request.args.get("with_prices", "0") not in ("0", "false", "no")

    try:
        from db.store import trades_generation
        _gen = trades_generation()
    except Exception:
        _gen = 0
    _fresh = request.args.get("fresh") in ("1", "true", "yes")
    _cache_key = (mode, session_id, period, with_bench, with_prices)
    _now = time.time()
    _hit = _PERF_CACHE.get(_cache_key)
    if not _fresh and _hit and _hit[1] == _gen and (_now - _hit[0]) < _hit[2]:
        return jsonify(_hit[3])

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
        # Project only the columns the KPIs, equity curve, trade list and analytics
        # tooltips actually read — drops binance_order_id / session_name / per-row mode
        # from this 60s-polled read. `reason` (tooltips) and `fee`/`fee_asset` (real
        # Binance commission or the sim 0.1% proxy) are kept — no displayed loss.
        history = _db_load(mode=mode, limit=2000, columns=[
            "id", "timestamp", "action", "symbol", "amount", "qty",
            "price", "pnl", "fee", "fee_asset", "reason", "session_id",
        ])
    except ImportError:
        history = load_history()

    cutoff   = datetime.utcnow() - PERIODS.get(period, PERIODS["all"])
    filtered = [t for t in history if datetime.fromisoformat(t["timestamp"]) >= cutoff]
    if session_id:
        filtered = [t for t in filtered if t.get("session_id") == session_id]
    else:
        # The permanent card compiles every session + manual orders. "(init)"
        # entries are per-session bookkeeping for capital a run inherited (the
        # position was already bought in an earlier session), not executed
        # orders — counting them here would double-count that capital. They stay
        # in the session-scoped view above so its equity curve can value them.
        filtered = [t for t in filtered if "(init)" not in (t.get("action") or "")]

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

    # Is the selected session still armed (live) or finished? Drives both the
    # benchmark end-anchor and the frozen-position valuation below.
    is_active = True
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
            is_active = True

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
    # The real catch-all view anchors PnL on the real capital injected (net USDC
    # deposits); a specific real session keeps its own initial_total_value, and
    # sims keep their chosen budget. Falls back to the manual budget until the
    # first Binance funding sync has run.
    real_base: float | None = None
    if mode == "real" and not session_id:
        try:
            from ..binance_sync import real_capital_base
            real_base = real_capital_base()
        except Exception:
            real_base = None
    effective_budget    = float(
        real_base if real_base is not None
        else (sess_budget if sess_budget is not None else config.get("budget", 100)))
    # Both the live sim and the real agent trade the *current* config watchlist
    # (re-read every cycle), not the one frozen in the session at arm time. So an
    # active run's passive baselines must use the live watchlist too — otherwise a
    # run armed when the list was narrower (e.g. BTC only) shows BH ≡ BTC even
    # though the strategy now trades the full list. Finished runs keep their frozen
    # watchlist as the best record of the universe they actually ran against.
    if is_active:
        effective_watchlist = config.get("watchlist", []) or sess_watchlist or []
    else:
        effective_watchlist = sess_watchlist if sess_watchlist else config.get("watchlist", [])
    if with_bench and (sorted_trades or sess_started_at):
        start_iso = sess_started_at or sorted_trades[0]["timestamp"]
        end_iso: str | None = None
        if session_id and not is_active and sorted_trades:
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

    cycle_timestamps: list[str] = []
    if session_id:
        try:
            from db.snapshots import load_cycle_timestamps
            cycle_timestamps = load_cycle_timestamps(session_id)
        except Exception:
            log.exception("Failed to load cycle timestamps")

    # Dense mark-to-market equity curve for the PnL chart — cash + positions
    # priced at each cycle's market close, the same definition the backtest and
    # the live sim use. A *running* sim reads its exact per-cycle total_value
    # straight from sim state; every other run (finished sim, real) reconstructs
    # it from trades + price_snapshots so held positions track the market instead
    # of sitting at their entry price. Empty list → caller falls back to the
    # client-side trade reconstruction (e.g. Firestore / purged logs).
    sim_value_series: list = []
    if mode == "simulation" and session_id and is_active:
        try:
            from .. import simulation as _sim_engine
            state_series = _sim_engine._load_state_value_series(session_id) or []
        except Exception:
            log.exception("Failed to load sim value timeseries")
            state_series = []
        # The in-state series is the live, exact per-cycle total_value, but it's
        # bounded (downsampled) and can reset on a restart, so it may no longer
        # reach back to the run start. Trust it only when it still spans (most of)
        # the run; otherwise fall through to the cycle-grid reconstruction so the
        # chart begins at the run's first cycle instead of mid-run.
        if state_series and cycle_timestamps:
            run_span    = _iso_to_ms(cycle_timestamps[-1]) - _iso_to_ms(cycle_timestamps[0])
            series_span = _iso_to_ms(state_series[-1]["ts"]) - _iso_to_ms(state_series[0]["ts"])
            if run_span <= 0 or series_span >= 0.9 * run_span:
                sim_value_series = state_series
        elif state_series:
            sim_value_series = state_series
    # Finished sim, real run, or a live sim whose state series was wiped or
    # truncated to a recent window (restart / repeated LLM errors) → reconstruct
    # the dense MTM curve from trades + snapshots so the curve spans the whole run
    # and held positions track the market instead of their entry price.
    if not sim_value_series and session_id:
        try:
            sim_value_series = _strategy_value_series(
                sorted_trades, cycle_timestamps, effective_budget)
        except Exception:
            log.exception("Failed to build strategy value timeseries")

    # Run-end prices for a *finished* run: a closed run takes no further action,
    # so its open positions must be valued at the run's end, not today's market.
    # We return the captured close per symbol at the run's last cycle; the
    # frontend applies them to the qty it already holds (sim: reconstructed from
    # history; real: from /api/portfolio). Pure snapshot read — no Binance call
    # in this 60s-polled endpoint.
    frozen_prices: dict = {}
    if session_id and not is_active:
        try:
            run_end_ts = cycle_timestamps[-1] if cycle_timestamps else (
                sorted_trades[-1]["timestamp"] if sorted_trades else None)
            if run_end_ts and effective_watchlist:
                from db.snapshots import prices_at
                frozen_prices = prices_at(effective_watchlist, run_end_ts)
        except Exception:
            log.exception("Failed to load run-end prices")

    # Per-crypto close series for the momentum chart (only when asked — the
    # Graphiques tab sets with_prices=1; the 60s-polled Performance tab skips it).
    price_series: list = []
    if with_prices and cycle_timestamps:
        try:
            price_series = _symbol_price_series(effective_watchlist, cycle_timestamps)
        except Exception:
            log.exception("Failed to build per-symbol price series")

    payload = {
        "period":            period,
        "mode":              mode,
        "trades":            len(filtered),
        "buys":              len(buys),
        "sells":             len(sells),
        "stop_losses":       len(stop_losses),
        "invested":          round(invested, 2),
        "recovered":         round(recovered, 2),
        "fees":              round(fees, 4),
        "net":               net,
        "win_rate":          win_rate,
        "best_trade":        best_trade,
        "worst_trade":       worst_trade,
        "history":           list(reversed(sorted_trades[-2000:])),
        "timeseries":        timeseries,
        "bh_timeseries":     bh_ts,
        "btc_timeseries":    btc_ts,
        "bh_breakdown":      bh_breakdown,
        "btc_breakdown":     btc_breakdown,
        "cycle_timestamps":  cycle_timestamps,
        "value_timeseries":  sim_value_series,
        "price_series":      price_series,
        "frozen_prices":     frozen_prices,
        "sessions":          sessions,
        "budget":            round(effective_budget, 2),
    }
    # A finished run's payload is stable until a trade mutates it (gen bump);
    # an active run advances only on the 5-min cycle grid → short TTL is lossless.
    ttl = _PERF_TTL_FINISHED if (session_id and not is_active) else _PERF_TTL_ACTIVE
    _PERF_CACHE[_cache_key] = (_now, _gen, ttl, payload)
    # Bound memory: drop entries past the longest TTL (cheap, runs on cache miss).
    if len(_PERF_CACHE) > 64:
        for k, v in list(_PERF_CACHE.items()):
            if _now - v[0] > _PERF_TTL_FINISHED:
                _PERF_CACHE.pop(k, None)
    return jsonify(payload)
