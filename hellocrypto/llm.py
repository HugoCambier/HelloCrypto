"""LLM provider abstraction.

Supported providers (set via ``config.json → llm.provider``):
- ``"claude"``  — Anthropic Claude  (requires ANTHROPIC_API_KEY)
- ``"gemini"``  — Google Gemini     (requires GEMINI_API_KEY)

Both providers receive the same system prompt and user prompt, and must
return a JSON string matching the trading decision schema.
"""

import json
import os
import re


# ── JSON helpers ──────────────────────────────────────────────────────────────

def _parse(raw: str) -> dict:
    """Parse JSON from a model response, stripping any markdown code fences."""
    text = raw.strip()
    # Remove ```json ... ``` or ``` ... ``` wrappers
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text.strip())


# ── Provider implementations ──────────────────────────────────────────────────

def _call_claude(prompt: str, system: str, llm_cfg: dict) -> dict:
    from anthropic import Anthropic

    model      = llm_cfg.get("model", "claude-opus-4-5")
    max_tokens = int(llm_cfg.get("max_tokens", 1000))
    client     = Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
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
    resp = client.models.generate_content(
        model=model,
        config=types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
        ),
        contents=prompt,
    )
    return _parse(resp.text)


# ── Public interface ──────────────────────────────────────────────────────────

_PROVIDERS = {
    "claude": _call_claude,
    "gemini": _call_gemini,
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
    provider = llm_cfg.get("provider", "claude").lower()

    fn = _PROVIDERS.get(provider)
    if fn is None:
        supported = ", ".join(f'"{p}"' for p in _PROVIDERS)
        raise ValueError(
            f"LLM provider '{provider}' non supporté. "
            f"Valeurs acceptées : {supported}."
        )

    return fn(prompt, system, llm_cfg)
