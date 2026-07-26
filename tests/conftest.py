"""Shared isolated database and service fixtures."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest_asyncio

from qq_ai_bot.config import Settings
from qq_ai_bot.domain.messages import OutboundMessage
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import (
    AgentActionRepository,
    ConversationRepository,
    EventLedgerRepository,
    GroupMemoryRepository,
    GroupSettingsRepository,
    MediaAnalysisRepository,
    MemoryRepository,
    PrivateUserSettingsRepository,
    ProcessedEventRepository,
    RelationshipJobRepository,
    RelationshipRepository,
    UserProfileRepository,
    WebSearchSourceRepository,
)
from qq_ai_bot.services.agent_tools import AgentToolService
from qq_ai_bot.services.chat import ChatService
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.deduplication import DeduplicationService
from qq_ai_bot.services.group_members import GroupMemberService
from qq_ai_bot.services.group_memories import GroupMemoryService
from qq_ai_bot.services.image_preprocessor import ImagePreprocessor
from qq_ai_bot.services.media_resolver import MediaResolver
from qq_ai_bot.services.processor import MessageProcessor
from qq_ai_bot.services.rate_limit import SlidingWindowRateLimiter
from qq_ai_bot.services.relationship_evaluator import FakeRelationshipEvaluator
from qq_ai_bot.services.relationship_worker import RelationshipWorker
from qq_ai_bot.services.source_policy import SourceDisplayPolicy
from qq_ai_bot.services.source_renderer import SourceRenderer
from qq_ai_bot.services.user_profiles import UserProfileService
from qq_ai_bot.services.vision_rate_limit import VisionRateLimiter
from qq_ai_bot.services.vision_service import VisionService
from qq_ai_bot.vision.base import VisionProvider
from qq_ai_bot.web.base import WebSearchProvider


class MemorySender:
    """Record outbound messages and optionally fail every send."""

    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[OutboundMessage] = []
        self.calls = 0
        self.fail = fail

    async def send(self, message: OutboundMessage) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("synthetic send failure")
        self.messages.append(message)


@dataclass(slots=True)
class Harness:
    settings: Settings
    database: Database
    conversations: ConversationRepository
    groups: GroupSettingsRepository
    private_users: PrivateUserSettingsRepository
    profiles: UserProfileRepository
    group_memories: GroupMemoryRepository
    relationships: RelationshipRepository
    relationship_jobs: RelationshipJobRepository
    relationship_worker: RelationshipWorker
    provider: LLMProvider
    concurrency: ConcurrencyManager
    processor: MessageProcessor
    vision: VisionService | None


def make_settings(database_url: str, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": database_url,
        "superusers_csv": "9000",
        "allowed_private_users_csv": "1001,1002,1003,1004,1005,1006,1007,1008,1009,1010",
        "enabled_groups_csv": "2001,2002",
        "ignored_bot_users_csv": "7777",
        "llm_provider": "fake",
        "llm_model": "fake-model",
        "global_llm_concurrency": 4,
        "per_user_requests_per_minute": 20,
        "per_group_requests_per_minute": 50,
        "daily_chat_message_delay_min_seconds": 0,
        "daily_chat_message_delay_max_seconds": 0,
    }
    values.update(overrides)
    return Settings.model_validate(values)


def build_harness(
    database: Database,
    settings: Settings,
    provider: LLMProvider | None = None,
    *,
    web_provider: WebSearchProvider | None = None,
    vision_provider: VisionProvider | None = None,
) -> Harness:
    conversations = ConversationRepository(database)
    groups = GroupSettingsRepository(database)
    private_users = PrivateUserSettingsRepository(
        database,
        initial_affection=settings.relationship_initial_affection,
        initial_trust=settings.relationship_initial_trust,
    )
    profiles = UserProfileRepository(
        database,
        initial_affection=settings.relationship_initial_affection,
        initial_trust=settings.relationship_initial_trust,
    )
    user_profiles = UserProfileService(profiles)
    group_members = GroupMemberService(profiles)
    group_memories = GroupMemoryRepository(database)
    processed_events = ProcessedEventRepository(database)
    ledger = EventLedgerRepository(database)
    memories = MemoryRepository(database)
    relationships = RelationshipRepository(
        database,
        initial_affection=settings.relationship_initial_affection,
        initial_trust=settings.relationship_initial_trust,
        trust_cap_offset=settings.trust_affection_cap_offset,
        max_affection_auto_delta=settings.affection_max_auto_delta,
        max_trust_auto_delta=settings.trust_max_auto_delta,
    )
    relationship_jobs = RelationshipJobRepository(
        database,
        max_attempts=settings.relationship_max_attempts,
    )
    web_sources = WebSearchSourceRepository(database)
    vision = (
        VisionService(
            provider=vision_provider,
            resolver=MediaResolver(
                max_download_bytes=settings.vision_max_download_bytes,
            ),
            preprocessor=ImagePreprocessor(
                max_dimension=settings.vision_max_dimension,
                max_pixels=settings.vision_max_pixels,
                max_prepared_bytes=settings.vision_max_prepared_bytes,
                gif_max_frames=8,
            ),
            analyses=MediaAnalysisRepository(database),
            rate_limiter=VisionRateLimiter(),
            max_prepared_bytes=settings.vision_max_prepared_bytes,
            global_concurrency=settings.vision_global_concurrency,
        )
        if vision_provider is not None
        else None
    )
    llm = provider or FakeLLMProvider()
    concurrency = ConcurrencyManager(settings.global_llm_concurrency)
    relationship_worker = RelationshipWorker(
        settings=settings,
        jobs=relationship_jobs,
        relationships=relationships,
        evaluator=FakeRelationshipEvaluator(),
    )
    group_memory_service = GroupMemoryService(
        settings=settings,
        repository=group_memories,
        provider=llm,
        concurrency=concurrency,
    )
    agent_tools = AgentToolService(
        settings=settings,
        ledger=ledger,
        memories=memories,
        actions=AgentActionRepository(database),
        web_provider=web_provider,
        web_sources=web_sources,
    )
    chat = ChatService(
        settings=settings,
        provider=llm,
        concurrency=concurrency,
        ledger=ledger,
        people=profiles,
        memories=memories,
        relationships=relationships,
        tools=agent_tools,
        web_sources=web_sources,
        source_policy=SourceDisplayPolicy(),
        source_renderer=SourceRenderer(),
        group_memories=group_memory_service,
    )
    processor = MessageProcessor(
        settings=settings,
        conversations=conversations,
        groups=groups,
        private_users=private_users,
        user_profiles=user_profiles,
        group_members=group_members,
        chat=chat,
        deduplication=DeduplicationService(
            processed_events,
            ttl_seconds=settings.processed_event_ttl_seconds,
        ),
        rate_limiter=SlidingWindowRateLimiter(
            per_user=settings.per_user_requests_per_minute,
            per_group=settings.per_group_requests_per_minute,
        ),
        concurrency=concurrency,
        onebot_connected=lambda: True,
        ledger=ledger,
        people=profiles,
        memories=memories,
        relationships=relationships,
        relationship_worker=relationship_worker,
        vision_service=vision,
    )
    return Harness(
        settings,
        database,
        conversations,
        groups,
        private_users,
        profiles,
        group_memories,
        relationships,
        relationship_jobs,
        relationship_worker,
        llm,
        concurrency,
        processor,
        vision,
    )


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> Database:
    path = (tmp_path / "test.db").as_posix()
    db = Database(f"sqlite+aiosqlite:///{path}")
    await db.create_schema()
    try:
        yield db
    finally:
        await db.close()
