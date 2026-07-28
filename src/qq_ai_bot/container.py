"""Application resource container and lifecycle management."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import cast

from nonebot import get_bots
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from qq_ai_bot import __version__
from qq_ai_bot.admin.action_service import ActionRegistry, AdminActionService
from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.capabilities import AdminCapabilityService
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.admin.permission_catalog import PermissionCatalogService
from qq_ai_bot.automation.executor import AutomationExecutor
from qq_ai_bot.automation.gateway import OneBotProactiveGateway
from qq_ai_bot.automation.handlers import AutomationCapabilityHandlers
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.automation.registry import build_capability_registry
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.automation.tools import AutomationToolService
from qq_ai_bot.automation.worker import AutomationWorker
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.emoji.admin import EmojiAdminService
from qq_ai_bot.emoji.classifier import EmojiClassifier
from qq_ai_bot.emoji.collector import EmojiCollector
from qq_ai_bot.emoji.detector import EmojiCandidateDetector
from qq_ai_bot.emoji.effects import EmojiReplyEffectService
from qq_ai_bot.emoji.grid import EmojiGridBuilder
from qq_ai_bot.emoji.lifecycle import EmojiLifecycleService
from qq_ai_bot.emoji.replacement import EmojiReplacementService
from qq_ai_bot.emoji.repository import EmojiRepository
from qq_ai_bot.emoji.retriever import EmojiRetriever
from qq_ai_bot.emoji.selector import EmojiSelector
from qq_ai_bot.emoji.storage import EmojiStorage
from qq_ai_bot.emoji.worker import EmojiWorker
from qq_ai_bot.llm.base import LLMProvider
from qq_ai_bot.llm.fake import FakeLLMProvider
from qq_ai_bot.llm.openai_compatible import OpenAICompatibleProvider
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import (
    AgentActionRepository,
    ConversationRepository,
    EmojiDescriptionRepository,
    EventLedgerRepository,
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
from qq_ai_bot.planner.context import PlannerContextBuilder
from qq_ai_bot.planner.observability import PlannerObservability
from qq_ai_bot.planner.provider import LLMPlannerProvider
from qq_ai_bot.planner.repository import PlannerRepository
from qq_ai_bot.planner.service import PlannerService
from qq_ai_bot.plugin_host.agent_backend import PluginAgentToolBackend
from qq_ai_bot.plugin_host.audit import PluginAuditService
from qq_ai_bot.plugin_host.automation_adapter import PluginAutomationAdapter
from qq_ai_bot.plugin_host.capability_adapter import PluginCapabilityAdapter
from qq_ai_bot.plugin_host.command_adapter import PluginCommandAdapter
from qq_ai_bot.plugin_host.config import BoundConfigFacade
from qq_ai_bot.plugin_host.discovery import PluginDiscovery
from qq_ai_bot.plugin_host.emoji_adapter import PluginEmojiSelectionSignalAdapter
from qq_ai_bot.plugin_host.event_bus import PluginEventBus
from qq_ai_bot.plugin_host.extension_registry import ExtensionKind, ExtensionRegistry
from qq_ai_bot.plugin_host.facades import (
    HostPluginContext,
    PluginFacadeServices,
    PluginInvocation,
    ToolRuntimeProjection,
)
from qq_ai_bot.plugin_host.http_client import BoundHttpFacade, SafeHttpClient
from qq_ai_bot.plugin_host.loader import PluginLoader
from qq_ai_bot.plugin_host.manager import PluginManager
from qq_ai_bot.plugin_host.manifest import PluginManifest
from qq_ai_bot.plugin_host.planner_adapter import PluginPlannerSignalAdapter
from qq_ai_bot.plugin_host.prompt_adapter import PluginPromptAdapter
from qq_ai_bot.plugin_host.repository import (
    PluginAuditRepository,
    PluginConfigRepository,
    PluginInstallationRepository,
    PluginStateRepository,
)
from qq_ai_bot.plugin_host.secrets import BoundSecretsFacade
from qq_ai_bot.plugin_host.session_facade import BoundAgentSessionFacade
from qq_ai_bot.plugin_host.session_repository import PluginAgentSessionRepository
from qq_ai_bot.plugin_host.storage import BoundStorageFacade
from qq_ai_bot.services.admin.config_admin import ConfigAdminService
from qq_ai_bot.services.admin.group_admin import GroupAdminService
from qq_ai_bot.services.admin.memory_admin import MemoryAdminService
from qq_ai_bot.services.admin.preference_admin import PreferenceAdminService
from qq_ai_bot.services.admin.private_access_admin import PrivateAccessAdminService
from qq_ai_bot.services.admin.relationship_admin import RelationshipAdminService
from qq_ai_bot.services.agent_tools import AgentToolService
from qq_ai_bot.services.autonomous_groups import AutonomousGroupService
from qq_ai_bot.services.chat import ChatService
from qq_ai_bot.services.command_service import CommandService
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.deduplication import DeduplicationService
from qq_ai_bot.services.image_preprocessor import ImagePreprocessor
from qq_ai_bot.services.media_resolver import MediaResolver
from qq_ai_bot.services.memory_worker import MemoryWorker
from qq_ai_bot.services.plugin_events import publish_notification
from qq_ai_bot.services.plugin_sessions import PluginAgentSessionService
from qq_ai_bot.services.processor import MessageProcessor
from qq_ai_bot.services.prompt_composer import PromptComposer
from qq_ai_bot.services.prompt_registry import PromptRegistry
from qq_ai_bot.services.rate_limit import SlidingWindowRateLimiter
from qq_ai_bot.services.relationship_evaluator import (
    FakeRelationshipEvaluator,
    LLMRelationshipEvaluator,
    RelationshipEvaluator,
)
from qq_ai_bot.services.relationship_worker import RelationshipWorker
from qq_ai_bot.services.reply_sequence import ReplySequenceManager
from qq_ai_bot.services.source_policy import SourceDisplayPolicy
from qq_ai_bot.services.source_renderer import SourceRenderer
from qq_ai_bot.services.turn_coordinator import ConversationTurnCoordinator
from qq_ai_bot.services.user_profiles import UserProfileService
from qq_ai_bot.services.vision_rate_limit import VisionRateLimiter
from qq_ai_bot.services.vision_service import VISION_PROMPT_VERSION, VisionService
from qq_ai_bot.time.service import TimeContextService
from qq_ai_bot.vision.base import VisionProvider
from qq_ai_bot.vision.fake import FakeVisionProvider
from qq_ai_bot.vision.qwen import QwenVisionProvider
from qq_ai_bot.web.base import WebSearchProvider
from qq_ai_bot.web.tavily import TavilyWebSearchProvider
from yuki_plugin_sdk.events import EventName
from yuki_plugin_sdk.permissions import PluginPermission

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
        self.processed_events = ProcessedEventRepository(self.database)
        self.ledger = EventLedgerRepository(self.database)
        self.memories = MemoryRepository(self.database)
        self.memory_jobs = MemoryJobRepository(self.database)
        self.agent_actions = AgentActionRepository(self.database)
        self.web_sources = WebSearchSourceRepository(self.database)
        self.media_analyses = MediaAnalysisRepository(self.database)
        self.emoji_descriptions = EmojiDescriptionRepository(self.database)
        self.emoji_repository = EmojiRepository(self.database)
        self.planner_runs = PlannerRepository(self.database)
        self.time_context = TimeContextService(
            self.database,
            default_timezone=settings.default_timezone,
        )
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
        self.media_resolver = MediaResolver(
            max_download_bytes=settings.vision_max_download_bytes,
            timeout_seconds=settings.vision_media_download_timeout_seconds,
            allow_private_urls=settings.vision_allow_private_urls,
        )
        self.image_preprocessor = ImagePreprocessor(
            max_dimension=settings.vision_max_dimension,
            max_pixels=settings.vision_max_pixels,
            max_prepared_bytes=settings.vision_max_prepared_bytes,
            gif_max_frames=8,
        )
        self.vision: VisionService | None = None
        if self.vision_provider is not None:
            self.vision = VisionService(
                provider=self.vision_provider,
                resolver=self.media_resolver,
                preprocessor=self.image_preprocessor,
                analyses=self.media_analyses,
                rate_limiter=VisionRateLimiter(),
                emoji_descriptions=self.emoji_descriptions,
                max_prepared_bytes=settings.vision_max_prepared_bytes,
                global_concurrency=settings.vision_global_concurrency,
                queue_max_pending=settings.vision_queue_max_pending,
                queue_timeout_seconds=settings.vision_queue_timeout_seconds,
                prompt_version=(
                    f"{VISION_PROMPT_VERSION}-{settings.vision_max_dimension:x}-"
                    f"{settings.vision_max_pixels:x}-{settings.vision_max_prepared_bytes:x}-"
                    f"{settings.emoji_analysis_version}"
                ),
                emoji_assets=self.emoji_repository,
                emoji_analysis_version=settings.emoji_analysis_version,
            )
        self.emoji_storage = EmojiStorage(
            settings.emoji_storage_root,
            preview_max_dimension=settings.emoji_preview_max_dimension,
        )
        self.emoji_lifecycle = EmojiLifecycleService(
            self.emoji_repository,
            replacement=EmojiReplacementService(
                self.provider,
                model=settings.llm_model or "fake",
                max_prompt_characters=settings.max_context_characters,
            ),
        )
        self.emoji_collector = EmojiCollector(
            detector=EmojiCandidateDetector(),
            resolver=self.media_resolver,
            storage=self.emoji_storage,
            repository=self.emoji_repository,
        )
        emoji_retriever = EmojiRetriever(self.emoji_repository, self.emoji_storage)
        self.emoji_selector = EmojiSelector(
            retriever=emoji_retriever,
            grid_builder=EmojiGridBuilder(self.emoji_storage),
            preprocessor=self.image_preprocessor,
            provider=self.vision_provider,
        )
        self.emoji_effects = EmojiReplyEffectService(
            selector=self.emoji_selector,
            repository=self.emoji_repository,
            storage=self.emoji_storage,
        )
        self.emoji_worker: EmojiWorker | None = None
        if self.vision_provider is not None:
            self.emoji_worker = EmojiWorker(
                repository=self.emoji_repository,
                classifier=EmojiClassifier(
                    provider=self.vision_provider,
                    preprocessor=self.image_preprocessor,
                    storage=self.emoji_storage,
                    analyses=self.media_analyses,
                ),
                lifecycle=self.emoji_lifecycle,
                storage=self.emoji_storage,
                runtime_config=self.runtime_config,
            )
        self.concurrency = ConcurrencyManager(settings.global_llm_concurrency)
        self.turn_coordinator = ConversationTurnCoordinator(
            cancel_replies_on_new_message=settings.reply_sequence_cancel_on_new_message,
            interrupt_autonomous_on_new_message=(
                settings.planner_interrupt_autonomous_on_new_message
            ),
        )
        self.prompt_registry = PromptRegistry(
            max_fragment_characters=settings.plugin_max_prompt_fragment_characters,
            max_characters_per_plugin=settings.plugin_max_prompt_characters_per_plugin,
            max_total_plugin_characters=settings.plugin_max_total_prompt_characters,
        )
        self.planner_observability = PlannerObservability()
        self.planner_provider = LLMPlannerProvider(
            self.provider,
            model=settings.planner_model,
            temperature=settings.planner_temperature,
            max_output_tokens=settings.planner_max_output_tokens,
            timeout_seconds=settings.planner_timeout_seconds,
            hard_max_messages=settings.reply_plan_hard_max_messages,
            max_wait_seconds=settings.planner_max_wait_seconds,
            observability=self.planner_observability,
            prompt_registry=self.prompt_registry,
        )
        self.planner = PlannerService(
            provider=self.planner_provider,
            observability=self.planner_observability,
            repository=self.planner_runs,
        )
        self.planner_context = PlannerContextBuilder(
            ledger=self.ledger,
            relationships=self.relationships,
        )
        self.reply_sequence = ReplySequenceManager(self.turn_coordinator)
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
        self.plugin_agent_tools = PluginAgentToolBackend(self.agent_tools)
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
            time_service=self.time_context,
            prompt_composer=PromptComposer(settings, self.prompt_registry),
            turn_coordinator=self.turn_coordinator,
            reply_sequence=self.reply_sequence,
            emoji_effects=self.emoji_effects,
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
        self.emoji_admin = EmojiAdminService(
            repository=self.emoji_repository,
            lifecycle=self.emoji_lifecycle,
            storage=self.emoji_storage,
            collector=self.emoji_collector,
            config=self.config_admin,
            worker=self.emoji_worker,
        )
        self.admin_actions = AdminActionService(
            settings=settings,
            relationships=self.relationship_admin,
            memories=self.memory_admin,
            preferences=self.preference_admin,
            groups=self.group_admin,
            private_access=self.private_access_admin,
            emoji=self.emoji_admin,
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
        self.automation_repository = AutomationRepository(self.database)
        self._automation_handlers = AutomationCapabilityHandlers(
            settings=settings,
            provider=self.provider,
            concurrency=self.concurrency,
            runtime_config=self.runtime_config,
            time_service=self.time_context,
            ledger=self.ledger,
            memories=self.memories,
            relationships=self.relationships,
            admin_actions=self.admin_actions,
            web_provider=self.web_provider,
            gateway_factory=lambda context: OneBotProactiveGateway(
                bot_user_id=context.bot_user_id,
                creator_user_id=context.creator_user_id,
                automation_id=context.automation_id,
                automation_run_id=context.automation_run_id,
                ledger=self.ledger,
                actions=self.agent_actions,
            ),
            emoji_repository=self.emoji_repository,
            emoji_selector=self.emoji_selector,
            emoji_storage=self.emoji_storage,
        )
        self.automation_registry = build_capability_registry(self._automation_handlers.mapping())
        self._automation_handlers.bind_registry(self.automation_registry)
        self.automation = AutomationService(
            settings=settings,
            repository=self.automation_repository,
            registry=self.automation_registry,
            time_service=self.time_context,
            audit=self.admin_audit,
        )
        self.automation_tools = AutomationToolService(self.automation)
        self.chat.set_automation_tools(self.automation_tools)
        self.automation_executor = AutomationExecutor(
            settings=settings,
            registry=self.automation_registry,
            repository=self.automation_repository,
            time_service=self.time_context,
        )
        self.automation_worker = AutomationWorker(
            settings=settings,
            repository=self.automation_repository,
            executor=self.automation_executor,
            time_service=self.time_context,
            bot_connected=self.bot_account_connected,
        )
        self.plugin_installations = PluginInstallationRepository(self.database)
        self.plugin_config_values = PluginConfigRepository(self.database)
        self.plugin_state = PluginStateRepository(self.database)
        self.plugin_audit_repository = PluginAuditRepository(self.database)
        self.plugin_audit = PluginAuditService(self.plugin_audit_repository)
        self.plugin_session_repository = PluginAgentSessionRepository(self.database)
        self.plugin_sessions = PluginAgentSessionService(
            provider=self.provider,
            concurrency=self.concurrency,
            runtime_config=self.runtime_config,
            repository=self.plugin_session_repository,
            max_history_messages=settings.plugin_ai_session_max_history_messages,
        )
        self.plugin_http = SafeHttpClient(
            timeout_seconds=settings.plugin_http_timeout_seconds,
            max_response_bytes=settings.plugin_http_max_response_bytes,
        )
        self.plugin_events = PluginEventBus(
            default_timeout_seconds=settings.plugin_hook_timeout_seconds,
        )
        self.emoji_collector.set_event_publisher(self.plugin_events)
        self.emoji_lifecycle.set_event_publisher(self.plugin_events)
        self.emoji_selector.set_event_publisher(self.plugin_events)
        self.emoji_effects.set_event_publisher(self.plugin_events)
        self.plugin_extensions = ExtensionRegistry()
        self.plugin_emoji_signals = PluginEmojiSelectionSignalAdapter(
            self.plugin_extensions,
            timeout_seconds=settings.plugin_hook_timeout_seconds,
        )
        self.emoji_selector.set_plugin_signals(self.plugin_emoji_signals)
        self.plugin_prompts = PluginPromptAdapter(
            self.plugin_extensions,
            self.prompt_registry,
        )
        self.plugin_automation = PluginAutomationAdapter(
            extensions=self.plugin_extensions,
            automation=self.automation_registry,
            invocation_scope=self._plugin_automation_invocation_scope,
        )
        self._plugin_contexts: dict[str, HostPluginContext] = {}
        self.plugin_manager = PluginManager(
            enabled=settings.plugin_system_enabled,
            discovery=PluginDiscovery(
                settings.plugin_directory,
                yuki_version=__version__,
                plugin_api=settings.plugin_api_version,
            ),
            installations=self.plugin_installations,
            loader=PluginLoader(),
            extensions=self.plugin_extensions,
            event_bus=self.plugin_events,
            context_factory=self._create_plugin_context,
            on_activated=self._activate_plugin_extensions,
            on_deactivated=self._deactivate_plugin_extensions,
            audit=self.plugin_audit_repository,
            start_timeout_seconds=settings.plugin_start_timeout_seconds,
            stop_timeout_seconds=settings.plugin_stop_timeout_seconds,
            background_task_limit=settings.plugin_background_task_limit,
            failure_disable_threshold=settings.plugin_failure_disable_threshold,
        )
        self.plugin_tools = PluginCapabilityAdapter(
            registry=self.plugin_extensions,
            installations=self.plugin_installations,
            audit=self.plugin_audit,
            invocation_scope=self._plugin_invocation_scope,
            is_running=lambda plugin_id: plugin_id in self.plugin_manager.running_plugin_ids,
        )
        self.chat.set_plugin_tools(self.plugin_tools)
        self.plugin_commands = PluginCommandAdapter(
            manager=self.plugin_manager,
            registry=self.plugin_extensions,
            superusers=settings.superusers,
            invocation_scope=self._plugin_invocation_scope,
        )
        self.plugin_planner_signals = PluginPlannerSignalAdapter(
            self.plugin_extensions,
            timeout_seconds=settings.plugin_hook_timeout_seconds,
            invocation_scope=self._plugin_signal_scope,
        )
        self.autonomous_groups = AutonomousGroupService(
            settings=settings,
            provider=self.provider,
            concurrency=self.concurrency,
            memories=self.memories,
            chat=self.chat,
            runtime_config=self.runtime_config,
            planner_context=self.planner_context,
            planner=self.planner,
            turn_coordinator=self.turn_coordinator,
            planner_signals=self.plugin_planner_signals,
        )
        self.command_service = CommandService(
            settings=settings,
            conversations=self.conversations,
            people=self.people,
            memories=self.memories,
            concurrency=self.concurrency,
            onebot_connected=self.onebot_connected,
            runtime_config=self.runtime_config,
            relationship_admin=self.relationship_admin,
            memory_admin=self.memory_admin,
            preference_admin=self.preference_admin,
            group_admin=self.group_admin,
            private_access_admin=self.private_access_admin,
            config_admin=self.config_admin,
            permission_catalog=self.permission_catalog,
            vision_service=self.vision,
            automation_service=self.automation,
            automation_repository=self.automation_repository,
            automation_worker=self.automation_worker,
            turn_coordinator=self.turn_coordinator,
            planner_observability=self.planner_observability,
            planner_repository=self.planner_runs,
            plugin_commands=self.plugin_commands,
            emoji_admin=self.emoji_admin,
        )
        self.processor = MessageProcessor(
            settings=settings,
            conversations=self.conversations,
            groups=self.groups,
            private_users=self.private_users,
            user_profiles=self.user_profiles,
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
            automation_service=self.automation,
            automation_repository=self.automation_repository,
            automation_worker=self.automation_worker,
            command_service=self.command_service,
            planner_context=self.planner_context,
            planner_service=self.planner,
            turn_coordinator=self.turn_coordinator,
            planner_signals=self.plugin_planner_signals,
            event_publisher=self.plugin_events,
            emoji_collector=self.emoji_collector,
            emoji_worker=self.emoji_worker,
        )
        self._cleanup_stop = asyncio.Event()
        self._cleanup_task: asyncio.Task[None] | None = None

    def _create_plugin_context(
        self,
        manifest: PluginManifest,
        permissions: frozenset[PluginPermission],
    ) -> HostPluginContext:
        schema_items = self.plugin_extensions.list(
            plugin_id=manifest.id,
            kind=ExtensionKind.CONFIG_SCHEMA,
        )
        config_schema = (
            cast(type[BaseModel], schema_items[0].registration) if schema_items else None
        )

        def config_factory(
            current_user_id: str | None,
            current_group_id: str | None,
        ) -> BoundConfigFacade:
            return BoundConfigFacade(
                repository=self.plugin_config_values,
                plugin_id=manifest.id,
                approved_permissions=permissions,
                schema=config_schema,
                current_user_id=current_user_id,
                current_group_id=current_group_id,
            )

        def session_factory(invocation: PluginInvocation) -> BoundAgentSessionFacade:
            return BoundAgentSessionFacade(
                service=self.plugin_sessions,
                plugin_id=manifest.id,
                actor_user_id=invocation.actor_user_id,
                current_group_id=invocation.current_group_id,
                approved_permissions=permissions,
            )

        agent_capabilities: set[str] = set()
        if PluginPermission.MESSAGE_HISTORY_READ in permissions:
            agent_capabilities.update({"get_recent_chat_history", "search_chat_history"})
        if PluginPermission.MEMORY_PERSON_READ in permissions:
            agent_capabilities.add("get_person_memories")
        if PluginPermission.MEMORY_GROUP_READ in permissions:
            agent_capabilities.add("get_group_memories")
        if PluginPermission.WEB_SEARCH in permissions:
            agent_capabilities.add("web_search")
        if PluginPermission.WEB_READ in permissions:
            agent_capabilities.add("read_webpage")

        context = HostPluginContext(
            plugin_id=manifest.id,
            approved_permissions=permissions,
            superuser_ids=self.settings.superusers,
            scheduler_task_limit=min(
                manifest.limits.background_tasks,
                self.settings.plugin_background_task_limit,
            ),
            services=PluginFacadeServices(
                ledger=self.ledger,
                people=self.people,
                groups=self.groups,
                memories=self.memories,
                relationships=self.relationships,
                memory_admin=self.memory_admin,
                relationship_admin=self.relationship_admin,
                runtime_config=self.runtime_config,
                agent_runner=self.chat._agent_runner,
                agent_tools=self.plugin_agent_tools,
                agent_capabilities=frozenset(agent_capabilities),
                web_provider=self.web_provider,
                vision=self.vision,
                emoji_repository=self.emoji_repository,
                emoji_collector=self.emoji_collector,
                emoji_selector=self.emoji_selector,
                emoji_lifecycle=self.emoji_lifecycle,
                automation=self.automation,
                storage=BoundStorageFacade(
                    repository=self.plugin_state,
                    plugin_id=manifest.id,
                    approved_permissions=permissions,
                    storage_mb=manifest.limits.storage_mb,
                ),
                config_factory=config_factory,
                secrets=BoundSecretsFacade(
                    plugin_id=manifest.id,
                    declared_names=manifest.secrets,
                ),
                http=BoundHttpFacade(
                    client=self.plugin_http,
                    approved_permissions=permissions,
                    allowed_hosts=manifest.network.allowed_hosts,
                    http_concurrency=manifest.limits.http_concurrency,
                ),
                agent_sessions_factory=session_factory,
                events=self.plugin_events,
                audit=self.plugin_audit,
            ),
        )
        self._plugin_contexts[manifest.id] = context
        return context

    def _plugin_invocation_scope(
        self,
        plugin_id: str,
        runtime: ToolRuntimeProjection,
        *,
        web_was_used: bool,
    ) -> object:
        context = self._plugin_contexts.get(plugin_id)
        if context is None:
            raise RuntimeError("plugin is not running")
        return context.invocation_scope(
            plugin_id,
            runtime,
            web_was_used=web_was_used,
        )

    def _plugin_automation_invocation_scope(
        self,
        plugin_id: str,
        invocation: PluginInvocation,
    ) -> AbstractAsyncContextManager[object]:
        context = self._plugin_contexts.get(plugin_id)
        if context is None:
            raise RuntimeError("plugin is not running")
        return context.bind(invocation)

    def _plugin_signal_scope(
        self,
        plugin_id: str,
        message: InboundMessage,
        origin: TurnOrigin,
        runtime: RuntimeConfigSnapshot,
    ) -> AbstractContextManager[object] | AbstractAsyncContextManager[object]:
        context = self._plugin_contexts.get(plugin_id)
        if context is None:
            raise RuntimeError("plugin is not running")
        return context.bind(
            PluginInvocation(
                plugin_id=plugin_id,
                origin=origin,
                actor_user_id=message.sender.user_id,
                bot_user_id=message.bot_user_id or "unknown-bot",
                inbound=message,
                runtime_config=runtime,
            )
        )

    def _activate_plugin_extensions(self, manifest: PluginManifest) -> None:
        self.plugin_prompts.activate(
            manifest.id,
            max_characters=manifest.limits.prompt_characters,
        )
        self.plugin_automation.activate(manifest)

    def _deactivate_plugin_extensions(self, plugin_id: str) -> None:
        self.plugin_prompts.deactivate(plugin_id)
        self.plugin_automation.deactivate(plugin_id)
        self._plugin_contexts.pop(plugin_id, None)

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

    def bot_account_connected(self, bot_user_id: str) -> bool:
        """Return whether the exact bot account delegated by a task is connected."""

        return any(str(getattr(bot, "self_id", "")) == bot_user_id for bot in get_bots().values())

    async def start(self) -> None:
        """Start maintenance tasks after migrations have run."""

        await self.plugin_session_repository.delete_ephemeral()
        await self.plugin_manager.start()
        await publish_notification(
            self.plugin_events,
            EventName.APPLICATION_STARTED,
            {"version": __version__},
        )
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(), name="processed-event-cleanup"
        )
        await self.memory_worker.start()
        await self.relationship_worker.start()
        if self.emoji_worker is not None:
            await self.emoji_worker.start()
        await self.automation_worker.start()

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
                emoji_deleted = await self.emoji_admin.cleanup_expired()
                if emoji_deleted:
                    logger.info("emoji_assets_cleaned count=%d", emoji_deleted)
                automation_runs_deleted = await self.automation_repository.cleanup_runs(
                    before=datetime.now(UTC)
                    - timedelta(days=self.settings.automation_run_retention_days)
                )
                if automation_runs_deleted:
                    logger.info("automation_runs_cleaned count=%d", automation_runs_deleted)
                plugin_state_deleted = await self.plugin_state.cleanup_expired()
                if plugin_state_deleted:
                    logger.info("plugin_state_cleaned count=%d", plugin_state_deleted)
                plugin_sessions_expired = await self.plugin_session_repository.expire_due()
                if plugin_sessions_expired:
                    logger.info(
                        "plugin_sessions_expired count=%d",
                        plugin_sessions_expired,
                    )
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
        await publish_notification(
            self.plugin_events,
            EventName.APPLICATION_STOPPING,
            {"version": __version__},
        )
        await self.plugin_manager.stop()
        await self.automation_worker.close()
        await self.emoji_collector.close()
        if self.emoji_worker is not None:
            await self.emoji_worker.close()
        await self.relationship_worker.close()
        await self.memory_worker.close()
        if self.web_provider is not None:
            await self.web_provider.close()
        if self.vision is not None:
            await self.vision.close()
        else:
            await self.media_resolver.close()
        await self.plugin_http.close()
        await self.plugin_session_repository.delete_ephemeral()
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
