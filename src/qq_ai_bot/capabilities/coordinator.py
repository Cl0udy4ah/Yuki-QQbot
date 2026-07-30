"""Provider-neutral ordered execution for one model tool-call batch."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Protocol

from qq_ai_bot.domain.messages import ToolCall


class CoordinatedToolBackend(Protocol):
    async def execute(self, name: str, arguments_json: str, runtime: Any) -> str: ...

    def parallel_safe(self, name: str, runtime: Any) -> bool: ...


@dataclass(frozen=True, slots=True)
class CoordinatedToolResult:
    calls: tuple[tuple[ToolCall, str, bool], ...]
    executed_count: int


class ToolInvocationCoordinator:
    """Run read-safe stretches concurrently while preserving model call order."""

    async def execute_batch(
        self,
        calls: tuple[ToolCall, ...],
        backend: CoordinatedToolBackend | None,
        runtime: Any,
        *,
        remaining_calls: int,
        max_parallel_calls: int,
    ) -> CoordinatedToolResult:
        if remaining_calls < 0 or max_parallel_calls <= 0:
            raise ValueError("tool call budgets must be non-negative and parallelism positive")
        if backend is None:
            unavailable = json.dumps(
                {"ok": False, "error": "tools_unavailable"},
                ensure_ascii=False,
            )
            return CoordinatedToolResult(
                tuple((call, unavailable, False) for call in calls),
                0,
            )
        executable = calls[:remaining_calls]
        overflow = calls[remaining_calls:]
        results: dict[str, str] = {}
        semaphore = asyncio.Semaphore(max_parallel_calls)

        async def execute_one(call: ToolCall) -> None:
            async with semaphore:
                results[call.id] = await backend.execute(
                    call.function.name,
                    call.function.arguments,
                    runtime,
                )

        def is_parallel_safe(call: ToolCall) -> bool:
            check = getattr(backend, "parallel_safe", None)
            return bool(callable(check) and check(call.function.name, runtime))

        index = 0
        while index < len(executable):
            call = executable[index]
            if not is_parallel_safe(call):
                await execute_one(call)
                index += 1
                continue
            end = index + 1
            while end < len(executable) and is_parallel_safe(executable[end]):
                end += 1
            async with asyncio.TaskGroup() as group:
                for candidate in executable[index:end]:
                    group.create_task(execute_one(candidate))
            index = end

        limited = json.dumps(
            {"ok": False, "error": "tool_limit_exceeded"},
            ensure_ascii=False,
        )
        ordered = tuple((call, results[call.id], True) for call in executable) + tuple(
            (call, limited, False) for call in overflow
        )
        return CoordinatedToolResult(ordered, len(executable))
