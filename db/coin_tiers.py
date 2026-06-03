"""Per-coin risk tier history (DB-backed).

Each row is a monthly snapshot of a coin's risk profile, computed from the
preceding 30 days of hourly klines. Tier ∈ [2, 9], lower = safer.

``computed_at`` is always the 1st of the month (UTC). The decider queries
the most recent tier with ``computed_at <= as_of_date`` so backtests use
the tier that was *known at the time of decision*, not future information.

Schema is intentionally append-only: we never overwrite historical tiers,
only insert (or UPSERT for re-runs of the compute script on the same date).
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .store import _USE_POSTGRES, _postgres, _sqlite


def init_coin_tiers_table() -> None:
    """Create the ``coin_risk_tiers`` table if it doesn't exist."""
    if _USE_POSTGRES:
        with _postgres() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS coin_risk_tiers (
                symbol         TEXT    NOT NULL,
                computed_at    DATE    NOT NULL,
                tier           INTEGER NOT NULL,
                vol_30d        DOUBLE PRECISION,
                max_dd_30d     DOUBLE PRECISION,
                beta_btc       DOUBLE PRECISION,
                composite      DOUBLE PRECISION,
                n_data_points  INTEGER,
                PRIMARY KEY (symbol, computed_at)
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_tiers_at ON coin_risk_tiers(computed_at)")
    else:
        with _sqlite() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS coin_risk_tiers (
                symbol         TEXT    NOT NULL,
                computed_at    TEXT    NOT NULL,
                tier           INTEGER NOT NULL,
                vol_30d        REAL,
                max_dd_30d     REAL,
                beta_btc       REAL,
                composite      REAL,
                n_data_points  INTEGER,
                PRIMARY KEY (symbol, computed_at)
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_tiers_at ON coin_risk_tiers(computed_at)")


def upsert_tier(symbol: str, computed_at: date, tier: int, *,
                vol_30d: float | None = None,
                max_dd_30d: float | None = None,
                beta_btc: float | None = None,
                composite: float | None = None,
                n_data_points: int | None = None) -> None:
    """Insert or update a tier row for (symbol, computed_at)."""
    row = (symbol, computed_at.isoformat(), tier,
           vol_30d, max_dd_30d, beta_btc, composite, n_data_points)
    if _USE_POSTGRES:
        with _postgres() as c:
            c.execute("""INSERT INTO coin_risk_tiers
                (symbol, computed_at, tier, vol_30d, max_dd_30d, beta_btc, composite, n_data_points)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol, computed_at) DO UPDATE SET
                    tier          = EXCLUDED.tier,
                    vol_30d       = EXCLUDED.vol_30d,
                    max_dd_30d    = EXCLUDED.max_dd_30d,
                    beta_btc      = EXCLUDED.beta_btc,
                    composite     = EXCLUDED.composite,
                    n_data_points = EXCLUDED.n_data_points""", row)
    else:
        with _sqlite() as c:
            c.execute("""INSERT OR REPLACE INTO coin_risk_tiers
                (symbol, computed_at, tier, vol_30d, max_dd_30d, beta_btc, composite, n_data_points)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", row)


def get_tier_at(symbol: str, as_of: date | None = None) -> int | None:
    """Return the most recent tier for *symbol* with ``computed_at <= as_of``.

    ``as_of=None`` queries the latest tier across history. Returns ``None``
    if no tier has ever been computed for this symbol (caller should fall
    back to the hardcoded baseline in ``hellocrypto/coin_tiers.py``).
    """
    if _USE_POSTGRES:
        with _postgres() as c:
            if as_of is None:
                c.execute("""SELECT tier FROM coin_risk_tiers
                    WHERE symbol = %s ORDER BY computed_at DESC LIMIT 1""", (symbol,))
            else:
                c.execute("""SELECT tier FROM coin_risk_tiers
                    WHERE symbol = %s AND computed_at <= %s
                    ORDER BY computed_at DESC LIMIT 1""", (symbol, as_of.isoformat()))
            row = c.fetchone()
    else:
        with _sqlite() as c:
            if as_of is None:
                row = c.execute("""SELECT tier FROM coin_risk_tiers
                    WHERE symbol = ? ORDER BY computed_at DESC LIMIT 1""", (symbol,)).fetchone()
            else:
                row = c.execute("""SELECT tier FROM coin_risk_tiers
                    WHERE symbol = ? AND computed_at <= ?
                    ORDER BY computed_at DESC LIMIT 1""", (symbol, as_of.isoformat())).fetchone()
    if row is None:
        return None
    return int(row["tier"] if hasattr(row, "keys") else row[0])


def list_tiers_for_date(as_of: date) -> dict[str, dict[str, Any]]:
    """Snapshot of every symbol's tier as of *as_of*, with metadata.

    Useful for diagnostics ("show me everyone's tier on 2023-06-01"). Each
    value carries the full row so callers can inspect vol/dd/beta too.
    """
    sql_pg = """
        SELECT DISTINCT ON (symbol) symbol, tier, vol_30d, max_dd_30d, beta_btc, composite, computed_at
        FROM coin_risk_tiers
        WHERE computed_at <= %s
        ORDER BY symbol, computed_at DESC
    """
    sql_sqlite = """
        SELECT t.* FROM coin_risk_tiers t
        WHERE t.computed_at = (
            SELECT MAX(computed_at) FROM coin_risk_tiers
            WHERE symbol = t.symbol AND computed_at <= ?
        )
    """
    out: dict[str, dict[str, Any]] = {}
    if _USE_POSTGRES:
        with _postgres() as c:
            c.execute(sql_pg, (as_of.isoformat(),))
            rows = c.fetchall()
    else:
        with _sqlite() as c:
            rows = c.execute(sql_sqlite, (as_of.isoformat(),)).fetchall()
    for r in rows:
        d = dict(r) if hasattr(r, "keys") else r
        out[d["symbol"]] = d
    return out
