"""Performance & watchlist API."""
import logging
from datetime import datetime

from flask import Blueprint, jsonify, request

from ..api import load_config, load_history, get_enriched_market_data, compute_scores
from .shared import PERIODS

log = logging.getLogger(__name__)

bp = Blueprint("performance", __name__)


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
    period     = request.args.get("period", "all")
    mode       = request.args.get("mode", "real")
    session_id = request.args.get("session_id")
    config     = load_config()

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

    invested  = sum(t.get("amount", 0) or 0 for t in buys)
    recovered = sum((t.get("qty", 0) or 0) * (t.get("price", 0) or 0) for t in all_sells)
    fees      = sum(t.get("fee", 0) or 0 for t in filtered)
    net       = round(recovered - invested - fees, 2)

    sells_pnl  = [t for t in all_sells if t.get("pnl") is not None]
    profitable = [t for t in sells_pnl if t["pnl"] > 0]
    win_rate   = round(len(profitable) / len(all_sells) * 100, 1) if all_sells else None
    best_trade  = round(max(t["pnl"] for t in sells_pnl), 2) if sells_pnl else None
    worst_trade = round(min(t["pnl"] for t in sells_pnl), 2) if sells_pnl else None

    sorted_trades = sorted(filtered, key=lambda t: t["timestamp"])
    timeseries, cum = [], 0.0
    for t in sorted_trades:
        if t["action"] == "BUY":
            cum -= (t.get("amount", 0) or 0) + (t.get("fee", 0) or 0)
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

    return jsonify({
        "period":      period,
        "mode":        mode,
        "trades":      len(filtered),
        "buys":        len(buys),
        "sells":       len(sells),
        "stop_losses": len(stop_losses),
        "invested":    round(invested, 2),
        "recovered":   round(recovered, 2),
        "fees":        round(fees, 4),
        "net":         net,
        "win_rate":    win_rate,
        "best_trade":  best_trade,
        "worst_trade": worst_trade,
        "history":     list(reversed(sorted_trades[-200:])),
        "timeseries":  timeseries,
        "sessions":    sessions,
        "budget":      config.get("budget", 100),
    })
