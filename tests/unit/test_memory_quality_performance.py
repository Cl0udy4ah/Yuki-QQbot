"""Synthetic large-shape performance runner contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from qq_ai_bot.memory.quality.models import QualityPerformanceScenario
from qq_ai_bot.memory.quality.performance import MemoryQualityPerformanceRunner

ROOT = Path(__file__).parents[2]


@pytest.mark.asyncio
async def test_performance_runner_is_isolated_and_reports_measured_counts() -> None:
    scenario = QualityPerformanceScenario(
        users=3,
        facts_per_user=4,
        groups=2,
        chat_events=30,
        query_count=3,
        keyset_batch_size=7,
    )
    report = await MemoryQualityPerformanceRunner(ROOT).run(scenario)

    assert report.populated_fact_count == 12
    assert report.active_embedded_fact_count == 6  # two contested facts per synthetic person
    assert report.populated_event_count == 30
    assert report.keyset_events_per_second > 0
    assert report.embedding_document_request_count > 0
    assert report.embedding_query_request_count == 3
    assert report.model_request_count == 0
    assert report.quality_suite_total_ms is None
