"""Binance REST API client.

Covers: HMAC authentication, market data, order execution,
trade history, and local persistence helpers.
"""

import os, time, json, hmac, hashlib
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import requests

BASE_URL    = "https://api.binance.com"
CONFIG_FILE = Path("config.json")

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
    if not r.ok:
        try:
            body = r.json()
            raise requests.exceptions.HTTPError(
                f"{r.status_code} {body.get('code','')} {body.get('msg', r.text)}",
                response=r,
            )
        except (ValueError, KeyError):
            r.raise_for_status()
    return r.json()


def api_post(endpoint: str, params: dict = {}) -> dict:
    p = dict(params)
    p["timestamp"] = int(time.time() * 1000)
    p["signature"] = _sign(p)
    r = requests.post(f"{BASE_URL}{endpoint}", params=p, headers=_headers(), timeout=10)
    if not r.ok:
        try:
            body = r.json()
            raise requests.exceptions.HTTPError(
                f"{r.status_code} {body.get('code','')} {body.get('msg', r.text)}",
                response=r,
            )
        except (ValueError, KeyError):
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


def _cycle_to_interval(cycle_seconds: int) -> str:
    """Map a cycle duration (seconds) to the closest Binance kline interval."""
    if cycle_seconds < 180:   return "1m"
    if cycle_seconds < 360:   return "3m"
    if cycle_seconds < 900:   return "5m"
    if cycle_seconds < 1800:  return "15m"
    if cycle_seconds < 3600:  return "30m"
    return "1h"


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


def _compute_ema(closes: list[float], period: int) -> float | None:
    """Exponential Moving Average."""
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema


def _compute_macd(closes: list[float]) -> dict | None:
    """MACD (12,26,9). Returns {macd, signal, histogram}."""
    if len(closes) < 35:
        return None
    ema12 = _compute_ema(closes, 12)
    ema26 = _compute_ema(closes, 26)
    if ema12 is None or ema26 is None:
        return None
    # Full series for signal line
    k12 = 2 / 13
    k26 = 2 / 27
    e12 = sum(closes[:12]) / 12
    e26 = sum(closes[:26]) / 26
    macd_series = []
    for i, c in enumerate(closes):
        if i < 12:
            continue
        e12 = c * k12 + e12 * (1 - k12)
        if i < 26:
            continue
        e26 = c * k26 + e26 * (1 - k26)
        macd_series.append(e12 - e26)
    if len(macd_series) < 9:
        return None
    k9 = 2 / 10
    signal = sum(macd_series[:9]) / 9
    for m in macd_series[9:]:
        signal = m * k9 + signal * (1 - k9)
    macd_val = macd_series[-1]
    return {"macd": round(macd_val, 6), "signal": round(signal, 6),
            "histogram": round(macd_val - signal, 6)}


def _compute_bollinger(closes: list[float], period: int = 20, num_std: float = 2.0) -> dict | None:
    """Bollinger Bands. Returns {upper, middle, lower, width_pct}."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    variance = sum((x - mid) ** 2 for x in window) / period
    std = variance ** 0.5
    upper = mid + num_std * std
    lower = mid - num_std * std
    width_pct = (upper - lower) / mid * 100 if mid else 0
    return {"upper": round(upper, 4), "middle": round(mid, 4),
            "lower": round(lower, 4), "width_pct": round(width_pct, 2)}


def _compute_atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    """Average True Range."""
    if len(closes) < period + 1 or len(highs) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def compute_score(d: dict) -> int:
    """Compute a 0-10 buy-signal score for a single symbol's enriched data dict."""
    score = 5  # neutral base
    # RSI sub-score
    rsi = d.get("rsi14")
    if rsi is not None:
        if rsi < 25:   score += 3
        elif rsi < 35: score += 2
        elif rsi < 45: score += 1
        elif rsi < 55: pass
        elif rsi < 65: score -= 1
        elif rsi < 75: score -= 2
        else:          score -= 3
    # Trend sub-score (1h)
    trend = d.get("trend", "neutre")
    if trend == "haussier":  score += 1
    elif trend == "baissier": score -= 1
    # Daily trend sub-score
    trend_1d = d.get("trend_1d")
    if trend_1d == "haussier":  score += 2
    elif trend_1d == "baissier": score -= 2
    # Volatility sub-score (low vol = stable = safer)
    vola = d.get("range_pct_24h")
    if vola is not None:
        if vola < 3:   score += 1
        elif vola > 8: score -= 1
    return max(0, min(10, score))


