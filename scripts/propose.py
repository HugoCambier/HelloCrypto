#!/usr/bin/env python3
"""Proposer-agent — autonomous strategy-parameter search, supervised promotion.

This is the research half of an "autonomous dev loop", deliberately scoped so
it can run unattended without ever touching money or merging code:

    propose candidates  →  bench on TRAIN  →  rank  →  bench winner on HOLDOUT
                        →  promotion gate   →  write a report a human approves

What it does NOT do (by design):
  - never edits config.json, never calls save_config, never touches live state;
  - never opens a PR or merges anything;
  - defaults to the deterministic ``rules`` decider, so a full run costs zero
    tokens and zero API calls — reproducible from a fixed seed.

The output is a single recommendation with numbers. A human reads
``eval/reports/proposer/<ts>.md`` and decides whether to adopt the params.

Search surface
--------------
Tunable ``StrategyConfig`` knobs (rule-decider thresholds + risk stops):
``min_confidence`` is LLM-only so it's excluded from the rules search.

Train / holdout separation
---------------------------
The promotion gate is only honest if the winner is chosen on data it was NOT
selected against. By default:
  - TRAIN   = eval/scenarios/holdout/compact/*.json  (1d, fast — search here)
  - HOLDOUT = eval/scenarios/holdout/full/*.json      (7d — gate only)
These overlap in market regime (same fear/greed × trend labels), so this is a
guard against threshold overfitting, not a clean walk-forward. Point
``--train-scenarios`` / ``--holdout-scenarios`` at disjoint regimes for a
stricter test once more scenarios exist.

Usage:
    poetry run python -m scripts.propose
    poetry run python -m scripts.propose --num-candidates 20 --seed 7
    poetry run python -m scripts.propose --provider gemini --model gemini-3.1-flash-lite
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import random
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any

# Project root on path (mirrors scripts/eval.py)
sys.path.insert(0, str(Path(__file__).parent.parent))

from hellocrypto.eval.bench import run_bench  # noqa: E402
from hellocrypto.eval.runner import StrategyConfig  # noqa: E402

log = logging.getLogger("propose")


# ── Search space ──────────────────────────────────────────────────────────────
# Each knob → discrete candidate values. The rules decider keys off
# buy_score_min / sell_score_max; the stops are strategy-wide.
SEARCH_SPACE: dict[str, list[Any]] = {
    "buy_score_min":        [7, 8, 9],
    "sell_score_max":       [2, 3, 4],
    "stop_loss_pct":        [15.0, 21.0, 30.0],
    "trailing_stop_pct":    [7.0, 10.0, 15.0],
    "sell_cooldown_cycles": [1, 3, 5],
}

# Ranking objective weights (composite, higher = better). Alpha is the primary
# signal; Sharpe rewards smoother equity curves; drawdown is penalised.
W_SHARPE = 1.0
W_DRAWDOWN = 0.5  # multiplies |mean drawdown| (drawdown is stored negative)

# Of the searched knobs, only these are decider-agnostic — they apply to the
# LIVE agent (Gemini/Claude) regardless of decider. buy_score_min/sell_score_max
# only steer the bench's `rules` decider, so they are NEVER written to
# config.json (they'd be meaningless for the live LLM agent) — only documented.
LIVE_APPLICABLE_KNOBS = {"stop_loss_pct", "trailing_stop_pct", "sell_cooldown_cycles"}
PROJECT_ROOT = Path(__file__).parent.parent


# ── Candidate generation ──────────────────────────────────────────────────────

def propose_candidates(n: int, seed: int) -> dict[str, dict[str, Any]]:
    """Sample ``n`` distinct parameter points from SEARCH_SPACE (seeded).

    Returns ``{candidate_name: override_dict}`` ready to feed to ``run_bench``
    as variants. This is the swap-in point for a smarter proposer later (e.g.
    an LLM that reads the behaviour journal and suggests prompt edits) — the
    contract is just "produce named override dicts".
    """
    rng = random.Random(seed)
    seen: set[tuple] = set()
    candidates: dict[str, dict[str, Any]] = {}
    attempts = 0
    while len(candidates) < n and attempts < n * 50:
        attempts += 1
        point = {knob: rng.choice(values) for knob, values in SEARCH_SPACE.items()}
        key = tuple(sorted(point.items()))
        if key in seen:
            continue
        seen.add(key)
        candidates[f"cand_{len(candidates):02d}"] = point
    return candidates


# ── Aggregation + scoring ──────────────────────────────────────────────────────

def _safe_mean(values: list[float | None]) -> float | None:
    nums = [v for v in values if isinstance(v, int | float)]
    return round(mean(nums), 4) if nums else None


def aggregate_variant(results: dict, variant: str) -> dict[str, float | None]:
    """Mean each metric for ``variant`` across all scenarios in ``results``."""
    per_scenario = [
        by_variant[variant].metrics
        for by_variant in results.values()
        if variant in by_variant
    ]
    keys = ("alpha_vs_btc_pct", "return_pct", "max_drawdown_pct",
            "sharpe", "win_rate_pct", "num_trades")
    return {k: _safe_mean([m.get(k) for m in per_scenario]) for k in keys}


def objective(agg: dict[str, float | None]) -> float:
    """Composite score (higher = better). None metrics count as 0/neutral."""
    alpha  = agg.get("alpha_vs_btc_pct") or 0.0
    sharpe = agg.get("sharpe") or 0.0
    dd     = agg.get("max_drawdown_pct") or 0.0  # negative
    return alpha + W_SHARPE * sharpe - W_DRAWDOWN * abs(dd)


# ── Promotion gate ──────────────────────────────────────────────────────────────

def holdout_gate(
    results: dict,
    baseline: str,
    candidate: str,
    dd_tolerance_pp: float,
) -> tuple[bool, str, dict]:
    """Decide if ``candidate`` should be recommended over ``baseline`` on holdout.

    Passes only if it beats baseline alpha on a MAJORITY of holdout scenarios
    AND its mean drawdown is not worse than baseline by more than the tolerance.
    This is the anti-overfitting guard: a candidate that only won on train
    (because the search optimised against train) gets rejected here.
    """
    per_scen_alpha_wins = 0
    n = 0
    for by_variant in results.values():
        if baseline not in by_variant or candidate not in by_variant:
            continue
        n += 1
        b_alpha = by_variant[baseline].metrics.get("alpha_vs_btc_pct") or 0.0
        c_alpha = by_variant[candidate].metrics.get("alpha_vs_btc_pct") or 0.0
        if c_alpha > b_alpha:
            per_scen_alpha_wins += 1

    agg_b = aggregate_variant(results, baseline)
    agg_c = aggregate_variant(results, candidate)
    b_dd = agg_b.get("max_drawdown_pct") or 0.0
    c_dd = agg_c.get("max_drawdown_pct") or 0.0
    dd_ok = c_dd >= b_dd - dd_tolerance_pp  # less negative = better

    alpha_majority = per_scen_alpha_wins > n // 2 if n else False
    mean_alpha_up  = (agg_c.get("alpha_vs_btc_pct") or 0.0) > (agg_b.get("alpha_vs_btc_pct") or 0.0)

    passed = alpha_majority and mean_alpha_up and dd_ok
    detail = {
        "alpha_wins": f"{per_scen_alpha_wins}/{n}",
        "mean_alpha_baseline":  agg_b.get("alpha_vs_btc_pct"),
        "mean_alpha_candidate": agg_c.get("alpha_vs_btc_pct"),
        "mean_dd_baseline":     b_dd,
        "mean_dd_candidate":    c_dd,
        "dd_tolerance_pp":      dd_tolerance_pp,
        "dd_ok":                dd_ok,
    }
    if passed:
        verdict = "PROMOTE — beats baseline on holdout alpha (majority) without worse drawdown"
    elif alpha_majority and not dd_ok:
        verdict = "REJECT — alpha up but drawdown degraded beyond tolerance"
    elif mean_alpha_up and not alpha_majority:
        verdict = "REJECT — mean alpha up but not on a majority of scenarios (fragile)"
    else:
        verdict = "REJECT — likely train overfit; does not beat baseline on holdout"
    return passed, verdict, detail


# ── Context: surface the last bench report (optional, informational) ─────────────

def latest_bench_summary(report_dirs: list[Path]) -> dict | None:
    files: list[Path] = []
    for d in report_dirs:
        if d.is_dir():
            files += list(d.glob("bench_*.json"))
    if not files:
        return None
    latest = max(files, key=lambda p: p.stat().st_mtime)
    try:
        data = json.loads(latest.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return {"file": str(latest), "generated_at": data.get("generated_at")}


# ── Report writing ──────────────────────────────────────────────────────────────

def write_report(out_dir: Path, payload: dict) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"propose_{ts}.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))

    md = _render_markdown(payload)
    md_path = out_dir / f"propose_{ts}.md"
    md_path.write_text(md)
    return json_path, md_path


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:+.2f}"
    return str(v)


def _fmtcount(v: Any) -> str:
    """Counts (trades) — rounded, unsigned."""
    return "—" if v is None else f"{round(v):d}"


def _render_markdown(p: dict) -> str:
    lines = [
        "# Proposer-agent report",
        "",
        f"- Generated: {p['generated_at']}",
        f"- Seed: {p['seed']} · candidates: {p['num_candidates']} · provider: `{p['provider']}`",
        f"- Train scenarios: {p['train_scenarios']}",
        f"- Holdout scenarios: {p['holdout_scenarios']}",
        "",
        "## Verdict",
        "",
        f"**{p['verdict']}**",
        "",
        "## Baseline (current config)",
        "",
        "```json",
        json.dumps(p["baseline_config"], indent=2),
        "```",
        "",
        "## Train ranking (top 5 by composite objective)",
        "",
        "| Rank | Candidate | Objective | Alpha vs BTC % | Sharpe | Max DD % | Trades | Overrides |",
        "|-----:|-----------|----------:|---------------:|-------:|---------:|-------:|-----------|",
    ]
    for i, row in enumerate(p["train_ranking"][:5], start=1):
        agg = row["aggregate"]
        ov = ", ".join(f"{k}={v}" for k, v in row["overrides"].items()) if row["overrides"] else "(baseline)"
        lines.append(
            f"| {i} | {row['name']} | {row['objective']:+.2f} | "
            f"{_fmt(agg.get('alpha_vs_btc_pct'))} | {_fmt(agg.get('sharpe'))} | "
            f"{_fmt(agg.get('max_drawdown_pct'))} | {_fmtcount(agg.get('num_trades'))} | {ov} |"
        )

    if p.get("winner"):
        w = p["winner"]
        lines += [
            "",
            "## Winning candidate (selected on TRAIN)",
            "",
            "```json",
            json.dumps(w["overrides"], indent=2),
            "```",
            "",
            "## Holdout gate (winner vs baseline — never seen during search)",
            "",
            "| Metric | Baseline | Winner |",
            "|--------|---------:|-------:|",
        ]
        gb = p["holdout"]["baseline_aggregate"]
        gc = p["holdout"]["winner_aggregate"]
        for key, label in [
            ("alpha_vs_btc_pct", "Alpha vs BTC %"),
            ("return_pct",       "Return %"),
            ("max_drawdown_pct", "Max DD %"),
            ("sharpe",           "Sharpe"),
            ("win_rate_pct",     "Win rate %"),
        ]:
            lines.append(f"| {label} | {_fmt(gb.get(key))} | {_fmt(gc.get(key))} |")
        lines.append(f"| Num trades | {_fmtcount(gb.get('num_trades'))} | "
                     f"{_fmtcount(gc.get('num_trades'))} |")
        lines += [
            "",
            f"- Per-scenario alpha wins: **{p['holdout']['detail']['alpha_wins']}**",
            f"- Drawdown OK (within {p['holdout']['detail']['dd_tolerance_pp']}pp): "
            f"**{p['holdout']['detail']['dd_ok']}**",
        ]

    lines += [
        "",
        "## Next step (human)",
        "",
        "This report changes nothing. If you approve the winner, apply its "
        "overrides to `config.json` (or a future per-run setting) yourself, or "
        "ask an agent to open a PR with these values — never auto-merged, never "
        "applied to a live run.",
        "",
    ]
    return "\n".join(lines)


# ── PR opening (PROMOTE only, worktree-isolated, never auto-merges) ─────────────

def _git(args: list[str], cwd: Path | None = None) -> str:
    res = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                         capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout.strip()


def _apply_config_overrides(config_path: Path, overrides: dict[str, Any]) -> dict[str, tuple]:
    """Write live-applicable overrides into config.json. Returns {key: (before, after)}."""
    cfg = json.loads(config_path.read_text())
    changed: dict[str, tuple] = {}
    for key in LIVE_APPLICABLE_KNOBS:
        if key not in overrides:
            continue
        new = overrides[key]
        if isinstance(new, float) and new.is_integer():
            new = int(new)  # keep config.json integer-clean (load_config float()s anyway)
        before = cfg.get(key)
        if before != new:
            cfg[key] = new
            changed[key] = (before, new)
    config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    return changed


def _pr_body(payload: dict, winner: dict, changed: dict[str, tuple]) -> str:
    h = payload["holdout"]
    applied = "\n".join(f"  - `{k}`: {b} → **{a}**" for k, (b, a) in changed.items()) or "  (none)"
    bench_only = {k: v for k, v in winner["overrides"].items() if k not in LIVE_APPLICABLE_KNOBS}
    provider = payload["provider"]
    warn = ("\n> ⚠️ Search ran with the **`rules`** decider. The applied stops/cooldown are "
            "decider-agnostic, but re-run `make propose ARGS=\"--provider gemini ...\"` to "
            "confirm the win transfers to the live LLM agent before merging.\n"
            if provider == "rules" else "")
    return f"""## Trader-agent proposal (auto-generated, **do not auto-merge**)

