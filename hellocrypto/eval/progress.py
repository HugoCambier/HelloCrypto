"""Pretty-print the live bench progress file (eval/reports/_progress.json).

Reads the JSON written by ``runner._progress_update`` and computes:
- per-scenario advancement (current variant + cycle / total)
- aggregate completion % across (scenarios × variants × cycles)
- pace and ETA from elapsed wall time

Invoked via ``make bench-progress``. Returns exit code 1 if no bench is
running (file absent), 0 otherwise.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from .runner import PROGRESS_FILE


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def main() -> int:
    if not PROGRESS_FILE.exists():
        print(f"No bench running ({PROGRESS_FILE} absent)")
        return 0
    try:
        state = json.loads(PROGRESS_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Could not read {PROGRESS_FILE}: {exc}")
        return 1

    scenarios = state.get("scenarios", {}) or {}
    variants_order = state.get("variants_order", []) or []
    n_cycles = state.get("n_cycles", 0) or 0
    started_at_str = state.get("started_at")
    if not (scenarios and variants_order and n_cycles and started_at_str):
        print("Progress file present but missing run metadata — likely the bench just started.")
        return 0

    n_variants = len(variants_order)
    work_per_scenario = n_variants * n_cycles
    work_total = work_per_scenario * len(scenarios)

    # Per-scenario lines + cumulative work units done.
    work_done = 0
    lines: list[str] = []
    for name in sorted(scenarios):
        slot = scenarios[name]
        if not slot:
            lines.append(f"  {name}: (not started)")
            continue
        variant     = slot.get("variant", "?")
        variant_idx = slot.get("variant_idx", 0)
        cycle       = slot.get("cycle", 0)
        scen_done   = variant_idx * n_cycles + cycle
        work_done  += scen_done
        scen_pct    = scen_done * 100 // work_per_scenario if work_per_scenario else 0
        var_pct     = cycle * 100 // n_cycles if n_cycles else 0
        lines.append(
            f"  {name}: {variant} ({variant_idx+1}/{n_variants}) "
            f"cycle {cycle:>3}/{n_cycles} ({var_pct:>3}%) "
            f"— scénario {scen_pct:>3}%"
        )

    started_at = datetime.fromisoformat(started_at_str)
    now = datetime.now(UTC)
    elapsed = (now - started_at).total_seconds()

    overall_pct = work_done * 100 // work_total if work_total else 0
    pace = work_done / elapsed if elapsed > 0 else 0  # work units / sec
    remaining = (work_total - work_done) / pace if pace > 0 else 0
    eta = now.timestamp() + remaining
    eta_str = datetime.fromtimestamp(eta).strftime("%H:%M") if pace > 0 else "—"

    print(f"Bench started: {started_at.strftime('%H:%M:%S')} — elapsed {_fmt_duration(elapsed)}")
    print(f"Variants: {' → '.join(variants_order)}")
    print(f"Scenarios:")
    for line in lines:
        print(line)
    print(
        f"\nOverall: {work_done}/{work_total} cycles ({overall_pct}%) "
        f"— pace {pace*60:.1f} cycles/min — "
        f"ETA {eta_str} (reste {_fmt_duration(remaining) if pace > 0 else '?'})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
