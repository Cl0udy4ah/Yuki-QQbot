from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from tests.conftest import make_settings

from qq_ai_bot.automation.authority import (
    DelegatedAuthority,
    effective_delegated_capabilities,
)
from qq_ai_bot.automation.executor import AutomationExecutor
from qq_ai_bot.automation.gateway import ProactiveGatewayError
from qq_ai_bot.automation.models import AutomationScript, AutomationStatus
from qq_ai_bot.automation.registry import (
    AutomationCapabilityRegistry,
    CapabilityResult,
    build_capability_registry,
)
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.automation.tools import AutomationToolService
from qq_ai_bot.automation.worker import AutomationWorker
from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import InboundMessage, SenderIdentity
from qq_ai_bot.persistence.models import AutomationStepRunModel, AutomationVersionModel
from qq_ai_bot.time.schedules import schedule_after_completion
from qq_ai_bot.time.service import TimeContextService


class FakeClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _inbound(user_id: str = "10001") -> InboundMessage:
    return InboundMessage(
        message_id="automation-create",
        event_type="private",
        scope_type=ScopeType.PRIVATE,
        sender=SenderIdentity(user_id=user_id, nickname="用户"),
        text="1秒后提醒我测试",
        raw_text="1秒后提醒我测试",
        bot_user_id="7777",
    )


def _script() -> AutomationScript:
    return AutomationScript.model_validate(
        {
            "version": 1,
            "name": "一次提醒",
            "timezone": "Asia/Shanghai",
            "schedule": {"type": "after", "seconds": 1},
            "context": {"scene": "none"},
            "steps": [
                {
                    "id": "send",
                    "call": "onebot.send_private_message",
                    "arguments": {"user_id": "$creator_user_id", "text": "测试"},
                }
            ],
            "limits": {
                "max_steps": 1,
                "max_llm_calls": 0,
                "max_tool_calls": 1,
                "max_messages": 1,
                "timeout_seconds": 30,
            },
        }
    )


@pytest.mark.asyncio
async def test_repository_persists_versions_and_owner_scope(database) -> None:
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    settings = make_settings(database.url, automation_enabled=True)
    registry = build_capability_registry()
    repository = AutomationRepository(database)
    service = AutomationService(
        settings=settings,
        repository=repository,
        registry=registry,
        time_service=TimeContextService(database, clock=clock),
    )
    row = await service.create(_script(), inbound=_inbound(), conversation_key="private:10001")

    assert row.id > 0
    assert (await repository.get(row.id)).script_hash == row.script_hash  # type: ignore[union-attr]
    assert len(await service.list("10001")) == 1
    with pytest.raises(ValueError, match="当前用户"):
        await service.require_owned(row.id, "20002")

    assert [item.id for item in await service.list_current("10001")] == [row.id]
    assert await service.list_completed("10001") == ()
    await repository.set_status(
        row.id,
        creator_user_id="10001",
        status=AutomationStatus.COMPLETED,
        now=clock.now(),
    )
    assert await service.list_current("10001") == ()
    assert [item.id for item in await service.list_completed("10001")] == [row.id]
    with pytest.raises(ValueError, match="当前任务编号"):
        await service.current_by_number("10001", 1)


def test_create_tool_teaches_the_model_the_proactive_message_gateway(database) -> None:
    settings = make_settings(database.url, automation_enabled=True)
    service = AutomationService(
        settings=settings,
        repository=AutomationRepository(database),
        registry=build_capability_registry(),
        time_service=TimeContextService(database),
    )

    tool = next(
        item
        for item in AutomationToolService(service).definitions()
        if item.name == "automation_create"
    )
    assert "onebot.send_private_message" in tool.description
    assert "不需要也不应改用聊天工具 call_onebot_api" in tool.description
    example = tool.parameters["properties"]["script"]["examples"][0]  # type: ignore[index]
    assert example["steps"][0]["arguments"]["user_id"] == "$creator_user_id"
    assert "automation_list_history" in {
        item.name for item in AutomationToolService(service).definitions()
    }


