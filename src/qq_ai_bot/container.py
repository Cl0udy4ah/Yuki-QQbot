"""Application resource container and lifecycle management."""

from __future__ import annotations

import asyncio
import logging
import time

from nonebot import get_bots
from sqlalchemy.exc import SQLAlchemyError

from qq_ai_bot.admin.action_service import ActionRegistry, AdminActionService
from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.capabilities import AdminCapabilityService
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.permission_catalog import PermissionCatalogService
from qq_ai_bot.config import Settings
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import (
    AgentActionRepository,
    ConversationRepository,
    EmojiDescriptionRepository,
    EventLedgerRepository,
    GroupMemoryRepository,
    GroupSettingsRepository,
    MediaAnalysisRepository,
    MemoryJobRepository,
    MemoryRepository,
    PrivateUserSettingsRepository,
    ProcessedEventRepository,
    RelationshipJobRepository,
    RelationshipRepository,
    UserProfileRepository,
    WebSearchSourceRepository,
)
from qq_ai_bot.services.admin.config_admin import ConfigAdminService
from qq_ai_bot.services.admin.group_admin import GroupAdminService
from qq_ai_bot.services.admin.memory_admin import MemoryAdminService
from qq_ai_bot.services.admin.preference_admin import PreferenceAdminService
from qq_ai_bot.services.admin.private_access_admin import PrivateAccessAdminService
from qq_ai_bot.services.admin.relationship_admin import RelationshipAdminService
from qq_ai_bot.services.agent_tools import AgentToolService
from qq_ai_bot.services.autonomous_groups import AutonomousGroupService
from qq_ai_bot.services.chat import ChatService
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.deduplication import DeduplicationService
from qq_ai_bot.services.group_members import GroupMemberService
from qq_ai_bot.services.group_memories import GroupMemoryService
from qq_ai_bot.services.image_preprocessor import ImagePreprocessor
from qq_ai_bot.services.media_resolver import MediaResolver
from qq_ai_bot.services.memory_worker import MemoryWorker
from qq_ai_bot.services.processor import MessageProcessor
from qq_ai_bot.services.rate_limit import SlidingWindowRateLimiter
from qq_ai_bot.services.relationship_evaluator import (
    FakeRelationshipEvaluator,
    LLMRelationshipEvaluator,
    RelationshipEvaluator,
)
from qq_ai_bot.services.relationship_worker import RelationshipWorker
from qq_ai_bot.services.source_policy import SourceDisplayPolicy
from qq_ai_bot.services.source_renderer import SourceRenderer
from qq_ai_bot.services.user_profiles import UserProfileService
from qq_ai_bot.services.vision_rate_limit import VisionRateLimiter
from qq_ai_bot.services.vision_service import VISION_PROMPT_VERSION, VisionService
from qq_ai_bot.vision.base import VisionProvider
from qq_ai_bot.vision.fake import FakeVisionProvider
from qq_ai_bot.vision.qwen import QwenVisionProvider
from qq_ai_bot.web.base import WebSearchProvider
from qq_ai_bot.web.tavily import TavilyWebSearchProvider

logger = logging.getLogger(__name__)


