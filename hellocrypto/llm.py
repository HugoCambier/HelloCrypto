"""LLM provider abstraction.

Supported providers (set via ``config.json → llm.provider``):
- ``"claude"``  — Anthropic Claude  (requires ANTHROPIC_API_KEY)
- ``"gemini"``  — Google Gemini     (requires GEMINI_API_KEY)
- ``"ollama"``  — Ollama local          (requires Ollama running on localhost)

Both providers receive the same system prompt and user prompt, and must
return a JSON string matching the trading decision schema.
"""

import json
import os
import re


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

def _call_claude(prompt: str, system: str, llm_cfg: dict) -> dict:
    from anthropic import Anthropic

    model      = llm_cfg.get("model", "claude-opus-4-5")
    max_tokens = int(llm_cfg.get("max_tokens", 1000))
    client     = Anthropic()
    temperature = float(llm_cfg.get("temperature", 1.0))
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse(resp.content[0].text)


def _call_gemini(prompt: str, system: str, llm_cfg: dict) -> dict:
    from google import genai
    from google.genai import types

    model      = llm_cfg.get("model", "gemini-2.0-flash")
    max_tokens = int(llm_cfg.get("max_tokens", 1000))
    client     = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    temperature = float(llm_cfg.get("temperature", 1.0))
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
