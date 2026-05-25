"""Cron HTTP endpoints — two surfaces, two schedules.

Both pinged by cron-job.org (GH Actions schedules proved unreliable):

- ``/api/cron/tick``  → every 5 min, runs the trading critical path only
                        (active simulation cycle, real agent cycle, log purge).
                        Must stay vif (<5s typical).
- ``/api/cron/learn`` → once per day at 03:00 UTC, runs the learning batch
                        (playbook + behavior rebuilds). Decoupled from the
                        trading tick so a slow rebuild can never delay a
                        decision cycle.

Both protected by the same ``CRON_SECRET`` bearer.
"""
from __future__ import annotations

import logging
import os
import time

from flask import Blueprint, jsonify, request

from ..cron import _maybe_rebuild_behavior, _maybe_rebuild_playbook, tick

bp  = Blueprint("cron", __name__)
log = logging.getLogger(__name__)


def _authorized(req) -> tuple[bool, tuple]:
    """Return (ok, error_response_tuple). Shared by both endpoints."""
    expected = os.getenv("CRON_SECRET", "")
    if not expected:
        return False, (jsonify({"error": "CRON_SECRET not configured"}), 500)
    auth = req.headers.get("Authorization", "")
    if auth != f"Bearer {expected}":
        return False, (jsonify({"error": "unauthorized"}), 401)
    return True, ()


@bp.post("/api/cron/tick")
def cron_tick():
    ok, err = _authorized(request)
    if not ok:
        return err

    started = time.time()
    try:
        result = tick()
    except Exception as exc:
        log.exception("Cron tick crashed")
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({
        "ok":         True,
        "result":     result,
        "elapsed_s":  round(time.time() - started, 2),
    })


@bp.post("/api/cron/learn")
def cron_learn():
    """Trigger playbook + behavior rebuilds. Idempotent — sentinel-gated so
    a duplicate ping within the cool-down window is a no-op.
    """
    ok, err = _authorized(request)
    if not ok:
        return err

    started = time.time()
    try:
        _maybe_rebuild_playbook()
        _maybe_rebuild_behavior()
    except Exception as exc:
        log.exception("Cron learn crashed")
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({
        "ok":        True,
        "elapsed_s": round(time.time() - started, 2),
    })
