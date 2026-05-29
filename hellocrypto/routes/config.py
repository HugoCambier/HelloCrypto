"""Global config CRUD."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request

from ..api import load_config, save_config

bp  = Blueprint("config", __name__)
log = logging.getLogger(__name__)

# Surfaced to the frontend through `/api/config` (injected into the response
# via `cfg.setdefault("llm_models", _DEFAULT_LLM_MODELS)`) so the new-run
# modal can populate its provider/model dropdowns without an extra endpoint.
_DEFAULT_LLM_MODELS = {
    "claude": ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001",
               "claude-opus-4-5", "claude-haiku-4-5"],
    "gemini": ["gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-2.5-flash",
               "gemini-2.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.5-flash"],
    "ollama": ["llama3.2", "llama3.1", "mistral", "deepseek-r1", "qwen2.5"],
}


@bp.get("/api/config")
def config_get():
    """Return the full config object (NoSQL JSON blob)."""
    cfg = load_config()
    cfg.setdefault("llm_models", _DEFAULT_LLM_MODELS)
    return jsonify(cfg)


@bp.post("/api/config")
def config_set():
    """Partial update: merge body into existing config and persist.

    Side-effects on the ``enabled`` flag drive the real-mode session
    lifecycle: enabling the runner in real mode opens a new ``sessions``
    record (mode='real') and stores its id in ``agent_state.active_real_session_id``;
    disabling clears that pointer. The session record itself stays for
    historical viewing.
    """
    body = request.json or {}
    if not isinstance(body, dict):
        return jsonify({"error": "JSON object required"}), 400
    cfg_before = load_config()
    cfg = {**cfg_before, **body}
    save_config(cfg)
    _maybe_toggle_real_session(cfg_before, cfg)
    return jsonify({"ok": True, "config": cfg})


def _maybe_toggle_real_session(before: dict, after: dict) -> None:
    """Open/close a real-mode session record based on ``enabled`` + ``mode``.

    The DB's ``active_real_session_id`` is the source of truth — the cron
    refuses to fire real cycles when it is absent, regardless of what
    ``config.json`` says. This hook keeps that pointer in sync with the
    user's intent expressed through the UI:

    - Resume (enabled=true, mode=real) AND no active session → open one
      (also covers the self-healing case where ``cfg.enabled`` was already
      true on disk before the lifecycle hook existed)
    - Stop (enabled=false OR mode≠real) AND active session → clear pointer

    Idempotent and best-effort: a DB failure must never block a config save.
    """
    try:
        from db.store import get_state, set_state, upsert_session

        is_real_on = bool(after.get("enabled")) and after.get("mode") == "real"
        active_sid = (get_state("active_real_session_id") or "") or None

        if is_real_on and not active_sid:
            # Open a new real session — initial_state captures params at the
            # moment the user armed the runner so we can show them later.
            sid  = uuid.uuid4().hex[:8]
            name = "Réel " + datetime.utcnow().strftime("%Y-%m-%d %H:%M")
            initial_state = {
                "budget":               after.get("budget"),
                "cycle_seconds":        after.get("cycle_seconds"),
                "risk_level":           after.get("risk_level"),
                "stop_loss_pct":        after.get("stop_loss_pct"),
                "trailing_stop_pct":    after.get("trailing_stop_pct"),
                "watchlist":            after.get("watchlist", []),
                "decider":              "llm",
                "llm":                  after.get("llm"),
            }
            upsert_session(sid, name, mode="real", initial_state=initial_state)
            set_state("active_real_session_id", sid)
            set_state("active_real_session_name", name)
            log.info("[REAL-SESSION] Nouvelle session réelle ouverte: %s (%s)", sid, name)
        elif not is_real_on and active_sid:
            # Stop — clear the pointer; the record stays for history.
            set_state("active_real_session_id", None)
            set_state("active_real_session_name", None)
            log.info("[REAL-SESSION] Session réelle %s arrêtée (record conservé)", active_sid)
    except Exception:
        log.warning("Real-session lifecycle hook a échoué", exc_info=True)
