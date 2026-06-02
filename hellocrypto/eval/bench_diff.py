"""Compare deux bench reports côte à côte.

Usage:
    poetry run python -m hellocrypto.eval.bench_diff \\
        eval/reports/champion.json eval/reports/bench/bench_*.json

Affiche :
  - tableau (variant × scenario) avec ret%/α% avant → après + Δ
  - récap par variant (moyenne cross-scenario) + verdict 🟢/🔴/⚪
  - wins / losses textuels pour faciliter la décision de promotion

Si le nouveau bench gagne, l'utilisateur peut le promouvoir manuellement :

    cp <new>.json eval/reports/champion.json
    # puis éditer eval/reports/CHANGELOG.md pour noter l'itération
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load(path: Path) -> dict:
    with path.open() as f:
        return json.load(f)


def _cell(results: dict, scenario: str, variant: str) -> dict | None:
    return ((results.get(scenario) or {}).get(variant) or {}).get("metrics")


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _verdict(delta_ret: float, delta_alpha: float, eps: float = 0.05) -> str:
    """🟢 nettement mieux, 🔴 nettement pire, ⚪ neutre (cache hit / égalité)."""
    if abs(delta_ret) < eps and abs(delta_alpha) < eps:
        return "⚪"
    score = delta_alpha + delta_ret
    return "🟢" if score > eps else "🔴" if score < -eps else "⚪"


def diff(champion: dict, latest: dict) -> str:
    c_res = champion.get("results", {})
    l_res = latest.get("results", {})

    # Union of scenarios and variants, preserving champion order then any extras.
    scenarios = list(c_res.keys()) + [s for s in l_res if s not in c_res]
    c_vars = list((champion.get("variants") or {}).keys())
    l_vars = list((latest.get("variants") or {}).keys())
    variants = c_vars + [v for v in l_vars if v not in c_vars]

    lines: list[str] = []
    lines.append(f"Champion: {champion.get('generated_at','?')}")
    lines.append(f"Latest:   {latest.get('generated_at','?')}")
    lines.append("")

    # Per-cell table
    lines.append(f"{'variant':<18} {'scenario':<28}  {'champ ret/α':<18} → {'new ret/α':<18}  Δret    Δα")
    lines.append("-" * 110)
    for v in variants:
        for s in scenarios:
            cm = _cell(c_res, s, v)
            lm = _cell(l_res, s, v)
            if cm is None and lm is None:
                continue
            cr = cm.get("return_pct") if cm else None
            ca = cm.get("alpha_vs_btc_pct") if cm else None
            lr = lm.get("return_pct") if lm else None
            la = lm.get("alpha_vs_btc_pct") if lm else None

            def fmt(r, a):
                if r is None: return "       —          "
                return f"{r:+5.2f}% / {(a or 0):+5.2f}%"

            dr = (lr or 0) - (cr or 0) if cr is not None and lr is not None else None
            da = (la or 0) - (ca or 0) if ca is not None and la is not None else None
            scen_short = s.replace("holdout_", "").replace("_1d", "")
            lines.append(
                f"{v:<18} {scen_short:<28}  {fmt(cr, ca):<18} → {fmt(lr, la):<18}  "
                f"{(f'{dr:+.2f}pp' if dr is not None else '—'):<7} "
                f"{(f'{da:+.2f}pp' if da is not None else '—')}"
            )

    # Per-variant summary (avg across scenarios)
    lines.append("")
    lines.append(f"{'variant':<18} {'champ avg ret/α':<22} {'new avg ret/α':<22}  Δret    Δα     verdict")
    lines.append("-" * 100)
    wins: list[str] = []
    losses: list[str] = []
    for v in variants:
        c_rets = [_cell(c_res, s, v)["return_pct"] for s in scenarios
                  if _cell(c_res, s, v) and _cell(c_res, s, v).get("return_pct") is not None]
        c_alps = [_cell(c_res, s, v)["alpha_vs_btc_pct"] for s in scenarios
                  if _cell(c_res, s, v) and _cell(c_res, s, v).get("alpha_vs_btc_pct") is not None]
        l_rets = [_cell(l_res, s, v)["return_pct"] for s in scenarios
                  if _cell(l_res, s, v) and _cell(l_res, s, v).get("return_pct") is not None]
        l_alps = [_cell(l_res, s, v)["alpha_vs_btc_pct"] for s in scenarios
                  if _cell(l_res, s, v) and _cell(l_res, s, v).get("alpha_vs_btc_pct") is not None]
        if not c_rets and not l_rets:
            continue
        cr, ca = _avg(c_rets), _avg(c_alps)
        lr, la = _avg(l_rets), _avg(l_alps)
        dr, da = lr - cr, la - ca
        verdict = _verdict(dr, da)
        lines.append(
            f"{v:<18} {f'{cr:+5.2f}% / {ca:+5.2f}%':<22} {f'{lr:+5.2f}% / {la:+5.2f}%':<22}  "
            f"{dr:+5.2f}pp {da:+5.2f}pp   {verdict}"
        )
        if verdict == "🟢":
            wins.append(f"{v}: α {ca:+.2f}% → {la:+.2f}% (Δ {da:+.2f}pp)")
        elif verdict == "🔴":
            losses.append(f"{v}: α {ca:+.2f}% → {la:+.2f}% (Δ {da:+.2f}pp)")

    lines.append("")
    if wins:
        lines.append("🟢 Wins:")
        lines.extend(f"  - {w}" for w in wins)
    if losses:
        lines.append("🔴 Losses:")
        lines.extend(f"  - {l}" for l in losses)
    if not wins and not losses:
        lines.append("⚪ No meaningful diff (likely cache hits / identical behavior)")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description="Diff two bench reports")
    p.add_argument("champion", type=Path, help="Path to champion bench JSON")
    p.add_argument("latest",   type=Path, help="Path to new bench JSON to compare")
    args = p.parse_args()
    if not args.champion.exists():
        print(f"champion not found: {args.champion}", file=sys.stderr)
        return 1
    if not args.latest.exists():
        print(f"latest not found: {args.latest}", file=sys.stderr)
        return 1
    print(diff(_load(args.champion), _load(args.latest)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
