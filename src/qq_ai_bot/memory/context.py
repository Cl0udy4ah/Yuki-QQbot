"""Entity-block projection for Memory V2 chat context."""

from __future__ import annotations

from typing import Any

from qq_ai_bot.memory.models import MemoryContextBlock, MemoryFact


def fact_context(fact: MemoryFact) -> dict[str, Any]:
    return {
        "fact_id": fact.id,
        "kind": fact.kind.value,
        "category": fact.category,
        "content": fact.content,
        "importance": fact.importance,
        "confidence": fact.confidence,
        "source_type": fact.source_type.value,
        "updated_at": fact.updated_at.isoformat(),
    }


def entity_block(block: MemoryContextBlock) -> dict[str, Any]:
    return {
        "subject_user_id": block.subject_user_id,
        "group_id": block.group_id,
        "facts": [fact_context(fact) for fact in block.facts],
    }


ENTITY_MEMORY_RULE = (
    "每条长期事实只属于它所在的 entity block。不得把 current_group 或其他人物的"
    "信息归给 current_person；没有事实时不得猜测。"
)
