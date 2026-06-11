"""Portfolio, Binance balance & manual trade API."""
from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

log = logging.getLogger(__name__)

from ..api import (
    get_balance,
    get_open_positions,
    get_ticker,
    load_config,
    load_history,
    market_buy,
    market_sell,
    record_buy,
    record_sell,
)
from ..ratelimit import rate_limit

bp = Blueprint("portfolio", __name__)


@bp.get("/api/portfolio")
def api_portfolio():
    try:
        config    = load_config()
        watchlist = config.get("watchlist", [])
        positions = get_open_positions(watchlist)
        cash      = get_balance("USDC")

        # Prices come from the latest captured snapshots (DB), NOT a live Binance
        # ticker. /api/portfolio is polled every 60s per open tab; hammering
        # Binance's public ticker from the dashboard risks rate-limiting the
        # shared IP/key and starving the decision cron of market data. The
        # capture cycle already writes fresh closes (~5min) we read for free.
        from db.snapshots import latest_prices
        prices = latest_prices(watchlist)

        portfolio_val = sum(
            p["qty"] * prices[sym]
            for sym, p in positions.items()
            if prices.get(sym)
        )
        total      = cash + portfolio_val
        # Real-mode capital base = net USDC deposited (Binance funding sync),
        # falling back to the manual budget until the first sync has run.
        from ..binance_sync import real_capital_base
        base       = real_capital_base()
        budget     = float(base if base is not None else config.get("budget", 100))
        gain       = total - budget
        try:
            from db.store import sum_fees
            total_fees = sum_fees(mode="real")
        except Exception:
            total_fees = sum(t.get("fee", 0) for t in load_history())

        return jsonify({
            "cash":          round(cash, 2),
            "portfolio_val": round(portfolio_val, 2),
            "total":         round(total, 2),
            "budget":        budget,
            "gain":          round(gain, 2),
            "gain_pct":      round(gain / budget * 100, 2) if budget else 0,
            "total_fees":    round(total_fees, 4),
            "positions": [
                {
                    "symbol":        sym,
                    "qty":           p["qty"],
                    "avg_price":     round(p["avg_price"], 4),
                    "current_price": prices.get(sym),
                    "value":         round(p["qty"] * prices[sym], 2) if prices.get(sym) else None,
                    "pnl_pct":       round((prices[sym] - p["avg_price"]) / p["avg_price"] * 100, 2)
                                     if prices.get(sym) else 0,
                }
                for sym, p in positions.items()
            ],
            "market": [
                {"symbol": sym, "price": prices.get(sym)}
                for sym in watchlist
                if prices.get(sym) is not None
            ],
        })
    except Exception:
        log.exception("Erreur api_portfolio")
        return jsonify({"error": "Erreur lors de la récupération du portefeuille"}), 500


@bp.post("/api/real/sync-binance")
@rate_limit(max_calls=3, per_seconds=120)  # signed Binance calls — limiter
def api_sync_binance():
    """Reconcile the real account against Binance: import manual/historical fills
    and refresh the net-deposits capital base. Idempotent (dedup on order id)."""
    try:
        from ..binance_sync import sync_all
        cfg = load_config()
        result = sync_all(cfg.get("watchlist", []))
        return jsonify({"ok": True, **result})
    except Exception:
        log.exception("Erreur api_sync_binance")
        return jsonify({"error": "Erreur lors de la synchronisation Binance"}), 500


@bp.post("/api/trade/buy")
@rate_limit(max_calls=10, per_seconds=60)  # garde-fou anti-spam ordres manuels
def api_buy():
    body   = request.json or {}
    symbol = body.get("symbol", "").strip().upper()
    amount = float(body.get("amount", 0))
    if not symbol or amount <= 0:
        return jsonify({"error": "symbol et amount requis"}), 400
    try:
        order, fee, fee_asset = market_buy(symbol, amount)
        price = get_ticker(symbol)
        record_buy(order, symbol, amount, price, "Ordre manuel — dashboard", fee, fee_asset)
        return jsonify({"ok": True, "price": price, "fee": fee, "fee_asset": fee_asset})
    except Exception:
        log.exception("Erreur api_buy")
        return jsonify({"error": "Erreur lors de l'exécution de l'ordre d'achat"}), 500


@bp.post("/api/trade/sell")
@rate_limit(max_calls=10, per_seconds=60)
def api_sell():
    body   = request.json or {}
    symbol = body.get("symbol", "").strip().upper()
    qty    = float(body.get("qty", 0))
    if not symbol or qty <= 0:
        return jsonify({"error": "symbol et qty requis"}), 400
    try:
        _, fee, fee_asset = market_sell(symbol, qty)
        price = get_ticker(symbol)
        record_sell("SELL", symbol, qty, price, "Ordre manuel — dashboard", fee, fee_asset)
        return jsonify({"ok": True, "price": price, "fee": fee, "fee_asset": fee_asset})
    except Exception:
        log.exception("Erreur api_sell")
        return jsonify({"error": "Erreur lors de l'exécution de l'ordre de vente"}), 500


@bp.post("/api/trade/liquidate")
@rate_limit(max_calls=2, per_seconds=300)  # liquidation totale — limiter strictement
def api_liquidate():
    """Market-sell every open position on Binance to USDC.

    REAL TRADING: emits actual sell orders. Used by the "Tout vendre"
    button in the Orders tab. Each sale is recorded as a SELL trade
    with reason "Liquidation totale — Tout vendre".
    """
    try:
        cfg       = load_config()
        watchlist = cfg.get("watchlist", [])
        positions = get_open_positions(watchlist)
        results: list = []
        errors:  list = []
        for sym, info in positions.items():
            qty = float(info.get("qty", 0))
            if qty <= 0:
                continue
            try:
                _, fee, fee_asset = market_sell(sym, qty)
                price = get_ticker(sym)
                record_sell("SELL", sym, qty, price,
                            "Liquidation totale — Tout vendre", fee, fee_asset,
                            avg_price=info.get("avg_price"))
                results.append({
                    "symbol": sym, "qty": round(qty, 8),
                    "price": price, "fee": fee, "fee_asset": fee_asset,
                })
            except Exception as exc:
                log.exception("Erreur liquidation %s", sym)
                errors.append({"symbol": sym, "error": str(exc)})
        return jsonify({
            "ok":            True,
            "sold":          results,
            "errors":        errors,
            "sold_count":    len(results),
            "error_count":   len(errors),
        })
    except Exception:
        log.exception("Erreur api_liquidate")
        return jsonify({"error": "Erreur lors de la liquidation"}), 500
