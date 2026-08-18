"""Small application-level request and concurrency budgets for paid AI work."""

import asyncio
import math
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int, reason: str):
        self.retry_after = max(1, retry_after)
        self.reason = reason
        super().__init__(reason)


class UserRateLimiter:
    """Enforce per-process per-user windows and in-flight limits.

    The current Render service runs a single API process, so this provides an
    immediate hard budget. The durable usage ledger will become the shared
    source of truth when worker concurrency is introduced.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = asyncio.Lock()
        self._requests: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._active: dict[tuple[str, str], int] = defaultdict(int)
        self._last_prune = 0.0

    def _prune_inactive_keys(self, now: float) -> None:
        """Discard expired idle users so one-off traffic cannot grow memory forever."""
        if now - self._last_prune < 60:
            return

        for key, window in list(self._requests.items()):
            while window and now - window[0] >= 60:
                window.popleft()
            if not window and not self._active.get(key, 0):
                self._requests.pop(key, None)
                self._active.pop(key, None)
        self._last_prune = now

    @asynccontextmanager
    async def limit(
        self,
        *,
        user_id: str,
        capability: str,
        requests_per_minute: int,
        max_concurrency: int,
    ) -> AsyncIterator[None]:
        key = (user_id, capability)
        now = self._clock()

        async with self._lock:
            self._prune_inactive_keys(now)
            window = self._requests[key]
            while window and now - window[0] >= 60:
                window.popleft()

            if self._active[key] >= max_concurrency:
                raise RateLimitExceeded(2, "concurrency_limit")
            if len(window) >= requests_per_minute:
                retry_after = math.ceil(60 - (now - window[0]))
                raise RateLimitExceeded(retry_after, "rate_limit")

            window.append(now)
            self._active[key] += 1

        try:
            yield
        finally:
            async with self._lock:
                self._active[key] = max(0, self._active[key] - 1)
                if not self._requests[key] and not self._active[key]:
                    self._requests.pop(key, None)
                    self._active.pop(key, None)


ai_rate_limiter = UserRateLimiter()
