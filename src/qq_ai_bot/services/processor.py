"""Person-centric message pipeline and deterministic `/ai` commands."""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from sqlalchemy.exc import SQLAlchemyError

from qq_ai_bot import __version__
from qq_ai_bot.admin.action_service import ActionRegistry
from qq_ai_bot.admin.audit import AdminAuditService
from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import (
    AdminActor,
    ConfigApplyMode,
    ConfigChangeResult,
    EffectiveConfigValue,
)
from qq_ai_bot.admin.permission_catalog import PermissionCatalogService
from qq_ai_bot.automation.repository import AutomationRepository
from qq_ai_bot.automation.service import AutomationService
from qq_ai_bot.automation.worker import AutomationWorker
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.conversations import ConversationIdentity, ConversationMode, ScopeType
from qq_ai_bot.domain.messages import InboundMessage, OutboundMessage
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.llm.base import LLMConfigurationError, LLMEmptyResponseError, LLMError
from qq_ai_bot.persistence.repositories import (
    ConversationRepository,
    EventLedgerRepository,
    GroupSettingsRepository,
    MemoryJobRepository,
    MemoryRepository,
    PeopleRepository,
    PrivateUserSettingsRepository,
    RelationshipJobRepository,
    RelationshipRepository,
)
from qq_ai_bot.services.admin.config_admin import ConfigAdminService
from qq_ai_bot.services.admin.group_admin import GroupAdminService
from qq_ai_bot.services.admin.memory_admin import MemoryAdminService
from qq_ai_bot.services.admin.preference_admin import PreferenceAdminService
from qq_ai_bot.services.admin.private_access_admin import PrivateAccessAdminService
from qq_ai_bot.services.admin.relationship_admin import RelationshipAdminService
from qq_ai_bot.services.autonomous_groups import AutonomousGroupService
from qq_ai_bot.services.chat import ChatService, OutboundSender
from qq_ai_bot.services.concurrency import ConcurrencyManager, RequestCancelledError
from qq_ai_bot.services.deduplication import DeduplicationService, build_event_key
from qq_ai_bot.services.group_members import GroupMemberResolver, GroupMemberService
from qq_ai_bot.services.media_resolver import OneBotMediaGateway
from qq_ai_bot.services.memory_worker import MemoryWorker
from qq_ai_bot.services.policies import (
    CommandName,
    EffectiveGroupPolicy,
    EffectivePrivatePolicy,
    command_requires_superuser,
    evaluate_message,
)
from qq_ai_bot.services.rate_limit import SlidingWindowRateLimiter
from qq_ai_bot.services.relationship_evaluator import LLMRelationshipEvaluator
from qq_ai_bot.services.relationship_worker import RelationshipWorker
from qq_ai_bot.services.renderer import sanitize_input
from qq_ai_bot.services.user_profiles import (
    UserProfileResolver,
    UserProfileService,
    sanitize_profile_name,
)
from qq_ai_bot.services.vision_service import (
    VisionProcessingError,
    VisionService,
    compact_visual_summary,
)
from qq_ai_bot.time.formatting import local_text
from qq_ai_bot.vision.models import VisualObservation

logger = logging.getLogger(__name__)

