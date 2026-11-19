"""Small in-memory failure limiter. State is per process: with several API workers or
instances each keeps its own counters (use Redis for a shared limiter)."""
import threading
import time
from collections import deque


class FailureLimiter:
    def __init__(self, max_failures: int, window_seconds: float, max_keys: int = 10_000):
        self.max_failures = max_failures
        self.window = window_seconds
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float] | None:
        q = self._hits.get(key)
        if q is None:
            return None
        while q and now - q[0] > self.window:
            q.popleft()
        if not q:
            del self._hits[key]
            return None
        return q

    def retry_after(self, key: str) -> int:
        """0 if the key may proceed, otherwise seconds until it may try again."""
        now = time.monotonic()
        with self._lock:
            q = self._prune(key, now)
            if q is None or len(q) < self.max_failures:
                return 0
            return max(1, int(self.window - (now - q[0])) + 1)

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            if key not in self._hits and len(self._hits) >= self.max_keys:
                for k in list(self._hits):  # sweep expired keys first
                    self._prune(k, now)
                if len(self._hits) >= self.max_keys:  # still full: evict the stalest key
                    stalest = min(self._hits, key=lambda k: self._hits[k][-1])
                    del self._hits[stalest]
            self._hits.setdefault(key, deque()).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()


# Credential stuffing: many different accounts from one IP.
ip_limiter = FailureLimiter(max_failures=20, window_seconds=300)
# Guessing one account's password from one IP.
account_limiter = FailureLimiter(max_failures=5, window_seconds=900)


def account_key(ip: str, tenant: str, email: str) -> str:
    return f"{ip}|{tenant}|{email.lower()}"