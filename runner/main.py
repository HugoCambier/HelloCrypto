#!/usr/bin/env python3
"""HelloCrypto — Cloud Run Job / local runner entry point.

Deux modes d'exécution :

  MODE CLOUD RUN JOB (défaut, RUNNER_LOOP non défini) :
    - Un seul cycle puis exit.
    - Cloud Scheduler déclenche le job à chaque intervalle configuré.
    - Usage : python runner/main.py --mode real

  MODE BOUCLE CONTINUE (VM locale ou prod avec RUNNER_LOOP=true) :
    - Boucle infinie avec sleep entre cycles.
    - RUNNER_LOOP=true  OU  --loop  pour l'activer.
    - Usage : RUNNER_LOOP=true python runner/main.py --mode simulation
    - Note : en mode simulation, --loop est TOUJOURS activé car la simulation
      gère elle-même sa boucle interne (stop_event).

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


def main() -> None:
    parser = argparse.ArgumentParser(description="HelloCrypto runner")
    parser.add_argument(
        "--mode",
        choices=["real", "simulation"],
        default=os.getenv("RUNNER_MODE", "real"),
        help="Trading mode: real (Binance) or simulation (paper trading)",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        default=os.getenv("RUNNER_LOOP", "").lower() in ("1", "true", "yes"),
        help="Loop continuously (VM/local mode). Default: single cycle then exit (Cloud Run Job mode).",
    )
    args = parser.parse_args()

    # Initialise the data store (creates SQLite tables if needed)
    import db.store as store
    store.init_db()

    from hellocrypto.api import load_config
    cfg = load_config()
    cycle_sec = int(cfg.get("cycle_seconds", 1800))

    log.info(
        "Runner démarré | mode=%s | loop=%s | cycle=%ss",
        args.mode, args.loop, cycle_sec,
    )

    if args.mode == "simulation":
        # La simulation gère sa propre boucle interne via stop_event.
        # On passe toujours max_cycles=None pour qu'elle tourne indéfiniment
        # jusqu'à réception de SIGTERM/SIGINT (qui déclenche _stop).
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
        import time
        from hellocrypto.agent import run_one_cycle
        while not _stop.is_set():
            run_one_cycle()
            _stop.wait(timeout=cycle_sec)
    else:
        # Cloud Run Job mode : un seul cycle puis exit
        # Cloud Scheduler se charge de déclencher au bon interval
        from hellocrypto.agent import run_one_cycle
        run_one_cycle()

    log.info("Runner terminé proprement.")


if __name__ == "__main__":
    main()
