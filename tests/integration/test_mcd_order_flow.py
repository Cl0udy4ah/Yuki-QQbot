"""Offline end-to-end coverage for a generic bundled MCP mutation flow."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from nonebot.adapters.onebot.v11 import Message
from tests.conftest import MemorySender, build_harness, make_settings
from tests.unit.test_normalizer import private_event

from qq_ai_bot.adapters.onebot.normalizer import normalize_event
from qq_ai_bot.domain.messages import ChatRequest, ChatResponse, ToolCall, ToolFunction
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.mcp.fake import FakeMCPConnection
from qq_ai_bot.mcp.manager import MCPManager
from qq_ai_bot.mcp.provider import MCPToolProvider
from qq_ai_bot.mcp.repository import MCPRepository
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.planner.fake import FakePlannerProvider
from qq_ai_bot.planner.models import (
    DeliveryMode,
    PlannerDecision,
    PlannerReasonCode,
    ToolMode,
    ToolSelection,
    TurnPlan,
)
from qq_ai_bot.planner.observability import PlannerObservability
from qq_ai_bot.planner.service import PlannerService

_REMOTE_SEQUENCE = (
    "query-store",
    "query-meals",
    "query-meal-detail",
    "calculate-price",
    "create-order",
    "create-order",
)


def _remote_tool(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        description=f"offline {name}",
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        outputSchema={"type": "object"},
        annotations=None,
    )


def _result(data: object) -> SimpleNamespace:
    return SimpleNamespace(content=(), structuredContent=data, isError=False)


@pytest.mark.asyncio
async def test_bundled_mcd_order_flow_commits_once_and_preserves_payment_url(
    database: Database,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "mcp-order.json"
    token = "offline-secret-token"
    tools = tuple(_remote_tool(name) for name in dict.fromkeys(_REMOTE_SEQUENCE))
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "mcd": {
                        "command": "fake-mcd",
                        "env": {"TOKEN": token},
                        "yuki": {
                            "scope": "mcp.mcd",
                            "toolBundles": {
                                "order": {
                                    "scope": "mcp.mcd.order",
                                    "summary": "完整查询、校价和创建待支付订单",
                                    "includeTools": [
                                        "query-store",
                                        "query-meals",
                                        "query-meal-detail",
                                        "calculate-price",
                                        "create-order",
                                    ],
                                }
                            },
                            "toolAnnotations": {
                                "query-store": {"readOnlyHint": True},
                                "query-meals": {"readOnlyHint": True},
                                "query-meal-detail": {"readOnlyHint": True},
                                "calculate-price": {"readOnlyHint": True},
                            },
                        },
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    connection = FakeMCPConnection(
        tools=tools,
        results={
            "query-store": _result({"storeCode": "1410135"}),
            "query-meals": _result({"mealCode": "spicy-burger"}),
            "query-meal-detail": _result({"name": "麦辣鸡腿堡"}),
            "calculate-price": _result({"amount": 25.0, "status": "confirmed"}),
            "create-order": _result(
                {
                    "success": True,
                    "orderId": "001",
                    "status": "pending_payment",
                    "payH5Url": "https://example.com/pay",
                }
            ),
        },
    )
    manager = MCPManager(
        enabled=True,
        config_path=config_path,
        cache_enabled=False,
        metadata_cache_ttl_seconds=60,
        connect_timeout_seconds=1,
        request_timeout_seconds=1,
        max_parallel_calls=2,
        repository=MCPRepository(database),
        connection_factory=lambda *_args, **_kwargs: connection,
    )
    await manager.start()

    requests: list[ChatRequest] = []
    committed_seen = False

    def model(request: ChatRequest) -> ChatResponse:
        nonlocal committed_seen
        requests.append(request)
        tool_messages = [message for message in request.messages if message.role == "tool"]
        if tool_messages:
            latest = json.loads(tool_messages[-1].content or "{}")
            if latest.get("tool_name") == "create-order" and latest.get("ok"):
                committed_seen = latest.get("mutation_committed") is True
        index = len(requests) - 1
        visible_names = {tool.name for tool in request.tools}
        expected_bundle = {f"mcp__mcd__{name}" for name in dict.fromkeys(_REMOTE_SEQUENCE)}
        assert expected_bundle.issubset(visible_names)
        assert "web_search" not in visible_names
        if index < len(_REMOTE_SEQUENCE):
            name = _REMOTE_SEQUENCE[index]
            return ChatResponse(
                content="",
                latency_seconds=0,
                tool_calls=(
                    ToolCall(
                        id=f"offline-{index}",
                        function=ToolFunction(
                            name=f"mcp__mcd__{name}",
                            arguments="{}",
                        ),
                    ),
                ),
            )
        duplicate = json.loads(tool_messages[-1].content or "{}")
        assert duplicate["error"] == "duplicate_mutation"
        return ChatResponse(
            content="待支付订单已经创建：https://example.com/pay",
            latency_seconds=0,
        )

    settings = make_settings(
        database.url,
        mcp_enabled=True,
        mcp_config_path=config_path,
        mcp_gateway_enabled=True,
        mcp_tool_selection_mode="catalog",
        tooling_selected_tool_limit=1,
        agent_max_tool_calls=10,
        agent_max_model_requests=10,
    )
    harness = build_harness(database, settings, FakeLLMProvider(model))
    harness.processor._chat.register_tool_provider(
        MCPToolProvider(manager, gateway_enabled=True, selection_mode="catalog")
    )
    plan = TurnPlan(
        decision=PlannerDecision.REPLY,
        intent="创建麦辣鸡腿堡待支付订单并返回支付链接",
        target_user_ids=("1001",),
        delivery_mode=DeliveryMode.SINGLE,
        desired_messages=1,
        tool_selection=ToolSelection(
            mode=ToolMode.INHERIT,
            scopes=("mcp.mcd.order",),
        ),
        confidence=1.0,
        reason_code=PlannerReasonCode.DIRECT_REQUEST,
    )
    harness.processor._planner = PlannerService(
        provider=FakePlannerProvider(plan),
        observability=PlannerObservability(),
    )
    sender = MemorySender()
    try:
        outcome = await harness.processor.handle(
            normalize_event(
                private_event(
                    Message("帮我点麦辣鸡腿堡，到店取餐，创建待支付订单，把链接发给我。"),
                    message_id=212,
                )
            ),
            sender,
        )
    finally:
        await manager.close()

    assert outcome.reason == "chat"
    assert [name for name, _arguments in connection.calls].count("create-order") == 1
    assert committed_seen
    assert "https://example.com/pay" in "\n".join(message.text for message in sender.messages)
    assert all("plugin" not in tool.name for request in requests for tool in request.tools)
    serialized_requests = json.dumps(
        [
            {
                "messages": [message.content for message in request.messages],
                "tools": [tool.name for tool in request.tools],
            }
            for request in requests
        ],
        ensure_ascii=False,
        default=str,
    )
    assert token not in serialized_requests
    assert all(request.messages[0].role == "system" for request in requests)