UNSUPPORTED_MESSAGE = "当前版本会保存媒体消息元数据，但尚未实现图片、语音或视频理解。"
RATE_LIMIT_MESSAGE = "请求过于频繁，请稍后再试。"
IMAGE_WRITE_ISOLATION_MESSAGE = "图片或回复图片所在的轮次不会执行写入操作，请改用纯文本消息。"
IMAGE_FAILURE_MESSAGE = "这张图片暂时没有识别成功，可以重新发送一张更清晰的版本。"
IMAGE_RATE_LIMIT_MESSAGE = "图片理解请求过于频繁，请稍后再试。"
IMAGE_QUEUE_BUSY_MESSAGE = "当前图片识别任务较多，请稍后再试。"
IMAGE_DOWNLOAD_TIMEOUT_MESSAGE = "图片下载超时，请稍后重试；如果仍然失败，请重新发送原图。"
IMAGE_DOWNLOAD_FAILED_MESSAGE = "图片资源下载失败或已经失效，请重新发送原图。"
IMAGE_RESOURCE_QUERY_FAILED_MESSAGE = "NapCat 未能取得图片资源，请重新发送原图。"
IMAGE_FORMAT_FAILED_MESSAGE = "图片文件无法解析，请尝试重新保存或转换为 PNG、JPEG 后发送。"
IMAGE_TOO_LARGE_MESSAGE = "图片尺寸、帧数或文件大小超过处理范围，请压缩后重新发送。"
IMAGE_PROVIDER_TIMEOUT_MESSAGE = "图片已取得，但视觉模型响应超时，请稍后再试。"
IMAGE_PROVIDER_FAILED_MESSAGE = "图片已取得，但视觉模型暂时不可用，请稍后再试。"
REPLY_IMAGE_UNAVAILABLE_MESSAGE = "回复中的图片资源已过期或无法读取，请重新发送原图。"
_NUMERIC_PLATFORM_ID = re.compile(r"[1-9][0-9]{4,19}")


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Observable result used by adapters and integration tests."""

    handled: bool
    sent_messages: int = 0
    reason: str = ""


def _vision_failure_message(error_code: str | None, *, reply_only: bool) -> str:
    """Return a useful pure-image failure without exposing provider internals."""

    if error_code == "rate_limited":
        return IMAGE_RATE_LIMIT_MESSAGE
    if error_code in {"queue_full", "queue_timeout"}:
        return IMAGE_QUEUE_BUSY_MESSAGE
    if error_code == "media_download_timeout":
        return IMAGE_DOWNLOAD_TIMEOUT_MESSAGE
    if error_code == "get_image_failed":
        return IMAGE_RESOURCE_QUERY_FAILED_MESSAGE
    if error_code in {
        "download_failed",
        "dns_failed",
        "private_url",
        "redirect_rejected",
        "empty_media",
    }:
        return REPLY_IMAGE_UNAVAILABLE_MESSAGE if reply_only else IMAGE_DOWNLOAD_FAILED_MESSAGE
    if error_code == "resource_unavailable" and reply_only:
        return REPLY_IMAGE_UNAVAILABLE_MESSAGE
    if error_code in {
        "too_large",
        "prepared_too_large",
        "decompression_bomb",
        "extreme_aspect_ratio",
        "too_many_frames",
    }:
        return IMAGE_TOO_LARGE_MESSAGE
    if error_code in {
        "invalid_base64",
        "invalid_media_type",
        "invalid_media",
        "invalid_dimensions",
        "unsupported_format",
        "corrupt_image",
        "frame_decode_failed",
    }:
        return IMAGE_FORMAT_FAILED_MESSAGE
    if error_code == "timeout":
        return IMAGE_PROVIDER_TIMEOUT_MESSAGE
    if error_code in {
        "connection_failed",
        "provider_unavailable",
        "authentication_failed",
        "provider_rejected",
        "invalid_response",
        "empty_response",
    }:
        return IMAGE_PROVIDER_FAILED_MESSAGE
    return IMAGE_FAILURE_MESSAGE


class MessageProcessor:
    """Admission → dedup → identity → ledger → memory → reply → relationship job."""

    def __init__(
        self,
        *,
        settings: Settings,
        conversations: ConversationRepository,
        groups: GroupSettingsRepository,
        private_users: PrivateUserSettingsRepository,
        user_profiles: UserProfileService,
        group_members: GroupMemberService,
        chat: ChatService,
        deduplication: DeduplicationService,
        rate_limiter: SlidingWindowRateLimiter,
        concurrency: ConcurrencyManager,
        onebot_connected: Callable[[], bool],
        ledger: EventLedgerRepository | None = None,
        people: PeopleRepository | None = None,
        memories: MemoryRepository | None = None,
        memory_worker: MemoryWorker | None = None,
        relationships: RelationshipRepository | None = None,
        relationship_worker: RelationshipWorker | None = None,
        autonomous_groups: AutonomousGroupService | None = None,
        runtime_config: RuntimeConfigService | None = None,
        relationship_admin: RelationshipAdminService | None = None,
        memory_admin: MemoryAdminService | None = None,
        preference_admin: PreferenceAdminService | None = None,
        group_admin: GroupAdminService | None = None,
        private_access_admin: PrivateAccessAdminService | None = None,
        config_admin: ConfigAdminService | None = None,
        permission_catalog: PermissionCatalogService | None = None,
        vision_service: VisionService | None = None,
        automation_service: AutomationService | None = None,
        automation_repository: AutomationRepository | None = None,
        automation_worker: AutomationWorker | None = None,
    ) -> None:
        database = conversations._database
        self._settings = settings
        self._conversations = conversations
        self._groups = groups
        self._private_users = private_users
        self._user_profiles = user_profiles
        self._group_members = group_members
        self._chat = chat
        self._deduplication = deduplication
        self._rate_limiter = rate_limiter
        self._concurrency = concurrency
        self._onebot_connected = onebot_connected
        self._ledger = ledger or EventLedgerRepository(database)
        self._people = people or PeopleRepository(database)
        self._memories = memories or MemoryRepository(database)
        self._memory_worker = memory_worker or MemoryWorker(
            settings=settings,
            jobs=MemoryJobRepository(database),
            memories=self._memories,
            provider=chat._provider,
            concurrency=concurrency,
        )
        self._relationships = relationships or RelationshipRepository(
            database,
            initial_affection=settings.relationship_initial_affection,
            initial_trust=settings.relationship_initial_trust,
            trust_cap_offset=settings.trust_affection_cap_offset,
            max_affection_auto_delta=settings.affection_max_auto_delta,
            max_trust_auto_delta=settings.trust_max_auto_delta,
        )
        self._relationship_worker = relationship_worker or RelationshipWorker(
            settings=settings,
            jobs=RelationshipJobRepository(
                database,
                max_attempts=settings.relationship_max_attempts,
            ),
            relationships=self._relationships,
            evaluator=LLMRelationshipEvaluator(
                settings=settings,
                provider=chat._provider,
                concurrency=concurrency,
            ),
        )
        self._autonomous = autonomous_groups
        self._runtime_config = runtime_config or RuntimeConfigService(
            settings=settings,
            database=database,
        )
        audit = AdminAuditService(database)
        self._relationship_admin = relationship_admin or RelationshipAdminService(
            settings=settings,
            relationships=self._relationships,
            audit=audit,
            runtime_config=self._runtime_config,
        )
        self._memory_admin = memory_admin or MemoryAdminService(
            settings=settings,
            memories=self._memories,
            audit=audit,
        )
        self._preference_admin = preference_admin or PreferenceAdminService(
            settings=settings,
            memories=self._memories,
            audit=audit,
        )
        self._group_admin = group_admin or GroupAdminService(
            settings=settings,
            groups=self._groups,
            runtime_config=self._runtime_config,
            audit=audit,
        )
        self._private_access_admin = private_access_admin or PrivateAccessAdminService(
            settings=settings,
            private_users=self._private_users,
            audit=audit,
            runtime_config=self._runtime_config,
        )
        self._config_admin = config_admin or ConfigAdminService(self._runtime_config)
        self._permission_catalog = permission_catalog or PermissionCatalogService(
            settings=settings,
            config_registry=self._runtime_config.registry,
            action_registry=ActionRegistry(),
        )
        self._vision = vision_service
        self._automation = automation_service
        self._automation_repository = automation_repository
        self._automation_worker = automation_worker

    async def handle(
        self,
        message: InboundMessage,
        sender: OutboundSender,
        profile_resolver: UserProfileResolver | None = None,
        group_member_resolver: GroupMemberResolver | None = None,
    ) -> ProcessResult:
        """Process one message without deriving authority from model-visible data."""

        started = time.perf_counter()
        group_policy = await self._effective_group_policy(message.group_id)
        private_policy = await self._effective_private_policy(message)
        decision = evaluate_message(
            message,
            self._settings,
            group_policy=group_policy,
            private_policy=private_policy,
        )
        if decision.reason == "bot_message":
            return ProcessResult(False, reason=decision.reason)
        is_superuser = message.sender.user_id in self._settings.superusers
        admin_candidate = bool(
            is_superuser
            and decision.command is None
            and (
                decision.should_respond
                or (
                    decision.reason == "group_disabled"
                    and (
                        message.mentions_bot
                        or message.text.strip().startswith(self._settings.ai_prefix)
                    )
                )
            )
        )
        should_observe = (
            message.scope_type is ScopeType.PRIVATE and decision.should_respond
        ) or bool(self._settings.observe_enabled_groups and group_policy and group_policy.enabled)
        if not decision.should_respond and not should_observe and not admin_candidate:
            return ProcessResult(False, reason=decision.reason)

        identity = (
            ConversationIdentity.private(message.sender.user_id)
            if message.scope_type is ScopeType.PRIVATE
            else ConversationIdentity.group(
                message.group_id or "",
                message.sender.user_id,
                ConversationMode.SHARED,
            )
        )
        event_key = build_event_key(message, identity.key)
        if not await self._deduplication.claim(event_key):
            return ProcessResult(False, reason="duplicate")

        runtime_snapshot = await self._runtime_config.snapshot(
            user_id=message.sender.user_id,
            group_id=message.group_id,
        )
        has_visual_input = VisionService.has_visual_input(message)
        image_blocks_command = bool(
            has_visual_input
            and decision.command is not None
            and self._command_may_write(decision.command, decision.content)
        )

        # forgetme is deliberately neither re-observed nor re-written to the ledger.
        if decision.command is CommandName.FORGETME and not image_blocks_command:
            profile = self._event_profile(message)
            return await self._handle_command(
                decision.command,
                message,
                identity,
                profile,
                decision.content,
                sender,
                event_key,
                started,
            )

        await self._observe_group_metadata(
            message,
            group_policy,
            profile_resolver,
        )
        profile = await self._user_profiles.capture(message, profile_resolver)
        record, created = await self._ledger.append_inbound(
            message, bot_user_id=message.bot_user_id or "unknown-bot"
        )
        if created:
            await self._memory_worker.enqueue(record.id)

        if not decision.should_respond and not admin_candidate:
            if (
                message.scope_type is ScopeType.GROUP
                and group_policy is not None
                and group_policy.autonomous_enabled
            ):
                if self._autonomous is not None:
                    self._autonomous.observe(message, profile, sender)
            return ProcessResult(False, reason="group_observed")

        category = "command" if decision.command is not None else "chat"
        rate = await self._rate_limiter.check(
            user_id=message.sender.user_id,
            group_id=message.group_id,
            category=category,
        )
        if not rate.allowed:
            sent = await self._send_text(message, sender, RATE_LIMIT_MESSAGE)
            return ProcessResult(True, int(sent), f"{rate.scope}_rate_limited")

        if decision.command is not None:
            if image_blocks_command:
                sent = await self._send_text(
                    message,
                    sender,
                    IMAGE_WRITE_ISOLATION_MESSAGE,
                )
                return ProcessResult(True, int(sent), "image_write_isolated")
            return await self._handle_command(
                decision.command,
                message,
                identity,
                profile,
                decision.content,
                sender,
                event_key,
                started,
            )

        visual_question = sanitize_input(decision.content or message.text)
        content = sanitize_input(decision.content or (message.text if admin_candidate else ""))
        if message.reply_text:
            quoted = sanitize_input(message.reply_text)
            if quoted:
                content = f"[回复的消息]\n{quoted}\n\n{content}".strip()
        visual_observation: VisualObservation | None = None
        visual_failure = False
        visual_error_code: str | None = None
        if has_visual_input:
            if self._vision is None or not self._settings.vision_enabled:
                visual_failure = True
                visual_error_code = "not_configured"
            else:
                source_event_id = record.id
                if (
                    not any(attachment.kind.value == "image" for attachment in message.attachments)
                    and message.reply_to_message_id
                ):
                    replied_event = await self._ledger.find_by_platform_message(
                        bot_user_id=message.bot_user_id or "unknown-bot",
                        platform_message_id=message.reply_to_message_id,
                    )
                    if replied_event is not None:
                        source_event_id = replied_event.id
                gateway = (
                    cast(OneBotMediaGateway, sender)
                    if callable(getattr(sender, "call_api", None))
                    else None
                )
                try:
                    visual_observation = await self._vision.analyze(
                        message,
                        question=visual_question,
                        runtime=runtime_snapshot.vision,
                        gateway=gateway,
                        source_event_id=source_event_id,
                        conversation_key=identity.key,
                    )
                    if source_event_id is not None:
                        await self._ledger.set_visual_summary(
                            source_event_id,
                            compact_visual_summary(visual_observation),
                        )
                except VisionProcessingError as exc:
                    visual_failure = True
                    visual_error_code = exc.code
                    logger.warning(
                        "vision_turn_failed event_key=%s error_category=%s",
                        event_key,
                        exc.code,
                    )
                except Exception as exc:
                    # Vision is an optional front-end. Repository, decoder, or
                    # provider defects must degrade this turn instead of escaping
                    # the NoneBot event handler. Never log exception text because
                    # third-party clients may embed signed media URLs in it.
                    visual_failure = True
                    visual_error_code = "internal_error"
                    logger.error(
                        "vision_turn_failed event_key=%s error_category=unexpected_%s",
                        event_key,
                        type(exc).__name__,
                    )
        if not content:
            if has_visual_input and visual_observation is not None:
                content = (
                    "[当前消息仅包含图片；后端视觉识别已成功，请根据本轮视觉观察直接回应图片内容]"
                )
            elif has_visual_input:
                text = _vision_failure_message(
                    visual_error_code,
                    reply_only=bool(message.reply_attachments and not message.attachments),
                )
                sent = await self._send_text(message, sender, text)
                return ProcessResult(True, int(sent), f"vision_{visual_error_code or 'failed'}")
            else:
                text = UNSUPPORTED_MESSAGE if message.attachments else "请输入要发送给 AI 的内容。"
                sent = await self._send_text(message, sender, text)
                return ProcessResult(
                    True,
                    int(sent),
                    "unsupported" if message.attachments else "empty",
                )
        if len(content) > self._settings.max_input_characters:
            sent = await self._send_text(
                message,
                sender,
                f"消息过长，请控制在 {self._settings.max_input_characters} 个字符以内。",
            )
            return ProcessResult(True, int(sent), "input_too_long")

        mentioned_members = await self._group_members.resolve(message, group_member_resolver)
        try:
            sent_count = await self._chat.respond(
                message,
                identity,
                profile,
                mentioned_members,
                content,
                sender,
                runtime_snapshot=runtime_snapshot,
                visual_observation=visual_observation,
                visual_input_present=has_visual_input,
                visual_failure=visual_failure,
            )
        except RequestCancelledError:
            return ProcessResult(True, reason="cancelled")
        except LLMConfigurationError:
            sent = await self._send_text(message, sender, "AI 服务尚未配置，请联系管理员。")
            return ProcessResult(True, int(sent), "llm_not_configured")
        except LLMEmptyResponseError:
            sent = await self._send_text(message, sender, "AI 返回了空内容，请稍后重试。")
            return ProcessResult(True, int(sent), "empty_llm_response")
        except LLMError as exc:
            logger.warning("llm_failure exception_category=%s", type(exc).__name__)
            sent = await self._send_text(message, sender, "AI 服务暂时不可用，请稍后重试。")
            return ProcessResult(True, int(sent), "llm_failure")
        except (OSError, RuntimeError) as exc:
            logger.error("message_send_or_storage_failure", exc_info=exc)
            return ProcessResult(True, reason="send_or_storage_failure")

        if created and sent_count > 0:
            try:
                await self._relationship_worker.enqueue(
                    trigger_event_id=record.id,
                    user_id=message.sender.user_id,
                    conversation_key=identity.key,
                )
            except (SQLAlchemyError, OSError, RuntimeError, ValueError) as exc:
                logger.warning(
                    "relationship_enqueue_failed exception_category=%s",
                    type(exc).__name__,
                )

        self._log_result(
            event_key,
            identity,
            message,
            handler="chat",
            started=started,
            success=True,
        )
        return ProcessResult(True, sent_count, "chat")

    async def _observe_group_metadata(
        self,
        message: InboundMessage,
        policy: EffectiveGroupPolicy | None,
        resolver: UserProfileResolver | None,
    ) -> None:
        if message.group_id is None or policy is None or not policy.enabled:
            return
        existing = await self._groups.get(message.group_id)
        group_name = ""
        method = getattr(resolver, "resolve_group_name", None)
        if (existing is None or not existing.name) and callable(method):
            resolve_name = cast(Callable[[str], Awaitable[str]], method)
            try:
                group_name = sanitize_profile_name(await resolve_name(message.group_id))
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                logger.warning(
                    "group_name_resolve_failed exception_category=%s",
                    type(exc).__name__,
                )
        await self._groups.observe(
            message.group_id,
            name=group_name,
            enabled_if_new=policy.enabled,
        )

    async def _effective_group_policy(self, group_id: str | None) -> EffectiveGroupPolicy | None:
        if group_id is None:
            return None
        setting = await self._groups.get(group_id)
        if setting is None:
            return EffectiveGroupPolicy(enabled=group_id in self._settings.enabled_groups)
        return EffectiveGroupPolicy(
            enabled=setting.enabled,
            require_mention=setting.require_mention,
            conversation_mode=ConversationMode.SHARED,
            autonomous_enabled=setting.autonomous_enabled,
        )

    async def _effective_private_policy(
        self, message: InboundMessage
    ) -> EffectivePrivatePolicy | None:
        if message.scope_type is not ScopeType.PRIVATE:
            return None
        setting = await self._private_users.get(message.sender.user_id)
        return EffectivePrivatePolicy(enabled=True if setting is None else setting.enabled)

    @staticmethod
    def _command_may_write(command: CommandName, argument: str) -> bool:
        """Conservatively close deterministic writes on every image-bearing turn."""

        if command in {
            CommandName.ON,
            CommandName.OFF,
            CommandName.GROUP,
            CommandName.PRIVATE,
            CommandName.FORGETME,
        }:
            return True
        operation = argument.split(maxsplit=1)[0].casefold() if argument.strip() else ""
        if command is CommandName.MEMORY:
            return operation not in {"", "list"}
        if command is CommandName.PREFERENCE:
            return operation not in {"", "list"}
        if command is CommandName.AFFECTION:
            return operation not in {"", "show", "history"}
        if command is CommandName.CONFIG:
            return operation not in {"", "list", "get", "history"}
        if command is CommandName.AUTOMATION:
            return operation not in {"", "list", "show", "history"}
        return False

    async def _handle_command(
        self,
        command: CommandName,
        message: InboundMessage,
        identity: ConversationIdentity,
        profile: UserProfileSnapshot,
        argument: str,
        sender: OutboundSender,
        event_key: str,
        started: float,
    ) -> ProcessResult:
        is_superuser = message.sender.user_id in self._settings.superusers
        actor = AdminActor(
            user_id=message.sender.user_id,
            is_superuser=is_superuser,
            trigger_message_id=message.message_id,
            conversation_key=identity.key,
            current_group_id=message.group_id,
            mentioned_user_ids=message.mentioned_user_ids,
            current_message_text=message.text,
        )
        record_reply = command is not CommandName.FORGETME
        reset_after_reply = False
        if command_requires_superuser(command) and not is_superuser:
            text = "权限不足：该命令仅限超级管理员。"
        elif command is CommandName.HELP:
            text = self._help_text()
        elif command is CommandName.NEW:
            text = "已开始新的当前场景上下文；永久聊天账本和人物记忆仍然保留。"
            reset_after_reply = True
        elif command is CommandName.STATUS:
            count = await self._conversations.count_messages(identity)
            pending_restart = await self._runtime_config.pending_restart_count()
            vision_busy = self._vision is not None and self._vision.busy
            vision_queue_depth = self._vision.queue_depth if self._vision is not None else 0
            vision_running = self._vision.running_count if self._vision is not None else 0
            automation_count = (
                await self._automation_repository.active_count()
                if self._automation_repository is not None
                else 0
            )
            automation_last_run = (
                await self._automation_repository.latest_run_at()
                if self._automation_repository is not None
                else None
            )
            automation_next_run = (
                await self._automation_repository.next_due_at()
                if self._automation_repository is not None
                else None
            )
            automation_worker_status = (
                "运行中"
                if self._automation_worker is not None and self._automation_worker.running
                else "未运行"
            )
            automation_last_text = automation_last_run.isoformat() if automation_last_run else "无"
            automation_next_text = automation_next_run.isoformat() if automation_next_run else "无"
            text = (
                f"OneBot 连接：{'已连接' if self._onebot_connected() else '未连接'}\n"
                f"模型：{self._settings.llm_model or '未配置'}\n"
                f"视觉功能：{'已启用' if self._settings.vision_enabled else '未启用'}\n"
                f"视觉模型：{self._settings.vision_model or '未配置'}\n"
                f"视觉请求繁忙：{'是' if vision_busy else '否'}\n"
                f"视觉排队/运行：{vision_queue_depth}/{vision_running}\n"
                f"当前切点后的事件数：{count}\n"
                f"请求处理中：{'是' if self._concurrency.is_processing(identity.key) else '否'}\n"
                f"待重启配置数：{pending_restart}\n"
                f"自动化：{'已启用' if self._settings.automation_enabled else '未启用'}\n"
                f"自动化 Worker：{automation_worker_status}\n"
                f"活跃自动化任务：{automation_count}\n"
                f"最近自动化执行：{automation_last_text}\n"
                f"最近待执行时间：{automation_next_text}\n"
                f"服务版本：{__version__}"
            )
        elif command is CommandName.STOP:
            cancelled = await self._concurrency.cancel(identity.key)
            text = "已取消当前 AI 请求。" if cancelled else "当前没有正在处理的 AI 请求。"
        elif command in {CommandName.ON, CommandName.OFF}:
            if message.scope_type is not ScopeType.GROUP or message.group_id is None:
                text = "该命令只能在群聊中使用。"
            else:
                enabled = command is CommandName.ON
                if enabled:
                    await self._group_admin.enable_current_group(actor, message.group_id)
                else:
                    await self._group_admin.disable_current_group(actor, message.group_id)
                text = "已启用当前群。" if enabled else "已停用当前群。"
        elif command in {CommandName.PRIVATE, CommandName.GROUP}:
            parsed = self._parse_access_switch(argument)
            if parsed is None:
                noun = "QQ号" if command is CommandName.PRIVATE else "群号"
                text = f"格式错误，请使用 /ai {command.value} <{noun}> on|off。"
            else:
                target_id, enabled = parsed
                try:
                    if command is CommandName.PRIVATE:
                        if enabled:
                            await self._private_access_admin.enable_user(actor, target_id)
                        else:
                            await self._private_access_admin.disable_user(actor, target_id)
                        text = (
                            "已开启指定 QQ 用户的私聊权限。"
                            if enabled
                            else "已关闭指定 QQ 用户的私聊权限。"
                        )
                    else:
                        if enabled:
                            await self._group_admin.enable_current_group(actor, target_id)
                        else:
                            await self._group_admin.disable_current_group(actor, target_id)
                        text = f"已{'启用' if enabled else '停用'}群 {target_id}。"
                except ValueError as exc:
                    text = str(exc)
        elif command is CommandName.PING:
            text = f"pong ({(time.perf_counter() - started) * 1000:.1f} ms)"
        elif command is CommandName.WHOAMI:
            text = await self._whoami(message, profile, argument)
        elif command is CommandName.FORGETME:
            if argument:
                text = "该命令不接受参数，只能删除发送者本人数据。"
            else:
                deleted = await self._people.delete_person(message.sender.user_id)
                text = (
                    "已彻底删除与你 QQ 号关联的人物、关系分数、记忆、成员关系和可归属聊天事件。"
                    if deleted
                    else "没有找到与你 QQ 号关联的数据。"
                )
        elif command is CommandName.MEMORY:
            text = await self._memory_command(
                actor=actor,
                argument=argument,
            )
        elif command is CommandName.PREFERENCE:
            text = await self._preference_command(
                actor=actor,
                argument=argument,
            )
        elif command is CommandName.AFFECTION:
            text = await self._affection_command(
                actor=actor,
                argument=argument,
            )
        elif command is CommandName.CAPABILITIES:
            text = self._capabilities_command(message, argument)
        elif command is CommandName.CONFIG:
            text = await self._config_command(actor, argument)
        elif command is CommandName.AUTOMATION:
            text = await self._automation_command(
                message=message,
                identity=identity,
                argument=argument,
            )
        else:
            text = "未知命令，请使用 /ai help 查看帮助。"

        sent = await self._send_text(message, sender, text, record=record_reply)
        if reset_after_reply and sent:
            await self._conversations.clear(identity)
        self._log_result(
            event_key,
            identity,
            message,
            handler=f"command_{command.value}",
            started=started,
            success=sent,
        )
        return ProcessResult(True, int(sent), f"command_{command.value}")

    async def _memory_command(self, *, actor: AdminActor, argument: str) -> str:
        parsed = self._parse_scoped_operation(
            argument,
            actor.user_id,
            actor.is_superuser,
        )
        if isinstance(parsed, str):
            return parsed
        operation, target, rest = parsed
        if operation == "list":
            rows = await self._memory_admin.list_memories(actor, target)
            if not rows:
                return f"QQ {target} 暂无人物记忆。"
            return "\n".join(f"{row.id}. [{row.source_type}] {row.content}" for row in rows)
        if operation == "add":
            content = " ".join(rest).strip()
            if not content:
                return "格式：/ai memory add <内容>"
            try:
                row = await self._memory_admin.add_memory(actor, target, content)
            except ValueError as exc:
                return str(exc)
            return f"已添加人物记忆 {row.id}。"
        if operation == "update":
            if len(rest) < 2 or not rest[0].isdigit():
                return "格式：/ai memory update <记忆ID> <内容>"
            updated = await self._memory_admin.update_memory(
                actor,
                target,
                int(rest[0]),
                " ".join(rest[1:]),
            )
            return "记忆已更新。" if updated else "没有找到可修改的记忆。"
        if operation == "delete":
            if len(rest) != 1 or not rest[0].isdigit():
                return "格式：/ai memory delete <记忆ID>"
            deleted = await self._memory_admin.delete_memory(
                actor,
                target,
                int(rest[0]),
            )
            return "记忆已删除。" if deleted else "没有找到该记忆。"
        return "可用操作：list、add、update、delete。"

    async def _preference_command(self, *, actor: AdminActor, argument: str) -> str:
        parsed = self._parse_scoped_operation(
            argument,
            actor.user_id,
            actor.is_superuser,
        )
        if isinstance(parsed, str):
            return parsed
        operation, target, rest = parsed
        if operation == "list":
            rows = await self._preference_admin.list_preferences(actor, target)
            if not rows:
                return f"QQ {target} 暂无交互偏好。"
            return "\n".join(f"{row.key} = {row.value}" for row in rows)
        if operation == "set":
            if len(rest) < 2:
                return "格式：/ai preference set <键> <值>"
            await self._preference_admin.set_preference(
                actor,
                target,
                rest[0],
                " ".join(rest[1:]),
            )
            return f"偏好 {rest[0]} 已设置。"
        if operation == "delete":
            if len(rest) != 1:
                return "格式：/ai preference delete <键>"
            deleted = await self._preference_admin.delete_preference(
                actor,
                target,
                rest[0],
            )
            return "偏好已删除。" if deleted else "没有找到该偏好。"
        return "可用操作：list、set、delete。"

    async def _affection_command(
        self,
        *,
        actor: AdminActor,
        argument: str,
    ) -> str:
        parts = argument.split()
        if not parts:
            return "格式：/ai affection show|history"
        operation = parts.pop(0).casefold()
        if operation in {"show", "history"}:
            target = actor.user_id
            if parts:
                if len(parts) != 2 or parts[0].casefold() != "user":
                    return f"格式：/ai affection {operation} [user <QQ号>]"
                if not actor.is_superuser:
                    return "只有超级管理员可以查看其他 QQ 人物的关系数据。"
                if _NUMERIC_PLATFORM_ID.fullmatch(parts[1]) is None:
                    return "目标 QQ 号格式错误。"
                target = parts[1]
            if operation == "show":
                snapshot = await self._relationship_admin.get_relationship(actor, target)
                return (
                    f"好感度：{snapshot.affection_score}\n"
                    f"信任度：{snapshot.trust_score}\n"
                    f"有效信任度：{snapshot.effective_trust}\n"
                    f"当前关系阶段：{snapshot.stage.name}"
                )
            history = await self._relationship_admin.get_history(actor, target, limit=10)
            if not history:
                return "暂无关系变化记录。"
            return "\n".join(
                (
                    f"{row.created_at:%Y-%m-%d %H:%M} "
                    f"好感{row.affection_delta:+d} 信任{row.trust_delta:+d} "
                    f"[{row.change_type}/{row.reason_code}]"
                )
                for row in history
            )

        if operation not in {"set", "adjust", "trust"}:
            return "可用操作：show、history；超级管理员另可使用 set、adjust、trust。"
        if not actor.is_superuser:
            return "权限不足：只有超级管理员可以修改关系分数。"
        if (
            len(parts) != 3
            or parts[0].casefold() != "user"
            or _NUMERIC_PLATFORM_ID.fullmatch(parts[1]) is None
        ):
            return f"格式：/ai affection {operation} user <QQ号> <数值>"
        try:
            value = int(parts[2])
        except ValueError:
            return "分数必须是整数。"
        target = parts[1]
        try:
            if operation == "set":
                _, snapshot = await self._relationship_admin.set_affection(actor, target, value)
            elif operation == "adjust":
                _, snapshot = await self._relationship_admin.adjust_affection(
                    actor,
                    target,
                    value,
                )
            else:
                _, snapshot = await self._relationship_admin.set_trust(actor, target, value)
        except ValueError:
            return "好感度/信任度必须在 0～100；好感度单次调整必须在 -20～20。"
        return (
            f"已更新 QQ {target}：好感度 {snapshot.affection_score}，"
            f"信任度 {snapshot.trust_score}，阶段 {snapshot.stage.name}。"
        )

    @staticmethod
    def _parse_scoped_operation(
        argument: str, actor: str, is_superuser: bool
    ) -> tuple[str, str, list[str]] | str:
        parts = argument.split()
        if not parts:
            return "缺少操作名。"
        operation = parts.pop(0).casefold()
        target = actor
        if len(parts) >= 2 and parts[0].casefold() == "user":
            if not is_superuser:
                return "只有超级管理员可以管理其他 QQ 人物。"
            candidate = parts[1]
            if _NUMERIC_PLATFORM_ID.fullmatch(candidate) is None:
                return "目标 QQ 号格式错误。"
            target = candidate
            del parts[:2]
        return operation, target, parts

    def _capabilities_command(self, message: InboundMessage, argument: str) -> str:
        category = argument.strip() or None
        report = self._permission_catalog.report_for_message(message, category=category)
        return report.render_text()

    async def _automation_command(
        self,
        *,
        message: InboundMessage,
        identity: ConversationIdentity,
        argument: str,
    ) -> str:
        """Manage owner-scoped tasks through the same service used by Agent tools."""

        if self._automation is None or not self._settings.automation_enabled:
            return "自动化功能当前未启用。"
        parts = argument.split()
        operation = parts.pop(0).casefold() if parts else "list"
        if operation == "list":
            if parts:
                return "格式：/ai automation list"
            rows = await self._automation.list_current(message.sender.user_id)
            if not rows:
                return "当前没有运行中或已暂停的自动化任务。\n已结束任务：/ai automation completed"
            timezone = await self._automation.timezone(message.sender.user_id)
            lines = [f"当前任务（{timezone}）："]
            lines.extend(
                f"#{index} [{row.status.value}] {row.name}；下次："
                f"{local_text(row.next_run_at, timezone)}"
                for index, row in enumerate(rows, start=1)
            )
            lines.append("已结束任务：/ai automation completed")
            return "\n".join(lines)
        if operation in {"completed", "archive"}:
            if parts:
                return "格式：/ai automation completed"
            rows = await self._automation.list_completed(message.sender.user_id)
            if not rows:
                return "完成历史为空。"
            timezone = await self._automation.timezone(message.sender.user_id)
            lines = [f"完成历史（{timezone}，不占用当前任务编号）："]
            lines.extend(
                f"H{index} [{row.status.value}] {row.name}；最后运行："
                f"{local_text(row.last_run_at, timezone)}"
                for index, row in enumerate(rows, start=1)
            )
            return "\n".join(lines)
        if len(parts) != 1 or not parts[0].isdigit():
            return "格式：/ai automation show|pause|resume|cancel|run|history <当前编号>"
        task_number = int(parts[0])
        try:
            current = await self._automation.current_by_number(message.sender.user_id, task_number)
            automation_id = current.id
            if operation == "show":
                timezone = await self._automation.timezone(message.sender.user_id)
                return (
                    f"当前任务 #{task_number}\n名称：{current.name}\n"
                    f"状态：{current.status.value}\n时区：{timezone}\n下次："
                    f"{local_text(current.next_run_at, timezone)}\n"
                    f"能力：{', '.join(current.required_capabilities)}"
                )
            if operation == "history":
                history_rows = await self._automation.history(
                    automation_id,
                    creator_user_id=message.sender.user_id,
                )
                if not history_rows:
                    return "该任务暂无执行记录。"
                timezone = await self._automation.timezone(message.sender.user_id)
                return "\n".join(
                    f"运行 #{row.id} [{row.status.value}] "
                    f"{local_text(row.scheduled_for, timezone)}"
                    + (f"；{row.error_category}" if row.error_category else "")
                    for row in history_rows
                )
            if operation == "pause":
                changed = await self._automation.pause(
                    automation_id,
                    inbound=message,
                    conversation_key=identity.key,
                )
                return "任务已暂停。" if changed else "任务状态没有改变。"
            if operation == "resume":
                changed = await self._automation.resume(
                    automation_id,
                    inbound=message,
                    conversation_key=identity.key,
                )
                return "任务已恢复。" if changed else "该任务不能恢复。"
            if operation == "cancel":
                changed = await self._automation.cancel(
                    automation_id,
                    inbound=message,
                    conversation_key=identity.key,
                )
                return "任务已取消。" if changed else "任务状态没有改变。"
            if operation == "run":
                changed = await self._automation.run_now(
                    automation_id,
                    inbound=message,
                    conversation_key=identity.key,
                )
                return "任务已进入待执行队列。" if changed else "该任务不能立即执行。"
        except ValueError as exc:
            return str(exc)
        return "可用操作：list、completed、show、pause、resume、cancel、run、history。"

    async def _config_command(self, actor: AdminActor, argument: str) -> str:
        parts = argument.split()
        if not parts:
            return (
                "格式：/ai config list|get|set|unset|history|rollback ...\n"
                "群级后缀：group current；用户级后缀：user <QQ号>"
            )
        operation = parts.pop(0).casefold()
        if operation == "list":
            category = parts[0] if parts else None
            if len(parts) > 1:
                return "格式：/ai config list [类别]"
            specs = self._config_admin.list_capabilities(category)
            if not specs:
                return "没有找到该类别的配置。"
            return "\n".join(
                (f"{spec.key} [{spec.apply_mode.value}] {'可修改' if spec.mutable else '受保护'}")
                for spec in specs
            )
        if operation == "get":
            if not parts:
                return "格式：/ai config get <key> [group current|user <QQ号>]"
            key = parts.pop(0)
            parsed_scope = self._parse_config_command_scope(parts, actor)
            if isinstance(parsed_scope, str):
                return parsed_scope
            scope_type, scope_id = parsed_scope
            try:
                value = await self._config_admin.get(
                    key,
                    user_id=scope_id if scope_type == "user" else None,
                    group_id=scope_id if scope_type == "group" else None,
                )
            except KeyError:
                return "未知配置键；使用 /ai config list 查看注册项。"
            return self._render_effective_config(value)
        if operation == "set":
            if len(parts) < 2:
                return "格式：/ai config set <key> <value> [group current|user <QQ号>]"
            key, raw_value = parts.pop(0), parts.pop(0)
            parsed_scope = self._parse_config_command_scope(parts, actor)
            if isinstance(parsed_scope, str):
                return parsed_scope
            scope_type, scope_id = parsed_scope
            result = await self._config_admin.set(
                actor,
                key=key,
                value=raw_value,
                scope_type=scope_type,
                scope_id=scope_id,
            )
            return self._render_config_change(result)
        if operation == "unset":
            if not parts:
                return "格式：/ai config unset <key> [group current|user <QQ号>]"
            key = parts.pop(0)
            parsed_scope = self._parse_config_command_scope(parts, actor)
            if isinstance(parsed_scope, str):
                return parsed_scope
            scope_type, scope_id = parsed_scope
            result = await self._config_admin.unset(
                actor,
                key=key,
                scope_type=scope_type,
                scope_id=scope_id,
            )
            return self._render_config_change(result)
        if operation == "history":
            if len(parts) > 1:
                return "格式：/ai config history [key]"
            try:
                rows = await self._config_admin.history(
                    key=parts[0] if parts else None,
                    actor_user_id=actor.user_id,
                    limit=20,
                )
            except KeyError:
                return "未知配置键。"
            if not rows:
                return "暂无配置修改记录。"
            return "\n".join(
                (
                    f"{row.id}. {row.operation} {row.target_id} "
                    f"{'成功' if row.success else '失败'} "
                    f"{row.created_at:%Y-%m-%d %H:%M}"
                )
                for row in rows
            )
        if operation == "rollback":
            if len(parts) != 1 or not parts[0].isdigit():
                return "格式：/ai config rollback <change_id>"
            result = await self._config_admin.rollback(actor, int(parts[0]))
            return self._render_config_change(result)
        return "可用操作：list、get、set、unset、history、rollback。"

    @staticmethod
    def _parse_config_command_scope(
        parts: list[str],
        actor: AdminActor,
    ) -> tuple[str, str] | str:
        if not parts:
            return "global", ""
        if len(parts) == 2 and parts[0].casefold() == "group":
            if parts[1].casefold() != "current":
                return "群级作用域只接受 group current。"
            if actor.current_group_id is None:
                return "当前消息不在群聊中。"
            return "group", actor.current_group_id
        if len(parts) == 2 and parts[0].casefold() == "user":
            if _NUMERIC_PLATFORM_ID.fullmatch(parts[1]) is None:
                return "目标 QQ 号格式错误。"
            return "user", parts[1]
        return "作用域格式错误：使用 group current 或 user <QQ号>。"

    @staticmethod
    def _render_effective_config(value: EffectiveConfigValue) -> str:
        if value.configured is not None:
            rendered_value = "已配置" if value.configured else "未配置"
        else:
            rendered_value = str(value.value)
        return (
            f"{value.key} = {rendered_value}\n"
            f"来源：{value.source}\n"
            f"生效方式：{value.apply_mode.value}"
        )

    @staticmethod
    def _render_config_change(result: ConfigChangeResult) -> str:
        if not result.success:
            return f"配置未修改：{result.detail or result.error_category or '未知错误'}"
        suffix = (
            "，需要重启 Bot 后生效"
            if (result.apply_mode is ConfigApplyMode.RESTART_REQUIRED and result.pending_restart)
            else (
                "，只影响之后新建的记录或任务"
                if result.apply_mode is ConfigApplyMode.FUTURE_ONLY
                else (
                    "，有效值未变化，无需重启"
                    if result.apply_mode is ConfigApplyMode.RESTART_REQUIRED
                    else "，已立即生效"
                )
            )
        )
        return (
            f"已将 {result.key} 从 {result.before} 改为 {result.after}{suffix}。"
            f" 变更编号：{result.change_id}"
        )

    async def _whoami(
        self,
        message: InboundMessage,
        profile: UserProfileSnapshot,
        argument: str,
    ) -> str:
        if argument:
            return "该命令不接受参数，只能查看发送者本人。"
        aliases = await self._people.aliases(profile.user_id)
        memory_count = await self._memories.count_person(profile.user_id)
        membership_count = await self._people.membership_count(profile.user_id)
        lines = [
            f"QQ：{profile.user_id}",
            f"当前昵称：{profile.nickname or '未获取'}",
        ]
        if message.scope_type is ScopeType.GROUP:
            lines.extend(
                [
                    f"本群群名片：{profile.group_card or '未设置'}",
                    f"当前群：{profile.group_id}",
                ]
            )
        else:
            lines.append("当前场景：私聊")
        lines.extend(
            [
                f"已知别名：{'、'.join(aliases) if aliases else '无'}",
                f"个人记忆数：{memory_count}",
                f"群成员关系数：{membership_count}",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _help_text() -> str:
        return (
            "QQ AI 助手命令：\n"
            "/ai help | new | status | stop | ping | whoami | forgetme\n"
            "/ai memory list|add|update|delete\n"
            "/ai preference list|set|delete\n"
            "/ai affection show|history\n"
            "/ai affection set|adjust|trust user <QQ号> <数值>（超级管理员）\n"
            "/ai capabilities [类别]（查看当前 QQ 的完整权限与可改范围）\n"
            "/ai config list|get|set|unset|history|rollback（超级管理员）\n"
            "/ai automation list|show|pause|resume|cancel|run|history <任务ID>\n"
            "/ai on|off（超级管理员，当前群）\n"
            "/ai group <群号> on|off（超级管理员）\n"
            "/ai private <QQ号> on|off（超级管理员；阻止/恢复私聊）\n"
            "超级管理员可在 memory/preference 操作名后加 user <QQ号>。"
        )

    @staticmethod
    def _parse_access_switch(argument: str) -> tuple[str, bool] | None:
        parts = argument.casefold().split()
        if len(parts) != 2 or _NUMERIC_PLATFORM_ID.fullmatch(parts[0]) is None:
            return None
        if parts[1] not in {"on", "off"}:
            return None
        return parts[0], parts[1] == "on"

    @staticmethod
    def _event_profile(message: InboundMessage) -> UserProfileSnapshot:
        return UserProfileSnapshot(
            user_id=message.sender.user_id,
            scope_type=message.scope_type,
            nickname=sanitize_profile_name(message.sender.nickname),
            group_id=message.group_id,
            group_card=sanitize_profile_name(message.sender.group_card),
        )

    async def _send_text(
        self,
        inbound: InboundMessage,
        sender: OutboundSender,
        text: str,
        *,
        record: bool = True,
    ) -> bool:
        try:
            result = await sender.send(OutboundMessage(text=text))
            if record:
                message_id: str | None = None
                if isinstance(result, str | int):
                    message_id = str(result)
                elif isinstance(result, dict):
                    raw_id = result.get("message_id") or result.get("id")
                    if raw_id is not None:
                        message_id = str(raw_id)
                await self._ledger.append(
                    bot_user_id=inbound.bot_user_id or "unknown-bot",
                    platform_message_id=message_id or f"out-{uuid.uuid4()}",
                    scope_type=inbound.scope_type,
                    sender_user_id=inbound.bot_user_id or "unknown-bot",
                    direction="outbound",
                    content=text,
                    segments=({"type": "text", "data": {"text": text}},),
                    group_id=inbound.group_id,
                    private_peer_user_id=(
                        inbound.sender.user_id if inbound.scope_type is ScopeType.PRIVATE else None
                    ),
                    sender_is_bot=True,
                )
            return True
        except (OSError, RuntimeError) as exc:
            logger.error("outbound_send_failed", exc_info=exc)
            return False

    @staticmethod
    def _log_result(
        event_key: str,
        identity: ConversationIdentity,
        message: InboundMessage,
        *,
        handler: str,
        started: float,
        success: bool,
    ) -> None:
        logger.info(
            "message_handled",
            extra={
                "event_key": event_key,
                "conversation_hash": hashlib.sha256(identity.key.encode()).hexdigest()[:16],
                "message_type": message.scope_type.value,
                "handler": handler,
                "total_latency_seconds": round(time.perf_counter() - started, 4),
                "success": success,
            },
        )