@pytest.mark.asyncio
async def test_worker_executes_once_and_prevents_duplicate_claim(database) -> None:
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    calls: list[dict[str, object]] = []

    async def send(arguments, context):
        calls.append(arguments)
        return CapabilityResult(data={"sent": True}, messages_sent=1)

    settings = make_settings(
        database.url,
        automation_enabled=True,
        automation_poll_seconds=0.01,
        automation_lease_seconds=30,
    )
    registry = build_capability_registry({"onebot.send_private_message": send})
    repository = AutomationRepository(database)
    time_service = TimeContextService(database, clock=clock)
    service = AutomationService(
        settings=settings,
        repository=repository,
        registry=registry,
        time_service=time_service,
    )
    row = await service.create(_script(), inbound=_inbound(), conversation_key="private:10001")
    clock.advance(2)
    first = await repository.claim_due(worker_id="first", now=clock.now(), lease_seconds=30)
    second = await repository.claim_due(worker_id="second", now=clock.now(), lease_seconds=30)
    assert [item.id for item in first] == [row.id]
    assert second == ()
    await repository.release_claim(row.id, worker_id="first", next_run_at=row.next_run_at)

    worker = AutomationWorker(
        settings=settings,
        repository=repository,
        executor=AutomationExecutor(
            settings=settings,
            registry=registry,
            repository=repository,
            time_service=time_service,
        ),
        time_service=time_service,
        bot_connected=lambda _bot_id: True,
    )
    await worker.start()
    await asyncio.sleep(0.08)
    await worker.close()

    completed = await repository.get(row.id)
    assert completed is not None
    assert completed.status is AutomationStatus.COMPLETED
    assert len(calls) == 1
    history = await repository.run_history(row.id)
    assert len(history) == 1
    assert history[0].messages_sent == 1
    assert (
        await repository.create_run(
            row.id,
            scheduled_for=history[0].scheduled_for,
            actual_started_at=clock.now(),
        )
        is None
    )


@pytest.mark.asyncio
async def test_superuser_authority_revocation_blocks_old_task(database) -> None:
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    settings = make_settings(database.url, automation_enabled=True, superusers_csv="9000")
    registry = build_capability_registry()
    repository = AutomationRepository(database)
    service = AutomationService(
        settings=settings,
        repository=repository,
        registry=registry,
        time_service=TimeContextService(database, clock=clock),
    )
    row = await service.create(_script(), inbound=_inbound("9000"), conversation_key="private:9000")
    clock.advance(2)
    run = await repository.create_run(
        row.id,
        scheduled_for=row.next_run_at,
        actual_started_at=clock.now(),  # type: ignore[arg-type]
    )
    assert run is not None
    revoked = make_settings(database.url, automation_enabled=True, superusers_csv="")
    result = await AutomationExecutor(
        settings=revoked,
        registry=registry,
        repository=repository,
        time_service=TimeContextService(database, clock=clock),
    ).execute(row, run)
    assert result.status.value == "blocked"


