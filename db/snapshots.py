"""Price snapshots store — captures per-cycle market state for journal/playbook.

A snapshot is one (symbol, timestamp) row carrying OHLCV, indicators
(RSI/MACD/BB/ATR/SMA/trend), pre-computed score, macro context (F&G,
BTC dominance), and pre-computed regime tags. Backfilled from Binance
historical klines and continuously appended during live cycles.

The (symbol, timestamp, interval) tuple is UNIQUE so backfill is
idempotent — re-running for the same window overwrites cleanly.
"""
from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

_DATABASE_URL  = os.getenv("DATABASE_URL", "")
_USE_POSTGRES  = _DATABASE_URL.startswith(("postgresql://", "postgres://"))
_USE_FIRESTORE = bool(os.getenv("GOOGLE_CLOUD_PROJECT")) and not _DATABASE_URL and not _USE_POSTGRES


_COLUMNS = (
    "timestamp", "symbol", "interval",
    "open", "high", "low", "close", "volume",
    "rsi14", "macd_hist", "bb_lower", "bb_middle", "bb_upper", "bb_pos",
    "atr14", "sma7", "sma25", "trend", "trend_1d", "score",
    "fng_value", "fng_label", "btc_dominance",
    "regime_fng", "regime_btc_trend", "regime_dom",
    "source", "session_id", "cycle",
)


def init_snapshots() -> None:
    """Create the price_snapshots table (idempotent)."""
    if _USE_FIRESTORE:
        return  # Firestore is schemaless; snapshots stored per-doc under collection
    if _USE_POSTGRES:
        _init_postgres()
    else:
        _init_sqlite()


def _init_sqlite() -> None:
    from db.store import _sqlite
    with _sqlite() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS price_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT    NOT NULL,
            symbol          TEXT    NOT NULL,
            interval        TEXT    NOT NULL DEFAULT '1h',
            open            REAL,
            high            REAL,
            low             REAL,
            close           REAL    NOT NULL,
            volume          REAL,
            rsi14           REAL,
            macd_hist       REAL,
            bb_lower        REAL,
            bb_middle       REAL,
            bb_upper        REAL,
            bb_pos          TEXT,
            atr14           REAL,
            sma7            REAL,
            sma25           REAL,
            trend           TEXT,
            trend_1d        TEXT,
            score           INTEGER,
            fng_value       INTEGER,
            fng_label       TEXT,
            btc_dominance   REAL,
            regime_fng      TEXT,
            regime_btc_trend TEXT,
            regime_dom      TEXT,
            source          TEXT    NOT NULL DEFAULT 'live',
            session_id      TEXT,
            cycle           INTEGER
        )""")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_snap_sym_ts_int "
                  "ON price_snapshots(symbol, timestamp, interval)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_snap_ts "
                  "ON price_snapshots(timestamp)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_snap_regime "
                  "ON price_snapshots(regime_fng, regime_btc_trend)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_snap_source "
                  "ON price_snapshots(source)")


def _init_postgres() -> None:
    from db.store import _postgres
    with _postgres() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS price_snapshots (
            id              SERIAL PRIMARY KEY,
            timestamp       TEXT             NOT NULL,
            symbol          TEXT             NOT NULL,
            interval        TEXT             NOT NULL DEFAULT '1h',
            open            DOUBLE PRECISION,
            high            DOUBLE PRECISION,
            low             DOUBLE PRECISION,
            close           DOUBLE PRECISION NOT NULL,
            volume          DOUBLE PRECISION,
            rsi14           DOUBLE PRECISION,
            macd_hist       DOUBLE PRECISION,
            bb_lower        DOUBLE PRECISION,
            bb_middle       DOUBLE PRECISION,
            bb_upper        DOUBLE PRECISION,
            bb_pos          TEXT,
            atr14           DOUBLE PRECISION,
            sma7            DOUBLE PRECISION,
            sma25           DOUBLE PRECISION,
            trend           TEXT,
            trend_1d        TEXT,
            score           INTEGER,
            fng_value       INTEGER,
            fng_label       TEXT,
            btc_dominance   DOUBLE PRECISION,
            regime_fng      TEXT,
            regime_btc_trend TEXT,
            regime_dom      TEXT,
            source          TEXT             NOT NULL DEFAULT 'live',
            session_id      TEXT,
            cycle           INTEGER
        )""")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_snap_sym_ts_int "
                  "ON price_snapshots(symbol, timestamp, interval)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_snap_ts "
                  "ON price_snapshots(timestamp)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_snap_regime "
                  "ON price_snapshots(regime_fng, regime_btc_trend)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_snap_source "
                  "ON price_snapshots(source)")
        # Ferme l'exposition via l'API REST Supabase (cf. db/store.py).
        c.execute("ALTER TABLE price_snapshots ENABLE ROW LEVEL SECURITY")


