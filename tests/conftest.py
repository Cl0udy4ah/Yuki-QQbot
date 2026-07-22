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
    ConversationRepository,
    GroupSettingsRepository,
    ProcessedEventRepository,
)
from qq_ai_bot.services.chat import ChatService
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.deduplication import DeduplicationService
from qq_ai_bot.services.processor import MessageProcessor
from qq_ai_bot.services.rate_limit import SlidingWindowRateLimiter


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
    provider: LLMProvider
    concurrency: ConcurrencyManager
    processor: MessageProcessor


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
    }
    values.update(overrides)
    return Settings.model_validate(values)


def build_harness(
    database: Database,
    settings: Settings,
    provider: LLMProvider | None = None,
) -> Harness:
    conversations = ConversationRepository(database)
    groups = GroupSettingsRepository(database)
    processed_events = ProcessedEventRepository(database)
    llm = provider or FakeLLMProvider()
    concurrency = ConcurrencyManager(settings.global_llm_concurrency)
    chat = ChatService(
        settings=settings,
        conversations=conversations,
        provider=llm,
        concurrency=concurrency,
    )
    processor = MessageProcessor(
        settings=settings,
        conversations=conversations,
        groups=groups,
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
    )
    return Harness(settings, database, conversations, groups, llm, concurrency, processor)


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> Database:
    path = (tmp_path / "test.db").as_posix()
    db = Database(f"sqlite+aiosqlite:///{path}")
    await db.create_schema()
    try:
        yield db
    finally:
        await db.close()
