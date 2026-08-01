"""Entity-block projection for Memory V2 chat context."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryConflictState,
    MemoryRetrievalMode,
    MemoryTargetRole,
)
from qq_ai_bot.memory.models import (
    MemoryContextBlock,
    MemoryEntityTarget,
    MemoryFact,
    MemoryRetrievalHit,
    MemoryRetrievalResult,
)
from qq_ai_bot.memory.query import MemoryQueryBuilder
from qq_ai_bot.memory.retrieval import MemoryRetriever
from qq_ai_bot.memory.service import MemoryFactService


def fact_context(fact: MemoryFact) -> dict[str, Any]:
    return {
        "fact_id": fact.id,
        "kind": fact.kind.value,
        "category": fact.category,
        "content": fact.content,
        "importance": fact.importance,
        "confidence": fact.confidence,
        "source_type": fact.source_type.value,
        "authority": fact.authority.value,
        "reported": fact.authority is MemoryAuthority.THIRD_PARTY,
        "contested": fact.conflict_state is MemoryConflictState.CONTESTED,
        "updated_at": fact.updated_at.isoformat(),
    }


def retrieval_fact_context(hit: MemoryRetrievalHit) -> dict[str, Any]:
    return {
        **fact_context(hit.fact),
        "retrieval_reason": hit.selection_reason,
    }


def entity_block(block: MemoryContextBlock) -> dict[str, Any]:
    return {
        "subject_user_id": block.subject_user_id,
        "group_id": block.group_id,
        "facts": [fact_context(fact) for fact in block.facts],
    }


ENTITY_MEMORY_RULE = (
    "每条长期事实只属于它所在的 entity block。不得把 current_group 或其他人物的"
    "信息归给 current_person；没有事实时不得猜测。third_party/reported 表示他人报告，"
    "不等于本人确认；contested=true 表示存在未解决冲突，不得当作确定事实。"
    "不要主动向用户泄露内部 confidence 或 authority 枚举。"
)


class MemoryContextService:
    """Compose deterministic retrieval for chat, tools, admin, and plugins."""

    def __init__(
        self,
        *,
        query_builder: MemoryQueryBuilder,
        retriever: MemoryRetriever,
        facts: MemoryFactService,
    ) -> None:
        self._queries = query_builder
        self._retriever = retriever
        self._facts = facts

    @property
    def retriever(self) -> MemoryRetriever:
        return self._retriever

    async def resolve_targets(
        self,
        inbound: InboundMessage,
        runtime: RuntimeConfigSnapshot,
    ) -> tuple[MemoryEntityTarget, ...]:
        return await self._queries.resolve_targets(
            inbound,
            max_referenced=runtime.context.related_people_limit,
        )

    async def retrieve_for_turn(
        self,
        *,
        inbound: InboundMessage,
        content: str,
        planner_intent: str,
        runtime: RuntimeConfigSnapshot,
    ) -> MemoryRetrievalResult:
        query = await self._queries.build(
            inbound=inbound,
            content=content,
            planner_intent=planner_intent,
            runtime=runtime,
        )
        if runtime.memory.retrieval_enabled:
            return await self._retriever.retrieve(query)
        current_targets = tuple(
            target
            for target in query.targets
            if target.role
            in {
                MemoryTargetRole.CURRENT_PERSON,
                MemoryTargetRole.CURRENT_PERSON_GROUP,
                MemoryTargetRole.CURRENT_GROUP,
            }
        )
        fallback = query.model_copy(
            update={
                "targets": current_targets,
                "limit_per_target": runtime.memory.context_limit_per_entity,
            }
        )
        return await self._retriever.retrieve(fallback, lexical_enabled=False)

    async def search(
        self,
        *,
        text: str,
        mode: MemoryRetrievalMode,
        targets: tuple[MemoryEntityTarget, ...],
        runtime: RuntimeConfigSnapshot,
        limit: int | None = None,
    ) -> MemoryRetrievalResult:
        query = self._queries.for_targets(
            text=text,
            mode=mode,
            targets=targets,
            runtime=runtime,
            limit=limit,
        )
        return await self._retriever.retrieve(query)

    async def mark_used(
        self,
        result: MemoryRetrievalResult,
        fact_ids: tuple[int, ...],
    ) -> int:
        selected = tuple(dict.fromkeys(fact_ids))
        updated = await self._facts.mark_used(selected)
        latest = self._retriever.metrics.latest
        if latest is not None and latest.query_hash == result.query_hash:
            self._retriever.metrics.record_context_selected(
                replace(latest, context_selected_count=len(selected))
            )
        return updated
