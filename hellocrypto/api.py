"""Binance REST API client.

Covers: HMAC authentication, market data, order execution,
trade history, and local persistence helpers.
"""

import os, time, json, hmac, hashlib
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import requests

BASE_URL     = "https://api.binance.com"
HISTORY_FILE = Path("data/history.json")
CONFIG_FILE  = Path("config.json")

_DEFAULT_CONFIG = {
    "budget": 100,
    "stop_loss_pct": 10,
    "cycle_seconds": 60,
    "watchlist": ["BTCUSDC", "ETHUSDC", "SOLUSDC", "XRPUSDC", "BNBUSDC"],
}


# ── Auth ──────────────────────────────────────────────────────────────────────

def _key() -> str:
    return os.environ["BINANCE_API_KEY"]

def _secret() -> str:
    return os.environ["BINANCE_API_SECRET"]

def _sign(params: dict) -> str:
    return hmac.new(
        _secret().encode(),
        urlencode(params).encode(),
        hashlib.sha256,
    ).hexdigest()

def _headers() -> dict:
    return {"X-MBX-APIKEY": _key()}


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def api_get(endpoint: str, params: dict = {}, signed: bool = False) -> dict:
    p = dict(params)
    if signed:
        p["timestamp"] = int(time.time() * 1000)
        p["signature"] = _sign(p)
    r = requests.get(f"{BASE_URL}{endpoint}", params=p, headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def api_post(endpoint: str, params: dict = {}) -> dict:
    p = dict(params)
    p["timestamp"] = int(time.time() * 1000)
    p["signature"] = _sign(p)
    r = requests.post(f"{BASE_URL}{endpoint}", params=p, headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


# ── Market data ───────────────────────────────────────────────────────────────

def get_ticker(symbol: str) -> float:
    """Return the latest ask price for *symbol*."""
    return float(api_get("/api/v3/ticker/price", {"symbol": symbol})["price"])


def get_ticker_stats(symbol: str) -> dict:
    """Return 24-hour statistics for *symbol* (no auth required).

    Returns a dict with keys: price, change_pct_24h, high_24h, low_24h, volume_usdc.
    """
    d = api_get("/api/v3/ticker/24hr", {"symbol": symbol})
    return {
        "price":          float(d["lastPrice"]),
        "change_pct_24h": float(d["priceChangePercent"]),
        "high_24h":       float(d["highPrice"]),
        "low_24h":        float(d["lowPrice"]),
        "volume_usdc":    float(d["quoteVolume"]),
    }


def get_klines(symbol: str, interval: str = "1h", limit: int = 26) -> list[list]:
    """Return the last *limit* candlesticks for *symbol* (no auth required).

    Each candle: [open_time, open, high, low, close, volume, ...]
    """
    return api_get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})


def _compute_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)