Holdout gate verdict: **{payload['verdict']}**
{warn}
### Applied to `config.json` (live-applicable knobs only)
{applied}

### Bench-only knobs (NOT applied — steer the `rules` decider, not the live agent)
```json
{json.dumps(bench_only, indent=2)}
```

### Holdout (winner vs baseline — never seen during search)
- Alpha vs BTC: {_fmt(h['baseline_aggregate'].get('alpha_vs_btc_pct'))} → {_fmt(h['winner_aggregate'].get('alpha_vs_btc_pct'))}
- Max drawdown: {_fmt(h['baseline_aggregate'].get('max_drawdown_pct'))} → {_fmt(h['winner_aggregate'].get('max_drawdown_pct'))}
- Per-scenario alpha wins: {h['detail']['alpha_wins']}

Provider: `{provider}` · seed: {payload['seed']} · candidates: {payload['num_candidates']}
Train: {payload['train_scenarios']} · Holdout: {payload['holdout_scenarios']}

---
This PR changes only the risk knobs above. It does **not** touch `enabled`/`mode`
and does **not** affect any running simulation. Human review + merge required.

🤖 Generated by `scripts/propose.py --open-pr`
"""


def open_pr(payload: dict, winner: dict) -> str | None:
    """Open a PR with the winner's live-applicable params, via an isolated worktree.

    Never switches your current branch, never merges, never flips enabled/live.
    Works regardless of a dirty working tree (the edit happens in a temp worktree
    branched off HEAD). Returns the PR URL, or None if nothing to apply / on error.
    """
    applicable = {k: v for k, v in winner["overrides"].items() if k in LIVE_APPLICABLE_KNOBS}
    if not applicable:
        log.warning("Winner has no live-applicable knobs (%s) — skipping PR.",
                    sorted(winner["overrides"]))
        return None

    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    branch = f"trader/propose-{ts}"
    wt = PROJECT_ROOT / ".worktrees" / f"propose-{ts}"
    wt.parent.mkdir(parents=True, exist_ok=True)

    try:
        _git(["worktree", "add", "-b", branch, str(wt), "HEAD"])
    except RuntimeError as exc:
        log.error("Could not create worktree: %s", exc)
        return None

    try:
        changed = _apply_config_overrides(wt / "config.json", applicable)
        if not changed:
            log.warning("Live-applicable knobs already match config.json — no diff, no PR.")
            return None
        _git(["add", "config.json"], cwd=wt)
        _git(["commit", "-m",
              f"trader: tune risk knobs ({', '.join(changed)}) — holdout PROMOTE"], cwd=wt)
        _git(["push", "-u", "origin", branch], cwd=wt)

        body = _pr_body(payload, winner, changed)
        res = subprocess.run(
            ["gh", "pr", "create", "--base", "main", "--head", branch,
             "--title", f"trader: tune {', '.join(changed)} (holdout PROMOTE)",
             "--body", body],
            cwd=str(wt), capture_output=True, text=True,
        )
        if res.returncode != 0:
            log.error("gh pr create failed: %s", res.stderr.strip())
            return None
        return res.stdout.strip()
    finally:
        # Always tear down the worktree; the branch stays (pushed) for review.
        try:
            _git(["worktree", "remove", str(wt), "--force"])
        except RuntimeError:
            pass


# ── Main ────────────────────────────────────────────────────────────────────────

def _base_config_from_file(provider: str, model: str, temperature: float,
                           budget: float, risk_level: int) -> StrategyConfig:
    """Build the baseline StrategyConfig from config.json (current prod settings)."""
    from hellocrypto.api import load_config
    cfg = load_config()
    return StrategyConfig(
        budget=budget,
        risk_level=risk_level if risk_level is not None else int(cfg.get("risk_level", 5)),
        stop_loss_pct=float(cfg.get("stop_loss_pct", 21.0)),
        trailing_stop_pct=float(cfg.get("trailing_stop_pct", 10.0)),
        sell_cooldown_cycles=int(cfg.get("sell_cooldown_cycles", 3)),
        provider=provider,
        model=model,
        temperature=temperature,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--train-scenarios",
                        default="eval/scenarios/holdout/compact/*.json",
                        help="Glob for the search/train set (fast).")
    parser.add_argument("--holdout-scenarios",
                        default="eval/scenarios/holdout/full/*.json",
                        help="Glob for the promotion gate (only the winner is run here).")
    parser.add_argument("--num-candidates", type=int, default=12)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--provider", default="rules",
                        help="'rules' (default, free, deterministic) or 'gemini'/'claude'.")
    parser.add_argument("--model", default="")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--budget", type=float, default=1000.0)
    parser.add_argument("--risk-level", type=int, default=None)
    parser.add_argument("--dd-tolerance-pp", type=float, default=2.0,
                        help="How many percentage-points worse the winner's mean "
                             "drawdown may be vs baseline before the gate rejects it.")
    parser.add_argument("--out-dir", default="eval/reports/proposer")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--open-pr", action="store_true",
                        help="On a PROMOTE verdict, open a PR with the winner's "
                             "live-applicable knobs (worktree-isolated, never "
                             "auto-merges, never touches enabled/live).")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    train = sorted(glob.glob(args.train_scenarios))
    holdout = sorted(glob.glob(args.holdout_scenarios))
    if not train:
        log.error("No TRAIN scenarios matched: %s", args.train_scenarios)
        return 2
    if not holdout:
        log.error("No HOLDOUT scenarios matched: %s", args.holdout_scenarios)
        return 2
    overlap = {Path(p).stem for p in train} & {Path(p).stem for p in holdout}
    if overlap:
        log.warning("TRAIN and HOLDOUT share scenarios %s — gate is not honest. "
                    "Use disjoint sets.", sorted(overlap))

    log.info("TRAIN (%d): %s", len(train), [Path(s).stem for s in train])
    log.info("HOLDOUT (%d): %s", len(holdout), [Path(s).stem for s in holdout])

    base_cfg = _base_config_from_file(
        args.provider, args.model, args.temperature, args.budget, args.risk_level,
    )
    log.info("Baseline config: %s", asdict(base_cfg))

    # ── 1. Propose candidates + run them on TRAIN (with the baseline) ──────────
    candidates = propose_candidates(args.num_candidates, args.seed)
    log.info("Proposed %d candidates (seed=%d)", len(candidates), args.seed)
    train_variants = {"baseline": {}, **candidates}
    train_results = run_bench(train, base_cfg, variants=train_variants, workers=args.workers)

    # ── 2. Rank candidates on TRAIN ───────────────────────────────────────────
    ranking = []
    for name, overrides in train_variants.items():
        agg = aggregate_variant(train_results, name)
        ranking.append({"name": name, "overrides": overrides,
                        "aggregate": agg, "objective": objective(agg)})
    ranking.sort(key=lambda r: r["objective"], reverse=True)

    baseline_obj = next(r["objective"] for r in ranking if r["name"] == "baseline")
    best = next((r for r in ranking if r["name"] != "baseline"
                 and r["objective"] > baseline_obj), None)

    payload: dict[str, Any] = {
        "generated_at":      datetime.now(UTC).isoformat(),
        "seed":              args.seed,
        "num_candidates":    args.num_candidates,
        "provider":          args.provider,
        "train_scenarios":   [Path(s).stem for s in train],
        "holdout_scenarios": [Path(s).stem for s in holdout],
        "baseline_config":   asdict(base_cfg),
        "train_ranking":     ranking,
        "last_bench":        latest_bench_summary(
            [Path("eval/reports/bench"), Path("data/eval_reports/bench")]),
        "winner":            None,
        "holdout":           None,
        "verdict":           "",
    }

    if best is None:
        payload["verdict"] = ("NO_CANDIDATE — none of the proposed parameter sets "
                              "beat the baseline objective on TRAIN. Baseline stands.")
        json_p, md_p = write_report(Path(args.out_dir), payload)
        _print_summary(payload, md_p)
        return 0

    # ── 3. Run the winner + baseline on HOLDOUT (the gate) ────────────────────
    log.info("Winner on TRAIN: %s %s — running holdout gate", best["name"], best["overrides"])
    holdout_variants = {"baseline": {}, "winner": best["overrides"]}
    holdout_results = run_bench(holdout, base_cfg, variants=holdout_variants, workers=args.workers)

    passed, verdict, detail = holdout_gate(
        holdout_results, "baseline", "winner", args.dd_tolerance_pp,
    )
    payload["winner"] = {"train_name": best["name"], "overrides": best["overrides"],
                         "train_aggregate": best["aggregate"]}
    payload["holdout"] = {
        "baseline_aggregate": aggregate_variant(holdout_results, "baseline"),
        "winner_aggregate":   aggregate_variant(holdout_results, "winner"),
        "detail":             detail,
        "passed":             passed,
    }
    payload["verdict"] = verdict

    pr_url = None
    if passed and args.open_pr:
        pr_url = open_pr(payload, payload["winner"])
        payload["pr_url"] = pr_url
    elif passed and not args.open_pr:
        log.info("PROMOTE verdict — pass --open-pr to open a PR with these params.")

    json_p, md_p = write_report(Path(args.out_dir), payload)
    _print_summary(payload, md_p, pr_url)
    return 0


def _print_summary(payload: dict, md_path: Path, pr_url: str | None = None) -> None:
    print("\n" + "═" * 78)
    print("PROPOSER-AGENT SUMMARY")
    print("═" * 78)
    print(f"Verdict: {payload['verdict']}")
    if payload.get("winner"):
        print(f"Winner overrides: {payload['winner']['overrides']}")
        h = payload["holdout"]
        print(f"Holdout alpha — baseline {_fmt(h['baseline_aggregate'].get('alpha_vs_btc_pct'))} "
              f"→ winner {_fmt(h['winner_aggregate'].get('alpha_vs_btc_pct'))} "
              f"(wins {h['detail']['alpha_wins']})")
    print(f"\nReport: {md_path}")
    if pr_url:
        print(f"PR opened (review + merge yourself): {pr_url}")
    print("Nothing was merged or applied to a live run.")
    print("═" * 78)


if __name__ == "__main__":
    sys.exit(main())
