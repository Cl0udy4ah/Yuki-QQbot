"""Qwen vision provider request, parsing, fallback, and retry behavior."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from qq_ai_bot.vision.base import VisionError
from qq_ai_bot.vision.models import (
    PreparedFrame,
    PreparedVisualInput,
    VisionAnalysisOptions,
)
from qq_ai_bot.vision.qwen import QwenVisionProvider


def _input() -> PreparedVisualInput:
    return PreparedVisualInput(
        media_hash="a" * 64,
        frames=(
            PreparedFrame(
                content_hash="b" * 64,
                mime_type="image/jpeg",
                width=10,
                height=10,
                frame_index=0,
                frame_count=1,
                data_url="data:image/jpeg;base64,YQ==",
            ),
        ),
        animated=False,
        source="current",
    )


def _success(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]},
    )


@pytest.mark.asyncio
async def test_general_request_uses_data_url_and_stays_fast_when_confident() -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _success(
            "```json\n"
            '{"items":[{"index":1,"description":"一只猫","confidence":1.2}],'
            '"overall_description":"猫的照片"}\n```'
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = QwenVisionProvider(
        base_url="https://vision.example/v1",
        api_key="secret-key",
        model="qwen-test",
        client=client,
    )
    observation = await provider.analyze((_input(),), "这是什么？")

    assert captured["enable_thinking"] is False
    assert captured["temperature"] == 0.1
    user_content = captured["messages"][1]["content"]
    assert user_content[-1]["type"] == "text"
    assert "用户问题：这是什么？" in user_content[-1]["text"]
    assert "recognized_character" in user_content[-1]["text"]
    assert any(
        item.get("image_url", {}).get("url", "").startswith("data:image/jpeg;base64,")
        for item in user_content
    )
    assert "secret-key" not in json.dumps(captured, ensure_ascii=False)
    assert observation.items[0].description == "一只猫"
    assert observation.items[0].confidence == 1.0
    assert provider.provider_name == "qwen"
    assert provider.model_name == "qwen-test"
    assert "secret-key" not in repr(provider)
    await client.aclose()


@pytest.mark.asyncio
async def test_character_mode_enables_thinking_budget_and_parses_identity() -> None:
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _success(
            '{"items":[{"index":1,"description":"黄色小恐龙",'
            '"recognized_character":"奶龙","franchise":"奶龙",'
            '"character_candidates":[{"name":"奶龙","work":"奶龙",'
            '"evidence":"黄色恐龙与白色腹部","confidence":0.94}],'
            '"confidence":0.92}],"overall_description":"奶龙表情包"}'
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = QwenVisionProvider(
        base_url="https://vision.example/v1",
        api_key="secret",
        client=client,
    )

    observation = await provider.analyze(
        (_input(),),
        "这是谁？",
        options=VisionAnalysisOptions(
            analysis_mode="character",
            thinking_enabled=True,
            thinking_budget=3072,
        ),
    )

    assert captured["enable_thinking"] is True
    assert captured["thinking_budget"] == 3072
    assert captured["max_tokens"] == 8192
    item = observation.items[0]
    assert item.recognized_character == "奶龙"
    assert item.franchise == "奶龙"
    assert item.character_candidates[0].name == "奶龙"
    assert item.character_candidates[0].confidence == 0.94
    await client.aclose()


@pytest.mark.asyncio
async def test_low_confidence_general_result_is_reviewed_with_thinking() -> None:
    thinking_flags: list[bool] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        thinking_flags.append(payload["enable_thinking"])
        if not payload["enable_thinking"]:
            return _success(
                '{"items":[{"index":1,"description":"模糊角色",'
                '"confidence":0.2}],"overall_description":"不确定"}'
            )
        return _success(
            '{"items":[{"index":1,"description":"黄色小恐龙",'
            '"recognized_character":"奶龙","confidence":0.91}],'
            '"overall_description":"奶龙"}'
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = QwenVisionProvider(
        base_url="https://vision.example/v1",
        api_key="secret",
        client=client,
    )

    observation = await provider.analyze(
        (_input(),),
        "描述图片",
        options=VisionAnalysisOptions(
            analysis_mode="general",
            thinking_enabled=True,
            low_confidence_retry_threshold=0.65,
        ),
    )

    assert thinking_flags == [False, True]
    assert observation.items[0].recognized_character == "奶龙"
    await client.aclose()


@pytest.mark.asyncio
async def test_invalid_model_json_degrades_without_a_second_request() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _success("看起来是一张模糊的表情图片")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = QwenVisionProvider(
        base_url="https://vision.example/v1",
        api_key="secret",
        client=client,
    )
    observation = await provider.analyze((_input(),), "解释表情")

    assert calls == 1
    assert observation.partial_failure is True
    assert "模糊" in observation.overall_description
    await client.aclose()


@pytest.mark.asyncio
async def test_duplicate_and_out_of_range_item_indices_are_ignored() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return _success(
            '{"items":['
            '{"index":1,"description":"有效"},'
            '{"index":1,"description":"重复"},'
            '{"index":2,"description":"越界"}'
            '],"overall_description":"完成"}'
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = QwenVisionProvider(
        base_url="https://vision.example/v1",
        api_key="secret",
        client=client,
    )

    observation = await provider.analyze((_input(),), "问题")

    assert [item.description for item in observation.items] == ["有效"]
    assert not observation.partial_failure
    await client.aclose()


@pytest.mark.asyncio
async def test_provider_rejects_non_data_url_frames_before_network_access() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _success("{}")

    unsafe = _input().model_copy(
        update={
            "frames": (_input().frames[0].model_copy(update={"data_url": "https://evil.test/x"}),)
        }
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = QwenVisionProvider(
        base_url="https://vision.example/v1",
        api_key="secret",
        client=client,
    )

    with pytest.raises(VisionError) as raised:
        await provider.analyze((unsafe,), "问题")
    assert raised.value.code == "invalid_frame"
    assert calls == 0
    await client.aclose()


@pytest.mark.asyncio
async def test_429_retries_once_but_400_does_not_retry() -> None:
    statuses = [429, 200]
    sleeps: list[float] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        status = statuses.pop(0)
        if status == 429:
            return httpx.Response(status, headers={"Retry-After": "0"})
        return _success('{"items":[],"overall_description":"完成"}')

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = QwenVisionProvider(
        base_url="https://vision.example/v1",
        api_key="secret",
        max_retries=1,
        client=client,
        sleep=sleep,
    )
    assert (await provider.analyze((_input(),), "问题")).overall_description == "完成"
    assert sleeps == [0.0]
    await client.aclose()

    calls = 0

    async def bad_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400)

    bad_client = httpx.AsyncClient(transport=httpx.MockTransport(bad_handler))
    bad_provider = QwenVisionProvider(
        base_url="https://vision.example/v1",
        api_key="secret",
        max_retries=1,
        client=bad_client,
    )
    with pytest.raises(VisionError) as raised:
        await bad_provider.analyze((_input(),), "问题")
    assert raised.value.code == "provider_rejected"
    assert calls == 1
    await bad_client.aclose()


@pytest.mark.asyncio
async def test_timeout_retries_once_then_returns_sanitized_error() -> None:
    calls = 0
    sleeps: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = QwenVisionProvider(
        base_url="https://vision.example/v1",
        api_key="secret",
        max_retries=1,
        client=client,
        sleep=sleep,
    )

    with pytest.raises(VisionError) as raised:
        await provider.analyze((_input(),), "问题")

    assert raised.value.code == "timeout"
    assert calls == 2
    assert sleeps == [0.25]
    assert "secret" not in raised.value.detail
    await client.aclose()


@pytest.mark.asyncio
async def test_5xx_retries_once_then_reports_provider_unavailable() -> None:
    calls = 0
    sleeps: list[float] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = QwenVisionProvider(
        base_url="https://vision.example/v1",
        api_key="secret",
        max_retries=1,
        client=client,
        sleep=sleep,
    )

    with pytest.raises(VisionError) as raised:
        await provider.analyze((_input(),), "问题")

    assert raised.value.code == "provider_unavailable"
    assert calls == 2
    assert sleeps == [0.25]
    await client.aclose()


@pytest.mark.asyncio
async def test_empty_provider_content_is_rejected_without_retry() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _success("")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = QwenVisionProvider(
        base_url="https://vision.example/v1",
        api_key="secret",
        max_retries=1,
        client=client,
    )

    with pytest.raises(VisionError) as raised:
        await provider.analyze((_input(),), "问题")

    assert raised.value.code == "empty_response"
    assert calls == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_explicit_content_refusal_is_not_retried_or_prompted_around() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "", "refusal": "policy"},
                        "finish_reason": "content_filter",
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = QwenVisionProvider(
        base_url="https://vision.example/v1",
        api_key="secret",
        max_retries=1,
        client=client,
    )

    with pytest.raises(VisionError) as raised:
        await provider.analyze((_input(),), "问题")

    assert raised.value.code == "content_refused"
    assert calls == 1
    await client.aclose()