class ApplicationContainer:
    """Own all external resources for the NoneBot application lifespan."""

    def __init__(
        self,
        settings: Settings,
        *,
        database: Database | None = None,
        runtime_config: RuntimeConfigService | None = None,
        vision_provider: VisionProvider | None = None,
    ) -> None:
        self.settings = settings
        self.started_at = time.monotonic()
        self.database = database or Database(settings.database_url)
        self.runtime_config = runtime_config or RuntimeConfigService(
            settings=settings,
            database=self.database,
        )
        self.admin_action_registry = ActionRegistry()
        self.permission_catalog = PermissionCatalogService(
            settings=settings,
            config_registry=self.runtime_config.registry,
            action_registry=self.admin_action_registry,
        )
        self.conversations = ConversationRepository(self.database)
        self.groups = GroupSettingsRepository(self.database)
        self.private_users = PrivateUserSettingsRepository(
            self.database,
            initial_affection=settings.relationship_initial_affection,
            initial_trust=settings.relationship_initial_trust,
        )
        self.user_profile_repository = UserProfileRepository(
            self.database,
            initial_affection=settings.relationship_initial_affection,
            initial_trust=settings.relationship_initial_trust,
        )
        self.people = self.user_profile_repository
        self.user_profiles = UserProfileService(
            self.user_profile_repository,
            self.runtime_config,
        )
        self.group_members = GroupMemberService(self.user_profile_repository)
        self.group_memory_repository = GroupMemoryRepository(self.database)
        self.processed_events = ProcessedEventRepository(self.database)
        self.ledger = EventLedgerRepository(self.database)
        self.memories = MemoryRepository(self.database)
        self.memory_jobs = MemoryJobRepository(self.database)
        self.agent_actions = AgentActionRepository(self.database)
        self.web_sources = WebSearchSourceRepository(self.database)
        self.media_analyses = MediaAnalysisRepository(self.database)
        self.emoji_descriptions = EmojiDescriptionRepository(self.database)
        self.relationships = RelationshipRepository(
            self.database,
            initial_affection=settings.relationship_initial_affection,
            initial_trust=settings.relationship_initial_trust,
            trust_cap_offset=settings.trust_affection_cap_offset,
            max_affection_auto_delta=settings.affection_max_auto_delta,
            max_trust_auto_delta=settings.trust_max_auto_delta,
        )
        self.relationship_jobs = RelationshipJobRepository(
            self.database,
            max_attempts=settings.relationship_max_attempts,
        )
        self.provider = self._build_provider(settings)
        self.web_provider = self._build_web_provider(settings)
        self.vision_provider = vision_provider or self._build_vision_provider(settings)
        self.vision: VisionService | None = None
        if self.vision_provider is not None:
            self.vision = VisionService(
                provider=self.vision_provider,
                resolver=MediaResolver(
                    max_download_bytes=settings.vision_max_download_bytes,
                    timeout_seconds=settings.vision_media_download_timeout_seconds,
                ),
                preprocessor=ImagePreprocessor(
                    max_dimension=settings.vision_max_dimension,
                    max_pixels=settings.vision_max_pixels,
                    max_prepared_bytes=settings.vision_max_prepared_bytes,
                    # The per-turn value is HOT-configurable up to eight frames.
                    # Construct the preprocessor at that reviewed hard ceiling so
                    # raising the runtime value does not remain capped by startup.
                    gif_max_frames=8,
                ),
                analyses=self.media_analyses,
                rate_limiter=VisionRateLimiter(),
                emoji_descriptions=self.emoji_descriptions,
                max_prepared_bytes=settings.vision_max_prepared_bytes,
                global_concurrency=settings.vision_global_concurrency,
                queue_max_pending=settings.vision_queue_max_pending,
                queue_timeout_seconds=settings.vision_queue_timeout_seconds,
                prompt_version=(
                    f"{VISION_PROMPT_VERSION}-{settings.vision_max_dimension:x}-"
                    f"{settings.vision_max_pixels:x}-{settings.vision_max_prepared_bytes:x}"
                ),
            )
        self.concurrency = ConcurrencyManager(settings.global_llm_concurrency)
        self.relationship_evaluator: RelationshipEvaluator
        if isinstance(self.provider, FakeLLMProvider):
            self.relationship_evaluator = FakeRelationshipEvaluator()
        else:
            self.relationship_evaluator = LLMRelationshipEvaluator(
                settings=settings,
                provider=self.provider,
                concurrency=self.concurrency,
                runtime_config=self.runtime_config,
            )
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
            runtime_config=self.runtime_config,
            permission_catalog=self.permission_catalog,
        )
        self.chat = ChatService(
            settings=settings,
            provider=self.provider,
            concurrency=self.concurrency,
            ledger=self.ledger,
            people=self.people,
            memories=self.memories,
            relationships=self.relationships,
            tools=self.agent_tools,
            web_sources=self.web_sources,
            source_policy=SourceDisplayPolicy(),
            source_renderer=SourceRenderer(),
            runtime_config=self.runtime_config,
        )
        self.memory_worker = MemoryWorker(
            settings=settings,
            jobs=self.memory_jobs,
            memories=self.memories,
            provider=self.provider,
            concurrency=self.concurrency,
        )
        self.relationship_worker = RelationshipWorker(
            settings=settings,
            jobs=self.relationship_jobs,
            relationships=self.relationships,
            evaluator=self.relationship_evaluator,
            runtime_config=self.runtime_config,
        )
        self.autonomous_groups = AutonomousGroupService(
            settings=settings,
            provider=self.provider,
            concurrency=self.concurrency,
            memories=self.memories,
            chat=self.chat,
            runtime_config=self.runtime_config,
        )
        self.admin_audit = AdminAuditService(self.database)
        self.relationship_admin = RelationshipAdminService(
            settings=settings,
            relationships=self.relationships,
            audit=self.admin_audit,
            runtime_config=self.runtime_config,
        )
        self.memory_admin = MemoryAdminService(
            settings=settings,
            memories=self.memories,
            audit=self.admin_audit,
        )
        self.preference_admin = PreferenceAdminService(
            settings=settings,
            memories=self.memories,
            audit=self.admin_audit,
        )
        self.group_admin = GroupAdminService(
            settings=settings,
            groups=self.groups,
            runtime_config=self.runtime_config,
            audit=self.admin_audit,
        )
        self.private_access_admin = PrivateAccessAdminService(
            settings=settings,
            private_users=self.private_users,
            audit=self.admin_audit,
            runtime_config=self.runtime_config,
        )
        self.config_admin = ConfigAdminService(self.runtime_config)
        self.admin_actions = AdminActionService(
            settings=settings,
            relationships=self.relationship_admin,
            memories=self.memory_admin,
            preferences=self.preference_admin,
            groups=self.group_admin,
            private_access=self.private_access_admin,
            registry=self.admin_action_registry,
        )
        self.admin_capabilities = AdminCapabilityService(
            settings=settings,
            runtime_config=self.runtime_config,
            actions=self.admin_actions,
            audit=self.admin_audit,
            permission_catalog=self.permission_catalog,
        )
        self.chat.set_admin_tools(self.admin_capabilities)
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
            relationships=self.relationships,
            relationship_worker=self.relationship_worker,
            autonomous_groups=self.autonomous_groups,
            runtime_config=self.runtime_config,
            relationship_admin=self.relationship_admin,
            memory_admin=self.memory_admin,
            preference_admin=self.preference_admin,
            group_admin=self.group_admin,
            private_access_admin=self.private_access_admin,
            config_admin=self.config_admin,
            permission_catalog=self.permission_catalog,
            vision_service=self.vision,
        )
        self._cleanup_stop = asyncio.Event()
        self._cleanup_task: asyncio.Task[None] | None = None

    @classmethod
    async def create(cls, settings: Settings) -> ApplicationContainer:
        """Load restart overrides before constructing long-lived clients and limits."""

        database = Database(settings.database_url)
        runtime_config = RuntimeConfigService(
            settings=settings,
            database=database,
        )
        try:
            await runtime_config.initialize()
            active_settings = settings.model_copy(
                update=await runtime_config.startup_settings_updates()
            )
            return cls(
                active_settings,
                database=database,
                runtime_config=runtime_config,
            )
        except Exception:
            await database.close()
            raise

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

    @staticmethod
    def _build_vision_provider(settings: Settings) -> VisionProvider | None:
        if not settings.vision_enabled:
            return None
        if settings.vision_provider.casefold() == "fake":
            return FakeVisionProvider(model=settings.vision_model)
        if settings.vision_provider.casefold() != "qwen":
            raise ValueError("VISION_PROVIDER must be qwen or fake")
        return QwenVisionProvider(
            base_url=settings.vision_base_url,
            api_key=settings.vision_api_key,
            model=settings.vision_model,
            timeout_seconds=settings.vision_timeout_seconds,
            max_retries=settings.vision_max_retries,
            global_concurrency=settings.vision_global_concurrency,
            max_output_tokens=settings.vision_max_output_tokens,
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
        await self.relationship_worker.start()

    async def _cleanup_loop(self) -> None:
        while not self._cleanup_stop.is_set():
            try:
                deleted = await self.processed_events.cleanup_expired()
                if deleted:
                    logger.info("processed_events_cleaned count=%d", deleted)
                runtime = await self.runtime_config.snapshot()
                web_deleted = await self.web_sources.cleanup_expired(
                    retention_days=runtime.web.source_retention_days
                )
                if web_deleted:
                    logger.info("web_source_runs_cleaned count=%d", web_deleted)
                vision_deleted = await self.media_analyses.cleanup_expired()
                if vision_deleted:
                    logger.info("media_analyses_cleaned count=%d", vision_deleted)
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
        await self.relationship_worker.close()
        await self.memory_worker.close()
        if self.web_provider is not None:
            await self.web_provider.close()
        if self.vision is not None:
            await self.vision.close()
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
