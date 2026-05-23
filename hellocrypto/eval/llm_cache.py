"""Content-addressed cache for LLM decisions.

Replaying a scenario shouldn't burn tokens on every iteration. We key
each cached entry on (provider, model, system, prompt, temperature) hashed
with SHA-256. Same input → same cached output. Cache files live under
data/llm_cache/<sha>.json so they survive across processes/CI runs.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).parent.parent.parent / "data" / "llm_cache"


def _digest(provider: str, model: str, system: str, prompt: str,
            temperature: float) -> str:
    h = hashlib.sha256()
    for part in (provider, model, system, prompt, f"{temperature:.4f}"):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


def _path(key: str) -> Path:
    return _CACHE_DIR / f"{key}.json"


def get(provider: str, model: str, system: str, prompt: str,
        temperature: float) -> dict | None:
    p = _path(_digest(provider, model, system, prompt, temperature))
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        log.warning("Cache corrompu, ignoré: %s", p.name)
        return None


def put(provider: str, model: str, system: str, prompt: str,
        temperature: float, decision: dict, usage: dict | None = None) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(_digest(provider, model, system, prompt, temperature))
    p.write_text(json.dumps({"decision": decision, "usage": usage},
                            indent=2, ensure_ascii=False))


def clear() -> int:
    """Remove all cache entries. Returns number of files deleted."""
    if not _CACHE_DIR.exists():
        return 0
    n = 0
    for p in _CACHE_DIR.glob("*.json"):
        p.unlink()
        n += 1
    return n


def stats() -> dict:
    if not _CACHE_DIR.exists():
        return {"entries": 0, "bytes": 0}
    files = list(_CACHE_DIR.glob("*.json"))
    return {
        "entries": len(files),
        "bytes":   sum(p.stat().st_size for p in files),
    }
