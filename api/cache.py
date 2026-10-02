"""Layer 10 — identical work is not recomputed, static bytes are cacheable at the edge.

The purge rule lives in docs/CACHE.md and is enforced by `build_static.py`, which
fingerprints every asset so an edge cache never has to be purged for a deploy.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from typing import Any

_store: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()
_hits = _misses = 0


def etag_for(payload: Any) -> str:
    raw = (
        payload
        if isinstance(payload, (bytes, bytearray))
        else json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
    )
    return 'W/"' + hashlib.sha256(raw).hexdigest()[:24] + '"'


def memo(key: str, ttl_s: int, fn: Callable[[], Any]) -> Any:
    global _hits, _misses
    now = time.time()
    with _lock:
        got = _store.get(key)
        if got and got[0] > now:
            _hits += 1
            return got[1]
    value = fn()
    _misses += 1
    with _lock:
        _store[key] = (now + ttl_s, value)
        if len(_store) > 512:
            for k, (exp, _) in list(_store.items()):
                if exp <= now:
                    _store.pop(k, None)
    return value


def purge(prefix: str = "") -> int:
    """Explicit invalidation — called on every write that changes a cached read."""
    with _lock:
        keys = [k for k in _store if k.startswith(prefix)] if prefix else list(_store)
        for k in keys:
            _store.pop(k, None)
    return len(keys)


def stats() -> dict[str, int | float]:
    total = _hits + _misses
    return {
        "entries": len(_store),
        "hits": _hits,
        "misses": _misses,
        "hit_rate_pct": round(_hits / total * 100, 1) if total else 0.0,
    }


# --- HTTP cache policy ----------------------------------------------------
def policy_for_path(
    path: str, fingerprinted: bool, max_age_immutable: int, html_max_age: int
) -> str:
    if fingerprinted:
        return f"public, max-age={max_age_immutable}, immutable"
    if path.endswith((".html", "/")) or "." not in path.rsplit("/", 1)[-1]:
        return f"public, max-age={html_max_age}, must-revalidate"
    return "public, max-age=3600"
