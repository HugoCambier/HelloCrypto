#!/usr/bin/env python3
"""HelloCrypto — Cloud Run Job entry point.

Usage:
    python runner/main.py --mode real         # live Binance trading
    python runner/main.py --mode simulation   # paper trading

Cloud Scheduler triggers this job at each configured interval.
SIGTERM is handled for graceful shutdown (Cloud Run sends it on job cancellation).
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
    # Also signal the agent if it's running
    try:
        from hellocrypto import agent as _agent
        _agent._stop_requested = True
    except Exception:
        pass


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
    args = parser.parse_args()

    # Initialise the data store (creates SQLite tables if needed)
    import db.store as store
    store.init_db()

    from hellocrypto.api import load_config
    cfg = load_config()

    log.info("Runner démarré | mode=%s | cycle=%ss", args.mode, cfg.get("cycle_seconds", 60))

    if args.mode == "simulation":
        from hellocrypto import simulation as sim
        sim.run(
            budget=float(cfg.get("budget", 100)),
            config=cfg,
            stop_event=_stop,
            resume=True,
        )
    else:
        from hellocrypto.agent import run_agent
        run_agent()

    log.info("Runner terminé proprement.")


if __name__ == "__main__":
    main()
