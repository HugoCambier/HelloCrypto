"""Cron tick logic — shared between local runner and Vercel HTTP endpoint.

GitHub Actions runners are on US Azure IPs that Binance blocks (HTTP 451).
Vercel functions pinned to cdg1 (Paris) work fine. So the cron schedule
stays on GitHub Actions, but the actual work runs on Vercel via a curl
to /api/cron/tick.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime

log = logging.getLogger(__name__)


def tick(stop_event: threading.Event | None = None) -> dict:
    """Execute one cron tick.

    Priority:
      1. Active simulation in DB → run one cycle (time-gated by cycle_seconds)
      2. Else config.enabled and mode=real → run one real agent cycle
      3. Else no-op
    """
    import db.store as store
    from hellocrypto.api import load_config

    if stop_event is None:
        stop_event = threading.Event()

    active_sim = store.get_state("active_sim")
    if active_sim:
        return _run_sim_cycle(active_sim, stop_event)

    cfg = load_config()
    if not cfg.get("enabled", False):
        return {"action": "skip", "reason": "config.enabled=false"}

    mode = cfg.get("mode", "simulation")
    if mode == "real":
        from hellocrypto.agent import run_one_cycle
        run_one_cycle()
        return {"action": "real_cycle"}

    return {"action": "skip", "reason": f"mode={mode}"}


def _run_sim_cycle(active_sim: dict, stop_event: threading.Event) -> dict:
    import db.store as store
    from hellocrypto.api import load_config
    from hellocrypto import simulation as sim

    params         = active_sim.get("params") or {}
    cycle_seconds  = int(params.get("cycle_seconds") or 300)
    sid            = active_sim.get("session_id", "")
    sname          = active_sim.get("session_name", "")
    is_first_cycle = not active_sim.get("started")

    # Time-gate: skip if the previous cycle (for this session) is too recent.
    # `saved_at` is when the previous cycle FINISHED, but we want intervals
    # between cycle STARTS. Without tolerance, a 5-min cycle that finished at
    # 16:00:20 would block the 16:05:00 fire (elapsed=280s < 300s) and only
    # run at 16:10:00 — wrong. With a 60s tolerance, 16:05:00 executes
    # because 280s >= (300 - 60). Tolerance is larger than typical cycle
    # duration (~20-30s), so cycle_seconds > 300 (10 min, 15 min, ...) still
    # correctly skip intermediate fires.
    GATE_TOLERANCE_SEC = 60
    last_state = store.get_state("simulation") or {}
    if last_state.get("session_id") == sid and last_state.get("saved_at"):
        try:
            elapsed = (datetime.utcnow() - datetime.fromisoformat(last_state["saved_at"])).total_seconds()
            if elapsed < cycle_seconds - GATE_TOLERANCE_SEC:
                log.info("[SIM] %.0fs since last cycle (need %ds - %ds tolerance) — skip",
                         elapsed, cycle_seconds, GATE_TOLERANCE_SEC)
                return {"action": "sim_skip", "elapsed": elapsed, "cycle_seconds": cycle_seconds}
        except Exception:
            log.warning("[SIM] Could not parse saved_at, running anyway", exc_info=True)

    cfg     = load_config()
    budget  = float(params.get("budget") or cfg.get("budget", 100))
    run_cfg = {**cfg, **{k: v for k, v in params.items() if v is not None}}
    initial_holdings = active_sim.get("initial_holdings") if is_first_cycle else None

    log.info("[SIM] Running 1 cycle | session=%s | first=%s | cycle_seconds=%ds",
             sid, is_first_cycle, cycle_seconds)

    # Short-circuit the post-cycle wait inside sim.run by setting stop_event
    # from the on_cycle callback. Without this, sim.run(max_cycles=1) idles
    # for cycle_seconds before exiting.
    def _short_circuit(_c: int, _s: dict) -> None:
        stop_event.set()

    sim.run(
        budget,
        config=run_cfg,
        on_cycle=_short_circuit,
        stop_event=stop_event,
        resume=not is_first_cycle,
        max_cycles=1,
        initial_holdings=initial_holdings,
        session_id=sid,
        session_name=sname,
        liquidate_at_end=False,
    )

    if is_first_cycle:
        store.set_state("active_sim", {**active_sim, "started": True})

    user_max  = active_sim.get("max_cycles")
    sim_ended = False
    if user_max:
        latest = store.get_state("simulation") or {}
        if int(latest.get("cycle", 0)) >= int(user_max):
            log.info("[SIM] max_cycles=%d atteint — fin", user_max)
            if active_sim.get("liquidate_at_end"):
                stop_event.clear()
                sim.run(
                    budget, config=run_cfg,
                    on_cycle=_short_circuit, stop_event=stop_event,
                    resume=True, max_cycles=1, liquidate_at_end=True,
                    session_id=sid, session_name=sname,
                )
            store.set_state("active_sim", None)
            sim_ended = True

    return {"action": "sim_cycle", "session_id": sid, "ended": sim_ended}
