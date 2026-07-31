"""Opt-in, privacy-free smoke test for the real DashScope embedding endpoint."""

from __future__ import annotations

import os

import pytest

from qq_ai_bot.memory.embedding.qwen import QwenDashScopeEmbeddingProvider


@pytest.mark.qwen_embedding_integration
@pytest.mark.asyncio
async def test_real_qwen_document_query_and_health() -> None:
    if os.getenv("QWEN_EMBEDDING_INTEGRATION_ENABLED", "").casefold() != "true":
        pytest.skip("set QWEN_EMBEDDING_INTEGRATION_ENABLED=true to call DashScope")
    base_url = os.getenv("MEMORY_EMBEDDING_BASE_URL", "")
    api_key = os.getenv("MEMORY_EMBEDDING_API_KEY", "")
    if not base_url or not api_key:
        pytest.skip("DashScope base URL and API key are required")

    provider = QwenDashScopeEmbeddingProvider(base_url=base_url, api_key=api_key)
    try:
        document = await provider.embed_documents(("A fixed privacy-free test document.",))
        query = await provider.embed_query("A fixed privacy-free test query.")
    finally:
        await provider.close()

    assert provider.profile.output_type == "dense"
    assert provider.profile.dimensions == 1024
    assert len(document.vectors) == len(query.vectors) == 1
    assert document.vectors[0].dimensions == query.vectors[0].dimensions == 1024
