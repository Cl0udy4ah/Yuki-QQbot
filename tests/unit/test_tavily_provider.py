"""Tavily REST provider tests using only httpx.MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest

from qq_ai_bot.web.base import WebSearchError, WebSearchValidationError, normalize_public_url
from qq_ai_bot.web.models import WebSearchRequest
from qq_ai_bot.web.tavily import TavilyWebSearchProvider


def search_payload(*, count: int = 2) -> dict[str, object]:
    return {
        "request_id": "request-1",
        "results": [
            {
                "title": f"页面 {index}",
                "url": f"https://example{index}.com/article",
                "content": f"搜索摘要 {index}",
                "score": 0.9 - index * 0.1,
            }
            for index in range(count)
        ],
    }


def extract_payload(*urls: str, failed: tuple[str, ...] = ()) -> dict[str, object]:
    return {
        "results": [{"url": url, "raw_content": f"{url} 的查询相关正文"} for url in urls],
        "failed_results": [{"url": url, "error": "failed"} for url in failed],
    }


@pytest.mark.asyncio
async def test_search_uses_fixed_parameters_deduplicates_and_batch_extracts_three() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer secret-key"
        payload = json.loads(request.content)
        if request.url.path == "/search":
            assert payload["include_answer"] is False
            assert payload["include_raw_content"] is False
            assert payload["include_images"] is False
            assert payload["search_depth"] == "advanced"
            results = search_payload(count=4)["results"]
            assert isinstance(results, list)
            results.insert(1, dict(results[0], url="https://example0.com/article#fragment"))
            return httpx.Response(
                200,
                request=request,
                json={"request_id": "request-1", "results": results},
            )
        assert request.url.path == "/extract"
        assert len(payload["urls"]) == 3
        assert payload["chunks_per_source"] == 3
        return httpx.Response(
            200,
            request=request,
            json=extract_payload(*payload["urls"]),
        )

    client = httpx.AsyncClient(
        base_url="https://api.tavily.com",
        transport=httpx.MockTransport(handler),
    )
    provider = TavilyWebSearchProvider(api_key="secret-key", client=client)
    response = await provider.search(WebSearchRequest(query="测试搜索"))
    await provider.close()

    assert len(requests) == 2
    assert response.provider_request_id == "request-1"
    assert len(response.sources) == 3
    assert not response.partial_failure
    assert all(source.relevant_content.endswith("查询相关正文") for source in response.sources)


@pytest.mark.asyncio
async def test_extract_partial_failure_keeps_search_snippet() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search":
            return httpx.Response(200, request=request, json=search_payload())
        return httpx.Response(
            200,
            request=request,
            json=extract_payload(
                "https://example0.com/article",
                failed=("https://example1.com/article",),
            ),
        )

    client = httpx.AsyncClient(
        base_url="https://api.tavily.com",
        transport=httpx.MockTransport(handler),
    )
    provider = TavilyWebSearchProvider(api_key="secret", client=client)
    response = await provider.search(WebSearchRequest(query="测试"))
    await provider.close()

    assert response.partial_failure
    assert response.sources[0].relevant_content.endswith("查询相关正文")
    assert response.sources[1].relevant_content == "搜索摘要 1"


@pytest.mark.asyncio
async def test_empty_search_results_raise_safe_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json={"results": []})

    client = httpx.AsyncClient(
        base_url="https://api.tavily.com",
        transport=httpx.MockTransport(handler),
    )
    provider = TavilyWebSearchProvider(api_key="secret", client=client)
    with pytest.raises(WebSearchError, match="没有找到"):
        await provider.search(WebSearchRequest(query="没有结果"))
    await provider.close()


@pytest.mark.asyncio
async def test_timeout_is_retried_once_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("contains provider details", request=request)

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("qq_ai_bot.web.tavily.asyncio.sleep", no_sleep)
    client = httpx.AsyncClient(
        base_url="https://api.tavily.com",
        transport=httpx.MockTransport(handler),
    )
    provider = TavilyWebSearchProvider(api_key="secret", max_retries=1, client=client)
    with pytest.raises(WebSearchError) as error:
        await provider.search(WebSearchRequest(query="超时"))
    await provider.close()

    assert attempts == 2
    assert error.value.code == "timeout"
    assert "provider details" not in error.value.detail


@pytest.mark.asyncio
async def test_429_uses_retry_after_once(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, request=request, headers={"Retry-After": "1.5"})
        if request.url.path == "/search":
            return httpx.Response(200, request=request, json=search_payload(count=1))
        return httpx.Response(
            200,
            request=request,
            json=extract_payload("https://example0.com/article"),
        )

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("qq_ai_bot.web.tavily.asyncio.sleep", record_sleep)
    client = httpx.AsyncClient(
        base_url="https://api.tavily.com",
        transport=httpx.MockTransport(handler),
    )
    provider = TavilyWebSearchProvider(api_key="secret", max_retries=1, client=client)
    response = await provider.search(WebSearchRequest(query="限流重试"))
    await provider.close()

    assert len(response.sources) == 1
    assert delays == [1.5]


@pytest.mark.asyncio
async def test_5xx_is_retried_at_most_once(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, request=request)

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("qq_ai_bot.web.tavily.asyncio.sleep", no_sleep)
    client = httpx.AsyncClient(
        base_url="https://api.tavily.com",
        transport=httpx.MockTransport(handler),
    )
    provider = TavilyWebSearchProvider(api_key="secret", max_retries=1, client=client)
    with pytest.raises(WebSearchError) as error:
        await provider.search(WebSearchRequest(query="服务异常"))
    await provider.close()
    assert attempts == 2
    assert error.value.code == "provider_unavailable"


@pytest.mark.asyncio
async def test_invalid_json_is_a_safe_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, content=b"not-json")

    client = httpx.AsyncClient(
        base_url="https://api.tavily.com",
        transport=httpx.MockTransport(handler),
    )
    provider = TavilyWebSearchProvider(api_key="secret", client=client)
    with pytest.raises(WebSearchError) as error:
        await provider.search(WebSearchRequest(query="非法 JSON"))
    await provider.close()
    assert error.value.code == "invalid_json"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "http://user:password@example.com/",
        "http://localhost/admin",
        "http://127.0.0.1/private",
        "http://10.0.0.1/private",
        "http://[::1]/private",
        "http://host.docker.internal/",
        "http://napcat/",
    ],
)
def test_private_or_unsafe_urls_are_rejected(url: str) -> None:
    with pytest.raises(WebSearchValidationError):
        normalize_public_url(url)
