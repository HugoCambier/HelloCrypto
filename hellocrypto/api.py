"""Binance REST API client.

Covers: HMAC authentication, market data, order execution,
trade history, and local persistence helpers.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

log = logging.getLogger(__name__)

BASE_URL    = "https://api.binance.com"
CONFIG_FILE = Path("config.json")


class NotionalTooSmall(Exception):
    """Binance rejected an order: value below the symbol's MIN_NOTIONAL filter.

    Raised for the specific ``-1013 Filter failure: NOTIONAL`` case — a position
    too small to trade (dust). Callers treat the order as a no-op rather than
    letting it abort the cycle.
    """

_DEFAULT_CONFIG = {
    "enabled": False,
    "mode": "simulation",
    "budget": 100,
    "stop_loss_pct": 21,
    "trailing_stop_pct": 10,
    "cycle_seconds": 300,
    "risk_level": 5,
    "sell_cooldown_cycles": 3,
    "llm_cooldown_seconds": 300,
    "price_change_threshold_pct": 0.5,
    "max_tokens": 1000,
    # Phase E: floor de confiance (0–1) en-dessous duquel les actions sont
    # ignorées. 0 = pas de gate ; 0.5 = mode prudent recommandé.
    "min_confidence": 0.5,
    # Minimal fallback only — the real list lives in config.json. Kept tiny
    # on purpose so a stale/forgotten default can never silently override the
    # checked-in watchlist. If you want to add coins, edit config.json.
    "watchlist": ["BTCUSDC", "ETHUSDC"],
    "llm": {"provider": "gemini", "model": "gemini-3.1-flash-lite"},
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

def api_get(endpoint: str, params: dict | None = None, signed: bool = False) -> dict:
    p = dict(params) if params else {}
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


def api_post(endpoint: str, params: dict | None = None) -> dict:
    p = dict(params) if params else {}
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


def compute_score_rules(d: dict) -> float:
    """Enriched 0-10 score for the *deterministic* decider only (not the LLM).

    Float-valued pour permettre la discrimination fine entre setups dans la
    zone de seuil (avant : tous les setups acceptables clusteraient à 8-10
    en int, sans discrimination → 8.10 score moyen pour winners ET losers).
    Composants discrets (trend/MACD/SMA cross) inchangés, composants continus
    (chg_24h, sma_strength, sma25_distance) en float pour la granularité.
    Base 5, additive, clamped 0-10.
    """
    score = 5.0  # neutral base
    # RSI sub-score (dominant mean-reversion signal)
    rsi = d.get("rsi14")
    if rsi is not None:
        if rsi < 25:   score += 3
        elif rsi < 35: score += 2
        elif rsi < 45: score += 1
        elif rsi < 55: pass
        elif rsi < 65: score -= 1
        elif rsi < 75: score -= 2
        else:          score -= 3
    # Daily trend (primary direction)
    trend_1d = d.get("trend_1d")
    if trend_1d == "haussier":   score += 2
    elif trend_1d == "baissier": score -= 2
    # Intraday trend (1h)
    trend = d.get("trend", "neutre")
    if trend == "haussier":   score += 1
    elif trend == "baissier": score -= 1
    # MACD histogram — momentum confirmation
    macd = d.get("macd") or {}
    hist = macd.get("histogram")
    if hist is not None:
        if hist > 0:   score += 1
        elif hist < 0: score -= 1
    # SMA7 vs SMA25 — gradué continu plutôt que binaire ±1. Une force de
    # tendance ±2 sur ±5% d'écart entre SMA7 et SMA25 → discrimine les
    # tendances fortes des micro-flips.
    sma7, sma25 = d.get("sma7"), d.get("sma25")
    if sma7 is not None and sma25 is not None and sma25 > 0:
        sma_gap = (sma7 - sma25) / sma25
        score += max(-2.0, min(2.0, sma_gap * 40.0))  # ±2 à ±5%
    # Volume-direction signal — flow confirmation on top of price action.
    # Asymétrique : reward high-volume green hours (real buying); high-volume
    # red peut être shake-out, pas de pénalité symétrique.
    vol_ratio = d.get("volume_ratio_1h")
    change_1h = d.get("change_pct_1h", 0)
    if vol_ratio is not None and vol_ratio > 1.5 and change_1h > 0:
        score += 1
    # Momentum 24h — gradué continu, comble le gap entre RSI (14h) et
    # trend_1d (SMA cross daily). ±2 à ±10% de variation 24h, linéaire.
    chg_24h = d.get("change_pct_24h")
    if chg_24h is not None:
        score += max(-2.0, min(2.0, chg_24h / 5.0))
    # Bollinger position — overbought/oversold relative to the bands
    bb = d.get("bollinger") or {}
    lower, upper = bb.get("lower"), bb.get("upper")
    price = d.get("price")
    if None not in (lower, upper, price) and upper > lower:
        bb_pos = (price - lower) / (upper - lower)
        if bb_pos < 0.2:   score += 1  # near lower band → oversold
        elif bb_pos > 0.8: score -= 1  # near upper band → overbought
    # Distance price ↔ SMA25 : pénalité continue pour over-extension late-cycle.
    # On préfère acheter sur léger pullback que sur parabolic blow-off top.
    if sma25 is not None and sma25 > 0 and price is not None:
        ext = price / sma25 - 1.0
        if   ext > 0.30: score -= 2.0   # très over-extended
        elif ext > 0.15: score -= 1.0   # over-extended
        elif -0.05 <= ext < 0.00 and trend_1d == "haussier":
            score += 1.0                # pullback en bull confirmé → opportunité
    return max(0.0, min(10.0, score))


def compute_scores_rules(data: dict) -> dict:
    """Return {symbol: enriched_score} for the deterministic decider."""
    return {sym: compute_score_rules(d) for sym, d in data.items()}


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
        if d.get("sma7") is not None and d.get("sma25") is not None:
            parts.append(f"SMA7: ${d['sma7']:.4f} SMA25: ${d['sma25']:.4f}")
        if d.get("spread_pct") is not None:
            parts.append(f"Spread: {d['spread_pct']:.3f}%")
        # Advanced indicators
        macd = d.get("macd")
        if macd:
            parts.append(f"MACD: {macd['macd']:+.6f} Signal: {macd['signal']:+.6f} Hist: {macd['histogram']:+.6f}")
        boll = d.get("bollinger")
        if boll:
            parts.append(f"Bollinger: [{boll['lower']:.2f} | {boll['middle']:.2f} | {boll['upper']:.2f}] (largeur: {boll['width_pct']:.1f}%)")
        if d.get("atr") is not None:
            parts.append(f"ATR(14): {d['atr']:.4f}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _bb_position(price: float, boll: dict | None) -> str:
    """Locate price within Bollinger bands as a compact tag."""
    if not boll:
        return "—"
    lower, mid, upper = boll["lower"], boll["middle"], boll["upper"]
    if price <= lower:
        return "↓lo"
    if price >= upper:
        return "↑hi"
    if price < mid:
        return "lo-mid"
    return "mid-hi"


def format_market_data_compact(data: dict[str, dict], watchlist: list[str],
                                scores: dict | None = None) -> str:
    """Token-lean format for `build_analysis`.

    One line per symbol with only the discriminant signals. Saves ~75 % of
    the tokens vs the verbose format while keeping every actionable bit
    (RSI, MACD histogram sign, Bollinger position, trend trio, volume tier,
    24h volatility, score).
    """
    lines = ["symbol | price | Δ24h | RSI1h | trend1h/short/d | MACDhist | BB-pos "
             "| vol24h | volat | score"]
    for sym in watchlist:
        if sym not in data:
            lines.append(f"{sym} | n/a")
            continue
        d = data[sym]
        price = d["price"]
        d24   = d.get("change_pct_24h", 0.0)
        rsi   = d.get("rsi14")
        rsi_s = f"{rsi:.0f}" if rsi is not None else "—"
        trend = "/".join(
            ("H" if t == "haussier" else "B" if t == "baissier" else "N")
            for t in (d.get("trend"), d.get("trend_short"), d.get("trend_1d"))
        )
        macd_h = d.get("macd", {}).get("histogram") if isinstance(d.get("macd"), dict) else None
        macd_tag = "—" if macd_h is None else (f"+{macd_h:.3f}" if macd_h > 0 else f"{macd_h:.3f}")
        bb_pos = _bb_position(price, d.get("bollinger"))
        vol    = d.get("volume_usdc", 0)
        vol_s  = f"${vol/1e9:.1f}B" if vol >= 1e9 else (f"${vol/1e6:.0f}M" if vol >= 1e6 else f"${vol/1e3:.0f}K")
        volat  = f"{d.get('range_pct_24h', 0):.1f}%"
        score  = scores.get(sym, "—") if scores else "—"
        lines.append(
            f"{sym} | ${price:,.4f} | {d24:+.1f}% | {rsi_s} | {trend} "
            f"| {macd_tag} | {bb_pos} | {vol_s} | {volat} | {score}"
        )
    return "\n".join(lines)


_EXTERNAL_CACHE: dict = {}
_CACHE_TTL = 300  # seconds
_CACHE_MAX_SIZE = 32


def _cached(key: str, fetcher):
    """Simple TTL cache for external API calls (bounded to _CACHE_MAX_SIZE)."""
    now = time.time()
    entry = _EXTERNAL_CACHE.get(key)
    if entry and now - entry["ts"] < _CACHE_TTL:
        return entry["value"]
    try:
        value = fetcher()
    except Exception:
        value = None
    # Evict expired entries if cache is full
    if len(_EXTERNAL_CACHE) >= _CACHE_MAX_SIZE:
        expired = [k for k, v in _EXTERNAL_CACHE.items() if now - v["ts"] >= _CACHE_TTL]
        for k in expired:
            del _EXTERNAL_CACHE[k]
        # If still full, evict oldest
        if len(_EXTERNAL_CACHE) >= _CACHE_MAX_SIZE:
            oldest = min(_EXTERNAL_CACHE, key=lambda k: _EXTERNAL_CACHE[k]["ts"])
            del _EXTERNAL_CACHE[oldest]
    _EXTERNAL_CACHE[key] = {"ts": now, "value": value}
    return value


def get_fear_and_greed() -> dict | None:
    """Return the Crypto Fear & Greed Index (cached 5 min)."""
    def _fetch():
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        d = r.json()["data"][0]
        return {"value": int(d["value"]), "label": d["value_classification"]}
    return _cached("fng", _fetch)


def get_fear_and_greed_history(days: int = 2000) -> dict[str, dict] | None:
    """Return historical FNG indexed by ``YYYY-MM-DD`` (UTC date).

    Used by the backtest so each simulated day sees the FNG value that
    actually held on that day, instead of polluting the whole replay with
    the live value of the moment the backtest was launched (which could
    shift PnL by tens of dollars if FNG crossed 25 or 75 between launches).

    Returns ``{date_iso: {"value": int, "label": str}}`` or ``None`` on
    fetch failure — callers must treat ``None`` as "no FNG modifier".
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    limit = min(max(days, 60), 3000)
    def _fetch():
        r = requests.get(f"https://api.alternative.me/fng/?limit={limit}",
                         timeout=10)
        out: dict[str, dict] = {}
        for d in r.json().get("data", []):
            iso = _dt.fromtimestamp(int(d["timestamp"]), tz=_UTC).date().isoformat()
            out[iso] = {"value": int(d["value"]),
                        "label": d["value_classification"]}
        return out
    return _cached(f"fng_hist_{limit}", _fetch)


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
            # On fetch 50 bougies 1h en un seul appel et on dérive le reste
            # localement (économise un /api/v3/klines par symbole/cycle).
            klines_ext = get_klines(sym, interval="1h", limit=50)
            klines     = klines_ext[-26:] if len(klines_ext) >= 26 else klines_ext
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

            # Daily trend + 7d drawdown (leading signal for bear protection).
            # Both derived from the same 1d klines fetch.
            trend_1d        = None
            drawdown_pct_7d = None
            try:
                klines_1d  = get_klines(sym, interval="1d", limit=30)
                closes_1d  = [float(k[4]) for k in klines_1d]
                highs_1d   = [float(k[2]) for k in klines_1d]
                sma7_1d    = _compute_sma(closes_1d, 7)
                sma25_1d   = _compute_sma(closes_1d, 25)
                if sma7_1d and sma25_1d:
                    trend_1d = "haussier" if sma7_1d > sma25_1d else "baissier"
                if len(highs_1d) >= 7 and price:
                    recent_high = max(highs_1d[-7:])
                    if recent_high > 0:
                        drawdown_pct_7d = round((recent_high - price) / recent_high * 100, 2)
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

            # MACD, Bollinger, ATR — réutilise les bougies déjà fetched
            macd_data = None
            bollinger = None
            atr_val   = None
            volume_ratio_1h = None  # current 1h volume / 24h average — flow signal
            try:
                closes_ext = [float(k[4]) for k in klines_ext]
                highs_ext  = [float(k[2]) for k in klines_ext]
                lows_ext   = [float(k[3]) for k in klines_ext]
                vols_ext   = [float(k[5]) for k in klines_ext]
                macd_data  = _compute_macd(closes_ext)
                bollinger  = _compute_bollinger(closes_ext)
                atr_val    = _compute_atr(highs_ext, lows_ext, closes_ext)
                if len(vols_ext) >= 25 and vols_ext[-1] > 0:
                    avg_24h_vol = sum(vols_ext[-25:-1]) / 24
                    if avg_24h_vol > 0:
                        volume_ratio_1h = round(vols_ext[-1] / avg_24h_vol, 2)
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
                "drawdown_pct_7d": drawdown_pct_7d,
                "spread_pct":     spread_pct,
                "macd":           macd_data,
                "bollinger":      bollinger,
                "atr":            round(atr_val, 4) if atr_val else None,
                "volume_ratio_1h": volume_ratio_1h,
            }
        except Exception as exc:
            log.warning("Données marché indisponibles pour %s: %s", sym, exc)
    return result


