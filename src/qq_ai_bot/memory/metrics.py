"""Content-free observability for Memory V2 retrieval."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from qq_ai_bot.memory.enums import MemoryRetrievalMode

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MemoryRetrievalMetric:
    mode: MemoryRetrievalMode
    query_hash: str
    target_count: int
    candidate_count: int
    selected_count: int
    context_selected_count: int
    fts_latency: float
    total_latency: float
    overview_used: bool
    short_query_fallback_used: bool
    referenced_person_count: int
    semantic_candidate_count: int = 0
    semantic_selected_count: int = 0
    hybrid_selected_count: int = 0
    semantic_degraded: bool = False
    semantic_search_latency: float = 0
    hybrid_rank_latency: float = 0


class MemoryRetrievalMetrics:
    """Retain only the latest redacted metric and emit counts to debug logs."""

    def __init__(self) -> None:
        self._latest: MemoryRetrievalMetric | None = None

    @property
    def latest(self) -> MemoryRetrievalMetric | None:
        return self._latest

    def record(self, metric: MemoryRetrievalMetric) -> None:
        self._latest = metric
        logger.debug(
            "memory_retrieval mode=%s query_hash=%s targets=%d candidates=%d "
            "selected=%d context_selected=%d fts_latency=%.6f total_latency=%.6f "
            "overview=%s short_fallback=%s referenced_people=%d semantic_candidates=%d "
            "semantic_selected=%d hybrid_selected=%d semantic_degraded=%s "
            "semantic_latency=%.6f hybrid_latency=%.6f",
            metric.mode.value,
            metric.query_hash,
            metric.target_count,
            metric.candidate_count,
            metric.selected_count,
            metric.context_selected_count,
            metric.fts_latency,
            metric.total_latency,
            metric.overview_used,
            metric.short_query_fallback_used,
            metric.referenced_person_count,
            metric.semantic_candidate_count,
            metric.semantic_selected_count,
            metric.hybrid_selected_count,
            metric.semantic_degraded,
            metric.semantic_search_latency,
            metric.hybrid_rank_latency,
        )
