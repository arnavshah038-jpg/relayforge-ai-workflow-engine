import threading
import time
from collections import defaultdict, deque


class FailureLimiter:
    """Counts failures per key inside a sliding window.
    In memory, so it is per process: fine for one server, use Redis for several."""

    def __init__(self, max_failures: int, window_s: float):
        self.max_failures = max_failures
        self.window_s = window_s
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, key: str, now: float) -> deque:
        q = self._hits[key]
        while q and now - q[0] > self.window_s:
            q.popleft()
        if not q:
            self._hits.pop(key, None)
        return q

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._trim(key, time.monotonic())) >= self.max_failures

    def add_failure(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._trim(key, now)
            self._hits[key].append(now)

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