def get_balance(asset: str = "USDC") -> float:
    """Return the free balance for *asset*."""
    for b in api_get("/api/v3/account", signed=True).get("balances", []):
        if b["asset"] == asset:
            return float(b["free"])
    return 0.0


def get_open_positions(watchlist: list[str]) -> dict:
    """Compute open spot positions from Binance trade history.

    Pré-filtre via /api/v3/account (un seul appel) pour ne demander
    /myTrades QUE des assets avec une qty libre/locked > 0. Pour une
    watchlist de 20 cryptos dont 2 détenues, ça passe de 20 appels
    signés à 3.
    """
    positions: dict = {}

    # Set des bases détenues (free + locked > 0). Si /account échoue,
    # on retombe sur le comportement précédent (interroge tous les symboles).
    held_bases: set[str] | None = None
    try:
        balances = api_get("/api/v3/account", signed=True).get("balances", [])
        held_bases = {
            b["asset"]
            for b in balances
            if (float(b.get("free", 0) or 0) + float(b.get("locked", 0) or 0)) > 0
        }
    except Exception:
        log.warning("/account indisponible, fallback sur scan complet", exc_info=True)

    for symbol in watchlist:
        if held_bases is not None:
            base = symbol.replace("USDC", "").replace("BUSD", "")
            if base not in held_bases:
                continue
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


