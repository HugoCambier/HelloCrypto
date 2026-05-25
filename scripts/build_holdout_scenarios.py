"""Build held-out evaluation scenarios from ``price_snapshots``.

A scenario is a frozen replay of N hourly cycles, ready to be fed to
``hellocrypto.eval.runner.run()``. Held-out scenarios let us A/B compare
prompt/strategy versions on the *same* market conditions — the only honest
way to know if a change improves things or just rode the market.

Three contrasting regimes are bundled by default (`--default-suite`):
  - fear+bear (2026-03-29, capitulation window)
  - neutral+bull (2026-05-03, consolidation/drift)
  - greed+bull (2025-07-20, euphoria)

Each scenario covers 7 days × 24h = 168 cycles per symbol.

Usage:
    poetry run python -m scripts.build_holdout_scenarios --default-suite
    poetry run python -m scripts.build_holdout_scenarios \\
        --start 2026-03-29 --days 7 --name fear_bear_march
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.snapshots import _USE_POSTGRES  # noqa: E402
from hellocrypto.eval.scenario import Cycle, Scenario, save  # noqa: E402

log = logging.getLogger("build_holdout")

HOLDOUT_DIR = Path("data/scenarios/holdout")

# Two suites of the same three regime windows, at different lengths:
#   - "compact" : 24 cycles each (1 day). Fast bench, free-tier friendly.
#                 ~144 unique LLM calls total → ~10 min throttled at 15 RPM.
#   - "full"    : 168 cycles each (7 days). Stronger statistical signal but
#                 ~1500 calls → must use throttling + spread over hours.
DEFAULT_SUITE_COMPACT = [
    {"name": "holdout_fear_bear_1d",    "start": "2026-03-29", "days": 1, "note": "Capitulation 1d (fear+bear)"},
    {"name": "holdout_neutral_bull_1d", "start": "2026-05-03", "days": 1, "note": "Consolidation 1d (neutral+bull)"},
    {"name": "holdout_greed_bull_1d",   "start": "2025-07-20", "days": 1, "note": "Euphoria 1d (greed+bull)"},
]

DEFAULT_SUITE_FULL = [
    {"name": "holdout_fear_bear_7d",    "start": "2026-03-29", "days": 7, "note": "Capitulation 7d (fear+bear)"},
    {"name": "holdout_neutral_bull_7d", "start": "2026-05-03", "days": 7, "note": "Consolidation 7d (neutral+bull)"},
    {"name": "holdout_greed_bull_7d",   "start": "2025-07-20", "days": 7, "note": "Euphoria 7d (greed+bull)"},
]


# ── Loading from DB ───────────────────────────────────────────────────────────

def _load_window(start: datetime, end: datetime, watchlist: list[str]) -> list[dict]:
    """Load all backfill snapshots in [start, end] for the watchlist, ordered by ts."""
    placeholders = ("%s" if _USE_POSTGRES else "?")
    in_clause    = ",".join([placeholders] * len(watchlist))
    sql = (
        f"SELECT * FROM price_snapshots "
        f"WHERE source='backfill' AND symbol IN ({in_clause}) "
        f"AND timestamp >= {placeholders} AND timestamp < {placeholders} "
        f"ORDER BY symbol, timestamp ASC"
    )
    params = (*watchlist, start.isoformat(), end.isoformat())

    if _USE_POSTGRES:
        from db.store import _postgres
        with _postgres() as c:
            c.execute(sql, params)
            rows = c.fetchall()
    else:
        from db.store import _sqlite
        with _sqlite() as c:
            rows = c.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ── Per-cycle market dict reconstruction ──────────────────────────────────────

def _snapshot_to_market_entry(s: dict, prev1h: dict | None, prev24h: dict | None) -> dict:
    """Reshape a snapshot row into the dict shape expected by the runner.

    ``prev1h`` / ``prev24h`` are the same symbol's snapshots 1h / 24h earlier
    (used to recompute change_pct_1h / change_pct_24h, which aren't stored
    directly in the snapshots schema).
    """
    close = s["close"]
    change_1h  = ((close - prev1h["close"]) / prev1h["close"] * 100) if (prev1h and prev1h["close"]) else 0.0
    change_24h = ((close - prev24h["close"]) / prev24h["close"] * 100) if (prev24h and prev24h["close"]) else 0.0

    out: dict = {
        "price":          close,
        "change_pct_1h":  round(change_1h, 3),
        "change_pct_24h": round(change_24h, 3),
        "volume_usdc":    s.get("volume"),
        "high_24h":       s.get("high"),
        "low_24h":        s.get("low"),
        "rsi14":          s.get("rsi14"),
        "sma7":           s.get("sma7"),
        "sma25":          s.get("sma25"),
        "trend":          s.get("trend"),
        "trend_1d":       s.get("trend_1d"),
        "range_pct_24h":  round((s.get("high", 0) - s.get("low", 0)) / close * 100, 3) if close else 0.0,
        "atr":            s.get("atr14"),
    }
    if s.get("macd_hist") is not None:
        # Runner only reads .histogram from the macd dict.
        out["macd"] = {"macd": None, "signal": None, "histogram": s["macd_hist"]}
    if all(s.get(k) is not None for k in ("bb_lower", "bb_middle", "bb_upper")):
        upper, lower, mid = s["bb_upper"], s["bb_lower"], s["bb_middle"]
        width = (upper - lower) / mid * 100 if mid else 0
        out["bollinger"] = {
            "lower":  lower,
            "middle": mid,
            "upper":  upper,
            "width_pct": round(width, 3),
        }
    return out


def build_scenario(name: str, start_ts: datetime, days: int, watchlist: list[str], note: str = "") -> Scenario:
    end_ts = start_ts + timedelta(days=days)
    log.info("Building '%s': %s → %s, watchlist=%s", name, start_ts.date(), end_ts.date(), watchlist)
    # Need a 24h prefix for change_pct_24h on the first cycle
    fetch_start = start_ts - timedelta(hours=25)
    rows = _load_window(fetch_start, end_ts, watchlist)
    log.info("  loaded %d rows", len(rows))

    # Group by (symbol, ts)
    by_sym: dict[str, dict[str, dict]] = {}
    for r in rows:
        by_sym.setdefault(r["symbol"], {})[r["timestamp"]] = r

    # Build cycles: one per hour in [start_ts, end_ts]
    cycles: list[Cycle] = []
    cur = start_ts
    while cur < end_ts:
        ts_iso = cur.isoformat()
        prev1h_iso  = (cur - timedelta(hours=1)).isoformat()
        prev24h_iso = (cur - timedelta(hours=24)).isoformat()

        market: dict[str, dict] = {}
        fng_value:   int | None  = None
        fng_label:   str | None  = None
        btc_dom:     float | None = None
        for sym in watchlist:
            s = by_sym.get(sym, {}).get(ts_iso)
            if not s:
                continue
            market[sym] = _snapshot_to_market_entry(
                s,
                by_sym.get(sym, {}).get(prev1h_iso),
                by_sym.get(sym, {}).get(prev24h_iso),
            )
            # F&G is denormalized in every snapshot — pick from BTC for stability
            if sym == "BTCUSDC":
                fng_value = s.get("fng_value")
                fng_label = s.get("fng_label")
                btc_dom   = s.get("btc_dominance")
        if not market:
            cur += timedelta(hours=1)
            continue
        cycle = Cycle(
            timestamp=ts_iso,
            market=market,
            fear_greed={"value": fng_value, "label": fng_label} if fng_value is not None else None,
            btc_dominance=btc_dom,
        )
        cycles.append(cycle)
        cur += timedelta(hours=1)

    return Scenario(
        name=name,
        start=start_ts.isoformat(),
        end=end_ts.isoformat(),
        watchlist=watchlist,
        cycle_seconds=3600,
        cycles=cycles,
        note=note,
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def _load_watchlist() -> list[str]:
    import json
    return json.loads(Path("config.json").read_text()).get("watchlist", [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("compact", "full", "both"),
                        help="Bundled suite to build (compact = 1d / full = 7d / both)")
    parser.add_argument("--start", help="Start date YYYY-MM-DD (UTC)")
    parser.add_argument("--days",  type=int, default=1)
    parser.add_argument("--name",  help="Output filename stem (no extension)")
    parser.add_argument("--note",  default="")
    parser.add_argument("--subdir", default="",
                        help="Optional subdirectory under data/scenarios/holdout/")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    watchlist = _load_watchlist()
    log.info("Using watchlist (%d): %s", len(watchlist), watchlist)

    def _build_suite(suite_cfg: list[dict], subdir: str) -> None:
        target_dir = HOLDOUT_DIR / subdir if subdir else HOLDOUT_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        for cfg in suite_cfg:
            start_dt = datetime.fromisoformat(cfg["start"]).replace(tzinfo=UTC)
            scen = build_scenario(
                name=cfg["name"], start_ts=start_dt,
                days=cfg["days"], watchlist=watchlist, note=cfg["note"],
            )
            path = save(scen, target_dir / f"{cfg['name']}.json")
            log.info("  → %s (%d cycles)", path, scen.n_cycles)

    if args.suite in ("compact", "both"):
        log.info("Building COMPACT suite (1d scenarios)…")
        _build_suite(DEFAULT_SUITE_COMPACT, "compact")
    if args.suite in ("full", "both"):
        log.info("Building FULL suite (7d scenarios)…")
        _build_suite(DEFAULT_SUITE_FULL, "full")
    if args.suite:
        return 0

    if not args.start or not args.name:
        parser.error("--start and --name are required when --suite is not set")
    start_dt = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    scen = build_scenario(args.name, start_dt, args.days, watchlist, args.note)
    target_dir = HOLDOUT_DIR / args.subdir if args.subdir else HOLDOUT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    path = save(scen, target_dir / f"{args.name}.json")
    log.info("Saved → %s (%d cycles)", path, scen.n_cycles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
