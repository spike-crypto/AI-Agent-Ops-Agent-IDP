"""Small in-process rate limiter for login and public endpoints.

One process only (the app runs a single uvicorn worker). Account lockout is stored
on the User row, so it still works if you scale to multiple workers.
"""

import threading
import time
from collections import defaultdict, deque

_lock = threading.Lock()
_hits: dict[str, deque[float]] = defaultdict(deque)


def hit(key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Count one attempt. Returns (allowed, retry_after_seconds)."""
    now = time.monotonic()
    cutoff = now - window_seconds
    with _lock:
        bucket = _hits[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            return False, max(1, int(bucket[0] + window_seconds - now) + 1)
        bucket.append(now)
        return True, 0


def reset(key: str) -> None:
    with _lock:
        _hits.pop(key, None)


def clear_all() -> None:
    """Tests only."""
    with _lock:
        _hits.clear()
