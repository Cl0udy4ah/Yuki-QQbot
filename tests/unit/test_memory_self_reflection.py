"""Bounded SELF-reflection scheduling and isolation contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.mcp.repository import MCPRepository
from qq_ai_bot.memory.self_reflection.repository import SelfReflectionRepository
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.models import MemoryToolReceiptModel
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
