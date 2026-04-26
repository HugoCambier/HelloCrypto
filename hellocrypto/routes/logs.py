"""SSE log stream + DB logs API."""
from __future__ import annotations

import json
import time

from flask import Blueprint, Response, jsonify, request

from .shared import _LOG_FILE

bp = Blueprint("logs", __name__)


@bp.get("/api/logs/stream")
def stream_logs():
    def generate():
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LOG_FILE.touch()
        with _LOG_FILE.open("r", encoding="utf-8") as fh:
            for line in fh.readlines()[-200:]:
                yield f"data: {json.dumps(line.rstrip())}\n\n"
            while True:
                line = fh.readline()
                if line:
                    yield f"data: {json.dumps(line.rstrip())}\n\n"
                else:
                    time.sleep(0.4)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@bp.get("/api/logs")
def api_logs():
    category   = request.args.get("category")
    mode       = request.args.get("mode")
    session_id = request.args.get("session_id")
    limit      = int(request.args.get("limit", 200))
    try:
        from db.store import load_logs
        return jsonify(load_logs(
            category=category or None,
            mode=mode or None,
            session_id=session_id or None,
            limit=limit,
        ))
    except ImportError:
        return jsonify([])
