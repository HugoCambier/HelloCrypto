"""Live snapshot capture — writes one ``price_snapshots`` row per symbol per cycle.

Called from the agent / simulation cycle loops. The market_data_raw dict
already carries every indicator we need (RSI/MACD/BB/ATR/SMA/trend) thanks
to ``api.get_enriched_market_data``. This module just shapes it into the
snapshot schema, tags the regime, and persists in a single batched write.

Without this, the playbook stays frozen on the backfill window forever.
With it, the dataset grows continuously and the next nightly playbook
regeneration sees the new data.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from db.snapshots import save_snapshots_batch
from hellocrypto.api import _bb_position, compute_score

from .playbook import btc_trend_bucket, fng_bucket

log = logging.getLogger(__name__)


def _market_row_to_snapshot(
    symbol: str,
    data: dict,
    timestamp: str,
    fear_greed: dict | None,
    btc_dominance: float | None,
    source: str,
    session_id: str | None,
    cycle: int | None,
    btc_trend_1d: str | None,
) -> dict:
    """Map one enriched market dict (from get_enriched_market_data) to a snapshot row."""
    macd = data.get("macd") if isinstance(data.get("macd"), dict) else None
    boll = data.get("bollinger") if isinstance(data.get("bollinger"), dict) else None
    price = data.get("price")

    fng_value = fear_greed.get("value") if fear_greed else None
    fng_label = fear_greed.get("label") if fear_greed else None

    return {
        "timestamp":        timestamp,
        "symbol":           symbol,
        "interval":         "1h",  # cycle independent; matches backfill convention
        "open":             None,  # not exposed by enriched fetcher; skip
        "high":             data.get("high_24h"),
        "low":              data.get("low_24h"),
        "close":            price,
        "volume":           data.get("volume_usdc"),
        "rsi14":            data.get("rsi14"),
        "macd_hist":        macd.get("histogram") if macd else None,
        "bb_lower":         boll.get("lower") if boll else None,
        "bb_middle":        boll.get("middle") if boll else None,
        "bb_upper":         boll.get("upper") if boll else None,
        "bb_pos":           _bb_position(price, boll) if (price and boll) else None,
        "atr14":            data.get("atr"),
        "sma7":             data.get("sma7"),
        "sma25":            data.get("sma25"),
        "trend":            data.get("trend"),
        "trend_1d":         data.get("trend_1d"),
        "score":            compute_score(data),
        "fng_value":        fng_value,
        "fng_label":        fng_label,
        "btc_dominance":    btc_dominance,
        "regime_fng":       fng_bucket(fng_value),
        "regime_btc_trend": btc_trend_bucket(data.get("trend_1d") or btc_trend_1d),
        "regime_dom":       None,   # no live bucketing for dominance yet
        "source":           source,
        "session_id":       session_id,
        "cycle":            cycle,
    }


def capture_snapshots(
    market_data_raw: dict,
    fear_greed: dict | None,
    btc_dominance: float | None,
    cycle: int | None = None,
    session_id: str | None = None,
    source: str = "live",
) -> int:
    """Persist one snapshot per symbol present in ``market_data_raw``.

    Returns the number of rows written. Silently swallows DB errors so a
    snapshot-save failure never breaks the live cycle — the data is
    "nice to have" for the playbook, but not on the critical path of the
    trading decision.
    """
    if not market_data_raw:
        return 0

    # Floor to the hour so live snapshots line up with the backfill grid
    # (which is hourly). Multiple captures within the same hour UPSERT the
    # same row — the latest intra-hour state wins, which matches what the
    # nightly playbook regen wants to see anyway.
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    ts  = now.isoformat()
    # Reuse BTCUSDC's daily trend as the macro trend fallback for symbols
    # missing trend_1d (e.g. a fresh listing).
    btc_trend_1d = (
        market_data_raw.get("BTCUSDC", {}).get("trend_1d")
        if "BTCUSDC" in market_data_raw else None
    )

    rows = [
        _market_row_to_snapshot(
            sym, data, ts, fear_greed, btc_dominance,
            source, session_id, cycle, btc_trend_1d,
        )
        for sym, data in market_data_raw.items()
        if data.get("price") is not None
    ]

    try:
        return save_snapshots_batch(rows)
    except Exception:
        log.exception("capture_snapshots: failed to persist %d rows", len(rows))
        return 0


def capture_snapshots_5min(
    market_data_raw: dict,
    fear_greed: dict | None,
    btc_dominance: float | None,
    source: str = "live_5m",
) -> int:
    """Persist one 5-min snapshot per symbol (decoupled from decision cycles).

    Same shape as ``capture_snapshots`` but timestamps are floored to the
    nearest 5-min boundary and stored with ``interval='5m'`` — so they
    coexist with hourly rows (UNIQUE on symbol+timestamp+interval).

    Beyond 7 days these are purged by ``purge_old_snapshots`` to keep the DB
    small; only the hourly grid is retained long-term.
    """
    if not market_data_raw:
        return 0

    now = datetime.now(UTC)
    floored_minute = (now.minute // 5) * 5
    ts = now.replace(minute=floored_minute, second=0, microsecond=0).isoformat()
    btc_trend_1d = (
        market_data_raw.get("BTCUSDC", {}).get("trend_1d")
        if "BTCUSDC" in market_data_raw else None
    )

    rows = []
    for sym, data in market_data_raw.items():
        if data.get("price") is None:
            continue
        row = _market_row_to_snapshot(
            sym, data, ts, fear_greed, btc_dominance,
            source, None, None, btc_trend_1d,
        )
        row["interval"] = "5m"
        rows.append(row)

    try:
        return save_snapshots_batch(rows)
    except Exception:
        log.exception("capture_snapshots_5min: failed to persist %d rows", len(rows))
        return 0
