"""Global config CRUD."""
from __future__ import annotations

import logging

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
    """Partial update: merge body into existing config and persist."""
    body = request.json or {}
    if not isinstance(body, dict):
        return jsonify({"error": "JSON object required"}), 400
    cfg = load_config()
    cfg.update(body)
    save_config(cfg)
    return jsonify({"ok": True, "config": cfg})
