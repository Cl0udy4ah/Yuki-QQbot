"""Application resource container and lifecycle management."""

from __future__ import annotations

import asyncio
import logging
import time

from nonebot import get_bots
from sqlalchemy.exc import SQLAlchemyError

from qq_ai_bot.config import Settings
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import (
    AgentActionRepository,
    ConversationRepository,
    EventLedgerRepository,
    GroupMemoryRepository,
    GroupSettingsRepository,
    MemoryJobRepository,
    MemoryRepository,
    PrivateUserSettingsRepository,
    ProcessedEventRepository,
    UserProfileRepository,
    WebSearchSourceRepository,
)
from qq_ai_bot.services.agent_tools import AgentToolService
from qq_ai_bot.services.autonomous_groups import AutonomousGroupService
from qq_ai_bot.services.chat import ChatService
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.deduplication import DeduplicationService
from qq_ai_bot.services.group_members import GroupMemberService
from qq_ai_bot.services.group_memories import GroupMemoryService
from qq_ai_bot.services.memory_worker import MemoryWorker
from qq_ai_bot.services.processor import MessageProcessor
from qq_ai_bot.services.rate_limit import SlidingWindowRateLimiter
from qq_ai_bot.services.source_policy import SourceDisplayPolicy
from qq_ai_bot.services.source_renderer import SourceRenderer
from qq_ai_bot.services.user_profiles import UserProfileService
from qq_ai_bot.web.base import WebSearchProvider
from qq_ai_bot.web.tavily import TavilyWebSearchProvider

logger = logging.getLogger(__name__)


class ApplicationContainer:
    """Own all external resources for the NoneBot application lifespan."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.started_at = time.monotonic()
        self.database = Database(settings.database_url)
        self.conversations = ConversationRepository(self.database)
        self.groups = GroupSettingsRepository(self.database)
        self.private_users = PrivateUserSettingsRepository(self.database)
        self.user_profile_repository = UserProfileRepository(self.database)
        self.people = self.user_profile_repository
        self.user_profiles = UserProfileService(self.user_profile_repository)
        self.group_members = GroupMemberService(self.user_profile_repository)
        self.group_memory_repository = GroupMemoryRepository(self.database)
        self.processed_events = ProcessedEventRepository(self.database)
        self.ledger = EventLedgerRepository(self.database)
        self.memories = MemoryRepository(self.database)
        self.memory_jobs = MemoryJobRepository(self.database)
        self.agent_actions = AgentActionRepository(self.database)
        self.web_sources = WebSearchSourceRepository(self.database)
        self.provider = self._build_provider(settings)
        self.web_provider = self._build_web_provider(settings)
        self.concurrency = ConcurrencyManager(settings.global_llm_concurrency)
        self.deduplication = DeduplicationService(
            self.processed_events,
            ttl_seconds=settings.processed_event_ttl_seconds,
        )
        self.rate_limiter = SlidingWindowRateLimiter(
            per_user=settings.per_user_requests_per_minute,
            per_group=settings.per_group_requests_per_minute,
        )
        self.group_memories = GroupMemoryService(
            settings=settings,
            repository=self.group_memory_repository,
            provider=self.provider,
            concurrency=self.concurrency,
        )
        self.agent_tools = AgentToolService(
            settings=settings,
            ledger=self.ledger,
            memories=self.memories,
            actions=self.agent_actions,
            web_provider=self.web_provider,
            web_sources=self.web_sources,
        )
        self.chat = ChatService(
            settings=settings,
            provider=self.provider,
            concurrency=self.concurrency,
            ledger=self.ledger,
            people=self.people,
            memories=self.memories,
            tools=self.agent_tools,
            web_sources=self.web_sources,
            source_policy=SourceDisplayPolicy(),
            source_renderer=SourceRenderer(),
        )
        self.memory_worker = MemoryWorker(
            settings=settings,
            jobs=self.memory_jobs,
            memories=self.memories,
            provider=self.provider,
            concurrency=self.concurrency,
        )
        self.autonomous_groups = AutonomousGroupService(
            settings=settings,
            provider=self.provider,
            concurrency=self.concurrency,
            memories=self.memories,
            chat=self.chat,
        )
        self.processor = MessageProcessor(
            settings=settings,
            conversations=self.conversations,
            groups=self.groups,
            private_users=self.private_users,
            user_profiles=self.user_profiles,
            group_members=self.group_members,
            chat=self.chat,
            deduplication=self.deduplication,
            rate_limiter=self.rate_limiter,
            concurrency=self.concurrency,
            onebot_connected=self.onebot_connected,
            ledger=self.ledger,
            people=self.people,
            memories=self.memories,
            memory_worker=self.memory_worker,
            autonomous_groups=self.autonomous_groups,
        )
        self._cleanup_stop = asyncio.Event()
        self._cleanup_task: asyncio.Task[None] | None = None

    @staticmethod
    def _build_provider(settings: Settings) -> LLMProvider:
        if settings.llm_provider.casefold() == "fake":
            return FakeLLMProvider()
        return OpenAICompatibleProvider(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout_seconds=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )

    @staticmethod
    def _build_web_provider(settings: Settings) -> WebSearchProvider | None:
        if not settings.web_enabled:
            return None
        return TavilyWebSearchProvider(
            api_key=settings.tavily_api_key,
            search_depth=settings.web_search_depth,
            extract_max_results=settings.web_extract_max_results,
            timeout_seconds=settings.web_timeout_seconds,
            max_retries=settings.web_max_retries,
            global_concurrency=settings.web_global_concurrency,
        )

    def onebot_connected(self) -> bool:
        """Return whether NoneBot currently has at least one connected adapter bot."""

        return bool(get_bots())

    async def start(self) -> None:
        """Start maintenance tasks after migrations have run."""

        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(), name="processed-event-cleanup"
        )
        await self.memory_worker.start()

    async def _cleanup_loop(self) -> None:
        while not self._cleanup_stop.is_set():
            try:
                deleted = await self.processed_events.cleanup_expired()
                if deleted:
                    logger.info("processed_events_cleaned count=%d", deleted)
                web_deleted = await self.web_sources.cleanup_expired(
                    retention_days=self.settings.web_source_retention_days
                )
                if web_deleted:
                    logger.info("web_source_runs_cleaned count=%d", web_deleted)
            except (SQLAlchemyError, OSError, RuntimeError) as exc:
                logger.error("processed_event_cleanup_failed", exc_info=exc)
            try:
                await asyncio.wait_for(
                    self._cleanup_stop.wait(),
                    timeout=self.settings.processed_event_cleanup_seconds,
                )
            except TimeoutError:
                continue

    async def close(self) -> None:
        """Gracefully stop tasks and close provider/database pools."""

        self._cleanup_stop.set()
        if self._cleanup_task is not None:
            await self._cleanup_task
        await self.autonomous_groups.close()
        await self.memory_worker.close()
        if self.web_provider is not None:
            await self.web_provider.close()
        await self.provider.close()
        await self.database.close()


_container: ApplicationContainer | None = None


def set_container(container: ApplicationContainer) -> None:
    """Publish the initialized lifespan container to adapter handlers."""

    global _container
    _container = container


def get_container() -> ApplicationContainer:
    """Return the initialized container or fail clearly during invalid lifecycle use."""

    if _container is None:
        raise RuntimeError("application container is not initialized")
    return _container
