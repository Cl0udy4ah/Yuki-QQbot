"""Deterministic ranking for Memory V2 lexical and overview candidates."""

from __future__ import annotations

from qq_ai_bot.memory.embedding.models import MemorySemanticCandidate
from qq_ai_bot.memory.enums import MemoryAuthority, MemoryConflictState
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
                -_authority_rank(fact.authority),
                1 if fact.conflict_state is MemoryConflictState.CONTESTED else 0,
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

    def rank_hybrid(
        self,
        *,
        facts: tuple[MemoryFact, ...],
        lexical_candidates: tuple[MemoryLexicalCandidate, ...],
        semantic_candidates: tuple[MemorySemanticCandidate, ...],
        target: MemoryEntityTarget,
        normalized_query: str,
        lexical_weight: float,
        semantic_weight: float,
        rrf_k: int,
        limit: int,
    ) -> tuple[MemoryRetrievalHit, ...]:
        """Fuse incomparable retrieval scores by rank, then apply stable tie breaks."""

        lexical_fact_ids = {candidate.fact_id for candidate in lexical_candidates}
        lexical_hits = self.rank_lexical(
            facts=tuple(fact for fact in facts if fact.id in lexical_fact_ids),
            candidates=lexical_candidates,
            target=target,
            normalized_query=normalized_query,
            limit=len(lexical_candidates),
        )
        lexical = {hit.fact.id: hit for hit in lexical_hits}
        semantic = {candidate.fact_id: candidate for candidate in semantic_candidates}

        def fusion(fact: MemoryFact) -> float:
            lexical_hit = lexical.get(fact.id)
            semantic_hit = semantic.get(fact.id)
            return (lexical_weight / (rrf_k + lexical_hit.rank) if lexical_hit else 0) + (
                semantic_weight / (rrf_k + semantic_hit.semantic_rank) if semantic_hit else 0
            )

        def exact(fact: MemoryFact) -> bool:
            hit = lexical.get(fact.id)
            return bool(hit and hit.selection_reason.endswith("_exact"))

        ordered = sorted(
            facts,
            key=lambda fact: (
                0 if exact(fact) else 1,
                -fusion(fact),
                -_authority_rank(fact.authority),
                1 if fact.conflict_state is MemoryConflictState.CONTESTED else 0,
                -fact.importance,
                -fact.confidence,
                -fact.updated_at.timestamp(),
                fact.id,
            ),
        )[:limit]
        hits: list[MemoryRetrievalHit] = []
        for rank, fact in enumerate(ordered, start=1):
            lexical_hit = lexical.get(fact.id)
            semantic_hit = semantic.get(fact.id)
            if lexical_hit and lexical_hit.selection_reason.endswith("_exact"):
                reason = lexical_hit.selection_reason
            elif lexical_hit and semantic_hit:
                reason = "hybrid_match"
            elif semantic_hit:
                reason = "semantic_match"
            else:
                reason = "lexical_match"
            sources = tuple(
                source
                for source, present in (
                    ("lexical", lexical_hit is not None),
                    ("semantic", semantic_hit is not None),
                )
                if present
            )
            hits.append(
                MemoryRetrievalHit(
                    fact=fact,
                    target=target,
                    rank=rank,
                    lexical_score=lexical_hit.lexical_score if lexical_hit else None,
                    semantic_score=(semantic_hit.cosine_similarity if semantic_hit else None),
                    fusion_score=fusion(fact),
                    lexical_rank=lexical_hit.rank if lexical_hit else None,
                    semantic_rank=semantic_hit.semantic_rank if semantic_hit else None,
                    exact_match=bool(lexical_hit and lexical_hit.exact_match),
                    matched_terms=lexical_hit.matched_terms if lexical_hit else (),
                    sources=sources,
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
                -_authority_rank(fact.authority),
                1 if fact.conflict_state is MemoryConflictState.CONTESTED else 0,
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
                sources=(reason,),
                selection_reason=reason,
            )
            for rank, fact in enumerate(ordered, start=1)
        )


def _authority_rank(authority: MemoryAuthority) -> int:
    return {
        MemoryAuthority.THIRD_PARTY: 0,
        MemoryAuthority.GROUP_REPORT: 1,
        MemoryAuthority.SELF_REPORT: 2,
        MemoryAuthority.EXPLICIT: 3,
    }[authority]
