"""Single query-driven read entrypoint for Memory V2 facts."""

from __future__ import annotations

import hashlib
import time

from qq_ai_bot.memory.enums import (
    MemoryRetrievalMode,
    MemoryScopeType,
    MemoryTargetRole,
)
from qq_ai_bot.memory.fts import MemoryLexicalIndex, build_safe_lexical_query
from qq_ai_bot.memory.metrics import MemoryRetrievalMetric, MemoryRetrievalMetrics
from qq_ai_bot.memory.models import (
    MemoryQuery,
    MemoryRetrievalBlock,
    MemoryRetrievalHit,
    MemoryRetrievalResult,
)
from qq_ai_bot.memory.ranking import MemoryRanker
from qq_ai_bot.memory.repository import MemoryFactRepository


class MemoryRetriever:
    """Retrieve each identity target independently and never widen its SQL scope."""

    def __init__(
        self,
        *,
        repository: MemoryFactRepository,
        lexical_index: MemoryLexicalIndex,
        ranker: MemoryRanker | None = None,
        metrics: MemoryRetrievalMetrics | None = None,
    ) -> None:
        self._repository = repository
        self._index = lexical_index
        self._ranker = ranker or MemoryRanker()
        self._metrics = metrics or MemoryRetrievalMetrics()

    @property
    def metrics(self) -> MemoryRetrievalMetrics:
        return self._metrics

    async def retrieve(
        self,
        query: MemoryQuery,
        *,
        lexical_enabled: bool = True,
    ) -> MemoryRetrievalResult:
        started = time.perf_counter()
        fts_latency = 0.0
        candidate_count = 0
        blocks: list[MemoryRetrievalBlock] = []
        all_hits: list[MemoryRetrievalHit] = []
        short_fallback_used = False
        for target in query.targets:
            hits: tuple[MemoryRetrievalHit, ...]
            if query.mode is MemoryRetrievalMode.OVERVIEW or not lexical_enabled:
                facts = await self._repository.list_overview(
                    target,
                    limit=query.limit_per_target,
                )
                candidate_count += len(facts)
                hits = self._ranker.rank_overview(
                    facts,
                    target=target,
                    limit=query.limit_per_target,
                    reason=("overview" if lexical_enabled else "retrieval_disabled_fallback"),
                )
            else:
                preferences = (
                    await self._repository.list_explicit_preferences(
                        target,
                        limit=query.always_on_explicit_preference_limit,
                    )
                    if target.scope_type is MemoryScopeType.PERSON
                    and target.role is MemoryTargetRole.CURRENT_PERSON
                    and query.always_on_explicit_preference_limit > 0
                    else ()
                )
                safe = build_safe_lexical_query(
                    query.normalized_text,
                    term_limit=query.query_term_limit,
                )
                search_started = time.perf_counter()
                candidates = await self._index.search(
                    target,
                    safe,
                    candidate_limit=query.candidate_limit,
                    kinds=query.kinds,
                    short_query_fallback_enabled=query.short_query_fallback_enabled,
                )
                fts_latency += time.perf_counter() - search_started
                short_fallback_used = short_fallback_used or bool(
                    safe.short_term and query.short_query_fallback_enabled
                )
                lexical_facts = await self._repository.get_active_for_target(
                    target,
                    tuple(candidate.fact_id for candidate in candidates),
                )
                candidate_count += len(preferences) + len(candidates)
                preference_hits = self._ranker.rank_overview(
                    preferences,
                    target=target,
                    limit=query.always_on_explicit_preference_limit,
                    reason="always_on_explicit_preference",
                )
                remaining = max(0, query.limit_per_target - len(preference_hits))
                lexical_hits = self._ranker.rank_lexical(
                    facts=lexical_facts,
                    candidates=candidates,
                    target=target,
                    normalized_query=query.normalized_text,
                    limit=remaining,
                )
                preference_ids = {hit.fact.id for hit in preference_hits}
                deduplicated = tuple(
                    hit for hit in lexical_hits if hit.fact.id not in preference_ids
                )
                combined = (*preference_hits, *deduplicated)[: query.limit_per_target]
                hits = tuple(
                    hit.model_copy(update={"rank": rank})
                    for rank, hit in enumerate(combined, start=1)
                )
            blocks.append(MemoryRetrievalBlock(target=target, hits=hits))
            all_hits.extend(hits)

        query_hash = hashlib.sha256(query.normalized_text.encode("utf-8")).hexdigest()
        result = MemoryRetrievalResult(
            blocks=tuple(blocks),
            hits=tuple(all_hits),
            candidate_count=candidate_count,
            selected_count=len(all_hits),
            query_hash=query_hash,
            mode=query.mode,
        )
        referenced = {
            target.subject_user_id
            for target in query.targets
            if target.role
            in {
                MemoryTargetRole.REFERENCED_PERSON,
                MemoryTargetRole.REFERENCED_PERSON_GROUP,
            }
        }
        self._metrics.record(
            MemoryRetrievalMetric(
                mode=query.mode,
                query_hash=query_hash,
                target_count=len(query.targets),
                candidate_count=candidate_count,
                selected_count=len(all_hits),
                context_selected_count=0,
                fts_latency=fts_latency,
                total_latency=time.perf_counter() - started,
                overview_used=query.mode is MemoryRetrievalMode.OVERVIEW,
                short_query_fallback_used=short_fallback_used,
                referenced_person_count=len(referenced - {None}),
            )
        )
        return result
