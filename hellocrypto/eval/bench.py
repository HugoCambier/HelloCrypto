"""A/B benchmark — compare two strategy versions on the same held-out scenarios.

Goal: tell honestly whether a change (new prompt, new sizing, new calibration)
improves performance, instead of trusting intuition or anecdotal in-sample
wins. Locks the variables that aren't being tested: same scenarios, same
LLM cache (so identical prompts get identical outputs across versions),
same starting budget.

Bundled variant set (``VARIANTS``):
  - ``baseline``: learning system OFF (no playbook, no behavior in prompt)
  - ``current``:  learning system ON (both injected)

Adding a new variant = one entry in ``VARIANTS``. The bench loops every
scenario × variant, runs ``eval.runner.run()``, and prints a delta table.

Usage:
    poetry run python -m hellocrypto.eval.bench
    poetry run python -m hellocrypto.eval.bench --scenarios "eval/scenarios/holdout/*.json"
    poetry run python -m hellocrypto.eval.bench --provider gemini --model gemini-3.1-flash-lite

Limitation noted: the playbook + behavior reports in DB reflect the
*current* state of the world, so when a scenario covers 2026-03 and the
playbook was built in 2026-05, there is look-ahead leakage. The bench
still measures whether the prompt+strategy combo *given today's playbook*
matters — useful but not a clean walk-forward. A time-anchored playbook
(``playbook as of date X``) is a future iteration.
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .runner import PROGRESS_FILE, StrategyConfig, progress_init, run
from .scenario import load as load_scenario

log = logging.getLogger(__name__)


# ── Variant definitions ───────────────────────────────────────────────────────

VARIANTS: dict[str, dict[str, Any]] = {
    "baseline": {
        "description":     "Pre-learning state — no playbook/behavior/calibration",
        "enable_playbook":               False,
        "enable_behavior":               False,
        "enable_confidence_calibration": False,
        "enable_regime_aware_thresholds": False,
    },
    "playbook": {
        "description":     "+ playbook section in prompt (no behavior, no calibration)",
        "enable_playbook":               True,
        "enable_behavior":               False,
        "enable_confidence_calibration": False,
        "enable_regime_aware_thresholds": False,
    },
    "full_prompt": {
        "description":     "+ behavior section in prompt (no calibration)",
        "enable_playbook":               True,
        "enable_behavior":               True,
        "enable_confidence_calibration": False,
        "enable_regime_aware_thresholds": False,
    },
    "calibrated": {
        "description":     "+ confidence auto-calibration",
        "enable_playbook":               True,
        "enable_behavior":               True,
        "enable_confidence_calibration": True,
        "enable_regime_aware_thresholds": False,
    },
    "full_learning": {
        "description":     "+ regime-aware min_confidence (final layer)",
        "enable_playbook":               True,
        "enable_behavior":               True,
        "enable_confidence_calibration": True,
        "enable_regime_aware_thresholds": True,
    },
}


# ── Bench execution ───────────────────────────────────────────────────────────

def _make_cfg(base: StrategyConfig, variant: dict) -> StrategyConfig:
    """Clone the base config and apply variant-specific overrides."""
    payload = asdict(base)
    for k, v in variant.items():
        if k in payload and k != "description":
            payload[k] = v
    return StrategyConfig(**payload)


def _run_scenario(
    scen_path: str,
    base_cfg: StrategyConfig,
    variants: dict[str, dict],
) -> tuple[str, dict]:
    """Run all variants for a single scenario, serially (cache-friendly)."""
    scen = load_scenario(scen_path)
    log.info("─── Scenario: %s (%d cycles) ───", scen.name, scen.n_cycles)
    out: dict = {}
    for vname, vcfg in variants.items():
        cfg = _make_cfg(base_cfg, vcfg)
        log.info("  [%s] variant=%s (playbook=%s, behavior=%s)",
                 scen.name, vname, cfg.enable_playbook, cfg.enable_behavior)
        report = run(scen, cfg, version=vname)
        out[vname] = report
        m = report.metrics
        log.info(
            "    [%s] → %s | ret=%+.2f%% | α_vs_btc=%+.2f%% | DD=%.2f%% | "
            "Sharpe=%s | win=%s%% | trades=%d | tokens=%d",
            scen.name, vname,
            m.get("return_pct", 0),
            m.get("alpha_vs_btc_pct", 0),
            m.get("max_drawdown_pct", 0),
            m.get("sharpe") if m.get("sharpe") is not None else "—",
            f"{m.get('win_rate_pct'):.0f}" if m.get("win_rate_pct") is not None else "—",
            m.get("num_trades", 0),
            m.get("tokens_total", 0),
        )
    return scen.name, out


def run_bench(
    scenarios: list[str],
    base_cfg: StrategyConfig,
    variants: dict[str, dict] = VARIANTS,
    workers: int = 1,
) -> dict:
    """Run every (scenario × variant) combination. Returns nested results.

    When ``workers > 1``, scenarios run concurrently (one thread per scenario).
    Variants within a scenario stay serial so the LLM cache populated by
    ``baseline``/``playbook``/``full_prompt`` is reused by ``calibrated`` and
    ``full_learning`` (which produce identical prompts). Concurrency is bounded
    by the Ollama server's ``OLLAMA_NUM_PARALLEL`` (set it to >= workers).
    """
    results: dict[str, dict] = {}
    if workers <= 1:
        for scen_path in scenarios:
            name, out = _run_scenario(scen_path, base_cfg, variants)
            results[name] = out
        return results

    log.info("Parallel mode: %d concurrent scenarios", workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_run_scenario, p, base_cfg, variants): p for p in scenarios}
        for fut in as_completed(futures):
            name, out = fut.result()
            results[name] = out
    return results


# ── Reporting ────────────────────────────────────────────────────────────────

def _delta(a: float | None, b: float | None) -> str:
    """Format a − b with sign and width-aligned. None-tolerant."""
    if a is None or b is None:
        return "  n/a"
    d = a - b
    return f"{d:+6.2f}"


def print_comparison_table(results: dict, primary: str = "full_learning", baseline: str = "baseline") -> None:
    """Side-by-side table of metrics: baseline vs primary, delta column."""
    print("\n" + "═" * 100)
    print(f"BENCH RESULT — {primary} vs {baseline}")
    print("═" * 100)

    header = (
        f"{'Scenario':<30} {'Metric':<20} "
        f"{baseline:>10} {primary:>10} {'Δ':>10}"
    )
    print(header)
    print("─" * 100)

    metric_keys = [
        ("return_pct",        "Return %"),
        ("alpha_vs_btc_pct",  "Alpha vs BTC %"),
        ("max_drawdown_pct",  "Max DD %"),
        ("sharpe",            "Sharpe"),
        ("win_rate_pct",      "Win rate %"),
        ("num_trades",        "Num trades"),
    ]

    deltas_by_metric: dict[str, list[float]] = {k: [] for k, _ in metric_keys}
    for scen_name, by_variant in results.items():
        base = by_variant.get(baseline)
        cur  = by_variant.get(primary)
        if not (base and cur):
            continue
        for i, (key, label) in enumerate(metric_keys):
            b_val = base.metrics.get(key)
            c_val = cur.metrics.get(key)
            prefix = f"{scen_name:<30}" if i == 0 else " " * 30
            b_str  = f"{b_val:>10.2f}" if isinstance(b_val, int | float) and b_val is not None else f"{'—':>10}"
            c_str  = f"{c_val:>10.2f}" if isinstance(c_val, int | float) and c_val is not None else f"{'—':>10}"
            print(f"{prefix} {label:<20} {b_str} {c_str} {_delta(c_val, b_val):>10}")
            if isinstance(b_val, int | float) and isinstance(c_val, int | float):
                deltas_by_metric[key].append(c_val - b_val)
        print("─" * 100)

    # Aggregate verdict
    print("AGGREGATE Δ across scenarios:")
    n_scenarios = len(results)
    for key, label in metric_keys:
        deltas = deltas_by_metric[key]
        if not deltas:
            continue
        mean_d = sum(deltas) / len(deltas)
        # For drawdown a less-negative delta is the improvement direction.
        label_with_dir = f"{label} (less negative = better)" if key == "max_drawdown_pct" else label
        print(f"  {label_with_dir:<40} mean Δ = {mean_d:+.3f}  (n={len(deltas)}/{n_scenarios})")

    # Decision rule from the original spec: ship if it wins on ≥2/3 scenarios on Sharpe + DD
    print("─" * 100)
    sharpe_deltas = deltas_by_metric.get("sharpe", [])
    dd_deltas     = deltas_by_metric.get("max_drawdown_pct", [])
    sharpe_wins = sum(1 for d in sharpe_deltas if d > 0)
    dd_wins     = sum(1 for d in dd_deltas if d > 0)  # higher = less negative = better
    print(f"SHIP CRITERION — Sharpe wins: {sharpe_wins}/{len(sharpe_deltas)} | "
          f"DD wins: {dd_wins}/{len(dd_deltas)}")
    if sharpe_wins >= 2 and dd_wins >= 2:
        verdict = "✓ SHIP — wins on ≥2 scenarios across Sharpe AND drawdown"
    elif sharpe_wins >= 2 or dd_wins >= 2:
        verdict = "~ MIXED — wins on one axis only, investigate"
    else:
        verdict = "✗ HOLD — does not improve risk-adjusted metrics"
    print(f"VERDICT: {verdict}")
    print("═" * 100)


# ── Persistence ──────────────────────────────────────────────────────────────

def write_bench_report(results: dict, out_dir: Path) -> Path:
    """Persist the full bench results (variant × scenario) for traceability."""
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "variants":     {k: {kk: vv for kk, vv in v.items() if kk != "description"}
                         | {"description": v.get("description", "")}
                         for k, v in VARIANTS.items()},
        "results":      {
            scen: {var: {
                "version":     rep.version,
                "config":      rep.config,
                "metrics":     rep.metrics,
                "cycles_run":  rep.cycles_run,
                "num_trades":  rep.metrics.get("num_trades"),
            } for var, rep in by_variant.items()}
            for scen, by_variant in results.items()
        },
    }
    path = out_dir / f"bench_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return path


# ── CLI ──────────────────────────────────────────────────────────────────────

def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", default="eval/scenarios/holdout/compact/*.json",
                        help="Glob pattern for scenario files (default: compact 1d suite ; "
                             "use eval/scenarios/holdout/full/*.json for the 7d suite)")
    parser.add_argument("--budget",    type=float, default=1000.0)
    parser.add_argument("--risk-level", type=int, default=5)
    parser.add_argument("--provider",  default="rules",
                        help="'rules' (deterministic, free) or 'gemini'/'claude' for real LLM bench")
    parser.add_argument("--model",     default="")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--min-confidence", type=float, default=0.5)
    parser.add_argument("--out-dir",   default="eval/reports/bench")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--workers",   type=int, default=1,
                        help="Parallel scenarios. Bounded by OLLAMA_NUM_PARALLEL "
                             "on the Ollama server. Default 1 (sequential).")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    scenarios = sorted(glob.glob(args.scenarios))
    if not scenarios:
        log.error("No scenarios matched glob: %s", args.scenarios)
        return 2
    log.info("Scenarios (%d): %s", len(scenarios), [Path(s).stem for s in scenarios])

    base_cfg = StrategyConfig(
        budget=args.budget,
        risk_level=args.risk_level,
        provider=args.provider,
        model=args.model,
        temperature=args.temperature,
        min_confidence=args.min_confidence,
    )

    # Seed progress file with run-wide meta (started_at, variants, cycle count)
    # so readers can compute aggregate progress + ETA without rescanning JSON.
    first = load_scenario(scenarios[0])
    progress_init(
        scenario_names=[Path(s).stem for s in scenarios],
        variants_order=list(VARIANTS.keys()),
        n_cycles_per_scenario=first.n_cycles,
    )

    try:
        results = run_bench(scenarios, base_cfg, workers=args.workers)
        print_comparison_table(results)
        out_path = write_bench_report(results, Path(args.out_dir))
        log.info("Full bench report → %s", out_path)
        return 0
    finally:
        # Progress file is only useful mid-run; remove on completion or crash.
        try:
            PROGRESS_FILE.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(_main())
