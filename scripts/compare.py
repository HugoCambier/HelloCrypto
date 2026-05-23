#!/usr/bin/env python3
"""Compare two eval reports side-by-side.

Usage:
  python scripts/compare.py data/eval_reports/v0__demo__X.json \
                            data/eval_reports/v1__demo__Y.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load(p: str) -> dict:
    return json.loads(Path(p).read_text())


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:+.2f}" if abs(v) < 1000 else f"{v:.0f}"
    return str(v)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("baseline")
    p.add_argument("candidate")
    args = p.parse_args()

    a = _load(args.baseline)
    b = _load(args.candidate)
    if a["scenario"] != b["scenario"]:
        print(f"[compare] WARNING: scénarios différents — {a['scenario']} vs {b['scenario']}",
              file=sys.stderr)

    keys = ["alpha_vs_btc_pct", "return_pct", "btc_return_pct",
            "max_drawdown_pct", "sharpe", "win_rate_pct",
            "num_trades", "total_fees", "tokens_total"]

    print(f"{'metric':<22} {'baseline':>14}  {'candidate':>14}  {'Δ':>14}")
    print("-" * 70)
    for k in keys:
        va = a["metrics"].get(k)
        vb = b["metrics"].get(k)
        delta = None
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            delta = vb - va
        print(f"{k:<22} {_fmt(va):>14}  {_fmt(vb):>14}  {_fmt(delta):>14}")

    # Quick verdict on the primary KPI
    alpha_a = a["metrics"].get("alpha_vs_btc_pct", 0) or 0
    alpha_b = b["metrics"].get("alpha_vs_btc_pct", 0) or 0
    dd_a    = a["metrics"].get("max_drawdown_pct", 0) or 0
    dd_b    = b["metrics"].get("max_drawdown_pct", 0) or 0
    verdict = "candidate ↑ alpha" if alpha_b > alpha_a else "candidate ↓ alpha"
    dd_note = "" if dd_b >= dd_a else f" (mais drawdown empire de {dd_a - dd_b:+.2f}pp)"
    print(f"\nVerdict : {verdict}{dd_note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
