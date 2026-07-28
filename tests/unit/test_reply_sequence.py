from __future__ import annotations

import asyncio

import pytest
from tests.conftest import MemorySender, make_settings

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.domain.messages import OutboundMessage
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.planner.models import (
    DeliveryMode,
    PlannerDecision,
    PlannerReasonCode,
    ToolMode,
    TurnPlan,
)
from qq_ai_bot.services.reply_sequence import ReplySequenceManager
from qq_ai_bot.services.turn_coordinator import (
    ConversationTurnCoordinator,
    ReplySequenceCancelled,
)


def _plan(mode: DeliveryMode, messages: int = 1) -> TurnPlan:
    return TurnPlan(
        decision=PlannerDecision.REPLY,
        intent="test",
        delivery_mode=mode,
        desired_messages=messages,
        tool_mode=ToolMode.NONE,
        confidence=1.0,
        reason_code=PlannerReasonCode.DIRECT_REQUEST,
    )


def test_structured_code_blocks_are_reopened_when_qq_limit_requires_split() -> None:
    assert _plan(DeliveryMode.STRUCTURED).delivery_mode is DeliveryMode.STRUCTURED
    block = "```python\n" + "\n".join(f"print({index})" for index in range(30)) + "\n```"
    chunks = ReplySequenceManager._split_preserving_structure(block, limit=80)
    assert len(chunks) > 1
    assert all(chunk.startswith("```python\n") and chunk.endswith("\n```") for chunk in chunks)


@pytest.mark.asyncio
async def test_blank_line_splits_even_a_single_mode_chat_reply(database: Database) -> None:
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    ).snapshot()
    manager = ReplySequenceManager(ConversationTurnCoordinator())

    assert manager.render(
        "先说第一件事。\n\n然后说第二件事。",
        plan=_plan(DeliveryMode.SINGLE),
        runtime=runtime,
    ) == ("先说第一件事。", "然后说第二件事。")


@pytest.mark.asyncio
async def test_structured_mode_preserves_internal_blank_lines(database: Database) -> None:
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    ).snapshot()
    manager = ReplySequenceManager(ConversationTurnCoordinator())
    text = "说明：\n\n```python\nprint('one')\n\nprint('two')\n```"

    assert manager.render(
        text,
        plan=_plan(DeliveryMode.STRUCTURED),
        runtime=runtime,
    ) == (text,)


@pytest.mark.asyncio
async def test_excess_blank_sections_merge_without_recreating_empty_line(
    database: Database,
) -> None:
    runtime_service = RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    )
    await runtime_service.set_override(
        "reply.plan_hard_max_messages",
        2,
        scope_type="global",
        scope_id="",
        actor_user_id="9000",
        trigger_message_id="blank-line-cap",
    )
    runtime = await runtime_service.snapshot()
    manager = ReplySequenceManager(ConversationTurnCoordinator())

    chunks = manager.render(
        "第一段\n\n第二段\n\n第三段",
        plan=_plan(DeliveryMode.SINGLE),
        runtime=runtime,
    )

    assert chunks == ("第一段\n第二段", "第三段")
    assert all("\n\n" not in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_only_first_message_in_sequence_quotes_planner_target(database: Database) -> None:
    runtime = await RuntimeConfigService(
        settings=make_settings(database.url),
        database=database,
    ).snapshot()
    coordinator = ConversationTurnCoordinator()
    token = await coordinator.notify_message("group:2001", TurnOrigin.USER_MESSAGE)
    manager = ReplySequenceManager(coordinator)
    sender = MemorySender()
    recorded: list[OutboundMessage] = []

    async def record(message: OutboundMessage, _result: object) -> None:
        recorded.append(message)

    result = await manager.send(
        text="第一条。\n\n第二条。",
        plan=_plan(DeliveryMode.NATURAL_MULTI, messages=2).model_copy(
            update={"reply_to_message_id": "12345"}
        ),
        runtime=runtime,
        token=token,
        sender=sender,
        record_outbound=record,
    )

    assert result.sent_messages == 2
    assert [message.reply_to_message_id for message in sender.messages] == ["12345", None]
    assert recorded == sender.messages


async def test_new_message_stops_unsent_reply_chunks() -> None:
    # Runtime snapshot construction is covered through RuntimeConfigService in
    # integration tests; this unit test focuses on coordinator cancellation.
    coordinator = ConversationTurnCoordinator()
    token = await coordinator.notify_message("group:1", TurnOrigin.USER_MESSAGE)
    entered = asyncio.Event()

    async def tracked() -> None:
        with pytest.raises(ReplySequenceCancelled):
            async with coordinator.track(token, "reply"):
                entered.set()
                await asyncio.Event().wait()

    task = asyncio.create_task(tracked())
    await entered.wait()
    await coordinator.notify_message("group:1", TurnOrigin.USER_MESSAGE)
    await task