def _compute_sma(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def format_market_data(data: dict[str, dict], watchlist: list[str]) -> str:
    """Format enriched market data dict into a prompt-ready string."""
    lines = []
    for sym in watchlist:
        if sym not in data:
            lines.append(f"{sym}: indisponible")
            continue
        d   = data[sym]
        vol = d.get("volume_usdc", 0)
        vol_str   = f"Vol24h: ${vol/1e6:.0f}M" if vol >= 1e6 else f"Vol24h: ${vol/1e3:.0f}K"
        parts = [
            f"{sym}: ${d['price']:,.4f}",
            f"Δ1h: {d['change_pct_1h']:+.2f}%",
            f"Δ24h: {d['change_pct_24h']:+.2f}%",
            vol_str,
        ]
        if d.get("rsi14") is not None:
            parts.append(f"RSI(14): {d['rsi14']}")
        if d.get("trend"):
            parts.append(f"Tendance: {d['trend']}")
        if d.get("range_pct_24h") is not None:
            parts.append(f"Volatilité24h: {d['range_pct_24h']:.1f}%")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def get_enriched_market_data(watchlist: list[str]) -> dict[str, dict]:
    """Fetch price + 24h stats + RSI + SMA for each symbol in *watchlist*.

    Returns ``{symbol: {price, change_pct_24h, volume_usdc, rsi14,
                         sma7, sma25, trend, change_pct_1h}}``.
    Failures are silently skipped so one bad symbol never blocks the rest.
    """
    result: dict[str, dict] = {}
    for sym in watchlist:
        try:
            stats  = get_ticker_stats(sym)
            klines = get_klines(sym, interval="1h", limit=26)
            closes = [float(k[4]) for k in klines]

            rsi14  = _compute_rsi(closes)
            sma7   = _compute_sma(closes, 7)
            sma25  = _compute_sma(closes, 25)

            if sma7 and sma25:
                trend = "haussier" if sma7 > sma25 else "baissier"
            else:
                trend = "neutre"

            change_1h = ((closes[-1] - closes[-2]) / closes[-2] * 100) if len(closes) >= 2 else 0.0

            price       = stats["price"]
            range_pct   = (stats["high_24h"] - stats["low_24h"]) / price * 100 if price else 0.0

            result[sym] = {
                **stats,
                "rsi14":          rsi14,
                "sma7":           round(sma7, 4) if sma7 else None,
                "sma25":          round(sma25, 4) if sma25 else None,
                "trend":          trend,
                "change_pct_1h":  round(change_1h, 2),
                "range_pct_24h":  round(range_pct, 2),
            }
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Données marché indisponibles pour %s: %s", sym, exc)
    return result


def get_balance(asset: str = "USDC") -> float:
    """Return the free balance for *asset*."""
    for b in api_get("/api/v3/account", signed=True).get("balances", []):
        if b["asset"] == asset:
            return float(b["free"])
    return 0.0


def get_open_positions(watchlist: list[str]) -> dict:
    """Compute open spot positions from Binance trade history.

    Uses a running FIFO cost-basis to derive average entry price.
    Returns ``{symbol: {qty, avg_price}}``.
    """
    positions: dict = {}
    for symbol in watchlist:
        try:
            trades = api_get(
                "/api/v3/myTrades",
                {"symbol": symbol, "limit": 500},
                signed=True,
            )
            qty = cost = 0.0
            for t in trades:
                q, p = float(t["qty"]), float(t["price"])
                if t["isBuyer"]:
                    cost += q * p
                    qty  += q
                else:
                    if qty > 0:
                        cost -= (cost / qty) * min(q, qty)
                    qty = max(0.0, qty - q)
            if qty > 0.0001:
                positions[symbol] = {
                    "qty":       round(qty, 8),
                    "avg_price": round(cost / qty, 6) if qty else 0.0,
                }
        except Exception:
            pass
    return positions


# ── Fee extraction ────────────────────────────────────────────────────────────

def _extract_fee_usdc(order: dict) -> tuple[float, str]:
    """Convert Binance fill commissions to a USDC-equivalent amount.

    Handles three cases:
    - ``commissionAsset == "USDC"``         → direct value
    - ``commissionAsset == base currency``  → converted via weighted fill price
    - ``commissionAsset == "BNB"``          → converted via BNBUSDC spot price
    """
    fills = order.get("fills", [])
    if not fills:
        return 0.0, "USDC"

    fee_asset = fills[0].get("commissionAsset", "USDC")
    total_raw = sum(float(f.get("commission", 0)) for f in fills)

    if fee_asset == "USDC":
        return round(total_raw, 6), fee_asset

    if fee_asset == "BNB":
        try:
            return round(total_raw * get_ticker("BNBUSDC"), 6), fee_asset
        except Exception:
            return 0.0, fee_asset

    # Base-asset fee (e.g. BTC for a BTCUSDC buy): convert via avg fill price
    total_qty = sum(float(f["qty"]) for f in fills)
    if not total_qty:
        return 0.0, fee_asset
    avg_price = sum(float(f["price"]) * float(f["qty"]) for f in fills) / total_qty
    return round(total_raw * avg_price, 6), fee_asset


# ── Order execution ───────────────────────────────────────────────────────────

def market_buy(symbol: str, usdc_amount: float) -> tuple[dict, float, str]:
    """Spend *usdc_amount* USDC on *symbol* at market price.

    Returns:
        ``(order_response, fee_usdc, fee_asset)``
    """
    order = api_post("/api/v3/order", {
        "symbol":            symbol,
        "side":              "BUY",
        "type":              "MARKET",
        "quoteOrderQty":     f"{usdc_amount:.2f}",
        "newClientOrderId":  f"hc_buy_{int(time.time() * 1000)}",
    })
    fee, asset = _extract_fee_usdc(order)
    return order, fee, asset


def market_sell(symbol: str, qty: float) -> tuple[dict, float, str]:
    """Sell *qty* units of *symbol* at market price.

    Returns:
        ``(order_response, fee_usdc, fee_asset)``
    """
    order = api_post("/api/v3/order", {
        "symbol":           symbol,
        "side":             "SELL",
        "type":             "MARKET",
        "quantity":         f"{qty:.6f}",
        "newClientOrderId": f"hc_sell_{int(time.time() * 1000)}",
    })
    fee, asset = _extract_fee_usdc(order)
    return order, fee, asset


# ── Persistence ───────────────────────────────────────────────────────────────

def load_history() -> list:
    try:
        return json.loads(HISTORY_FILE.read_text())
    except FileNotFoundError:
        return []


def save_trade(
    action: str,
    symbol: str,
    amount: float,
    price: float,
    reason: str,
    fee: float = 0.0,
    fee_asset: str = "USDC",
) -> None:
    """Append a trade record to the local JSON history."""
    history = load_history()
    history.append({
        "timestamp": datetime.utcnow().isoformat(),
        "action":    action,
        "symbol":    symbol,
        "amount":    amount,
        "price":     price,
        "reason":    reason,
        "fee":       fee,
        "fee_asset": fee_asset,
    })
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except FileNotFoundError:
        return _DEFAULT_CONFIG
