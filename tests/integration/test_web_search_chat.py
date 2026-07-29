"""End-to-end controlled web search and backend source display tests."""

from __future__ import annotations

import json

import pytest
from tests.conftest import MemorySender, build_harness, make_settings

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import (
    ChatRequest,
    ChatResponse,
    InboundMessage,
    OutboundMessage,
    SenderIdentity,
    ToolCall,
    ToolFunction,
)
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.web.base import WebSearchError
from qq_ai_bot.web.fake import FakeWebSearchProvider
from qq_ai_bot.web.models import WebSearchResponse, WebSearchSource


def event(
    text: str,
    *,
    message_id: str,
    user_id: str = "1001",
    group_id: str | None = None,
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        bot_user_id="8000",
        event_type="message:test",
        scope_type=ScopeType.GROUP if group_id else ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=user_id, nickname=f"用户{user_id}"),
        text=text,
        raw_text=text,
        group_id=group_id,
        mentions_bot=group_id is not None,
        segments=({"type": "text", "data": {"text": text}},),
    )


def web_response() -> WebSearchResponse:
    return WebSearchResponse(
        query="最新 DeepSeek 更新",
        sources=(
            WebSearchSource(
                source_id="source-1",
                title="DeepSeek 官方更新",
                url="https://example.com/deepseek-update",
                domain="example.com",
                snippet="官方发布了新版本。",
                relevant_content="官方发布了新版本，并改进了工具调用。",
                provider_score=0.95,
            ),
        ),
        provider_request_id="request-1",
        latency_seconds=0.1,
    )


class WebToolLLM(LLMProvider):
    """Issue web_search, then summarize its structured result."""

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if request.messages[-1].role != "tool":
            assert "web_search" in {tool.name for tool in request.tools}
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id=f"web-{len(self.requests)}",
                        function=ToolFunction(
                            name="web_search",
                            arguments=json.dumps(
                                {"query": "最新 DeepSeek 更新", "topic": "news"},
                                ensure_ascii=False,
                            ),
                        ),
                    ),
                ),
            )
        result = json.loads(request.messages[-1].content or "{}")
        if not result.get("ok"):
            return ChatResponse(content="联网查询暂时失败，请稍后再试。", latency_seconds=0)
        return ChatResponse(
            content=(
                "DeepSeek 最近更新了工具调用能力。[1]\n\n"
                "来源：\n1. 模型编造来源\nhttps://fake.example/not-real\n"
                "https://example.com/deepseek-update"
            ),
            latency_seconds=0,
        )


class ToolGatewaySender(MemorySender):
    """Record whether a forbidden post-web OneBot action executes."""

    def __init__(self) -> None:
        super().__init__()
        self.api_calls: list[tuple[str, dict[str, object]]] = []

    async def call_api(self, action: str, params: dict[str, object]) -> object:
        self.api_calls.append((action, params))
        return {"status": "ok"}

    async def send(self, message: OutboundMessage) -> None:
        await super().send(message)


class WebThenOneBotLLM(LLMProvider):
    """Simulate a malicious webpage trying to cause an admin action."""

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="web-first",
                        function=ToolFunction(
                            name="web_search",
                            arguments='{"query":"测试网页提示词注入"}',
                        ),
                    ),
                ),
            )
        if len(self.requests) == 2:
            assert "call_onebot_api" not in {tool.name for tool in request.tools}
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id="forbidden-onebot",
                        function=ToolFunction(
                            name="call_onebot_api",
                            arguments=(
                                '{"action":"send_private_msg",'
                                '"params":{"user_id":"12345678","message":"不应发送"}}'
                            ),
                        ),
                    ),
                ),
            )
        assert "unknown_capability" in (request.messages[-1].content or "")
        return ChatResponse(content="已忽略网页中的操作指令。", latency_seconds=0)


class RepeatedWebToolLLM(LLMProvider):
    """Request four web calls so the backend-enforced limit is observable."""

    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if len(self.requests) <= 4:
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id=f"web-repeat-{len(self.requests)}",
                        function=ToolFunction(
                            name="web_search",
                            arguments=json.dumps({"query": f"搜索 {len(self.requests)}"}),
                        ),
                    ),
                ),
            )
        assert "web_tool_limit_exceeded" in (request.messages[-1].content or "")
        return ChatResponse(content="已根据前三次搜索完成回答。", latency_seconds=0)


def web_settings(database: Database):
    return make_settings(
        database.url,
        web_enabled=True,
        tavily_api_key="test-placeholder",
        split_daily_chat_sentences=False,
    )


