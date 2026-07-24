"""Persistent small-batch extraction of person, group, and membership memories."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

from qq_ai_bot.config import Settings
from qq_ai_bot.domain.messages import ChatMessage, ChatRequest
from qq_ai_bot.llm.base import LLMError, LLMProvider
from qq_ai_bot.persistence.repositories import (
    EventRecord,
    MemoryJobRecord,
    MemoryJobRepository,
    MemoryRepository,
)
from qq_ai_bot.services.concurrency import ConcurrencyManager

logger = logging.getLogger(__name__)


class MemoryWorker:
    """Wake every interval or queue threshold and process at most 20 events."""

    def __init__(
        self,
        *,
        settings: Settings,
        jobs: MemoryJobRepository,
        memories: MemoryRepository,
        provider: LLMProvider,
        concurrency: ConcurrencyManager,
    ) -> None:
        self._settings = settings
        self._jobs = jobs
        self._memories = memories
        self._provider = provider
        self._concurrency = concurrency
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._queued_since_wake = 0

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="memory-worker")

    async def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            await self._task

    async def enqueue(self, event_id: int) -> None:
        await self._jobs.enqueue(event_id)
        self._queued_since_wake += 1
        if self._queued_since_wake >= self._settings.memory_batch_trigger_count:
            self._queued_since_wake = 0
            self._wake.set()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self._settings.memory_batch_seconds
                )
            except TimeoutError:
                pass
            self._wake.clear()
            if self._stop.is_set():
                break
            await self.process_once()

    async def process_once(self) -> int:
        jobs = await self._jobs.claim(limit=self._settings.memory_batch_max_events)
        if not jobs:
            return 0
        try:
            operations = await self._extract(jobs)
            await self._apply(operations, jobs)
        except (LLMError, OSError, RuntimeError, TypeError, ValueError) as exc:
            category = type(exc).__name__
            logger.warning("memory_batch_failed exception_category=%s", category)
            for job in jobs:
                await self._jobs.fail(job.job_id, category)
            return 0
        await self._jobs.complete(tuple(job.job_id for job in jobs))
        return len(jobs)

    async def _extract(self, jobs: tuple[MemoryJobRecord, ...]) -> list[dict[str, Any]]:
        events = [self._event_json(job.event) for job in jobs]
        request = ChatRequest(
            model=self._settings.llm_model or "fake",
            temperature=0.1,
            max_output_tokens=min(self._settings.llm_max_output_tokens, 2048),
            thinking_enabled=False,
            messages=(
                ChatMessage(
                    role="system",
                    content=(
                        "你是 QQ 记忆整理器。只提取未来聊天有用、可验证的稳定事实、"
                        "称呼、关系、习惯和机器人交互偏好。不要保存临时闲聊。"
                        "输出 JSON 数组；每项字段：scope(person|group|person_group|preference)、"
                        "user_id、group_id、key、category、content、importance(1-5)、"
                        "source_type(automatic|explicit)。出现“记住”等明确要求时用 explicit。"
                        "同义或修正信息复用稳定 key，以便合并，不要随意覆盖 explicit 记忆。"
                    ),
                ),
                ChatMessage(
                    role="user",
                    content=json.dumps(events, ensure_ascii=False, default=str),
                ),
            ),
        )
        response = await self._concurrency.run_llm(
            "memory-worker", lambda: self._provider.complete(request)
        )
        raw = response.content.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.startswith("json"):
                raw = raw[4:].lstrip()
        decoded = json.loads(raw)
        if not isinstance(decoded, list):
            raise ValueError("memory extractor must return a list")
        return [item for item in decoded if isinstance(item, dict)]

    async def _apply(
        self,
        operations: list[dict[str, Any]],
        jobs: tuple[MemoryJobRecord, ...],
    ) -> None:
        event_ids = {job.event.id for job in jobs}
        for item in operations:
            scope = item.get("scope")
            if scope == "preference":
                user_id = self._string(item.get("user_id"))
                key = self._string(item.get("key"))
                value = self._string(item.get("content"))
                if user_id and key and value:
                    await self._memories.set_preference(
                        user_id,
                        key[:64],
                        value,
                        limit=self._settings.preference_max_entries,
                        source_type=(
                            "explicit" if item.get("source_type") == "explicit" else "automatic"
                        ),
                    )
                continue
            if scope not in {"person", "group", "person_group"}:
                continue
            user_id = self._string(item.get("user_id"))
            group_id = self._string(item.get("group_id"))
            content = self._string(item.get("content"))
            key = self._string(item.get("key"))
            if not content:
                continue
            source_event_id = self._integer(item.get("source_event_id"))
            if source_event_id not in event_ids:
                source_event_id = jobs[-1].event.id
            await self._memories.upsert(
                scope=scope,
                user_id=user_id,
                group_id=group_id,
                memory_key=(key or self._stable_key(scope, content))[:128],
                content=content,
                category=(self._string(item.get("category")) or "fact")[:32],
                importance=max(1, min(5, self._integer(item.get("importance")) or 3)),
                source_type=("explicit" if item.get("source_type") == "explicit" else "automatic"),
                source_event_id=source_event_id,
                limit=self._scope_limit(scope),
            )

    def _scope_limit(self, scope: str) -> int:
        if scope == "person":
            return self._settings.person_memory_max_entries
        if scope == "group":
            return self._settings.group_memory_max_entries
        return self._settings.person_group_memory_max_entries

    @staticmethod
    def _event_json(event: EventRecord) -> dict[str, Any]:
        return {
            "event_id": event.id,
            "scope": event.scope_type.value,
            "sender_user_id": event.sender_user_id,
            "group_id": event.group_id,
            "direction": event.direction,
            "content": event.content,
            "occurred_at": event.occurred_at.isoformat(),
        }

    @staticmethod
    def _stable_key(scope: str, content: str) -> str:
        digest = hashlib.sha256(content.encode()).hexdigest()[:16]
        return f"{scope}-{digest}"

    @staticmethod
    def _string(value: Any) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _integer(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        return None