def get_my_trades(symbol: str, limit: int = 1000) -> list[dict]:
    """Return the account's executed fills for *symbol* (most recent ``limit``).

    Each fill: ``{id, orderId, price, qty, quoteQty, commission, commissionAsset,
    time, isBuyer, ...}``. Signed endpoint — authoritative source for the real
    trade history (manual + agent), used by the Binance import.
    """
    return api_get("/api/v3/myTrades", {"symbol": symbol, "limit": limit}, signed=True)


def _funding_history(endpoint: str, coin: str, ok_status: int,
                     time_field: str) -> list[dict]:
    """Page a SAPI funding endpoint in 90-day windows back to 2023-01-01.

    Binance caps deposit/withdraw history queries at a 90-day range, so we walk
    backwards window by window. Returns only rows in ``ok_status`` (success).
    """
    out: list[dict] = []
    now_ms   = int(time.time() * 1000)
    floor_ms = 1_672_531_200_000  # 2023-01-01T00:00:00Z
    window   = 90 * 24 * 3600 * 1000
    end = now_ms
    while end > floor_ms:
        start = max(floor_ms, end - window)
        try:
            rows = api_get(endpoint,
                           {"coin": coin, "startTime": start, "endTime": end},
                           signed=True)
        except Exception:
            log.warning("Funding history fetch failed (%s) for window %d-%d",
                        endpoint, start, end, exc_info=True)
            rows = []
        for r in rows or []:
            if int(r.get("status", -1)) == ok_status:
                out.append(r)
        end = start - 1
    return out


