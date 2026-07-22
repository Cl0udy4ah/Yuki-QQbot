"""Single-process sliding-window rate limiting."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    """Rate-limit decision for a user and optional group."""

    allowed: bool
    scope: str | None = None


class SlidingWindowRateLimiter:
    """Keep separate command/chat buckets for users and groups."""

    def __init__(
        self,
        *,
        per_user: int,
        per_group: int,
        window_seconds: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._per_user = per_user
        self._per_group = per_group
        self._window_seconds = window_seconds
        self._clock = clock
        self._buckets: defaultdict[tuple[str, str, str], deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(
        self,
        *,
        user_id: str,
        group_id: str | None,
        category: str,
    ) -> RateLimitResult:
        """Consume capacity atomically if both applicable scopes allow it."""

        async with self._lock:
            now = self._clock()
            user_key = (category, "user", user_id)
            self._prune(self._buckets[user_key], now)
            if len(self._buckets[user_key]) >= self._per_user:
                return RateLimitResult(False, "user")
            group_key: tuple[str, str, str] | None = None
            if group_id is not None:
                group_key = (category, "group", group_id)
                self._prune(self._buckets[group_key], now)
                if len(self._buckets[group_key]) >= self._per_group:
                    return RateLimitResult(False, "group")
            self._buckets[user_key].append(now)
            if group_key is not None:
                self._buckets[group_key].append(now)
            return RateLimitResult(True)

    def _prune(self, bucket: deque[float], now: float) -> None:
        cutoff = now - self._window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
