"""Lightweight in-memory rate limiter for Flask routes.

Per-process, per-(route, IP) sliding window. Best-effort on serverless:
cold starts reset state, but the guarantee we need is "no single client
can spam an expensive endpoint in a tight loop within one container's
lifetime" — that holds.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from functools import wraps

from flask import jsonify, request

_lock: threading.Lock = threading.Lock()
_hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)


def _client_key() -> str:
    # Prefer X-Forwarded-For when behind a proxy (Vercel sets it).
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",", 1)[0].strip()
    return request.remote_addr or "unknown"


def rate_limit(max_calls: int, per_seconds: float) -> Callable:
    """Decorator: allow at most `max_calls` per `per_seconds` per (route, client)."""
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            key = (request.endpoint or request.path, _client_key())
            now = time.monotonic()
            cutoff = now - per_seconds
            with _lock:
                bucket = _hits[key]
                while bucket and bucket[0] < cutoff:
                    bucket.popleft()
                if len(bucket) >= max_calls:
                    retry_after = max(1, int(per_seconds - (now - bucket[0])))
                    resp = jsonify({"error": "Trop de requêtes — réessayer plus tard",
                                    "retry_after": retry_after})
                    resp.status_code = 429
                    resp.headers["Retry-After"] = str(retry_after)
                    return resp
                bucket.append(now)
            return view(*args, **kwargs)
        return wrapper
    return decorator
