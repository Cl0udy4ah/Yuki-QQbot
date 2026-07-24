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

from qq_ai_bot import __version__
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
)
from qq_ai_bot.services.autonomous_groups import AutonomousGroupService
from qq_ai_bot.services.chat import ChatService, OutboundSender
from qq_ai_bot.services.concurrency import ConcurrencyManager, RequestCancelledError
from qq_ai_bot.services.deduplication import DeduplicationService, build_event_key
from qq_ai_bot.services.group_members import GroupMemberResolver, GroupMemberService
from qq_ai_bot.services.memory_worker import MemoryWorker
from qq_ai_bot.services.policies import (
    CommandName,
    EffectiveGroupPolicy,
    EffectivePrivatePolicy,
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

UNSUPPORTED_MESSAGE = "当前版本会保存媒体消息元数据，但尚未实现图片、语音或视频理解。"
RATE_LIMIT_MESSAGE = "请求过于频繁，请稍后再试。"
_NUMERIC_PLATFORM_ID = re.compile(r"[1-9][0-9]{4,19}")


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Observable result used by adapters and integration tests."""

    handled: bool
    sent_messages: int = 0
    reason: str = ""


class MessageProcessor:
    """Admission → dedup → identity → ledger → memory job → command/reply."""

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
        autonomous_groups: AutonomousGroupService | None = None,
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
        self._autonomous = autonomous_groups

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
        should_observe = (
            message.scope_type is ScopeType.PRIVATE and decision.should_respond
        ) or bool(self._settings.observe_enabled_groups and group_policy and group_policy.enabled)
        if not decision.should_respond and not should_observe:
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

        # forgetme is deliberately neither re-observed nor re-written to the ledger.
        if decision.command is CommandName.FORGETME:
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

        if not decision.should_respond:
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
            text = UNSUPPORTED_MESSAGE if message.attachments else "请输入要发送给 AI 的内容。"
            sent = await self._send_text(message, sender, text)
            return ProcessResult(True, int(sent), "unsupported" if message.attachments else "empty")
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
            text = (
                f"OneBot 连接：{'已连接' if self._onebot_connected() else '未连接'}\n"
                f"模型：{self._settings.llm_model or '未配置'}\n"
                f"当前切点后的事件数：{count}\n"
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
                text = "已启用当前群。" if enabled else "已停用当前群。"
        elif command in {CommandName.PRIVATE, CommandName.GROUP}:
            parsed = self._parse_access_switch(argument)
            if parsed is None:
                noun = "QQ号" if command is CommandName.PRIVATE else "群号"
                text = f"格式错误，请使用 /ai {command.value} <{noun}> on|off。"
            else:
                target_id, enabled = parsed
                if (
                    command is CommandName.PRIVATE
                    and target_id in self._settings.superusers
                    and not enabled
                ):
                    text = "不能关闭超级用户的私聊权限。"
                elif command is CommandName.PRIVATE:
                    await self._private_users.set_enabled(target_id, enabled)
                    text = (
                        "已开启指定 QQ 用户的私聊权限。"
                        if enabled
                        else "已关闭指定 QQ 用户的私聊权限。"
                    )
                else:
                    await self._groups.set_enabled(target_id, enabled)
                    text = f"已{'启用' if enabled else '停用'}群 {target_id}。"
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
                    "已彻底删除与你 QQ 号关联的人物、记忆、成员关系和可归属聊天事件。"
                    if deleted
                    else "没有找到与你 QQ 号关联的数据。"
                )
        elif command is CommandName.MEMORY:
            text = await self._memory_command(
                actor=message.sender.user_id,
                argument=argument,
                is_superuser=is_superuser,
            )
        elif command is CommandName.PREFERENCE:
            text = await self._preference_command(
                actor=message.sender.user_id,
                argument=argument,
                is_superuser=is_superuser,
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

    async def _memory_command(self, *, actor: str, argument: str, is_superuser: bool) -> str:
        parsed = self._parse_scoped_operation(argument, actor, is_superuser)
        if isinstance(parsed, str):
            return parsed
        operation, target, rest = parsed
        if operation == "list":
            rows = await self._memories.list_person(
                target, limit=self._settings.person_memory_max_entries
            )
            if not rows:
                return f"QQ {target} 暂无人物记忆。"
            return "\n".join(f"{row.id}. [{row.source_type}] {row.content}" for row in rows)
        if operation == "add":
            content = " ".join(rest).strip()
            if not content:
                return "格式：/ai memory add <内容>"
            if (
                await self._memories.count_person(target)
                >= self._settings.person_memory_max_entries
            ):
                return "人物记忆已达到上限，请先删除或合并旧记忆。"
            row = await self._memories.upsert(
                scope="person",
                user_id=target,
                memory_key=f"explicit-{uuid.uuid4()}",
                content=content,
                category="explicit",
                importance=5,
                source_type="explicit",
                limit=self._settings.person_memory_max_entries,
            )
            return f"已添加人物记忆 {row.id}。"
        if operation == "update":
            if len(rest) < 2 or not rest[0].isdigit():
                return "格式：/ai memory update <记忆ID> <内容>"
            updated = await self._memories.update_explicit(
                int(rest[0]), user_id=target, content=" ".join(rest[1:])
            )
            return "记忆已更新。" if updated else "没有找到可修改的记忆。"
        if operation == "delete":
            if len(rest) != 1 or not rest[0].isdigit():
                return "格式：/ai memory delete <记忆ID>"
            deleted = await self._memories.delete_person_memory(int(rest[0]), user_id=target)
            return "记忆已删除。" if deleted else "没有找到该记忆。"
        return "可用操作：list、add、update、delete。"

    async def _preference_command(self, *, actor: str, argument: str, is_superuser: bool) -> str:
        parsed = self._parse_scoped_operation(argument, actor, is_superuser)
        if isinstance(parsed, str):
            return parsed
        operation, target, rest = parsed
        if operation == "list":
            rows = await self._memories.list_preferences(
                target, limit=self._settings.preference_max_entries
            )
            if not rows:
                return f"QQ {target} 暂无交互偏好。"
            return "\n".join(f"{row.key} = {row.value}" for row in rows)
        if operation == "set":
            if len(rest) < 2:
                return "格式：/ai preference set <键> <值>"
            await self._memories.set_preference(
                target,
                rest[0],
                " ".join(rest[1:]),
                limit=self._settings.preference_max_entries,
            )
            return f"偏好 {rest[0]} 已设置。"
        if operation == "delete":
            if len(rest) != 1:
                return "格式：/ai preference delete <键>"
            deleted = await self._memories.delete_preference(target, rest[0])
            return "偏好已删除。" if deleted else "没有找到该偏好。"
        return "可用操作：list、set、delete。"

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
