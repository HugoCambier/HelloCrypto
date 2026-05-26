"""Frozen market-data scenarios used to replay the trading strategy.

A scenario is a sequence of N cycles, each capturing everything the strategy
needs (prices, indicators, F&G, BTC dominance) at a given timestamp. By
construction, replaying a scenario with the same strategy config and LLM
seed/cache produces the exact same trades — that's the property we need to
A/B-test prompts and feature sets.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_SCENARIOS_DIR = Path(__file__).parent.parent.parent / "eval" / "scenarios"


@dataclass
class Cycle:
    """One cycle worth of market context, ready to be fed to the strategy."""
    timestamp:     str
    market:        dict[str, dict[str, Any]]  # symbol → enriched indicators
    fear_greed:    dict[str, Any] | None = None
    btc_dominance: float | None = None


@dataclass
class Scenario:
    name:          str
    start:         str
    end:           str
    watchlist:     list[str]
    cycle_seconds: int
    cycles:        list[Cycle] = field(default_factory=list)
    note:          str = ""

    @property
    def n_cycles(self) -> int:
        return len(self.cycles)


def save(scenario: Scenario, path: Path | None = None) -> Path:
    """Persist a scenario to disk under eval/scenarios/<name>.json."""
    if path is None:
        _SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
        path = _SCENARIOS_DIR / f"{scenario.name}.json"
    payload = asdict(scenario)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return path


def load(name_or_path: str | Path) -> Scenario:
    """Load a scenario by short name (looked up in eval/scenarios/) or full path."""
    p = Path(name_or_path)
    if not p.suffix:
        p = _SCENARIOS_DIR / f"{p.name}.json"
    data = json.loads(p.read_text())
    cycles = [Cycle(**c) for c in data.pop("cycles", [])]
    return Scenario(cycles=cycles, **data)


def list_scenarios() -> list[str]:
    """Return all scenario names available under eval/scenarios/."""
    if not _SCENARIOS_DIR.exists():
        return []
    return sorted(p.stem for p in _SCENARIOS_DIR.glob("*.json"))
