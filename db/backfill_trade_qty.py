#!/usr/bin/env python3
"""Backfill ``qty`` (and fix ``amount``) on real-mode trades written before the
qty fix landed.

Real BUY/SELL trades used to be persisted without ``qty``, and SELL trades stored
the *coin quantity* in the ``amount`` column instead of the USDC value. The
dashboard equity curve reconstructs each position from the trade log and values
it at ``qty × price`` — with ``qty`` missing the bought tokens read as $0, so the
curve showed the full spend as a loss (a POL buy of $11 sat flat at -$11). The
recovered/net KPIs broke for the same reason.

For every ``mode='real'`` row whose ``qty`` is missing (the signature of the bug):
  - BUY : qty = amount / price             (amount already holds the USDC spent)
  - SELL: qty = amount; amount = qty*price (amount wrongly held the coin qty)

``pnl`` is left as-is — the entry price needed to compute realized PnL isn't
available from the trade row alone.

Idempotent: only rows with NULL/0 ``qty`` are touched, so re-running is safe and
never affects trades written by the fixed code.

Usage:
    python -m db.backfill_trade_qty            # dry-run — prints what would change
    python -m db.backfill_trade_qty --apply    # perform the update
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.store import _USE_FIRESTORE, _USE_POSTGRES

# Rows the bug left behind: real trades with no usable qty.
_WHERE = "mode = 'real' AND (qty IS NULL OR qty = 0) AND price > 0 AND amount IS NOT NULL"
_BUY_WHERE  = f"{_WHERE} AND action LIKE '%BUY%'"
_SELL_WHERE = f"{_WHERE} AND action LIKE '%SELL%'"


def _run_sql(apply: bool) -> None:
    from db.store import _postgres, _sqlite

    with (_postgres() if _USE_POSTGRES else _sqlite()) as conn:
        # _postgres() yields a cursor; _sqlite() yields a Connection.
        cur = conn if _USE_POSTGRES else conn.cursor()

        cur.execute(f"SELECT COUNT(*) FROM trades WHERE {_BUY_WHERE}")
        n_buy = cur.fetchone()[0]
        cur.execute(f"SELECT COUNT(*) FROM trades WHERE {_SELL_WHERE}")
        n_sell = cur.fetchone()[0]
        print(f"BUY rows to fix : {n_buy}  (qty = amount / price)")
        print(f"SELL rows to fix: {n_sell}  (qty = amount; amount = amount * price)")

        if not apply:
            print("\nDry-run — nothing written. Re-run with --apply to update.")
            return

        # RHS expressions reference the pre-update row in both Postgres and
        # SQLite, so reading `amount` while reassigning it is well-defined.
        cur.execute(f"UPDATE trades SET qty = amount / price WHERE {_BUY_WHERE}")
        cur.execute(f"UPDATE trades SET qty = amount, amount = amount * price WHERE {_SELL_WHERE}")
        print(f"\n✓ Updated {n_buy} BUY + {n_sell} SELL real trades.")


def _run_firestore(apply: bool) -> None:
    from db.store import _fs
    docs = _fs().collection("trades").where("mode", "==", "real").stream()
    buys, sells = [], []
    for d in docs:
        t = d.to_dict()
        if t.get("qty"):
            continue
        price, amount = t.get("price"), t.get("amount")
        if not price or price <= 0 or amount is None:
            continue
        (buys if "BUY" in (t.get("action") or "") else sells).append((d.reference, amount, price))
    print(f"BUY rows to fix : {len(buys)}  (qty = amount / price)")
    print(f"SELL rows to fix: {len(sells)}  (qty = amount; amount = amount * price)")
    if not apply:
        print("\nDry-run — nothing written. Re-run with --apply to update.")
        return
    for ref, amount, price in buys:
        ref.update({"qty": amount / price})
    for ref, qty, price in sells:
        ref.update({"qty": qty, "amount": round(qty * price, 2)})
    print(f"\n✓ Updated {len(buys)} BUY + {len(sells)} SELL real trades.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill qty on legacy real trades")
    parser.add_argument("--apply", action="store_true",
                        help="perform the update (default: dry-run)")
    args = parser.parse_args()

    backend = "Firestore" if _USE_FIRESTORE else ("PostgreSQL" if _USE_POSTGRES else "SQLite")
    print(f"Backend: {backend}\n")
    if _USE_FIRESTORE:
        _run_firestore(args.apply)
    else:
        _run_sql(args.apply)


if __name__ == "__main__":
    main()