def get_usdc_funding() -> dict:
    """Net USDC capital injected: Σ deposits − Σ withdrawals (successful only).

    Returns ``{"deposits": x, "withdrawals": y, "net": x - y}``. This is the
    real capital base the dashboard measures PnL against in real mode.
    """
    deposits = _funding_history("/sapi/v1/capital/deposit/hisrec", "USDC",
                                ok_status=1, time_field="insertTime")
    withdraws = _funding_history("/sapi/v1/capital/withdraw/history", "USDC",
                                 ok_status=6, time_field="applyTime")
    dep = sum(float(d.get("amount", 0) or 0) for d in deposits)
    wd  = sum(float(w.get("amount", 0) or 0) for w in withdraws)
    return {"deposits": round(dep, 2), "withdrawals": round(wd, 2),
            "net": round(dep - wd, 2)}


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
    try:
        order = api_post("/api/v3/order", {
            "symbol":           symbol,
            "side":             "SELL",
            "type":             "MARKET",
            "quantity":         f"{qty:.5f}",
            "newClientOrderId": f"hc_sell_{int(time.time() * 1000)}",
        })
    except requests.exceptions.HTTPError as e:
        if _is_notional_failure(e):
            raise NotionalTooSmall(symbol) from e
        raise
    fee, asset = _extract_fee_usdc(order)
    return order, fee, asset


