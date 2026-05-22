"""Cron HTTP endpoint — pinged by GitHub Actions every 5 minutes.

GH Actions can't call Binance directly (US IPs → HTTP 451), so the scheduler
just curls this Vercel endpoint, which runs the tick from cdg1 where Binance
is reachable. Protected by a shared bearer token (CRON_SECRET env var).
"""
from __future__ import annotations

import logging
import os
import time

from flask import Blueprint, jsonify, request

from ..cron import tick

bp  = Blueprint("cron", __name__)
log = logging.getLogger(__name__)


@bp.post("/api/cron/tick")
def cron_tick():
    expected = os.getenv("CRON_SECRET", "")
    if not expected:
        return jsonify({"error": "CRON_SECRET not configured"}), 500
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {expected}":
        return jsonify({"error": "unauthorized"}), 401

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
