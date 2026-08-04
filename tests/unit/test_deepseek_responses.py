"""DeepSeek Responses request, response, and continuation contract tests."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from qq_ai_bot.domain.messages import (
    ChatMessage,
    ChatRequest,
    ChatTool,
    FunctionCallOutput,
    ModelResponseStatus,
    NativeToolDefinition,
    NativeToolStatus,
    NativeToolType,
    ReasoningEffort,
)
from qq_ai_bot.llm.base import (
    LLMAuthenticationError,
    LLMInvalidRequestError,
    LLMInvalidResponseError,
    LLMRateLimitError,
    LLMUnavailableError,
)
from qq_ai_bot.llm.deepseek_responses import DeepSeekResponsesProvider

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "deepseek_responses"


def _fixture(name: str) -> dict[str, object]:
    payload = json.loads((_FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _request(**overrides: object) -> ChatRequest:
    values: dict[str, object] = {
        "messages": (
            ChatMessage(role="system", content="trusted system"),
            ChatMessage(role="developer", content="trusted developer"),
            ChatMessage(role="user", content="hello"),
        ),
        "model": "deepseek-v4-flash",
        "max_output_tokens": 100,
    }
    values.update(overrides)
    return ChatRequest(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_request_mapping_is_responses_native_and_flat() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/responses"
        payload = json.loads(request.content)
        assert payload["instructions"] == "trusted system\n\ntrusted developer"
        assert payload["input"] == [{"role": "user", "content": "hello"}]
        assert payload["tools"] == [
            {
                "type": "function",
                "name": "memory_change",
                "description": "change memory",
                "parameters": {"type": "object"},
            },
            {"type": "web_search"},
        ]
        assert payload["tool_choice"] == "required"
        assert payload["reasoning"] == {"effort": "max"}
        assert payload["stream"] is False
        return httpx.Response(200, request=request, json=_fixture("text_completed.json"))

    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        provider = DeepSeekResponsesProvider(
            base_url="https://api.deepseek.com",
            api_key="secret",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        response = await provider.complete(
            _request(
                thinking_enabled=True,
                reasoning_effort=ReasoningEffort.MAX,
                tools=(
                    ChatTool(
                        name="memory_change",
                        description="change memory",
                        parameters={"type": "object"},
                    ),
                ),
                native_tools=(NativeToolDefinition(type=NativeToolType.WEB_SEARCH),),
                tool_choice="required",
            )
        )

    assert response.content == "这是脱敏后的测试回答。"
    assert response.status is ModelResponseStatus.COMPLETED
    assert response.prompt_tokens == 12
    assert response.completion_tokens == 8
    assert response.cached_prompt_tokens == 3
    assert response.reasoning_tokens == 2
    assert response.continuation is not None


@pytest.mark.asyncio
async def test_function_output_follows_cumulative_continuation() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        fixture = (
            _fixture("function_calls.json")
            if len(requests) == 1
            else _fixture("text_completed.json")
        )
        return httpx.Response(200, request=request, json=fixture)

    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        provider = DeepSeekResponsesProvider(
            base_url="https://api.deepseek.com",
            api_key="secret",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        first = await provider.complete(_request())
        assert [call.id for call in first.tool_calls] == ["call_fixture_1", "call_fixture_2"]
        assert first.continuation is not None
        await provider.complete(
            _request(
                continuation=first.continuation,
                function_outputs=(
                    FunctionCallOutput(call_id="call_fixture_1", output='{"ok":true}'),
                    FunctionCallOutput(call_id="call_fixture_2", output='{"ok":true}'),
                ),
            )
        )

    second_inputs = requests[1]["input"]
    assert isinstance(second_inputs, list)
    assert second_inputs[0] == {"role": "user", "content": "hello"}
    assert [item["type"] for item in second_inputs[1:]] == [
        "function_call",
        "function_call",
        "function_call_output",
        "function_call_output",
    ]
    assert second_inputs[-2]["call_id"] == "call_fixture_1"


@pytest.mark.asyncio
async def test_native_web_events_and_last_message_are_parsed_from_incomplete_fixture() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json=_fixture("native_web_incomplete.json"),
        )

    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        response = await DeepSeekResponsesProvider(
            base_url="https://api.deepseek.com",
            api_key="secret",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        ).complete(_request())

    assert response.status is ModelResponseStatus.INCOMPLETE
    assert response.incomplete_reason == "max_output_tokens"
    assert response.content.startswith("最终信息来自公开文档")
    assert len(response.native_tool_events) == 3
    assert response.native_tool_events[1].status is NativeToolStatus.FAILED
    assert not response.tool_calls


@pytest.mark.asyncio
async def test_failed_and_malformed_responses_are_not_normal_answers() -> None:
    fixtures: list[object] = [_fixture("failed.json"), ["not", "an", "object"]]
    expected = [LLMUnavailableError, LLMInvalidResponseError]
    for payload, error in zip(fixtures, expected, strict=True):

        def handler(request: httpx.Request, body: object = payload) -> httpx.Response:
            return httpx.Response(200, request=request, json=body)

        async with httpx.AsyncClient(
            base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
        ) as client:
            provider = DeepSeekResponsesProvider(
                base_url="https://api.deepseek.com",
                api_key="secret",
                timeout_seconds=1,
                max_retries=0,
                client=client,
            )
            with pytest.raises(error):
                await provider.complete(_request())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, LLMInvalidRequestError),
        (401, LLMAuthenticationError),
        (403, LLMAuthenticationError),
        (429, LLMRateLimitError),
    ],
)
async def test_http_errors_remain_distinguishable(status: int, error: type[Exception]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, request=request, json={"error": "sanitized"})

    async with httpx.AsyncClient(
        base_url="https://api.deepseek.com", transport=httpx.MockTransport(handler)
    ) as client:
        provider = DeepSeekResponsesProvider(
            base_url="https://api.deepseek.com",
            api_key="secret",
            timeout_seconds=1,
            max_retries=0,
            client=client,
        )
        with pytest.raises(error):
            await provider.complete(_request())
