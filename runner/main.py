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

    # ── Single-shot mode (default): delegate to the shared cron tick ──────────
    # Used by `python runner/main.py` locally without --loop. GitHub Actions
    # now pings Vercel's /api/cron/tick directly (Binance is blocked from GH
    # Actions runners), so this path is mainly for local dev / debugging.
    if not args.loop:
        from hellocrypto.cron import tick
        result = tick(_stop)
        log.info("Cron tick: %s", result)
        sys.exit(0)

    mode      = args.mode or cfg.get("mode", "simulation")
    cycle_sec = int(cfg.get("cycle_seconds", 300))

    # Source of truth for arming the real runner is ``active_real_session_id``
    # in DB, not ``config.enabled``. A stale enabled=true on disk (from a
    # previous Resume that wasn't followed by a Stop) must NOT cause the
    # local loop to auto-start — the user clicks Resume from the dashboard.
    if mode == "real" and not store.get_state("active_real_session_id"):
        log.info("Mode réel mais aucune session active en DB — arrêt immédiat. "
                 "Clique Resume sur 'Activité réelle' dans le dashboard pour démarrer.")
        sys.exit(0)

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
