"""Onboard a new symbol into the watchlist with all required initialization.

Steps (idempotent — re-running is safe):

  1. Validate the symbol trades on Binance (``/api/v3/exchangeInfo``).
  2. Append it to ``config.json`` watchlist (if missing).
  3. Backfill hourly klines into ``price_snapshots`` for the requested window.
  4. Compute historical monthly tiers in ``coin_risk_tiers`` from --from-date.

Usage:
    poetry run python -m scripts.add_coin LTCUSDC
    poetry run python -m scripts.add_coin LTCUSDC --days 1700 --from 2022-01-01
    poetry run python -m scripts.add_coin LTCUSDC --skip-backfill   # already have klines
    poetry run python -m scripts.add_coin LTCUSDC --skip-tiers      # tiers will fall back to DEFAULT_TIER

The DB tier lookup falls back to ``COIN_RISK_TIERS_BASELINE`` then ``DEFAULT_TIER=6``
if no row has been computed yet — so a new coin is usable immediately after step 3,
but step 4 is what gives it a calibrated tier based on real volatility/drawdown/beta.
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

log = logging.getLogger("add_coin")

CONFIG_PATH = Path("config.json")
BINANCE_EXCHANGE_INFO = "https://api.binance.com/api/v3/exchangeInfo"


def validate_symbol_on_binance(symbol: str) -> dict:
    """Hit Binance exchangeInfo for *symbol*. Returns the symbol record or raises."""
    r = requests.get(BINANCE_EXCHANGE_INFO, params={"symbol": symbol}, timeout=15)
    if r.status_code == 400:
        raise ValueError(f"Binance rejected symbol '{symbol}' (does not exist)")
    r.raise_for_status()
    data = r.json()
    syms = data.get("symbols") or []
    if not syms:
        raise ValueError(f"Binance returned no symbol record for '{symbol}'")
    rec = syms[0]
    if rec.get("status") != "TRADING":
        raise ValueError(f"Symbol '{symbol}' exists but status={rec.get('status')} (not TRADING)")
    return rec


def add_to_watchlist(symbol: str) -> bool:
    """Append *symbol* to config.json watchlist. Returns True if added, False if already present."""
    cfg = json.loads(CONFIG_PATH.read_text())
    wl = cfg.get("watchlist", [])
    if symbol in wl:
        log.info("Already in watchlist (%d symbols total) — no config change", len(wl))
        return False
    wl.append(symbol)
    cfg["watchlist"] = wl
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
    log.info("Added to watchlist (now %d symbols)", len(wl))
    return True


def run_step(label: str, cmd: list[str]) -> None:
    """Run a child step inheriting stdio so the user sees progress in real time."""
    log.info("─── %s ───", label)
    log.info("    $ %s", " ".join(cmd))
    completed = subprocess.run(cmd, check=False)
    if completed.returncode != 0:
        raise SystemExit(f"{label} failed with exit code {completed.returncode}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("symbol", help="Binance symbol to add (e.g. LTCUSDC)")
    p.add_argument("--days", type=int, default=1700,
                   help="Klines history to backfill (default 1700 ≈ early 2022 onward)")
    p.add_argument("--from-date", default="2022-01-01",
                   help="Earliest tier snapshot date (1st of month). Default 2022-01-01.")
    p.add_argument("--skip-config", action="store_true",
                   help="Don't touch config.json (e.g. you've already added it manually)")
    p.add_argument("--skip-backfill", action="store_true",
                   help="Don't backfill klines (you already have them)")
    p.add_argument("--skip-tiers", action="store_true",
                   help="Don't compute historical tiers (falls back to DEFAULT_TIER until run)")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    symbol = args.symbol.strip().upper()

    log.info("Onboarding %s — days=%d, from=%s", symbol, args.days, args.from_date)

    # ── 1. Binance validation ────────────────────────────────────────────────
    log.info("─── Validating %s on Binance ───", symbol)
    rec = validate_symbol_on_binance(symbol)
    log.info("    OK: base=%s quote=%s status=%s",
             rec.get("baseAsset"), rec.get("quoteAsset"), rec.get("status"))
    if rec.get("quoteAsset") != "USDC":
        log.warning("    ⚠ quoteAsset is %s, not USDC — strategy assumes USDC pairs",
                    rec.get("quoteAsset"))

    # ── 2. Watchlist update ──────────────────────────────────────────────────
    if not args.skip_config:
        log.info("─── Updating %s ───", CONFIG_PATH)
        add_to_watchlist(symbol)
    else:
        log.info("Skipping config.json update (--skip-config)")

    # ── 3. Klines backfill ───────────────────────────────────────────────────
    if not args.skip_backfill:
        run_step(
            f"Backfilling {args.days}d klines for {symbol}",
            [sys.executable, "-m", "scripts.backfill_binance",
             "--symbols", symbol, "--days", str(args.days)],
        )
    else:
        log.info("Skipping klines backfill (--skip-backfill)")

    # ── 4. Tier history ──────────────────────────────────────────────────────
    if not args.skip_tiers:
        run_step(
            f"Computing tier history for {symbol} from {args.from_date}",
            [sys.executable, "-m", "scripts.compute_coin_tiers",
             "--symbols", symbol, "--from", args.from_date],
        )
    else:
        log.info("Skipping tier compute (--skip-tiers) — will fall back to DEFAULT_TIER=6")

    log.info("Done. %s is ready for backtests & live decisions.", symbol)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
