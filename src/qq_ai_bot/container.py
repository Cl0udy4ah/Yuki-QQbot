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
    ConversationRepository,
    GroupSettingsRepository,
    ProcessedEventRepository,
    UserProfileRepository,
)
from qq_ai_bot.services.chat import ChatService
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.deduplication import DeduplicationService
from qq_ai_bot.services.processor import MessageProcessor
from qq_ai_bot.services.rate_limit import SlidingWindowRateLimiter
from qq_ai_bot.services.user_profiles import UserProfileService

logger = logging.getLogger(__name__)


class ApplicationContainer:
    """Own all external resources for the NoneBot application lifespan."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.started_at = time.monotonic()
        self.database = Database(settings.database_url)
        self.conversations = ConversationRepository(self.database)
        self.groups = GroupSettingsRepository(self.database)
        self.user_profile_repository = UserProfileRepository(self.database)
        self.user_profiles = UserProfileService(self.user_profile_repository)
        self.processed_events = ProcessedEventRepository(self.database)
        self.provider = self._build_provider(settings)
        self.concurrency = ConcurrencyManager(settings.global_llm_concurrency)
        self.deduplication = DeduplicationService(
            self.processed_events,
            ttl_seconds=settings.processed_event_ttl_seconds,
        )
        self.rate_limiter = SlidingWindowRateLimiter(
            per_user=settings.per_user_requests_per_minute,
            per_group=settings.per_group_requests_per_minute,
        )
        self.chat = ChatService(
            settings=settings,
            conversations=self.conversations,
            provider=self.provider,
            concurrency=self.concurrency,
        )
        self.processor = MessageProcessor(
            settings=settings,
            conversations=self.conversations,
            groups=self.groups,
            user_profiles=self.user_profiles,
            chat=self.chat,
            deduplication=self.deduplication,
            rate_limiter=self.rate_limiter,
            concurrency=self.concurrency,
            onebot_connected=self.onebot_connected,
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

    def onebot_connected(self) -> bool:
        """Return whether NoneBot currently has at least one connected adapter bot."""

        return bool(get_bots())

    async def start(self) -> None:
        """Start maintenance tasks after migrations have run."""

        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(), name="processed-event-cleanup"
        )

    async def _cleanup_loop(self) -> None:
        while not self._cleanup_stop.is_set():
            try:
                deleted = await self.processed_events.cleanup_expired()
                if deleted:
                    logger.info("processed_events_cleaned count=%d", deleted)
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