def _is_notional_failure(exc: requests.exceptions.HTTPError) -> bool:
    """True when *exc* is Binance's ``-1013`` MIN_NOTIONAL filter rejection.

    -1013 also covers LOT_SIZE / PRICE_FILTER, so we match the NOTIONAL message
    specifically rather than the bare code.
    """
    body = {}
    if exc.response is not None:
        try:
            body = exc.response.json()
        except ValueError:
            return False
    return body.get("code") == -1013 and "NOTIONAL" in str(body.get("msg", "")).upper()


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
    qty: float | None = None,
    pnl: float | None = None,
    session_id: str | None = None,
    session_name: str | None = None,
) -> None:
    from db.store import save_trade as _db_save
    _db_save(action=action, symbol=symbol, amount=amount, price=price,
             reason=reason, fee=fee, fee_asset=fee_asset, qty=qty, pnl=pnl,
             mode="real", session_id=session_id, session_name=session_name)


def record_buy(order: dict, symbol: str, usdc_amount: float, price: float,
               reason: str, fee: float, fee_asset: str,
               session_id: str | None = None, session_name: str | None = None) -> None:
    """Persist a real BUY with its executed base-asset quantity.

    The dashboard equity curve reconstructs each position from the trade log and
    values it at ``qty × price``; without ``qty`` the bought tokens read as $0 and
    the curve shows the full spend as a loss. Binance market orders return
    ``executedQty`` (base filled) — fall back to ``usdc_amount / price`` if absent.
    """
    qty = 0.0
    try:
        qty = float((order or {}).get("executedQty") or 0)
    except (TypeError, ValueError):
        qty = 0.0
    if qty <= 0 and price:
        qty = usdc_amount / price
    save_trade("BUY", symbol, usdc_amount, price, reason, fee, fee_asset,
               qty=qty, session_id=session_id, session_name=session_name)


def record_sell(action: str, symbol: str, qty: float, price: float, reason: str,
                fee: float, fee_asset: str, avg_price: float | None = None,
                session_id: str | None = None, session_name: str | None = None) -> None:
    """Persist a real SELL: ``amount`` is the USDC recovered, ``qty`` the base sold.

    ``amount`` must hold the USDC value (qty × price), not the coin quantity — the
    performance endpoint reads ``amount`` for buys and ``qty × price`` for sells, so
    storing qty in ``amount`` corrupts both. ``pnl`` is the realized result when the
    position's entry price is known.
    """
    amount = round(qty * price, 2) if price else None
    pnl = round((price - avg_price) * qty - (fee or 0), 4) if avg_price else None
    save_trade(action, symbol, amount, price, reason, fee, fee_asset,
               qty=qty, pnl=pnl, session_id=session_id, session_name=session_name)


def load_config() -> dict:
    """Load config — file is authoritative for local dev, DB is fallback."""
    try:
        return json.loads(CONFIG_FILE.read_text())
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("config.json read failed, trying DB")
    try:
        from db.store import get_state
        cfg = get_state("config")
        if cfg is not None:
            return cfg
    except Exception:
        log.exception("DB load_config failed, using defaults")
    return dict(_DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    """Save config to DB (authoritative). Mirror to file for local dev convenience."""
    try:
        from db.store import set_state
        set_state("config", cfg)
    except Exception:
        log.exception("DB save_config failed, writing to file only")
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    except Exception:
        pass
