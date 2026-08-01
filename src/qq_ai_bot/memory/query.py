"""Backend-owned query construction for lexical Memory V2 retrieval."""

from __future__ import annotations

import re
import unicodedata

from pydantic import ValidationError

from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.memory.enums import MemoryContextMode, MemoryRetrievalMode, MemoryTargetRole
from qq_ai_bot.memory.errors import MemoryRetrievalError
from qq_ai_bot.memory.models import MemoryEntityTarget, MemoryQuery
from qq_ai_bot.memory.targets import MemoryTargetResolver

_WHITESPACE = re.compile(r"\s+")
_OVERVIEW_EXPRESSIONS = (
    "你记得我什么",
    "关于我你知道什么",
    "我之前说过什么",
    "你还记得哪些关于我的事",
    "你对这个群记得什么",
    "what do you remember about me",
    "what do you know about me",
    "what do you remember about this group",
)


def normalize_query_text(value: str, *, maximum: int = 1200) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return _WHITESPACE.sub(" ", normalized).strip()[:maximum]


def is_overview_query(value: str) -> bool:
    compact = re.sub(r"[\W_]+", "", normalize_query_text(value), flags=re.UNICODE)
    return any(
        re.sub(r"[\W_]+", "", expression.casefold(), flags=re.UNICODE) in compact
        for expression in _OVERVIEW_EXPRESSIONS
    )


class MemoryQueryBuilder:
    """Build a strict query from current-event text and bounded planner metadata."""

    def __init__(self, targets: MemoryTargetResolver) -> None:
        self._targets = targets

    async def resolve_targets(
        self,
        inbound: InboundMessage,
        *,
        max_referenced: int,
    ) -> tuple[MemoryEntityTarget, ...]:
        return await self._targets.resolve(inbound, max_referenced=max_referenced)

    async def build(
        self,
        *,
        inbound: InboundMessage,
        content: str,
        planner_intent: str,
        runtime: RuntimeConfigSnapshot,
        memory_mode: MemoryContextMode = MemoryContextMode.HYBRID,
    ) -> MemoryQuery:
        targets = await self.resolve_targets(
            inbound,
            max_referenced=runtime.context.related_people_limit,
        )
        mode = (
            MemoryRetrievalMode.OVERVIEW
            if memory_mode is MemoryContextMode.OVERVIEW or is_overview_query(content)
            else MemoryRetrievalMode.RELEVANT
        )
        if mode is MemoryRetrievalMode.OVERVIEW:
            targets = tuple(
                target
                for target in targets
                if target.role
                in {
                    MemoryTargetRole.CURRENT_PERSON,
                    MemoryTargetRole.CURRENT_PERSON_GROUP,
                    MemoryTargetRole.CURRENT_GROUP,
                }
            )
        parts = [content]
        if inbound.reply_text:
            parts.append(inbound.reply_text[:500])
        if planner_intent:
            parts.append(planner_intent[:300])
        text = "\n".join(part for part in parts if part.strip())
        query = self.for_targets(
            text=text,
            mode=mode,
            targets=targets,
            runtime=runtime,
        )
        if memory_mode in {MemoryContextMode.LEXICAL, MemoryContextMode.OVERVIEW}:
            query = query.model_copy(update={"semantic_enabled": False})
        return query

    @staticmethod
    def for_targets(
        *,
        text: str,
        mode: MemoryRetrievalMode,
        targets: tuple[MemoryEntityTarget, ...],
        runtime: RuntimeConfigSnapshot,
        limit: int | None = None,
    ) -> MemoryQuery:
        memory = runtime.memory
        default_limit = (
            memory.overview_limit_per_entity
            if mode is MemoryRetrievalMode.OVERVIEW
            else memory.context_limit_per_entity
        )
        try:
            return MemoryQuery(
                text=text[:1200],
                normalized_text=normalize_query_text(text),
                mode=mode,
                targets=targets,
                candidate_limit=memory.lexical_candidate_limit,
                limit_per_target=limit if limit is not None else default_limit,
                always_on_explicit_preference_limit=(memory.always_on_explicit_preference_limit),
                query_term_limit=memory.query_term_limit,
                short_query_fallback_enabled=memory.short_query_fallback_enabled,
                semantic_enabled=memory.semantic_enabled,
                semantic_candidate_limit=memory.semantic_candidate_limit,
                semantic_min_similarity=memory.semantic_min_similarity,
                hybrid_lexical_weight=memory.hybrid_lexical_weight,
                hybrid_semantic_weight=memory.hybrid_semantic_weight,
                hybrid_rrf_k=memory.hybrid_rrf_k,
            )
        except ValidationError as exc:
            raise MemoryRetrievalError("memory_query_invalid") from exc
