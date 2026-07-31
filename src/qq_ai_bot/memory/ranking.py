"""Deterministic ranking for Memory V2 lexical and overview candidates."""

from __future__ import annotations

from qq_ai_bot.memory.models import (
    MemoryEntityTarget,
    MemoryFact,
    MemoryLexicalCandidate,
    MemoryRetrievalHit,
)
from qq_ai_bot.memory.query import normalize_query_text


class MemoryRanker:
    """Rank without an LLM and always end with a stable fact-id tie break."""

    def rank_lexical(
        self,
        *,
        facts: tuple[MemoryFact, ...],
        candidates: tuple[MemoryLexicalCandidate, ...],
        target: MemoryEntityTarget,
        normalized_query: str,
        limit: int,
    ) -> tuple[MemoryRetrievalHit, ...]:
        candidate_by_id = {candidate.fact_id: candidate for candidate in candidates}

        def key(fact: MemoryFact) -> tuple[object, ...]:
            candidate = candidate_by_id[fact.id]
            memory_key = normalize_query_text(fact.memory_key)
            content = normalize_query_text(fact.normalized_content)
            category = normalize_query_text(fact.category)
            return (
                0 if memory_key == normalized_query else 1,
                0 if content == normalized_query else 1,
                0 if category == normalized_query else 1,
                candidate.fts_rank,
                -fact.importance,
                -fact.confidence,
                -fact.updated_at.timestamp(),
                fact.id,
            )

        ordered = sorted(facts, key=key)[:limit]
        hits: list[MemoryRetrievalHit] = []
        for rank, fact in enumerate(ordered, start=1):
            candidate = candidate_by_id[fact.id]
            if normalize_query_text(fact.memory_key) == normalized_query:
                reason = "memory_key_exact"
            elif normalize_query_text(fact.normalized_content) == normalized_query:
                reason = "content_exact"
            elif normalize_query_text(fact.category) == normalized_query:
                reason = "category_exact"
            else:
                reason = "lexical_match"
            hits.append(
                MemoryRetrievalHit(
                    fact=fact,
                    target=target,
                    rank=rank,
                    lexical_score=-candidate.fts_rank,
                    exact_match=candidate.exact_match,
                    matched_terms=candidate.matched_terms,
                    selection_reason=reason,
                )
            )
        return tuple(hits)

    @staticmethod
    def rank_overview(
        facts: tuple[MemoryFact, ...],
        *,
        target: MemoryEntityTarget,
        limit: int,
        reason: str = "overview",
    ) -> tuple[MemoryRetrievalHit, ...]:
        ordered = sorted(
            facts,
            key=lambda fact: (
                -fact.importance,
                -fact.confidence,
                -fact.updated_at.timestamp(),
                fact.id,
            ),
        )[:limit]
        return tuple(
            MemoryRetrievalHit(
                fact=fact,
                target=target,
                rank=rank,
                lexical_score=0,
                selection_reason=reason,
            )
            for rank, fact in enumerate(ordered, start=1)
        )
