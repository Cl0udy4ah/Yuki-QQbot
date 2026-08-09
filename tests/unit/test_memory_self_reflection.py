"""Bounded SELF-reflection scheduling and isolation contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.mcp.repository import MCPRepository
from qq_ai_bot.memory.self_reflection.models import SelfReflectionOutput
from qq_ai_bot.memory.self_reflection.repository import SelfReflectionRepository
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import (
    MemorySelfReflectionStateModel,
    MemoryToolReceiptModel,
)
from qq_ai_bot.persistence.repositories import EventLedgerRepository


@pytest.mark.asyncio
async def test_first_enable_baselines_history_and_only_collects_new_events(
    database: Database,
) -> None:
    ledger = EventLedgerRepository(database)
    await ledger.append(
        bot_user_id="8000",
        platform_message_id="historical",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content="历史消息不应进入自省",
        group_id="3001",
    )
    repository = SelfReflectionRepository(database)

    assert await repository.scan_new_events() == 0
    assert (
        await repository.claim_due(
            scheduled_slot="2026-08-08:04",
            local_date="2026-08-08",
            event_threshold=1,
            character_threshold=1,
            max_wait_seconds=1,
            max_sessions=3,
            max_daily_calls=9,
            max_events=20,
            max_characters=8000,
        )
        == ()
    )


@pytest.mark.asyncio
async def test_reflection_episode_is_bounded_and_requires_yuki_participation(
    database: Database,
) -> None:
    ledger = EventLedgerRepository(database)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    for index in range(11):
        await ledger.append(
            bot_user_id="8000",
            platform_message_id=f"episode-user-{index}",
            scope_type=ScopeType.GROUP,
            sender_user_id="1001",
            direction="inbound",
            content=f"第 {index} 条真实群消息",
            group_id="3001",
        )
    await ledger.append(
        bot_user_id="8000",
        platform_message_id="episode-yuki",
        scope_type=ScopeType.GROUP,
        sender_user_id="8000",
        direction="outbound",
        content="这是 Yuki 已确认投递的回复",
        group_id="3001",
        sender_is_bot=True,
    )
    assert await repository.scan_new_events() == 12

    episodes = await repository.claim_due(
        scheduled_slot="2026-08-08:12",
        local_date="2026-08-08",
        event_threshold=12,
        character_threshold=6000,
        max_wait_seconds=28800,
        max_sessions=3,
        max_daily_calls=9,
        max_events=20,
        max_characters=8000,
    )

    assert len(episodes) == 1
    assert len(episodes[0].events) == 12
    assert {item.group_id for item in episodes[0].events} == {"3001"}
    await repository.complete(episodes[0], proposals=0, committed=0)


@pytest.mark.asyncio
async def test_reflection_schedule_slot_and_daily_budget_are_idempotent(
    database: Database,
) -> None:
    ledger = EventLedgerRepository(database)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    for group_number in range(4):
        group_id = str(4000 + group_number)
        await ledger.append(
            bot_user_id="8000",
            platform_message_id=f"signal-{group_id}",
            scope_type=ScopeType.GROUP,
            sender_user_id="1001",
            direction="inbound",
            content="你刚才说错了，这是一次重要纠正",
            group_id=group_id,
            occurred_at=datetime.now(UTC),
        )
        await ledger.append(
            bot_user_id="8000",
            platform_message_id=f"reply-{group_id}",
            scope_type=ScopeType.GROUP,
            sender_user_id="8000",
            direction="outbound",
            content="我接受这次纠正",
            group_id=group_id,
            sender_is_bot=True,
        )
    await repository.scan_new_events()

    episodes = await repository.claim_due(
        scheduled_slot="2026-08-08:20",
        local_date="2026-08-08",
        event_threshold=12,
        character_threshold=6000,
        max_wait_seconds=28800,
        max_sessions=3,
        max_daily_calls=3,
        max_events=20,
        max_characters=8000,
    )

    assert len(episodes) == 3
    assert (
        await repository.claim_due(
            scheduled_slot="2026-08-08:20",
            local_date="2026-08-08",
            event_threshold=1,
            character_threshold=1,
            max_wait_seconds=1,
            max_sessions=3,
            max_daily_calls=3,
            max_events=20,
            max_characters=8000,
        )
        == ()
    )


@pytest.mark.asyncio
async def test_tool_receipt_is_bounded_and_redacts_nested_json_secrets(
    database: Database,
) -> None:
    ledger = EventLedgerRepository(database)
    event, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id="tool-receipt-source",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        direction="inbound",
        content="请查询状态",
        private_peer_user_id="1001",
    )
    repository = MCPRepository(database, reflection_excerpt_characters=120)
    await repository.record_invocation(
        conversation_key="private:1001",
        provider_id="test",
        tool_name="status",
        success=True,
        latency_seconds=0,
        result_size=200,
        artifact_created=False,
        error_category=None,
        trigger_message_id=event.platform_message_id,
        bot_user_id=event.bot_user_id,
        result_excerpt=json.dumps(
            {
                "data": {"token": "secret-value", "result": "ok"},
                "api-key": "another-secret",
            }
        ),
    )

    async with database.sessions() as session:
        receipt = await session.scalar(select(MemoryToolReceiptModel))
    assert receipt is not None
    assert "secret-value" not in receipt.result_excerpt
    assert "another-secret" not in receipt.result_excerpt
    assert receipt.result_excerpt.count("[redacted]") == 2
    assert len(receipt.result_excerpt) <= 120


@pytest.mark.asyncio
async def test_reflection_uses_oldest_window_context_and_keeps_concurrent_arrivals(
    database: Database,
) -> None:
    ledger = EventLedgerRepository(database)
    historical, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id="episode-context-before-baseline",
        scope_type=ScopeType.GROUP,
        sender_user_id="1001",
        direction="inbound",
        content="这是上线前的一句前置上下文",
        group_id="3001",
    )
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    pending_ids: list[int] = []
    for index in range(12):
        event, _ = await ledger.append(
            bot_user_id="8000",
            platform_message_id=f"oldest-window-{index}",
            scope_type=ScopeType.GROUP,
            sender_user_id="8000" if index == 2 else "1001",
            direction="outbound" if index == 2 else "inbound",
            content=f"上线后的第 {index + 1} 条连续消息",
            group_id="3001",
            sender_is_bot=index == 2,
        )
        pending_ids.append(event.id)
    assert await repository.scan_new_events() == 12

    first = (
        await repository.claim_due(
            scheduled_slot="2026-08-09:04",
            local_date="2026-08-09",
            event_threshold=1,
            character_threshold=8000,
            max_wait_seconds=28800,
            max_sessions=1,
            max_daily_calls=9,
            max_events=5,
            max_characters=8000,
            context_events=4,
        )
    )[0]
    assert [event.id for event in first.events] == pending_ids[:5]
    assert [event.id for event in first.context_events] == [historical.id]

    concurrent_ids: list[int] = []
    for index in range(2):
        event, _ = await ledger.append(
            bot_user_id="8000",
            platform_message_id=f"during-reflection-{index}",
            scope_type=ScopeType.GROUP,
            sender_user_id="8000" if index == 1 else "1001",
            direction="outbound" if index == 1 else "inbound",
            content=f"模型运行期间新到的第 {index + 1} 条消息",
            group_id="3001",
            sender_is_bot=index == 1,
        )
        concurrent_ids.append(event.id)
    assert await repository.scan_new_events() == 2
    await repository.complete(first, proposals=0, committed=0)

    async with database.sessions() as session:
        state = await session.scalar(select(MemorySelfReflectionStateModel))
    assert state is not None
    assert state.last_event_id == pending_ids[4]
    assert state.pending_events == 9
    assert state.latest_event_id == concurrent_ids[-1]

    second = (
        await repository.claim_due(
            scheduled_slot="2026-08-09:12",
            local_date="2026-08-09",
            event_threshold=1,
            character_threshold=8000,
            max_wait_seconds=28800,
            max_sessions=1,
            max_daily_calls=9,
            max_events=5,
            max_characters=8000,
            context_events=4,
        )
    )[0]
    assert [event.id for event in second.events] == pending_ids[5:10]
    assert [event.id for event in second.context_events] == pending_ids[1:5]


@pytest.mark.asyncio
async def test_reflection_character_limit_keeps_the_oldest_event(database: Database) -> None:
    ledger = EventLedgerRepository(database)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    first, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id="oversized-oldest-event",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        direction="inbound",
        content="长" * 9000,
        private_peer_user_id="1001",
    )
    await ledger.append(
        bot_user_id="8000",
        platform_message_id="reply-after-oversized-event",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="8000",
        direction="outbound",
        content="我看到了",
        private_peer_user_id="1001",
        sender_is_bot=True,
    )
    await repository.scan_new_events()

    batch = (
        await repository.claim_due(
            scheduled_slot="2026-08-09:20",
            local_date="2026-08-09",
            event_threshold=1,
            character_threshold=1,
            max_wait_seconds=28800,
            max_sessions=1,
            max_daily_calls=9,
            max_events=20,
            max_characters=8000,
        )
    )[0]

    assert [event.id for event in batch.events] == [first.id]
    assert batch.max_input_characters == 8000


@pytest.mark.asyncio
async def test_reflection_batches_same_conversation_separately_per_bot(
    database: Database,
) -> None:
    ledger = EventLedgerRepository(database)
    repository = SelfReflectionRepository(database)
    await repository.scan_new_events()
    for bot_user_id in ("8000", "9000"):
        await ledger.append(
            bot_user_id=bot_user_id,
            platform_message_id=f"{bot_user_id}-user-message",
            scope_type=ScopeType.GROUP,
            sender_user_id="1001",
            direction="inbound",
            content=f"只属于机器人 {bot_user_id} 的输入",
            group_id="3001",
        )
        await ledger.append(
            bot_user_id=bot_user_id,
            platform_message_id=f"{bot_user_id}-bot-reply",
            scope_type=ScopeType.GROUP,
            sender_user_id=bot_user_id,
            direction="outbound",
            content=f"机器人 {bot_user_id} 的真实回复",
            group_id="3001",
            sender_is_bot=True,
        )
    await repository.scan_new_events()

    batches = await repository.claim_due(
        scheduled_slot="2026-08-10:04",
        local_date="2026-08-10",
        event_threshold=1,
        character_threshold=8000,
        max_wait_seconds=28800,
        max_sessions=3,
        max_daily_calls=9,
        max_events=20,
        max_characters=8000,
    )

    assert len(batches) == 2
    assert {batch.state.bot_user_id for batch in batches} == {"8000", "9000"}
    assert all(
        {event.bot_user_id for event in batch.events} == {batch.state.bot_user_id}
        for batch in batches
    )


def test_reflection_output_allows_zero_to_two_free_episodes() -> None:
    assert SelfReflectionOutput.model_validate({}).episodes == ()
    one = SelfReflectionOutput.model_validate(
        {"episodes": [{"content": "我记得那天终于把问题修好了", "importance": 4}]}
    )
    assert one.episodes[0].content == "我记得那天终于把问题修好了"
    two = SelfReflectionOutput.model_validate(
        {
            "episodes": [
                {"content": "QQ 2186567848 当时说终于成功了", "importance": 4},
                {"content": "在群 1049765710 里，我后来觉得这件事挺有趣", "importance": 3},
            ]
        }
    )
    assert len(two.episodes) == 2
    with pytest.raises(ValidationError, match="at most two episodes"):
        SelfReflectionOutput.model_validate(
            {
                "episodes": [
                    {"content": "第一条"},
                    {"content": "第二条"},
                    {"content": "第三条"},
                ]
            }
        )
