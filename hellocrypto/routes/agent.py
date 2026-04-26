"""Agent process lifecycle routes."""
from __future__ import annotations

import subprocess
import sys

from flask import Blueprint, jsonify

from .shared import _ROOT

bp = Blueprint("agent", __name__)

_agent_process = None


@bp.get("/api/agent/status")
def agent_status():
    global _agent_process
    running = _agent_process is not None and _agent_process.poll() is None
    return jsonify({"running": running, "pid": _agent_process.pid if running else None})


@bp.post("/api/agent/start")
def agent_start():
    global _agent_process
    if _agent_process and _agent_process.poll() is None:
        return jsonify({"status": "already_running", "pid": _agent_process.pid})
    _agent_process = subprocess.Popen(
        [sys.executable, "-m", "hellocrypto.agent"],
        cwd=str(_ROOT),
    )
    return jsonify({"status": "started", "pid": _agent_process.pid})


@bp.post("/api/agent/stop")
def agent_stop():
    global _agent_process
    if _agent_process and _agent_process.poll() is None:
        _agent_process.terminate()
        _agent_process.wait(timeout=5)
        return jsonify({"status": "stopped"})
    return jsonify({"status": "not_running"})