@pytest.mark.asyncio
async def test_pause_resume_cancel_and_run_now(database) -> None:
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    settings = make_settings(database.url, automation_enabled=True)
    repository = AutomationRepository(database)
    registry = build_capability_registry()
    service = AutomationService(
        settings=settings,
        repository=repository,
        registry=registry,
        time_service=TimeContextService(database, clock=clock),
    )
    inbound = _inbound()
    row = await service.create(_script(), inbound=inbound, conversation_key="private:10001")
    assert await service.pause(row.id, inbound=inbound, conversation_key="private:10001")
    assert (await repository.get(row.id)).status is AutomationStatus.PAUSED  # type: ignore[union-attr]
    assert await service.resume(row.id, inbound=inbound, conversation_key="private:10001")
    assert await service.run_now(row.id, inbound=inbound, conversation_key="private:10001")
    assert await service.cancel(row.id, inbound=inbound, conversation_key="private:10001")
    assert (await repository.get(row.id)).status is AutomationStatus.CANCELLED  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_removed_capability_blocks_task_and_new_capability_is_not_granted(
    database,
) -> None:
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    settings = make_settings(database.url, automation_enabled=True)
    original = build_capability_registry()
    repository = AutomationRepository(database)
    time_service = TimeContextService(database, clock=clock)
    service = AutomationService(
        settings=settings,
        repository=repository,
        registry=original,
        time_service=time_service,
    )
    row = await service.create(_script(), inbound=_inbound(), conversation_key="private:10001")
    authority = DelegatedAuthority.model_validate(row.authority_snapshot)

    expanded = build_capability_registry()
    expanded.register(replace(expanded.require("yuki.generate"), name="future.read"))
    effective = effective_delegated_capabilities(
        authority,
        settings=settings,
        registry=expanded,
    )
    assert effective == frozenset({"onebot.send_private_message"})
    assert "future.read" not in effective

    removed = AutomationCapabilityRegistry()
    for definition in original.list():
        if definition.name != "onebot.send_private_message":
            removed.register(definition)
    clock.advance(2)
    run = await repository.create_run(
        row.id,
        scheduled_for=row.next_run_at,
        actual_started_at=clock.now(),  # type: ignore[arg-type]
    )
    assert run is not None
    result = await AutomationExecutor(
        settings=settings,
        registry=removed,
        repository=repository,
        time_service=time_service,
    ).execute(row, run)
    assert result.status.value == "blocked"


@pytest.mark.asyncio
async def test_bot_disconnect_keeps_due_slot_without_creating_a_run(database) -> None:
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    settings = make_settings(
        database.url,
        automation_enabled=True,
        automation_poll_seconds=0.01,
    )
    repository = AutomationRepository(database)
    time_service = TimeContextService(database, clock=clock)
    service = AutomationService(
        settings=settings,
        repository=repository,
        registry=build_capability_registry(),
        time_service=time_service,
    )
    row = await service.create(_script(), inbound=_inbound(), conversation_key="private:10001")
    clock.advance(2)
    worker = AutomationWorker(
        settings=settings,
        repository=repository,
        executor=AutomationExecutor(
            settings=settings,
            registry=build_capability_registry(),
            repository=repository,
            time_service=time_service,
        ),
        time_service=time_service,
        bot_connected=lambda _bot_id: False,
    )
    await worker.start()
    await asyncio.sleep(0.05)
    await worker.close()

    retained = await repository.get(row.id)
    assert retained is not None
    assert retained.status is AutomationStatus.ACTIVE
    assert retained.next_run_at == row.next_run_at
    assert await repository.run_history(row.id) == ()


@pytest.mark.asyncio
async def test_misfired_once_task_is_marked_missed_without_sending(database) -> None:
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    calls = 0

    async def send(arguments, context):
        nonlocal calls
        calls += 1
        return CapabilityResult(data={"sent": True}, messages_sent=1)

    settings = make_settings(
        database.url,
        automation_enabled=True,
        automation_poll_seconds=0.01,
        automation_default_misfire_grace_seconds=30,
    )
    registry = build_capability_registry({"onebot.send_private_message": send})
    repository = AutomationRepository(database)
    time_service = TimeContextService(database, clock=clock)
    service = AutomationService(
        settings=settings,
        repository=repository,
        registry=registry,
        time_service=time_service,
    )
    row = await service.create(_script(), inbound=_inbound(), conversation_key="private:10001")
    clock.advance(60)
    worker = AutomationWorker(
        settings=settings,
        repository=repository,
        executor=AutomationExecutor(
            settings=settings,
            registry=registry,
            repository=repository,
            time_service=time_service,
        ),
        time_service=time_service,
        bot_connected=lambda _bot_id: True,
    )
    await worker.start()
    await asyncio.sleep(0.05)
    await worker.close()

    finished = await repository.get(row.id)
    assert finished is not None
    assert finished.status is AutomationStatus.COMPLETED
    assert calls == 0
    assert (await repository.run_history(row.id))[0].status.value == "missed"


