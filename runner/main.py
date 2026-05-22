#!/usr/bin/env python3
"""HelloCrypto runner entry point.

La config (mode, budget, watchlist, enabled, ...) est lue depuis la DB
au démarrage de chaque exécution. Le flag --mode permet de surcharger
le mode depuis la ligne de commande (utile pour les tests).

  MODE GITHUB ACTIONS (défaut, RUNNER_LOOP non défini) :
    - Un seul cycle puis exit.
    - GitHub Actions déclenche le job toutes les 5 minutes.
    - Si config["enabled"] est False → exit immédiat.

  MODE BOUCLE CONTINUE (VM locale ou RUNNER_LOOP=true) :
    - Boucle infinie avec sleep entre cycles.
    - RUNNER_LOOP=true  OU  --loop  pour l'activer.

SIGTERM/SIGINT → arrêt propre en fin de cycle courant.
"""
import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

# Ensure the project root is on sys.path (works both locally and in Docker)
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("runner")

# Global stop event — set on SIGTERM/SIGINT for graceful shutdown
_stop = threading.Event()


def _handle_signal(signum, frame):
    log.info("Signal %d reçu — arrêt propre en cours...", signum)
    _stop.set()


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def _run_sim_cycle_if_due(active_sim: dict, stop_event: threading.Event) -> None:
    """Run a single simulation cycle if cycle_seconds has elapsed since the last one.

    Implements the "run + execute" dual-boolean pattern: active_sim presence is
    the "run" flag; the time delta vs. cycle_seconds is the "execute" gate.
    """
    from datetime import datetime
    import db.store as store
    from hellocrypto.api import load_config
    from hellocrypto import simulation as sim

    params         = active_sim.get("params") or {}
    cycle_seconds  = int(params.get("cycle_seconds") or 300)
    sid            = active_sim.get("session_id", "")
    sname          = active_sim.get("session_name", "")
    is_first_cycle = not active_sim.get("started")

    # Time-gate: skip if the previous cycle is too recent
    last_state = store.get_state("simulation") or {}
    if (
        last_state.get("session_id") == sid
        and last_state.get("saved_at")
    ):
        try:
            last_dt = datetime.fromisoformat(last_state["saved_at"])
            elapsed = (datetime.utcnow() - last_dt).total_seconds()
            if elapsed < cycle_seconds:
                log.info("[SIM] %.0fs since last cycle (need %ds) — skip", elapsed, cycle_seconds)
                return
        except Exception:
            log.warning("[SIM] Could not parse saved_at, running anyway", exc_info=True)

    cfg     = load_config()
    budget  = float(params.get("budget") or cfg.get("budget", 100))
    run_cfg = {**cfg, **{k: v for k, v in params.items() if v is not None}}
    initial_holdings = active_sim.get("initial_holdings") if is_first_cycle else None

    log.info("[SIM] Running 1 cycle | session=%s | first=%s | cycle_seconds=%ds",
             sid, is_first_cycle, cycle_seconds)

    # sim.run's main loop waits cycle_seconds AFTER each cycle before checking
    # max_cycles. With max_cycles=1 + cycle_seconds=300, that's a useless 5-min
    # wait every cron fire. Short-circuit by setting stop_event in on_cycle, so
    # the post-cycle wait returns immediately and the loop exits cleanly.
    def _short_circuit_after_cycle(_cycle: int, _snap: dict) -> None:
        stop_event.set()

    sim.run(
        budget,
        config=run_cfg,
        on_cycle=_short_circuit_after_cycle,
        stop_event=stop_event,
        resume=not is_first_cycle,
        max_cycles=1,
        initial_holdings=initial_holdings,
        session_id=sid,
        session_name=sname,
        liquidate_at_end=False,
    )

    # Persist "started=True" so subsequent fires resume instead of re-seeding
    if is_first_cycle:
        store.set_state("active_sim", {**active_sim, "started": True})

    # Check if user-requested max_cycles has been reached → end the simulation
    user_max = active_sim.get("max_cycles")
    if user_max:
        latest = store.get_state("simulation") or {}
        if int(latest.get("cycle", 0)) >= int(user_max):
            log.info("[SIM] max_cycles=%d atteint — fin de simulation", user_max)
            if active_sim.get("liquidate_at_end"):
                log.info("[SIM] Cycle de liquidation final")
                # Reset stop_event (set by short-circuit above) before re-using it
                stop_event.clear()
                sim.run(
                    budget, config=run_cfg,
                    on_cycle=_short_circuit_after_cycle,
                    stop_event=stop_event,
                    resume=True, max_cycles=1, liquidate_at_end=True,
                    session_id=sid, session_name=sname,
                )
            store.set_state("active_sim", None)


def main() -> None:
    parser = argparse.ArgumentParser(description="HelloCrypto runner")
    parser.add_argument(
        "--mode",
        choices=["real", "simulation"],
        default=None,
        help="Override config mode (real or simulation). Default: read from config.",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        default=os.getenv("RUNNER_LOOP", "").lower() in ("1", "true", "yes"),
        help="Loop continuously (VM/local mode). Default: single cycle then exit.",
    )
    args = parser.parse_args()

    # Initialise the data store (creates SQLite tables if needed)
    import db.store as store
    store.init_db()

    from hellocrypto.api import load_config
    cfg = load_config()

    # ── Dashboard-driven simulation: 1 cycle per cron fire, gated by elapsed time ──
    # When the user clicks "Start simulation" on the (serverless) dashboard, the
    # request is persisted to agent_state.active_sim instead of starting a thread
    # (which wouldn't survive Vercel's function lifecycle). We run one cycle per
    # cron fire, skipping if cycle_seconds hasn't elapsed since the last cycle.
    active_sim = store.get_state("active_sim")
    if active_sim:
        _run_sim_cycle_if_due(active_sim, _stop)
        sys.exit(0)

    if not cfg.get("enabled", False):
        log.info("Config.enabled=false — arrêt immédiat.")
        sys.exit(0)

    mode      = args.mode or cfg.get("mode", "simulation")
    cycle_sec = int(cfg.get("cycle_seconds", 300))

    from datetime import datetime
    store.set_state("last_run_at", datetime.utcnow().isoformat())

    log.info(
        "Runner démarré | mode=%s | loop=%s | cycle=%ss",
        mode, args.loop, cycle_sec,
    )

    if mode == "simulation":
        # La simulation gère sa propre boucle interne via stop_event.
        from hellocrypto import simulation as sim
        log.info("Mode simulation — boucle continue jusqu'à SIGTERM")
        sim.run(
            budget=float(cfg.get("budget", 100)),
            config=cfg,
            stop_event=_stop,
            resume=True,
            max_cycles=None,
        )
    elif args.loop:
        # VM / local mode : boucle infinie avec sleep entre cycles
        from hellocrypto.agent import run_one_cycle
        while not _stop.is_set():
            run_one_cycle()
            _stop.wait(timeout=cycle_sec)
    else:
        # GitHub Actions mode : un seul cycle puis exit
        from hellocrypto.agent import run_one_cycle
        run_one_cycle()

    log.info("Runner terminé proprement.")


if __name__ == "__main__":
    main()
