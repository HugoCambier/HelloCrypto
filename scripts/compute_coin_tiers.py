"""Compute monthly risk tiers for each watchlist coin from historical klines.

For a given snapshot date (1st of the month), reads the prior 30 days of
hourly closes from ``price_snapshots`` and derives:

  - **vol_30d** : annualized stddev of hourly log-returns
  - **max_dd_30d** : peak-to-trough drawdown over the window
  - **beta_btc** : OLS slope of (sym returns) vs (BTC returns)

Composite score (vol 40% + dd 30% + beta 30%) → integer tier ∈ [2, 9].
The result is upserted into ``coin_risk_tiers``.

Usage:
    # Recompute the latest month only (cron job)
    poetry run python -m scripts.compute_coin_tiers

    # Backfill the tier history from 2022-01-01 forward
    poetry run python -m scripts.compute_coin_tiers --from 2022-01-01

    # Single date
    poetry run python -m scripts.compute_coin_tiers --at 2024-03-01
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.coin_tiers import init_coin_tiers_table, upsert_tier  # noqa: E402
from db.store import _USE_POSTGRES, _postgres, _sqlite  # noqa: E402

log = logging.getLogger("compute_tiers")

WATCHLIST_PATH = Path("config.json")
WINDOW_DAYS    = 30
HOURS_PER_DAY  = 24
MIN_DATA_POINTS = 24 * 14  # need at least 14 days of hourly data for a meaningful tier
BTC_SYMBOL     = "BTCUSDC"


def _load_watchlist() -> list[str]:
    return json.loads(WATCHLIST_PATH.read_text()).get("watchlist", [])


# ── Stats ─────────────────────────────────────────────────────────────────────

def _hourly_log_returns(closes: list[float]) -> list[float]:
    """log(close_t / close_{t-1}) — robust to outliers vs simple returns."""
    out = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0 and closes[i] > 0:
            out.append(math.log(closes[i] / closes[i - 1]))
    return out


def _stddev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    mean = sum(xs) / len(xs)
    var = sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var)


def _max_drawdown(closes: list[float]) -> float:
    """Worst peak-to-trough loss over the window (returned as a negative ratio)."""
    if not closes:
        return 0.0
    peak = closes[0]
    worst = 0.0
    for c in closes[1:]:
        peak = max(peak, c)
        if peak > 0:
            dd = (c - peak) / peak
            worst = min(worst, dd)
    return worst


def _beta_vs(sym_returns: list[float], btc_returns: list[float]) -> float:
    """OLS slope of sym_returns regressed on btc_returns (covariance / variance).

    Returns 0.0 if the two series can't be aligned or BTC has zero variance.
    """
    n = min(len(sym_returns), len(btc_returns))
    if n < 10:
        return 0.0
    s = sym_returns[-n:]
    b = btc_returns[-n:]
    mean_s = sum(s) / n
    mean_b = sum(b) / n
    cov = sum((si - mean_s) * (bi - mean_b) for si, bi in zip(s, b, strict=True)) / n
    var_b = sum((bi - mean_b) ** 2 for bi in b) / n
    if var_b == 0:
        return 0.0
    return cov / var_b


# ── DB read ───────────────────────────────────────────────────────────────────

def _load_closes(symbol: str, end_dt: datetime, days: int = WINDOW_DAYS) -> list[float]:
    """Load hourly closes for *symbol* over [end_dt - days, end_dt), ordered ascending."""
    start_dt = end_dt - timedelta(days=days)
    placeholder = "%s" if _USE_POSTGRES else "?"
    sql = (
        f"SELECT close FROM price_snapshots "
        f"WHERE source='backfill' AND symbol={placeholder} "
        f"AND timestamp >= {placeholder} AND timestamp < {placeholder} "
        f"ORDER BY timestamp ASC"
    )
    args = (symbol, start_dt.isoformat(), end_dt.isoformat())
    if _USE_POSTGRES:
        with _postgres() as c:
            c.execute(sql, args)
            rows = c.fetchall()
    else:
        with _sqlite() as c:
            rows = c.execute(sql, args).fetchall()
    return [float(r["close"]) for r in rows]


# ── Tier mapping ──────────────────────────────────────────────────────────────

def _vol_sub_score(vol_30d_annualized: float) -> float:
    """Map annualized vol to a 0-10 risk sub-score. 30% vol → ~3, 100% → ~9."""
    return max(0.0, min(10.0, vol_30d_annualized * 10))


def _dd_sub_score(max_dd: float) -> float:
    """Map worst drawdown (negative ratio) to 0-10. -15% → 3, -40% → 8."""
    return max(0.0, min(10.0, abs(max_dd) * 20))


def _beta_sub_score(beta: float) -> float:
    """Reward beta ~1 (moves with the market), penalize extremes (decoupled
    or amplified). Beta 1 → 3, beta 0 or 2 → 8, beta 3+ → 10."""
    return max(0.0, min(10.0, abs(beta - 1.0) * 5 + 3))


def composite_to_tier(vol: float, dd: float, beta: float) -> tuple[int, float]:
    """Return (tier, raw_composite). Tier is clamped to [2, 9]."""
    composite = (
        _vol_sub_score(vol) * 0.40
        + _dd_sub_score(dd)  * 0.30
        + _beta_sub_score(beta) * 0.30
    )
    tier = max(2, min(9, round(composite)))
    return tier, composite


# ── Compute one date ──────────────────────────────────────────────────────────

def compute_for_date(snapshot_date: date, watchlist: list[str]) -> int:
    """Compute and upsert tiers for every symbol at *snapshot_date*. Returns count."""
    end_dt = datetime(snapshot_date.year, snapshot_date.month, snapshot_date.day, tzinfo=UTC)
    btc_closes = _load_closes(BTC_SYMBOL, end_dt)
    btc_returns = _hourly_log_returns(btc_closes)
    if len(btc_closes) < MIN_DATA_POINTS:
        log.warning("  %s: BTC has only %d candles for the window — skipping date",
                    snapshot_date, len(btc_closes))
        return 0

    inserted = 0
    for sym in watchlist:
        closes = _load_closes(sym, end_dt)
        if len(closes) < MIN_DATA_POINTS:
            log.info("  %s: %s only %d candles (need %d) — skipped",
                     snapshot_date, sym, len(closes), MIN_DATA_POINTS)
            continue

        returns = _hourly_log_returns(closes)
        # Annualize hourly stddev: sqrt(24 * 365)
        vol = _stddev(returns) * math.sqrt(HOURS_PER_DAY * 365)
        dd  = _max_drawdown(closes)
        beta = _beta_vs(returns, btc_returns) if sym != BTC_SYMBOL else 1.0
        tier, composite = composite_to_tier(vol, dd, beta)

        upsert_tier(
            sym, snapshot_date, tier,
            vol_30d=round(vol, 4),
            max_dd_30d=round(dd, 4),
            beta_btc=round(beta, 4),
            composite=round(composite, 3),
            n_data_points=len(closes),
        )
        log.info("  %s: %s → tier=%d (vol=%.2f, dd=%.2f, β=%.2f, comp=%.2f)",
                 snapshot_date, sym, tier, vol, dd, beta, composite)
        inserted += 1
    return inserted


# ── Month iteration ───────────────────────────────────────────────────────────

def _first_of_next_month(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def _month_starts(start: date, end: date) -> list[date]:
    """All 1st-of-month dates in [start, end] inclusive."""
    cur = date(start.year, start.month, 1)
    if cur < start:
        cur = _first_of_next_month(cur)
    out = []
    while cur <= end:
        out.append(cur)
        cur = _first_of_next_month(cur)
    return out


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--from", dest="from_date", help="Backfill from YYYY-MM-DD (1st of month)")
    p.add_argument("--to",   dest="to_date",
                   help="Backfill up to YYYY-MM-DD (default: today)")
    p.add_argument("--at",   help="Single date YYYY-MM-DD (overrides --from/--to)")
    p.add_argument("--symbols",
                   help="Comma-separated symbols to restrict the compute to (default: full config watchlist)")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    init_coin_tiers_table()
    if args.symbols:
        watchlist = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        watchlist = _load_watchlist()
    log.info("Watchlist (%d symbols): %s", len(watchlist), ", ".join(watchlist))

    if args.at:
        dates = [date.fromisoformat(args.at)]
    elif args.from_date:
        start = date.fromisoformat(args.from_date)
        end   = date.fromisoformat(args.to_date) if args.to_date else date.today()
        dates = _month_starts(start, end)
    else:
        # Default: just today's 1st-of-month (cron use case)
        today = date.today()
        dates = [date(today.year, today.month, 1)]

    log.info("Computing tiers for %d snapshot date(s)…", len(dates))
    total = 0
    for d in dates:
        log.info("─── %s ───", d)
        total += compute_for_date(d, watchlist)
    log.info("Done. %d tier rows upserted across %d dates.", total, len(dates))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
