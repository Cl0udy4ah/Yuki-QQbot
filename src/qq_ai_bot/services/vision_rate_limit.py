"""Independent in-memory rate limiting for billable vision requests."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Callable


class VisionRateLimiter:
    """Bound billable vision calls without consuming the text-LLM limiter."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._user_windows: dict[str, deque[float]] = defaultdict(deque)
        self._group_windows: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(
        self,
        *,
        user_id: str,
        group_id: str | None,
        per_user_per_minute: int,
        per_group_per_minute: int,
    ) -> bool:
        """Reserve one provider request when both exact scopes have capacity."""

        now = self._clock()
        cutoff = now - 60.0
        async with self._lock:
            user_window = self._user_windows[user_id]
            self._prune(user_window, cutoff)
            group_window = self._group_windows[group_id] if group_id is not None else None
            if group_window is not None:
                self._prune(group_window, cutoff)
            if len(user_window) >= per_user_per_minute:
                return False
            if group_window is not None and len(group_window) >= per_group_per_minute:
                return False
            user_window.append(now)
            if group_window is not None:
                group_window.append(now)
            return True

    @staticmethod
    def _prune(window: deque[float], cutoff: float) -> None:
        while window and window[0] <= cutoff:
            window.popleft()