@pytest.mark.asyncio
async def test_uncertain_send_is_never_retried(database) -> None:
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    calls = 0

    async def uncertain(arguments, context):
        nonlocal calls
        calls += 1
        raise ProactiveGatewayError("onebot_transport_uncertain", uncertain=True)

    settings = make_settings(database.url, automation_enabled=True)
    registry = build_capability_registry({"onebot.send_private_message": uncertain})
    repository = AutomationRepository(database)
    time_service = TimeContextService(database, clock=clock)
    service = AutomationService(
        settings=settings,
        repository=repository,
        registry=registry,
        time_service=time_service,
    )
    row = await service.create(_script(), inbound=_inbound(), conversation_key="private:10001")
    clock.advance(2)
    run = await repository.create_run(
        row.id,
        scheduled_for=row.next_run_at,
        actual_started_at=clock.now(),  # type: ignore[arg-type]
    )
    assert run is not None
    result = await AutomationExecutor(
        settings=settings,
        registry=registry,
        repository=repository,
        time_service=time_service,
    ).execute(row, run)
    assert result.status.value == "uncertain"
    assert result.error_category == "onebot_transport_uncertain"
    assert calls == 1


@pytest.mark.asyncio
async def test_web_result_cannot_be_followed_by_admin_mutation(database) -> None:
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    config_calls = 0

    async def web(arguments, context):
        return CapabilityResult(data={"sources": [{"summary": "外部内容"}]})

    async def config(arguments, context):
        nonlocal config_calls
        config_calls += 1
        return CapabilityResult(data={"ok": True})

    settings = make_settings(database.url, automation_enabled=True, superusers_csv="9000")
    registry = build_capability_registry({"web.search": web, "config.set": config})
    repository = AutomationRepository(database)
    time_service = TimeContextService(database, clock=clock)
    service = AutomationService(
        settings=settings,
        repository=repository,
        registry=registry,
        time_service=time_service,
    )
    script = AutomationScript.model_validate(
        {
            "version": 1,
            "name": "联网后修改配置",
            "timezone": "Asia/Shanghai",
            "schedule": {"type": "after", "seconds": 1},
            "steps": [
                {
                    "id": "search",
                    "call": "web.search",
                    "arguments": {"query": "测试", "topic": "general"},
                },
                {
                    "id": "mutate",
                    "call": "config.set",
                    "arguments": {
                        "key": "reply.daily_split_enabled",
                        "scope_type": "global",
                        "scope_id": "",
                        "value": False,
                    },
                },
            ],
            "limits": {
                "max_steps": 2,
                "max_llm_calls": 0,
                "max_tool_calls": 2,
                "max_messages": 0,
                "timeout_seconds": 30,
            },
        }
    )
    row = await service.create(
        script,
        inbound=_inbound("9000"),
        conversation_key="private:9000",
    )
    clock.advance(2)
    run = await repository.create_run(
        row.id,
        scheduled_for=row.next_run_at,
        actual_started_at=clock.now(),  # type: ignore[arg-type]
    )
    assert run is not None
    result = await AutomationExecutor(
        settings=settings,
        registry=registry,
        repository=repository,
        time_service=time_service,
    ).execute(row, run)
    assert result.status.value == "failed"
    assert result.error_category == "web_mutation_isolation"
    assert config_calls == 0


