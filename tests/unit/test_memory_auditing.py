"""Separated, bounded and dry-run-first memory audit contracts."""

from __future__ import annotations

import json
from typing import cast

import pytest
from tests.conftest import make_settings

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.messages import ChatRequest, ChatResponse
from qq_ai_bot.memory.auditing import (
    MemoryAuditAction,
    MemoryAuditCoordinator,
    SelfMemoryAuditor,
    UserMemoryAuditor,
)
from qq_ai_bot.memory.candidates import MemoryConflictCandidateResolver
from qq_ai_bot.memory.claim_processor import MemoryClaimProcessor
from qq_ai_bot.memory.classifier import MemoryRelationClassifier
from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryEvidenceRelation,
    MemoryKind,
    MemoryScopeType,
    MemorySourceType,
    SelfMemoryVisibility,
)
from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFactCreate, MemoryFactQuery
from qq_ai_bot.memory.mutation.service import MemoryMutationService
from qq_ai_bot.memory.repository import MemoryFactRepository
from qq_ai_bot.memory.resolution import MemoryResolutionPolicy
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.model_runtime.models import (
    ModelCapability,
    ModelProtocol,
    ModelTask,
    StructuredOutputMode,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import EventLedgerRepository
from qq_ai_bot.services.concurrency import ConcurrencyManager


class _AuditExecutor:
    def __init__(self) -> None:
        self.tasks: list[ModelTask] = []
        self.prompts: list[str] = []

    async def execute(self, task: ModelTask, request: ChatRequest) -> ChatResponse:
        self.tasks.append(task)
        self.prompts.append(request.messages[0].content or "")
        return ChatResponse(
            content=json.dumps(
                {"action": "keep", "reason": "证据与范围一致", "confidence": 0.9},
                ensure_ascii=False,
            ),
            latency_seconds=0,
        )

    def model_name(self, task: ModelTask) -> str:
        return "audit-fake"

    def structured_output_mode(self, task: ModelTask) -> StructuredOutputMode:
        return StructuredOutputMode.TEXT_JSON

    def protocol(self, task: ModelTask) -> ModelProtocol:
        return ModelProtocol.CHAT_COMPLETIONS

    def capabilities(self, task: ModelTask) -> frozenset[ModelCapability]:
        return frozenset({ModelCapability.STRUCTURED_OUTPUT})


class _CorrectingAuditExecutor(_AuditExecutor):
    async def execute(self, task: ModelTask, request: ChatRequest) -> ChatResponse:
        self.tasks.append(task)
        self.prompts.append(request.messages[0].content or "")
        return ChatResponse(
            content=json.dumps(
                {
                    "action": "correct",
                    "corrected_content": "审计器不应改写 SELF",
                    "reason": "测试后端动作边界",
                    "confidence": 0.9,
                },
                ensure_ascii=False,
            ),
            latency_seconds=0,
        )


def _coordinator(
    database: Database,
    executor: _AuditExecutor,
) -> tuple[MemoryAuditCoordinator, MemoryFactService, EventLedgerRepository]:
    settings = make_settings("sqlite+aiosqlite:///:memory:", self_memory_enabled=True)
    repository = MemoryFactRepository(database)
    facts = MemoryFactService(repository)
    ledger = EventLedgerRepository(database)
    processor = MemoryClaimProcessor(
        settings=settings,
        facts=facts,
        candidate_resolver=MemoryConflictCandidateResolver(repository),
        relation_classifier=cast(MemoryRelationClassifier, object()),
        resolution_policy=MemoryResolutionPolicy(),
    )
    mutations = MemoryMutationService(
        settings=settings,
        facts=facts,
        processor=processor,
        ledger=ledger,
    )
    concurrency = ConcurrencyManager(2)
    return (
        MemoryAuditCoordinator(
            facts=facts,
            ledger=ledger,
            mutations=mutations,
            user_auditor=UserMemoryAuditor(executor, concurrency),
            self_auditor=SelfMemoryAuditor(executor, concurrency),
        ),
        facts,
        ledger,
    )


@pytest.mark.asyncio
async def test_user_and_self_audits_use_separate_tasks_and_prompts(database: Database) -> None:
    executor = _AuditExecutor()
    coordinator, facts, ledger = _coordinator(database, executor)
    event, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id="audit-source",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        direction="inbound",
        content="我喜欢咖啡，也认为 Yuki 应该复查工具结果",
        private_peer_user_id="1001",
    )
    user_fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
            kind=MemoryKind.PREFERENCE,
            memory_key="preference:coffee",
            category="preference",
            content="喜欢咖啡",
            source_type=MemorySourceType.AUTOMATIC,
        ),
        evidence=MemoryEvidenceCreate(
            event_id=event.id,
            source_speaker_user_id="1001",
            relation=MemoryEvidenceRelation.SELF_STATEMENT,
            excerpt="我喜欢咖啡",
        ),
    )
    self_fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.SELF,
            visibility_type=SelfMemoryVisibility.PRIVATE,
            visibility_user_id="1001",
            kind=MemoryKind.PREFERENCE,
            memory_key="principle:verify",
            category="self_principle",
            content="重要工具结果需要复查",
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.AGENT_REFLECTION,
        ),
        evidence=MemoryEvidenceCreate(
            event_id=event.id,
            source_speaker_user_id="1001",
            relation=MemoryEvidenceRelation.AGENT_REFLECTION,
            authority=MemoryAuthority.AGENT_REFLECTION,
            excerpt="认为 Yuki 应该复查工具结果",
        ),
    )

    user_report = await coordinator.audit_fact(user_fact.id)
    self_report = await coordinator.audit_fact(self_fact.id)

    assert user_report.dry_run and self_report.dry_run
    assert user_report.decision.action is MemoryAuditAction.KEEP
    assert executor.tasks == [
        ModelTask.MEMORY_EXTRACTION,
        ModelTask.MEMORY_SELF_REFLECTION,
    ]
    assert executor.prompts[0] != executor.prompts[1]


