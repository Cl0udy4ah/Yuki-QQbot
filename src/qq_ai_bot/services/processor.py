"""End-to-end normalized message processor and command implementation."""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from qq_ai_bot import __version__
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.conversations import ConversationIdentity, ConversationMode, ScopeType
from qq_ai_bot.domain.messages import InboundMessage, OutboundMessage
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.llm.base import LLMConfigurationError, LLMEmptyResponseError, LLMError
from qq_ai_bot.persistence.repositories import ConversationRepository, GroupSettingsRepository
from qq_ai_bot.services.chat import ChatService, OutboundSender
from qq_ai_bot.services.concurrency import ConcurrencyManager, RequestCancelledError
from qq_ai_bot.services.deduplication import DeduplicationService, build_event_key
from qq_ai_bot.services.policies import (
    CommandName,
    EffectiveGroupPolicy,
    command_requires_superuser,
    evaluate_message,
)
from qq_ai_bot.services.rate_limit import SlidingWindowRateLimiter
from qq_ai_bot.services.renderer import sanitize_input
from qq_ai_bot.services.user_profiles import (
    UserProfileResolver,
    UserProfileService,
    sanitize_profile_name,
)

logger = logging.getLogger(__name__)

UNSUPPORTED_MESSAGE = "当前版本暂不支持该消息类型。"
RATE_LIMIT_MESSAGE = "请求过于频繁，请稍后再试。"


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Observable result used by adapters and integration tests."""

    handled: bool
    sent_messages: int = 0
    reason: str = ""


class MessageProcessor:
    """Apply policy, idempotency, rate limits, commands, and chat orchestration."""

    def __init__(
        self,
        *,
        settings: Settings,
        conversations: ConversationRepository,
        groups: GroupSettingsRepository,
        user_profiles: UserProfileService,
        chat: ChatService,
        deduplication: DeduplicationService,
        rate_limiter: SlidingWindowRateLimiter,
        concurrency: ConcurrencyManager,
        onebot_connected: Callable[[], bool],
    ) -> None:
        self._settings = settings
        self._conversations = conversations
        self._groups = groups
        self._user_profiles = user_profiles
        self._chat = chat
        self._deduplication = deduplication
        self._rate_limiter = rate_limiter
        self._concurrency = concurrency
        self._onebot_connected = onebot_connected

    async def handle(
        self,
        message: InboundMessage,
        sender: OutboundSender,
        profile_resolver: UserProfileResolver | None = None,
    ) -> ProcessResult:
        """Process one normalized message without leaking transport objects."""

        started = time.perf_counter()
        group_policy = await self._effective_group_policy(message.group_id)
        decision = evaluate_message(message, self._settings, group_policy=group_policy)
        if not decision.should_respond:
            return ProcessResult(False, reason=decision.reason)

        shared = bool(group_policy and group_policy.conversation_mode is ConversationMode.SHARED)
        identity = message.conversation(shared_group=shared)
        event_key = build_event_key(message, identity.key)
        if not await self._deduplication.claim(event_key):
            return ProcessResult(False, reason="duplicate")

        if decision.command is CommandName.FORGETME:
            profile = self._event_profile(message)
        else:
            profile = await self._user_profiles.capture(message, profile_resolver)

        category = "command" if decision.command is not None else "chat"
        rate = await self._rate_limiter.check(
            user_id=message.sender.user_id,
            group_id=message.group_id,
            category=category,
        )
        if not rate.allowed:
            sent = await self._safe_send(sender, RATE_LIMIT_MESSAGE)
            self._log_result(
                event_key,
                identity,
                message,
                handler="rate_limit",
                started=started,
                success=sent,
                exception_category=None,
            )
            return ProcessResult(True, int(sent), f"{rate.scope}_rate_limited")

        if decision.command is not None:
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

        content = sanitize_input(decision.content)
        if message.reply_text:
            quoted = sanitize_input(message.reply_text)
            if quoted:
                content = f"[回复的消息]\n{quoted}\n\n{content}".strip()
        if not content:
            response = UNSUPPORTED_MESSAGE if message.attachments else "请输入要发送给 AI 的内容。"
            sent = await self._safe_send(sender, response)
            return ProcessResult(True, int(sent), "unsupported" if message.attachments else "empty")
        if len(content) > self._settings.max_input_characters:
            sent = await self._safe_send(
                sender,
                f"消息过长，请控制在 {self._settings.max_input_characters} 个字符以内。",
            )
            return ProcessResult(True, int(sent), "input_too_long")

        try:
            sent_count = await self._chat.respond(message, identity, profile, content, sender)
        except RequestCancelledError:
            self._log_result(
                event_key,
                identity,
                message,
                handler="chat",
                started=started,
                success=False,
                exception_category="RequestCancelledError",
            )
            return ProcessResult(True, reason="cancelled")
        except LLMConfigurationError:
            sent = await self._safe_send(sender, "AI 服务尚未配置，请联系管理员。")
            return ProcessResult(True, int(sent), "llm_not_configured")
        except LLMEmptyResponseError:
            sent = await self._safe_send(sender, "AI 返回了空内容，请稍后重试。")
            return ProcessResult(True, int(sent), "empty_llm_response")
        except LLMError as exc:
            logger.warning("llm_failure exception_category=%s", type(exc).__name__)
            sent = await self._safe_send(sender, "AI 服务暂时不可用，请稍后重试。")
            return ProcessResult(True, int(sent), "llm_failure")
        except (OSError, RuntimeError) as exc:
            logger.error("message_send_or_storage_failure", exc_info=exc)
            return ProcessResult(True, reason="send_or_storage_failure")

        self._log_result(
            event_key,
            identity,
            message,
            handler="chat",
            started=started,
            success=True,
            exception_category=None,
        )
        return ProcessResult(True, sent_count, "chat")

    async def _effective_group_policy(self, group_id: str | None) -> EffectiveGroupPolicy | None:
        if group_id is None:
            return None
        setting = await self._groups.get(group_id)
        if setting is None:
            return EffectiveGroupPolicy(enabled=group_id in self._settings.enabled_groups)
        return EffectiveGroupPolicy(
            enabled=setting.enabled,
            require_mention=setting.require_mention,
            conversation_mode=setting.conversation_mode,
        )

    async def _handle_command(
        self,
        command: CommandName,
        message: InboundMessage,
        identity: ConversationIdentity,
        profile: UserProfileSnapshot,
        command_argument: str,
        sender: OutboundSender,
        event_key: str,
        started: float,
    ) -> ProcessResult:
        is_superuser = message.sender.user_id in self._settings.superusers
        if command_requires_superuser(command) and not is_superuser:
            text = "权限不足：该命令仅限超级用户。"
        elif command is CommandName.HELP:
            text = (
                "QQ AI 助手命令：\n"
                "/ai help - 显示帮助\n"
                "/ai new - 清空当前会话\n"
                "/ai status - 查看状态\n"
                "/ai stop - 取消当前请求\n"
                "/ai ping - 连通性检查\n"
                "/ai whoami - 查看当前身份\n"
                "/ai forgetme - 删除自己的身份资料\n"
                "/ai on | off - 超级用户启用或停用当前群"
            )
        elif command is CommandName.NEW:
            async with self._concurrency.conversation(identity.key):
                await self._conversations.clear(identity)
            text = "已清空当前会话，下一条消息将开始新对话。"
        elif command is CommandName.STATUS:
            count = await self._conversations.count_messages(identity)
            text = (
                f"OneBot 连接：{'已连接' if self._onebot_connected() else '未连接'}\n"
                f"模型：{self._settings.llm_model or '未配置'}\n"
                f"当前会话消息数：{count}\n"
                f"请求处理中：{'是' if self._concurrency.is_processing(identity.key) else '否'}\n"
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
                await self._groups.set_enabled(message.group_id, enabled)
                text = "已在当前群启用 AI。" if enabled else "已在当前群停用 AI。"
        elif command is CommandName.PING:
            elapsed_ms = (time.perf_counter() - started) * 1000
            text = f"pong ({elapsed_ms:.1f} ms)"
        elif command is CommandName.WHOAMI:
            if command_argument:
                text = "该命令不接受参数，只能查看发送者自己的身份。"
            elif message.scope_type is ScopeType.GROUP:
                text = (
                    f"QQ号：{profile.user_id}\n"
                    f"QQ昵称：{profile.nickname or '未获取'}\n"
                    f"本群群名片：{profile.group_card or '未设置'}\n"
                    f"当前识别名称：{profile.display_name}\n"
                    f"场景：群聊（群号 {profile.group_id}）"
                )
            else:
                text = (
                    f"QQ号：{profile.user_id}\n"
                    f"QQ昵称：{profile.nickname or '未获取'}\n"
                    f"当前识别名称：{profile.display_name}\n"
                    "场景：私聊"
                )
        elif command is CommandName.FORGETME:
            if command_argument:
                text = "该命令不接受参数，只能删除发送者自己的身份资料。"
            else:
                deleted = await self._user_profiles.forget(message.sender.user_id)
                if deleted is None:
                    text = "身份资料暂时无法删除，请稍后重试。"
                elif deleted:
                    text = (
                        "已删除你的昵称和全部群名片资料。聊天记录未删除；"
                        "如需清空当前会话，请使用 /ai new。"
                    )
                else:
                    text = (
                        "没有找到你的身份资料。聊天记录未删除；如需清空当前会话，请使用 /ai new。"
                    )
        else:
            text = "未知命令，请使用 /ai help 查看帮助。"

        sent = await self._safe_send(sender, text)
        self._log_result(
            event_key,
            identity,
            message,
            handler=f"command_{command.value}",
            started=started,
            success=sent,
            exception_category=None if sent else "SendError",
        )
        return ProcessResult(True, int(sent), f"command_{command.value}")

    @staticmethod
    def _event_profile(message: InboundMessage) -> UserProfileSnapshot:
        """Build a current-event snapshot without reading or writing storage."""

        return UserProfileSnapshot(
            user_id=message.sender.user_id,
            scope_type=message.scope_type,
            nickname=sanitize_profile_name(message.sender.nickname),
            group_id=message.group_id,
            group_card=sanitize_profile_name(message.sender.group_card),
        )

    @staticmethod
    async def _safe_send(
        sender: OutboundSender,
        text: str,
    ) -> bool:
        try:
            await sender.send(OutboundMessage(text=text))
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
        exception_category: str | None,
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
                "exception_category": exception_category,
            },
        )
