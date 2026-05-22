"""LLM provider abstraction.

Supported providers (set via ``config.json → llm.provider``):
- ``"claude"``  — Anthropic Claude  (requires ANTHROPIC_API_KEY)
- ``"gemini"``  — Google Gemini     (requires GEMINI_API_KEY)
- ``"ollama"``  — Ollama local          (requires Ollama running on localhost)

Both providers receive the same system prompt and user prompt, and must
return a JSON string matching the trading decision schema.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time

log = logging.getLogger(__name__)

# Transient HTTP status codes that warrant a retry
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 5
_RETRY_BASE_DELAY = 2.0  # seconds (doubles each attempt, with jitter)

# Stable fallback models used when the primary model stays unavailable.
_FALLBACK_MODELS = {
    "gemini": "gemini-2.0-flash",
    "claude": "claude-haiku-4-5",
}


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter to avoid thundering-herd retries."""
    base = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
    return base + random.uniform(0, base * 0.3)


# ── JSON helpers ──────────────────────────────────────────────────────────────

def _parse(raw: str) -> dict:
    """Parse JSON from a model response, robust against markdown fences and extra text."""
    text = raw.strip()
    # Strip ```json ... ``` or ``` ... ``` wrappers
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Extract the outermost {...} block (handles preamble / trailing text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    # Last resort: strip trailing commas before } or ] then retry
    cleaned = re.sub(r",\s*([}\]])", r"\1", text)
    return json.loads(cleaned)


# ── Provider implementations ──────────────────────────────────────────────────

def _claude_request(model: str, prompt: str, system: str, llm_cfg: dict) -> dict:
    from anthropic import Anthropic, APIStatusError

    max_tokens  = int(llm_cfg.get("max_tokens", 1000))
    temperature = float(llm_cfg.get("temperature", 1.0))
    client      = Anthropic()

    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            return _parse(resp.content[0].text)
        except APIStatusError as exc:
            if exc.status_code not in _TRANSIENT_STATUS or attempt == _MAX_RETRIES:
                raise
            delay = _backoff_delay(attempt)
            log.warning("[LLM] Claude(%s) %d — retry %d/%d dans %.1fs",
                        model, exc.status_code, attempt, _MAX_RETRIES, delay)
            time.sleep(delay)
            last_exc = exc
    raise last_exc  # type: ignore[misc]


def _call_claude(prompt: str, system: str, llm_cfg: dict) -> dict:
    model    = llm_cfg.get("model", "claude-opus-4-5")
    fallback = _FALLBACK_MODELS["claude"]
    try:
        return _claude_request(model, prompt, system, llm_cfg)
    except Exception as exc:
        if model == fallback:
            raise
        log.warning("[LLM] Claude(%s) indisponible, fallback → %s : %s", model, fallback, exc)
        return _claude_request(fallback, prompt, system, llm_cfg)


def _gemini_is_transient(exc: Exception) -> bool:
    """Detect transient Gemini errors (503/UNAVAILABLE, 429/RESOURCE_EXHAUSTED, etc)."""
    try:
        from google.api_core.exceptions import GoogleAPICallError  # type: ignore
    except ImportError:
        GoogleAPICallError = Exception  # type: ignore
    if isinstance(exc, GoogleAPICallError):
        status = getattr(exc, "grpc_status_code", None)
        http_status = getattr(exc, "code", None)
        if status is not None and status.value[0] in (14, 8):  # UNAVAILABLE, RESOURCE_EXHAUSTED
            return True
        if http_status in _TRANSIENT_STATUS:
            return True
    s = str(exc)
    return any(k in s for k in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "500", "502", "504"))


def _gemini_request(model: str, prompt: str, system: str, llm_cfg: dict) -> dict:
    from google import genai
    from google.genai import types

    max_tokens  = int(llm_cfg.get("max_tokens", 1000))
    temperature = float(llm_cfg.get("temperature", 1.0))
    client      = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = client.models.generate_content(
                model=model,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    max_output_tokens=max_tokens,
                    temperature=temperature,
                ),
                contents=prompt,
            )
            return _parse(resp.text)
        except Exception as exc:
            if not _gemini_is_transient(exc) or attempt == _MAX_RETRIES:
                raise
            delay = _backoff_delay(attempt)
            log.warning("[LLM] Gemini(%s) erreur transitoire — retry %d/%d dans %.1fs: %s",
                        model, attempt, _MAX_RETRIES, delay, exc)
            time.sleep(delay)
            last_exc = exc
    raise last_exc  # type: ignore[misc]


def _call_gemini(prompt: str, system: str, llm_cfg: dict) -> dict:
    model    = llm_cfg.get("model", "gemini-2.0-flash")
    fallback = _FALLBACK_MODELS["gemini"]
    try:
        return _gemini_request(model, prompt, system, llm_cfg)
    except Exception as exc:
        if model == fallback or not _gemini_is_transient(exc):
            raise
        log.warning("[LLM] Gemini(%s) indisponible après %d retries, fallback → %s",
                    model, _MAX_RETRIES, fallback)
        return _gemini_request(fallback, prompt, system, llm_cfg)


def _call_ollama(prompt: str, system: str, llm_cfg: dict) -> dict:
    import urllib.request

    model      = llm_cfg.get("model", "llama3.2")
    max_tokens = int(llm_cfg.get("max_tokens", 2000))
    base_url   = llm_cfg.get("base_url", "http://localhost:11434").rstrip("/")

    payload = json.dumps({
        "model":    model,
        "messages": [
            {"role": "system",  "content": system},
            {"role": "user",    "content": prompt},
        ],
        "stream":  False,
        "format":  "json",          # force structured JSON output
        "options": {"num_predict": max_tokens, "temperature": float(llm_cfg.get("temperature", 0.5))},
    }).encode()

    req = urllib.request.Request(
        f"{base_url}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return _parse(data["message"]["content"])


# ── Public interface ──────────────────────────────────────────────────────────

_PROVIDERS = {
    "claude": _call_claude,
    "gemini": _call_gemini,
    "ollama": _call_ollama,
}


def call(prompt: str, system: str, config: dict) -> dict:
    """Send *prompt* to the configured LLM and return the parsed JSON decision.

    Args:
        prompt:  User-turn content (built by ``prompts.build_analysis``).
        system:  System prompt defining the model's persona.
        config:  Full app config dict (reads ``config["llm"]``).

    Returns:
        Parsed trading decision dict.

    Raises:
        ValueError: If ``llm.provider`` is not a supported value.
        KeyError:   If the required API key env var is missing.
    """
    llm_cfg  = {**config.get("llm", {}), "max_tokens": config.get("max_tokens", 1000)}
    provider = llm_cfg.get("provider").lower()

    fn = _PROVIDERS.get(provider)
    if fn is None:
        supported = ", ".join(f'"{p}"' for p in _PROVIDERS)
        raise ValueError(
            f"LLM provider '{provider}' non supporté. "
            f"Valeurs acceptées : {supported}."
        )

    return fn(prompt, system, llm_cfg)