@pytest.mark.asyncio
async def test_self_audit_backend_rejects_semantic_rewrite(database: Database) -> None:
    executor = _CorrectingAuditExecutor()
    coordinator, facts, ledger = _coordinator(database, executor)
    event, _ = await ledger.append(
        bot_user_id="8000",
        platform_message_id="self-audit-boundary",
        scope_type=ScopeType.PRIVATE,
        sender_user_id="1001",
        direction="inbound",
        content="Yuki 认为重要结果要复查",
        private_peer_user_id="1001",
    )
    fact = await facts.remember(
        MemoryFactCreate(
            scope_type=MemoryScopeType.SELF,
            visibility_type=SelfMemoryVisibility.PRIVATE,
            visibility_user_id="1001",
            kind=MemoryKind.PREFERENCE,
            memory_key="principle:audit-boundary",
            category="self_principle",
            content="重要结果需要复查",
            source_type=MemorySourceType.AUTOMATIC,
            authority=MemoryAuthority.AGENT_REFLECTION,
        ),
        evidence=MemoryEvidenceCreate(
            event_id=event.id,
            source_speaker_user_id="1001",
            relation=MemoryEvidenceRelation.AGENT_REFLECTION,
            authority=MemoryAuthority.AGENT_REFLECTION,
            excerpt=event.content,
        ),
    )

    report = await coordinator.audit_fact(fact.id, dry_run=False)

    assert not report.applied
    assert report.reason_code == "self_audit_action_not_allowed"
    unchanged = await facts.get_fact(fact.id)
    assert unchanged is not None and unchanged.content == "重要结果需要复查"


@pytest.mark.asyncio
async def test_entity_audit_never_crosses_the_exact_target(database: Database) -> None:
    executor = _AuditExecutor()
    coordinator, facts, ledger = _coordinator(database, executor)
    for user_id in ("1001", "2002"):
        event, _ = await ledger.append(
            bot_user_id="8000",
            platform_message_id=f"audit-{user_id}",
            scope_type=ScopeType.PRIVATE,
            sender_user_id=user_id,
            direction="inbound",
            content="我喜欢咖啡",
            private_peer_user_id=user_id,
        )
        await facts.remember(
            MemoryFactCreate(
                scope_type=MemoryScopeType.PERSON,
                subject_user_id=user_id,
                memory_key="preference:coffee",
                category="preference",
                content="喜欢咖啡",
                source_type=MemorySourceType.AUTOMATIC,
            ),
            evidence=MemoryEvidenceCreate(
                event_id=event.id,
                source_speaker_user_id=user_id,
                relation=MemoryEvidenceRelation.SELF_STATEMENT,
                excerpt="我喜欢咖啡",
            ),
        )

    reports, cursor = await coordinator.audit_entity(
        MemoryFactQuery(
            scope_type=MemoryScopeType.PERSON,
            subject_user_id="1001",
        ),
        limit=1000,
    )

    assert len(reports) == 1
    assert cursor is None