def compute_scores(data: dict) -> dict:
    """Return {symbol: score} for all symbols in *data*."""
    return {sym: compute_score(d) for sym, d in data.items()}


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
            parts.append(f"RSI(1h): {d['rsi14']}")
        if d.get("trend"):
            parts.append(f"Tendance1h: {d['trend']}")
        if d.get("range_pct_24h") is not None:
            parts.append(f"Volatilité24h: {d['range_pct_24h']:.1f}%")
        if d.get("rsi_short") is not None:
            ivl = d.get("interval_short", "court")
            parts.append(f"RSI({ivl}): {d['rsi_short']}")
        if d.get("trend_short"):
            ivl = d.get("interval_short", "court")
            parts.append(f"Tendance{ivl}: {d['trend_short']}")
        if d.get("trend_1d"):
            parts.append(f"TendanceJ: {d['trend_1d']}")
        if d.get("spread_pct") is not None:
            parts.append(f"Spread: {d['spread_pct']:.3f}%")
        # New indicators
        macd = d.get("macd")
        if macd:
            parts.append(f"MACD: {macd['macd']:+.6f} Signal: {macd['signal']:+.6f} Hist: {macd['histogram']:+.6f}")
        boll = d.get("bollinger")
        if boll:
            parts.append(f"Bollinger: [{boll['lower']:.2f} - {boll['upper']:.2f}] (largeur: {boll['width_pct']:.1f}%)")
        if d.get("atr") is not None:
            parts.append(f"ATR(14): {d['atr']:.4f}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


_EXTERNAL_CACHE: dict = {}
_CACHE_TTL = 300  # seconds


def _cached(key: str, fetcher):
    """Simple TTL cache for external API calls."""
    entry = _EXTERNAL_CACHE.get(key)
    if entry and time.time() - entry["ts"] < _CACHE_TTL:
        return entry["value"]
    try:
        value = fetcher()
    except Exception:
        value = None
    _EXTERNAL_CACHE[key] = {"ts": time.time(), "value": value}
    return value


def get_fear_and_greed() -> dict | None:
    """Return the Crypto Fear & Greed Index (cached 5 min)."""
    def _fetch():
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        d = r.json()["data"][0]
        return {"value": int(d["value"]), "label": d["value_classification"]}
    return _cached("fng", _fetch)


def get_btc_dominance() -> float | None:
    """Return BTC market cap dominance % from CoinGecko (cached 5 min)."""
    def _fetch():
        r = requests.get("https://api.coingecko.com/api/v3/global", timeout=5)
        return round(r.json()["data"]["market_cap_percentage"]["btc"], 1)
    return _cached("btc_dom", _fetch)


def get_enriched_market_data(watchlist: list[str], cycle_seconds: int = 60) -> dict[str, dict]:
    """Fetch price + 24h stats + RSI + SMA for each symbol in *watchlist*.

    Returns ``{symbol: {price, change_pct_24h, volume_usdc, rsi14,
                         sma7, sma25, trend, change_pct_1h,
                         rsi_short, trend_short, interval_short}}``.
    ``cycle_seconds`` controls the short-term candle interval: the closest
    Binance interval to the cycle frequency is used, giving the LLM a
    micro-trend signal that matches the simulation's actual refresh rate.
    Failures are silently skipped so one bad symbol never blocks the rest.
    """
    interval_short = _cycle_to_interval(cycle_seconds)
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

            # Short-term candles (interval matches cycle frequency)
            rsi_short   = None
            trend_short = None
            if interval_short != "1h":
                try:
                    klines_s  = get_klines(sym, interval=interval_short, limit=30)
                    closes_s  = [float(k[4]) for k in klines_s]
                    rsi_short = _compute_rsi(closes_s)
                    sma7_s    = _compute_sma(closes_s, 7)
                    sma14_s   = _compute_sma(closes_s, 14)
                    if sma7_s and sma14_s:
                        trend_short = "haussier" if sma7_s > sma14_s else "baissier"
                except Exception:
                    pass

            # Daily trend
            trend_1d = None
            try:
                klines_1d  = get_klines(sym, interval="1d", limit=30)
                closes_1d  = [float(k[4]) for k in klines_1d]
                sma7_1d    = _compute_sma(closes_1d, 7)
                sma25_1d   = _compute_sma(closes_1d, 25)
                if sma7_1d and sma25_1d:
                    trend_1d = "haussier" if sma7_1d > sma25_1d else "baissier"
            except Exception:
                pass

            # Order book spread
            spread_pct = None
            try:
                depth      = api_get("/api/v3/depth", {"symbol": sym, "limit": 5})
                best_bid   = float(depth["bids"][0][0])
                best_ask   = float(depth["asks"][0][0])
                spread_pct = round((best_ask - best_bid) / best_bid * 100, 4)
            except Exception:
                pass

            # MACD, Bollinger, ATR — need more candles
            macd_data = None
            bollinger = None
            atr_val   = None
            try:
                klines_ext = get_klines(sym, interval="1h", limit=50)
                closes_ext = [float(k[4]) for k in klines_ext]
                highs_ext  = [float(k[2]) for k in klines_ext]
                lows_ext   = [float(k[3]) for k in klines_ext]
                macd_data  = _compute_macd(closes_ext)
                bollinger  = _compute_bollinger(closes_ext)
                atr_val    = _compute_atr(highs_ext, lows_ext, closes_ext)
            except Exception:
                pass

            result[sym] = {
                **stats,
                "rsi14":          rsi14,
                "sma7":           round(sma7, 4) if sma7 else None,
                "sma25":          round(sma25, 4) if sma25 else None,
                "trend":          trend,
                "change_pct_1h":  round(change_1h, 2),
                "range_pct_24h":  round(range_pct, 2),
                "rsi_short":      rsi_short,
                "trend_short":    trend_short,
                "interval_short": interval_short,
                "trend_1d":       trend_1d,
                "spread_pct":     spread_pct,
                "macd":           macd_data,
                "bollinger":      bollinger,
                "atr":            round(atr_val, 4) if atr_val else None,
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
        "quantity":         f"{qty:.5f}",
        "newClientOrderId": f"hc_sell_{int(time.time() * 1000)}",
    })
    fee, asset = _extract_fee_usdc(order)
    return order, fee, asset


# ── Persistence ───────────────────────────────────────────────────────────────

def load_history() -> list:
    from db.store import load_history as _db_load
    return _db_load(mode="real")


def save_trade(
    action: str,
    symbol: str,
    amount: float,
    price: float,
    reason: str,
    fee: float = 0.0,
    fee_asset: str = "USDC",
) -> None:
    from db.store import save_trade as _db_save
    _db_save(action=action, symbol=symbol, amount=amount, price=price,
             reason=reason, fee=fee, fee_asset=fee_asset, mode="real")


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except FileNotFoundError:
        return _DEFAULT_CONFIG


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
