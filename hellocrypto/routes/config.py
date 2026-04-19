"""LLM config & Ollama routes."""
import json
import logging
import subprocess
import sys

from flask import Blueprint, jsonify, request

from ..api import load_config, save_config

bp  = Blueprint("config", __name__)
log = logging.getLogger(__name__)

_DEFAULT_LLM_MODELS = {
    "claude": ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001",
               "claude-opus-4-5", "claude-haiku-4-5"],
    "gemini": ["gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-1.5-pro",
               "gemini-3.1-flash-lite-preview"],
    "ollama": ["llama3.2", "llama3.1", "mistral", "deepseek-r1", "qwen2.5"],
}


def _llm_models() -> dict:
    return load_config().get("llm_models", _DEFAULT_LLM_MODELS)


@bp.get("/api/config/llm")
def config_llm_get():
    cfg    = load_config()
    models = _llm_models()
    return jsonify({
        "provider":    cfg.get("llm", {}).get("provider", "gemini"),
        "model":       cfg.get("llm", {}).get("model", ""),
        "base_url":    cfg.get("llm", {}).get("base_url", "http://localhost:11434"),
        "temperature": float(cfg.get("llm", {}).get("temperature", 1.0)),
        "max_tokens":  int(cfg.get("max_tokens", 1000)),
        "providers":   list(models.keys()),
        "models":      models,
    })


@bp.post("/api/config/llm")
def config_llm_set():
    body     = request.json or {}
    provider = body.get("provider", "").lower().strip()
    model    = body.get("model", "").strip()
    base_url = body.get("base_url", "").strip()
    max_tok  = body.get("max_tokens")
    temp     = body.get("temperature")
    models   = _llm_models()

    if not provider or provider not in models:
        return jsonify({"error": f"Provider invalide. Valeurs: {list(models.keys())}"}), 400
    if not model:
        return jsonify({"error": "model requis"}), 400

    cfg = load_config()
    cfg["llm"] = {"provider": provider, "model": model}
    if base_url and provider == "ollama":
        cfg["llm"]["base_url"] = base_url
    if temp is not None:
        cfg["llm"]["temperature"] = max(0.0, min(2.0, float(temp)))
    if max_tok is not None:
        cfg["max_tokens"] = max(100, int(max_tok))
    save_config(cfg)
    return jsonify({"ok": True, "provider": provider, "model": model})


@bp.get("/api/ollama/status")
def ollama_status():
    import urllib.request as _ur
    cfg      = load_config()
    base_url = cfg.get("llm", {}).get("base_url", "http://localhost:11434").rstrip("/")
    try:
        with _ur.urlopen(f"{base_url}/api/tags", timeout=3) as r:
            tags = json.loads(r.read())
        models = [m["name"] for m in tags.get("models", [])]
        return jsonify({"running": True, "models": models})
    except Exception:
        log.warning("Ollama ne répond pas", exc_info=True)
        return jsonify({"running": False, "models": []})


@bp.post("/api/ollama/start")
def ollama_start():
    import urllib.request as _ur
    cfg      = load_config()
    base_url = cfg.get("llm", {}).get("base_url", "http://localhost:11434").rstrip("/")
    try:
        with _ur.urlopen(f"{base_url}/api/tags", timeout=2):
            return jsonify({"ok": True, "already_running": True})
    except Exception:
        log.debug("Ollama pas encore accessible, tentative de lancement")
    try:
        if sys.platform == "darwin":
            cmd = ["open", "-a", "Ollama"]
        else:
            cmd = ["ollama", "serve"]
        log.info("Lancement Ollama: %s", " ".join(cmd))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            proc.wait(timeout=1.0)
            stderr_out = proc.stderr.read().decode(errors="replace").strip()
            if proc.returncode != 0:
                log.error("Ollama a quitté immédiatement (code %d): %s", proc.returncode, stderr_out)
                return jsonify({"ok": False, "error": "Ollama a quitté immédiatement"}), 500
        except subprocess.TimeoutExpired:
            pass  # Process still running — normal for a server
        return jsonify({"ok": True, "already_running": False})
    except Exception as exc:
        log.exception("Erreur lancement Ollama")
        return jsonify({"ok": False, "error": "Impossible de lancer Ollama"}), 500
