"""Replay engine — runs the strategy against a frozen Scenario.

Designed for reproducibility:
- Market data comes from the scenario (frozen, never re-fetched).
- LLM calls are cached on disk by (provider, model, system, prompt, temp).
- A built-in deterministic rule-based decider lets you exercise the harness
  without any tokens (--no-llm mode).
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .. import prompts as prompts_mod
from .. import strategy
from ..api import compute_scores, format_market_data_compact
from . import llm_cache
from .metrics import summarize
from .scenario import Scenario

log = logging.getLogger(__name__)


# Inspectable progress file written after each cycle. Bench tasks update
# their slot; readers parse it to display a clean cross-scenario view
# without grepping the log. Removed by ``bench._main`` once the run ends.
PROGRESS_FILE = Path("eval/reports/_progress.json")
_progress_lock = threading.Lock()


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _compute_overall(state: dict) -> dict:
    """Aggregate completion + ETA across all scenarios/variants.

    Pace = cumulative work units / elapsed wall time. Self-correcting:
    cache-hit variants (calibrated/full_learning) fly through their cycles
    and pull the ETA closer to reality as the run progresses.
    """
    scenarios = state.get("scenarios", {}) or {}
    variants_order = state.get("variants_order", []) or []
    n_cycles = state.get("n_cycles", 0) or 0
    started_at_str = state.get("started_at")
    if not (scenarios and variants_order and n_cycles and started_at_str):
        return {}

    n_variants = len(variants_order)
    work_per_scenario = n_variants * n_cycles
    work_total = work_per_scenario * len(scenarios)
    work_done = sum(
        slot.get("variant_idx", 0) * n_cycles + slot.get("cycle", 0)
        for slot in scenarios.values() if slot
    )

    elapsed = (datetime.now(UTC) - datetime.fromisoformat(started_at_str)).total_seconds()
    pace = work_done / elapsed if elapsed > 0 else 0.0  # units/sec
    remaining = (work_total - work_done) / pace if pace > 0 else 0.0
    eta_ts = datetime.now(UTC).timestamp() + remaining

    return {
        "cycles_done":   work_done,
        "cycles_total":  work_total,
        "pct":           work_done * 100 // work_total if work_total else 0,
        "pace_per_min":  round(pace * 60, 1),
        "elapsed":       _fmt_duration(elapsed),
        "remaining":     _fmt_duration(remaining) if pace > 0 else "?",
        "eta_local":     datetime.fromtimestamp(eta_ts).strftime("%Y-%m-%d %H:%M") if pace > 0 else "—",
    }


def _progress_update(scenario_name: str, variant: str, cycle: int, n_cycles: int) -> None:
    """Atomically write this scenario's slot + aggregate ETA in the progress file.

    ``variant_idx`` is filled in by the bench startup writer (``progress_init``)
    so the reader can compute aggregate progress without knowing the variant
    ordering — we just merge cycle/variant here, preserving the existing meta.
    """
    try:
        with _progress_lock:
            state: dict = {}
            if PROGRESS_FILE.exists():
                try:
                    state = json.loads(PROGRESS_FILE.read_text())
                except (json.JSONDecodeError, OSError):
                    state = {}
            variants_order: list = state.get("variants_order", [])
            try:
                variant_idx = variants_order.index(variant)
            except ValueError:
                variant_idx = 0
            scen_slot = state.setdefault("scenarios", {}).setdefault(scenario_name, {})
            scen_slot.update({
                "variant":     variant,
                "variant_idx": variant_idx,
                "cycle":       cycle,
                "n_cycles":    n_cycles,
                "updated_at":  datetime.now(UTC).isoformat(timespec="seconds"),
            })
            state["overall"] = _compute_overall(state)
            PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = PROGRESS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2))
            tmp.replace(PROGRESS_FILE)
    except OSError:
        pass  # Progress tracking is best-effort, never block the bench.


def progress_init(scenario_names: list[str], variants_order: list[str], n_cycles_per_scenario: int) -> None:
    """Seed the progress file with run-wide metadata at bench startup.

    Stores ``started_at``, the variant ordering, and the per-scenario cycle
    count so readers can compute aggregate progress + ETA without rescanning
    scenario JSON files.
    """
    try:
        with _progress_lock:
            state = {
                "started_at":     datetime.now(UTC).isoformat(timespec="seconds"),
                "variants_order": list(variants_order),
                "n_cycles":       n_cycles_per_scenario,
                "scenarios":      {name: {} for name in scenario_names},
            }
            PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = PROGRESS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2))
            tmp.replace(PROGRESS_FILE)
    except OSError:
        pass


@dataclass
class StrategyConfig:
    budget:               float = 1000.0
    risk_level:           int   = 5
    stop_loss_pct:        float = 21.0
    trailing_stop_pct:    float = 10.0
    sell_cooldown_cycles: int   = 3
    min_confidence:       float = 0.0   # Phase E: gate buys when LLM emits low confidence
    # LLM
    provider:    str   = "rules"  # "rules" (no LLM), "gemini", "claude"
    model:       str   = ""
    temperature: float = 0.0
    max_tokens:  int   = 1000
    # Decision threshold for rule-based mode
    buy_score_min:  int = 7
    sell_score_max: int = 3
    # Learning-system toggles — let the bench A/B compare with/without each layer
    enable_playbook: bool = True
    enable_behavior: bool = True
    enable_confidence_calibration: bool = True
    # OFF by default everywhere because overfitting risk is real — turn on
    # only after the bench shows it improves things on held-out scenarios.
    enable_regime_aware_thresholds: bool = False


@dataclass
class RunReport:
    version:      str
    scenario:     str
    config:       dict
    metrics:      dict
    snapshots:    list[dict] = field(default_factory=list)
    trades:       list[dict] = field(default_factory=list)
    cycles_run:   int        = 0


def _rule_based_decision(market: dict, scores: dict, holdings: dict,
                         cfg: StrategyConfig) -> dict:
    """Deterministic baseline: buy top score above threshold, sell stale low scores."""
    actions = []
    for sym, sc in sorted(scores.items(), key=lambda kv: -kv[1]):
        if sc >= cfg.buy_score_min and sym not in holdings:
            actions.append({"type": "buy", "symbol": sym, "usdc_amount": 9999,
                            "score": sc, "horizon": "medium",
                            "reason": f"rule: score>={cfg.buy_score_min}"})
            break  # one buy per cycle
    for sym, pos in holdings.items():
        sc = scores.get(sym, 5)
        if sc <= cfg.sell_score_max:
            actions.append({"type": "sell", "symbol": sym, "qty": pos["qty"],
                            "score": sc,
                            "reason": f"rule: score<={cfg.sell_score_max}"})
    if not actions:
        actions.append({"type": "hold", "symbol": "", "score": 5,
                        "reason": "rule: pas de signal"})
    return {
        "market_sentiment": "neutral",
        "summary":          "rule-based baseline",
        "actions":          actions,
    }


DecisionFn = Callable[[dict, dict, dict, StrategyConfig], tuple[dict, dict | None]]


def _llm_decision_via_cache(prompt: str, system: str, cfg: StrategyConfig
                            ) -> tuple[dict, dict | None]:
    """Call the LLM with on-disk caching. Returns (decision, usage_or_None)."""
    cached = llm_cache.get(cfg.provider, cfg.model, system, prompt, cfg.temperature)
    if cached:
        return cached["decision"], cached.get("usage")
    from ..llm import call as llm_call
    from ..llm import last_usage
    from ..prompts import DECISION_SCHEMA
    decision = llm_call(
        prompt=prompt,
        system=system,
        config={"llm": {"provider": cfg.provider, "model": cfg.model,
                        "temperature": cfg.temperature,
                        "schema": DECISION_SCHEMA},
                "max_tokens": cfg.max_tokens},
    )
    usage = last_usage()
    llm_cache.put(cfg.provider, cfg.model, system, prompt, cfg.temperature,
                  decision, usage)
    return decision, usage


def run(
    scenario: Scenario,
    cfg: StrategyConfig,
    *,
    version: str = "v0",
    decision_fn: DecisionFn | None = None,
) -> RunReport:
    """Replay `scenario` with `cfg`. Returns a RunReport with metrics."""
    cash: float          = cfg.budget
    holdings: dict       = {}
    history: list        = []
    peak_prices: dict    = {}
    cooldown_map: dict   = {}
    snapshots: list      = []
    recent_decisions: list = []
    total_fees: float    = 0.0
    tokens_in = tokens_out = 0
    initial_prices: dict = {}
    btc_initial: float   = 0.0
    btc_final: float     = 0.0

    stop_loss  = cfg.stop_loss_pct  / 100
    trail_stop = cfg.trailing_stop_pct / 100

    n_cycles = len(scenario.cycles)
    for idx, cyc in enumerate(scenario.cycles, start=1):
        if idx == 1 or idx == n_cycles or idx % 5 == 0:
            log.info("    cycle %d/%d (%s)", idx, n_cycles, scenario.name)
        _progress_update(scenario.name, version, idx, n_cycles)
        prices = {sym: d["price"] for sym, d in cyc.market.items() if "price" in d}
        if not prices:
            continue

        if not initial_prices:
            initial_prices = dict(prices)
            btc_initial = next((p for s, p in prices.items() if "BTC" in s), 0.0)
        btc_final = next((p for s, p in prices.items() if "BTC" in s), btc_final)

        strategy.update_peak_prices(holdings, prices, peak_prices)

        # stop-loss / trailing
        recv, fees, stop_trades = strategy.apply_paper_stops(
            holdings, prices, peak_prices, cooldown_map,
            stop_loss, trail_stop, idx,
        )
        cash += recv
        total_fees += fees
        history.extend(t.to_history() for t in stop_trades)

        # decide
        scores = compute_scores(cyc.market)
        if decision_fn is not None:
            decision, usage = decision_fn(cyc.market, scores, holdings, cfg)
        elif cfg.provider == "rules":
            decision = _rule_based_decision(cyc.market, scores, holdings, cfg)
            usage = None
        else:
            # Optionally inject playbook + behavior sections so the bench can
            # A/B compare prompts with vs without the learning system.
            playbook_section = None
            behavior_section = None
            if cfg.enable_playbook:
                from .playbook import section_for_cycle as _pb_sec
                playbook_section = _pb_sec(cyc.fear_greed, cyc.market, scores=scores)
            if cfg.enable_behavior:
                from .behavior import section_for_cycle as _bh_sec
                behavior_section = _bh_sec(cyc.fear_greed, cyc.market)
            prompt = prompts_mod.build_analysis(
                market_data=format_market_data_compact(cyc.market, scenario.watchlist, scores),
                positions=holdings,
                cash=cash,
                budget=cfg.budget,
                risk_level=cfg.risk_level,
                recent_decisions=recent_decisions,
                fear_greed=cyc.fear_greed,
                btc_dominance=cyc.btc_dominance,
                scores=scores,
                prices=prices,
                peak_prices=peak_prices,
                cooldown_map=cooldown_map,
                total_fees=total_fees,
                cycle=idx,
                playbook_section=playbook_section,
                behavior_section=behavior_section,
            )
            decision, usage = _llm_decision_via_cache(prompt, prompts_mod.SYSTEM, cfg)

        if usage:
            tokens_in  += int(usage.get("in")  or 0)
            tokens_out += int(usage.get("out") or 0)
        recent_decisions = (recent_decisions + [decision])[-3:]

        calibration = None
        if cfg.enable_confidence_calibration:
            from .behavior import _cached_behavior
            _bh = _cached_behavior() or {}
            calibration = _bh.get("confidence_calibration")

        # Regime-aware threshold adjustment (off by default).
        effective_min_conf = cfg.min_confidence
        if cfg.enable_regime_aware_thresholds:
            from .playbook import _cached_playbook, current_regime, regime_aware_min_confidence
            _pb = _cached_playbook()
            btc_trend_1d = cyc.market.get("BTCUSDC", {}).get("trend_1d") if "BTCUSDC" in cyc.market else None
            _regime = current_regime(cyc.fear_greed, btc_trend_1d)
            effective_min_conf = regime_aware_min_confidence(_pb, _regime, cfg.min_confidence)

        new_cash, fees, action_trades = strategy.apply_paper_actions(
            actions=decision.get("actions", []),
            holdings=holdings, cash=cash, prices=prices,
            peak_prices=peak_prices, cooldown_map=cooldown_map,
            market_raw=cyc.market, cycle=idx,
            risk_level=cfg.risk_level,
            sell_cooldown_cycles=cfg.sell_cooldown_cycles,
            min_confidence=effective_min_conf,
            confidence_calibration=calibration,
        )
        cash = new_cash
        total_fees += fees
        history.extend(t.to_history() for t in action_trades)

        # snapshot total value
        portfolio_val = sum(h["qty"] * prices.get(sym, h["avg_price"])
                            for sym, h in holdings.items())
        total = cash + portfolio_val
        snapshots.append({"cycle": idx, "ts": cyc.timestamp,
                          "cash": round(cash, 2), "value": round(total, 2)})

    final_value = snapshots[-1]["value"] if snapshots else cfg.budget
    sells = [h for h in history if "SELL" in h["action"]]
    metrics = summarize(
        initial_value=cfg.budget,
        final_value=final_value,
        btc_initial=btc_initial,
        btc_final=btc_final,
        value_series=[s["value"] for s in snapshots],
        sells=sells,
        total_fees=total_fees,
        num_trades=len([h for h in history if h["action"] in ("BUY", "SELL")
                        or "SELL (" in h["action"]]),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cycle_seconds=scenario.cycle_seconds,
    )

    return RunReport(
        version=version,
        scenario=scenario.name,
        config={
            "budget":     cfg.budget,
            "risk_level": cfg.risk_level,
            "stop_loss":  cfg.stop_loss_pct,
            "trail_stop": cfg.trailing_stop_pct,
            "provider":   cfg.provider,
            "model":      cfg.model,
            "temperature": cfg.temperature,
        },
        metrics=metrics,
        snapshots=snapshots,
        trades=history,
        cycles_run=len(snapshots),
    )


def write_report(report: RunReport, out_dir: str | None = None) -> str:
    """Persist a RunReport to JSON. Returns the file path."""
    import json
    from pathlib import Path
    base = Path(out_dir) if out_dir else (Path(__file__).parent.parent.parent
                                          / "data" / "eval_reports")
    base.mkdir(parents=True, exist_ok=True)
    name = f"{report.version}__{report.scenario}__{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    path = base / name
    path.write_text(json.dumps({
        "version":    report.version,
        "scenario":   report.scenario,
        "config":     report.config,
        "metrics":    report.metrics,
        "snapshots":  report.snapshots,
        "trades":     report.trades,
        "cycles_run": report.cycles_run,
    }, indent=2, ensure_ascii=False, default=str))
    return str(path)