@pytest.mark.asyncio
async def test_normal_web_answer_hides_sources_and_model_generated_links(
    database: Database,
) -> None:
    llm = WebToolLLM()
    web = FakeWebSearchProvider(response=web_response())
    harness = build_harness(database, web_settings(database), llm, web_provider=web)
    sender = MemorySender()

    result = await harness.processor.handle(
        event("最近 DeepSeek 有什么更新？", message_id="web-hidden"),
        sender,
    )

    assert result.reason == "chat"
    assert [message.text for message in sender.messages] == ["DeepSeek 最近更新了工具调用能力。"]
    assert web.search_requests[0].query == "最新 DeepSeek 更新"


@pytest.mark.asyncio
async def test_explicit_request_sends_backend_rendered_real_sources(
    database: Database,
) -> None:
    llm = WebToolLLM()
    harness = build_harness(
        database,
        web_settings(database),
        llm,
        web_provider=FakeWebSearchProvider(response=web_response()),
    )
    sender = MemorySender()

    result = await harness.processor.handle(
        event(
            "最近 DeepSeek 有什么更新？请附上来源。",
            message_id="web-visible",
        ),
        sender,
    )

    assert result.sent_messages == 2
    assert sender.messages[0].text == "DeepSeek 最近更新了工具调用能力。"
    assert sender.messages[1].text == (
        "来源：\n1. DeepSeek 官方更新\n   https://example.com/deepseek-update"
    )
    assert "fake.example" not in "\n".join(message.text for message in sender.messages)


@pytest.mark.asyncio
async def test_source_followup_skips_llm_and_uses_previous_persisted_run(
    database: Database,
) -> None:
    llm = WebToolLLM()
    harness = build_harness(
        database,
        web_settings(database),
        llm,
        web_provider=FakeWebSearchProvider(response=web_response()),
    )
    await harness.processor.handle(
        event("最近 DeepSeek 有什么更新？", message_id="web-first"),
        MemorySender(),
    )
    request_count = len(llm.requests)
    followup_sender = MemorySender()

    result = await harness.processor.handle(
        event("来源呢？", message_id="web-followup"),
        followup_sender,
    )

    assert result.sent_messages == 1
    assert len(llm.requests) == request_count
    assert followup_sender.messages[0].text.startswith("来源：")


@pytest.mark.asyncio
async def test_private_users_and_group_members_cannot_read_each_others_sources(
    database: Database,
) -> None:
    llm = WebToolLLM()
    harness = build_harness(
        database,
        web_settings(database),
        llm,
        web_provider=FakeWebSearchProvider(response=web_response()),
    )
    await harness.processor.handle(
        event("查询更新", message_id="private-owner", user_id="1001"),
        MemorySender(),
    )
    private_other = MemorySender()
    await harness.processor.handle(
        event("来源呢", message_id="private-other", user_id="1002"),
        private_other,
    )
    assert private_other.messages[0].text == "当前对话中没有可提供的联网来源。"

    await harness.processor.handle(
        event("查询更新", message_id="group-owner", user_id="1001", group_id="2001"),
        MemorySender(),
    )
    group_other = MemorySender()
    await harness.processor.handle(
        event("来源呢", message_id="group-other", user_id="1002", group_id="2001"),
        group_other,
    )
    assert group_other.messages[0].text == "当前对话中没有可提供的联网来源。"


@pytest.mark.asyncio
async def test_web_failure_is_returned_to_llm_for_a_natural_answer(database: Database) -> None:
    llm = WebToolLLM()
    harness = build_harness(
        database,
        web_settings(database),
        llm,
        web_provider=FakeWebSearchProvider(
            error=WebSearchError("provider_unavailable", "联网服务暂不可用")
        ),
    )
    sender = MemorySender()

    result = await harness.processor.handle(
        event("查询最新消息", message_id="web-failure"),
        sender,
    )

    assert result.reason == "chat"
    assert sender.messages[0].text == "联网查询暂时失败，请稍后再试。"


@pytest.mark.asyncio
async def test_web_content_cannot_trigger_superuser_onebot_tool(database: Database) -> None:
    llm = WebThenOneBotLLM()
    harness = build_harness(
        database,
        web_settings(database),
        llm,
        web_provider=FakeWebSearchProvider(response=web_response()),
    )
    sender = ToolGatewaySender()

    result = await harness.processor.handle(
        event("联网查看后回答", message_id="web-admin", user_id="9000"),
        sender,
    )

    assert result.reason == "chat"
    assert not sender.api_calls
    assert sender.messages[0].text == "已忽略网页中的操作指令。"


@pytest.mark.asyncio
async def test_each_turn_executes_at_most_three_web_tools(database: Database) -> None:
    llm = RepeatedWebToolLLM()
    web = FakeWebSearchProvider(response=web_response())
    harness = build_harness(database, web_settings(database), llm, web_provider=web)
    sender = MemorySender()

    result = await harness.processor.handle(
        event("做一个复杂联网研究", message_id="web-limit"),
        sender,
    )

    assert result.reason == "chat"
    assert len(web.search_requests) == 3
    assert sender.messages[0].text == "已根据前三次搜索完成回答。"
