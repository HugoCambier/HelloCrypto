"""DB logs API."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

bp = Blueprint("logs", __name__)


@bp.get("/api/logs")
def api_logs():
    category   = request.args.get("category")
    mode       = request.args.get("mode")
    session_id = request.args.get("session_id")
    since      = request.args.get("since")
    limit      = int(request.args.get("limit", 200))
    try:
        from db.store import load_logs
        return jsonify(load_logs(
            category=category or None,
            mode=mode or None,
            session_id=session_id or None,
            limit=limit,
            since=since or None,
        ))
    except ImportError:
        return jsonify([])
