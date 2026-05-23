#!/usr/bin/env python3
"""Run the strategy against one or more scenarios and write a report.

Usage:
  python scripts/eval.py --version v0_baseline --scenarios demo --no-llm
  python scripts/eval.py --version v1_prompt --scenarios btc_crash_2025_03 \
      --provider gemini --model gemini-2.0-flash-lite
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hellocrypto.eval import runner, scenario  # noqa: E402
from hellocrypto.eval.runner import StrategyConfig  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--version",   required=True, help="ID de la version (libre)")
    p.add_argument("--scenarios", required=True,
                   help="Liste séparée par virgule des scénarios à rejouer")
    p.add_argument("--budget",     type=float, default=1000)
    p.add_argument("--risk-level", type=int,   default=5)
    p.add_argument("--stop-loss",  type=float, default=21.0)
    p.add_argument("--trail-stop", type=float, default=10.0)
    p.add_argument("--cooldown",   type=int,   default=3)
    p.add_argument("--min-confidence", type=float, default=0.5,
                   help="Phase E: ignorer actions LLM avec confidence < seuil")
    p.add_argument("--no-llm",     action="store_true",
                   help="Mode rule-based (pas d'appel LLM, déterministe)")
    p.add_argument("--provider",   default="gemini")
    p.add_argument("--model",      default="gemini-2.0-flash-lite")
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--max-tokens", type=int,   default=1500)
    args = p.parse_args()

    cfg = StrategyConfig(
        budget=args.budget, risk_level=args.risk_level,
        stop_loss_pct=args.stop_loss, trailing_stop_pct=args.trail_stop,
        sell_cooldown_cycles=args.cooldown,
        min_confidence=args.min_confidence,
        provider="rules" if args.no_llm else args.provider,
        model=args.model, temperature=args.temperature,
        max_tokens=args.max_tokens,
    )

    paths: list[str] = []
    for name in args.scenarios.split(","):
        name = name.strip()
        if not name:
            continue
        try:
            scn = scenario.load(name)
        except FileNotFoundError:
            avail = scenario.list_scenarios() or ["(aucun)"]
            print(f"[eval] Scénario introuvable : {name}. Dispo : {', '.join(avail)}",
                  file=sys.stderr)
            return 2
        print(f"[eval] Replay {scn.name} ({scn.n_cycles} cycles, "
              f"cycle_seconds={scn.cycle_seconds}) ...", flush=True)
        report = runner.run(scn, cfg, version=args.version)
        path   = runner.write_report(report)
        paths.append(path)
        m = report.metrics
        print(f"[eval]   alpha vs BTC : {m['alpha_vs_btc_pct']:+.2f}%  "
              f"| return {m['return_pct']:+.2f}%  | BTC {m['btc_return_pct']:+.2f}%  "
              f"| trades {m['num_trades']}  | tokens {m['tokens_total']}  "
              f"| → {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