def save_snapshots_batch(rows: list[dict]) -> int:
    """Bulk-upsert snapshots. Returns the number of rows written.

    Each row must carry all fields listed in ``_COLUMNS`` (missing keys → NULL).
    """
    if not rows:
        return 0
    if _USE_FIRESTORE:
        # Snapshots aren't a hot read path; in Firestore mode we expect Cloud Run
        # users to also have a SQL DB. Soft-fail to keep the pipeline running.
        log.warning("save_snapshots_batch: Firestore backend not supported, skipping")
        return 0

    values = [tuple(r.get(col) for col in _COLUMNS) for r in rows]
    cols   = ",".join(_COLUMNS)

    if _USE_POSTGRES:
        from db.store import _postgres
        ph = ",".join(["%s"] * len(_COLUMNS))
        with _postgres() as c:
            c.executemany(
                f"INSERT INTO price_snapshots ({cols}) VALUES ({ph}) "
                f"ON CONFLICT (symbol, timestamp, interval) DO UPDATE SET "
                + ", ".join(f"{col}=EXCLUDED.{col}" for col in _COLUMNS if col not in ("symbol", "timestamp", "interval")),
                values,
            )
        return len(values)

    from db.store import _sqlite
    ph = ",".join(["?"] * len(_COLUMNS))
    with _sqlite() as c:
        c.executemany(
            f"INSERT INTO price_snapshots ({cols}) VALUES ({ph}) "
            f"ON CONFLICT (symbol, timestamp, interval) DO UPDATE SET "
            + ", ".join(f"{col}=excluded.{col}" for col in _COLUMNS if col not in ("symbol", "timestamp", "interval")),
            values,
        )
    return len(values)


def count_snapshots(
    symbol: str | None = None,
    source: str | None = None,
) -> int:
    """Row count, optionally filtered by symbol/source."""
    if _USE_FIRESTORE:
        return 0
    conditions: list[str] = []
    params: list[Any] = []
    ph = "%s" if _USE_POSTGRES else "?"
    if symbol:
        conditions.append(f"symbol={ph}")
        params.append(symbol)
    if source:
        conditions.append(f"source={ph}")
        params.append(source)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"SELECT COUNT(*) FROM price_snapshots {where}"
    if _USE_POSTGRES:
        from db.store import _postgres
        with _postgres() as c:
            c.execute(sql, params)
            return int(c.fetchone()[0])
    from db.store import _sqlite
    with _sqlite() as c:
        row = c.execute(sql, params).fetchone()
    return int(row[0])


def load_snapshots(
    symbol: str | None = None,
    source: str | None = None,
    start_ts: str | None = None,
    end_ts: str | None = None,
    limit: int = 5000,
) -> list[dict]:
    """Load snapshots ordered by timestamp ASC. Useful for journal computation."""
    if _USE_FIRESTORE:
        return []
    conditions: list[str] = []
    params: list[Any] = []
    ph = "%s" if _USE_POSTGRES else "?"
    if symbol:
        conditions.append(f"symbol={ph}")
        params.append(symbol)
    if source:
        conditions.append(f"source={ph}")
        params.append(source)
    if start_ts:
        conditions.append(f"timestamp>={ph}")
        params.append(start_ts)
    if end_ts:
        conditions.append(f"timestamp<={ph}")
        params.append(end_ts)
    where  = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    sql = f"SELECT * FROM price_snapshots {where} ORDER BY timestamp ASC LIMIT {ph}"
    if _USE_POSTGRES:
        from db.store import _postgres
        with _postgres() as c:
            c.execute(sql, params)
            return [dict(r) for r in c.fetchall()]
    from db.store import _sqlite
    with _sqlite() as c:
        rows = c.execute(sql, params).fetchall()
    return [dict(r) for r in rows]