@pytest.mark.asyncio
async def test_update_creates_version_and_step_audit_redacts_secrets(database) -> None:
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))
    settings = make_settings(database.url, automation_enabled=True)
    repository = AutomationRepository(database)
    registry = build_capability_registry()
    service = AutomationService(
        settings=settings,
        repository=repository,
        registry=registry,
        time_service=TimeContextService(database, clock=clock),
    )
    inbound = _inbound()
    row = await service.create(_script(), inbound=inbound, conversation_key="private:10001")
    payload = _script().model_dump(mode="json")
    payload["name"] = "更新后的提醒"
    await service.update(
        row.id,
        payload,
        inbound=inbound,
        conversation_key="private:10001",
    )
    run = await repository.create_run(
        row.id,
        scheduled_for=row.next_run_at,
        actual_started_at=clock.now(),  # type: ignore[arg-type]
    )
    assert run is not None
    await repository.record_step(
        run_id=run.id,
        step_id="audit",
        capability="fake.read",
        status="succeeded",
        input_summary={"api_key": "should-not-persist", "query": "safe"},
        output_summary={"token": "also-secret"},
        started_at=clock.now(),
        finished_at=clock.now(),
        error_category=None,
    )
    async with database.sessions() as session:
        versions = await session.scalar(
            select(func.count(AutomationVersionModel.id)).where(
                AutomationVersionModel.automation_id == row.id
            )
        )
        step = await session.scalar(select(AutomationStepRunModel))
    assert versions == 2
    assert step is not None
    assert "should-not-persist" not in step.input_summary_json
    assert "also-secret" not in step.output_summary_json
    assert "[redacted]" in step.input_summary_json


@pytest.mark.asyncio
async def test_three_consecutive_failures_stop_periodic_task(database) -> None:
    clock = FakeClock(datetime(2026, 7, 27, tzinfo=UTC))

    async def fail(arguments, context):
        raise ProactiveGatewayError("onebot_rejected")

    settings = make_settings(
        database.url,
        automation_enabled=True,
        automation_max_consecutive_failures=3,
    )
    registry = build_capability_registry({"onebot.send_private_message": fail})
    repository = AutomationRepository(database)
    time_service = TimeContextService(database, clock=clock)
    service = AutomationService(
        settings=settings,
        repository=repository,
        registry=registry,
        time_service=time_service,
    )
    payload = _script().model_dump(mode="json")
    payload["schedule"] = {"type": "interval", "seconds": 60}
    row = await service.create(
        AutomationScript.model_validate(payload),
        inbound=_inbound(),
        conversation_key="private:10001",
    )
    executor = AutomationExecutor(
        settings=settings,
        registry=registry,
        repository=repository,
        time_service=time_service,
    )
    for index in range(3):
        clock.advance(61)
        claimed = await repository.claim_due(
            worker_id="failure-worker",
            now=clock.now(),
            lease_seconds=30,
        )
        assert len(claimed) == 1
        current = claimed[0]
        assert current.next_run_at is not None
        run = await repository.create_run(
            current.id,
            scheduled_for=current.next_run_at,
            actual_started_at=clock.now(),
        )
        assert run is not None
        result = await executor.execute(current, run)
        await repository.finish_run(
            run.id,
            status=result.status,
            steps_completed=result.steps_completed,
            llm_calls=result.llm_calls,
            tool_calls=result.tool_calls,
            messages_sent=result.messages_sent,
            error_category=result.error_category,
            summary=result.summary,
            finished_at=clock.now(),
        )
        next_run = schedule_after_completion(
            current.script.schedule,
            current.next_run_at,
            clock.now(),
            current.timezone,
        )
        await repository.finish_automation_run(
            current.id,
            worker_id="failure-worker",
            status=result.status,
            next_run_at=next_run,
            now=clock.now(),
            max_consecutive_failures=3,
        )
        if index < 2:
            current_row = await repository.get(row.id)
            assert current_row is not None
            assert current_row.status is AutomationStatus.ACTIVE
    stopped = await repository.get(row.id)
    assert stopped is not None
    assert stopped.status is AutomationStatus.FAILED
    assert stopped.consecutive_failures == 3
    assert stopped.next_run_at is None
