"""Person-centric context assembly, bounded Agent loop, sending, and ledger writes."""

from __future__ import annotations

import asyncio
import json
import random
import uuid
from dataclasses import replace
from typing import Any, Protocol, cast

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.admin.permission_catalog import contains_internal_capability_payload
from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.capabilities import (
    AuthorityContext,
    CapabilityPolicyContext,
    CapabilityPolicyEngine,
    CapabilityRegistry,
    CapabilityRisk,
    CapabilityTrustSource,
    ChatToolCapabilityProvider,
)
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.conversations import ConversationIdentity, ScopeType
from qq_ai_bot.domain.messages import (
    AttachmentKind,
    ChatMessage,
    ChatTool,
    InboundMessage,
    OutboundMedia,
    OutboundMessage,
    ToolCall,
)
from qq_ai_bot.domain.profiles import UserProfileSnapshot
from qq_ai_bot.emoji.effects import EmojiReplyEffectService
from qq_ai_bot.emoji.models import EmojiPlacement, EmojiReplyMode, PendingReplyEffect
from qq_ai_bot.model_runtime.executor import ModelCompleter, ModelExecutor, require_model_executor
from qq_ai_bot.persistence.repositories import (
    EventLedgerRepository,
    MemoryRepository,
    PeopleRepository,
    RelationshipRepository,
    WebSearchSourceRepository,
)
from qq_ai_bot.planner.models import PlannedTurn, ToolGroup, ToolMode, ToolSelection
from qq_ai_bot.services.agent_runner import (
    AgentRunner,
    AgentRuntime,
    AgentToolBackend,
)
from qq_ai_bot.services.agent_tools import AgentToolService, OneBotToolGateway, ToolRuntime
from qq_ai_bot.services.concurrency import ConcurrencyManager
from qq_ai_bot.services.context_assembler import ContextAssembler
from qq_ai_bot.services.plugin_events import (
    LifecycleEventPublisher,
    publish_notification,
)
from qq_ai_bot.services.prompt_composer import PromptComposer
from qq_ai_bot.services.renderer import (
    clean_model_output,
    split_daily_chat_sentences,
    split_qq_message,
)
from qq_ai_bot.services.reply_sequence import ReplySequenceManager
from qq_ai_bot.services.source_policy import SourceDisplayPolicy
from qq_ai_bot.services.source_renderer import SourceRenderer
from qq_ai_bot.services.turn_coordinator import ConversationTurnCoordinator, TurnToken
from qq_ai_bot.speech.models import VoiceAgentToolPolicy
from qq_ai_bot.speech.reply_effect import (
    PendingVoiceReplyEffect,
    PreparedVoiceReply,
    VoiceReplyEffectService,
)
from qq_ai_bot.time.service import TimeContextService
from qq_ai_bot.vision.models import VisualObservation
from yuki_plugin_sdk.events import EventName

_ADMIN_RETRYABLE_ERRORS = frozenset(
    {
        "invalid_json",
        "invalid_arguments",
        "validation_error",
        "unknown_capability",
        "ValueError",
    }
)


class OutboundSender(Protocol):
    """Adapter-provided sender used by the business layer."""

    async def send(self, message: OutboundMessage) -> Any:
        """Send one normal message and optionally return a platform message id."""


class AdminToolService(Protocol):
    """Backend-verified administrator tools used by the single chat Agent."""

    def definitions(self) -> tuple[ChatTool, ...]:
        """Return reviewed administrator tool schemas."""

    def is_mutating_call(self, name: str, arguments_json: str) -> bool:
        """Return whether this exact registered operation changes backend state."""

    async def execute(
        self,
        name: str,
        arguments_json: str,
        runtime: ToolRuntime,
    ) -> str:
        """Execute against authority derived from the current real event."""


class AutomationToolProvider(Protocol):
    """Owner-scoped automation tools available to every real direct user turn."""

    def definitions(self) -> tuple[ChatTool, ...]: ...

    def owns(self, name: str) -> bool: ...

    async def execute(self, name: str, arguments_json: str, runtime: ToolRuntime) -> str: ...


class PluginToolProvider(Protocol):
    """Approved Plugin API tools merged into the existing Yuki Agent loop."""

    def definitions(
        self,
        runtime: ToolRuntime,
        *,
        web_was_used: bool,
    ) -> tuple[ChatTool, ...]: ...

    def owns(self, name: str) -> bool: ...

    def is_mutating(self, name: str) -> bool: ...

    def is_read_only(self, name: str) -> bool: ...

    async def execute(
        self,
        name: str,
        arguments_json: str,
        runtime: ToolRuntime,
        *,
        web_was_used: bool,
    ) -> str: ...


class _ChatAgentBackend(AgentToolBackend):
    """Preserve event-bound chat policies behind the shared model tool loop."""

    def __init__(self, service: ChatService, runtime: ToolRuntime) -> None:
        self._service = service
        self._runtime = runtime
        self._tools_closed = False
        self._web_was_used = False
        self._web_calls_used = 0
        self._capability_was_used = False
        self._admin_retry_constraint: tuple[str, str] | None = None
        self._admin_terminal_failure: dict[str, object] | None = None
        self._completed_admin_mutations: set[tuple[str, str]] = set()
        self._batch: list[ToolCall] = []
        self._capabilities = CapabilityRegistry()

    def definitions(self, runtime: AgentRuntime, *, web_was_used: bool) -> tuple[ChatTool, ...]:
        self._web_was_used = self._web_was_used or web_was_used
        if self._tools_closed:
            return ()
        request_runtime = self._request_runtime()
        core_definitions = self._service._tools.definitions(request_runtime)
        definitions = core_definitions
        providers = [
            ChatToolCapabilityProvider(
                core_definitions,
                source=CapabilityTrustSource.CORE,
            )
        ]
        automation = self._service._automation_tools
        if request_runtime.allow_automation and automation is not None:
            automation_definitions = automation.definitions()
            definitions += automation_definitions
            providers.append(
                ChatToolCapabilityProvider(
                    automation_definitions,
                    source=CapabilityTrustSource.AUTOMATION,
                )
            )
        admin = self._service._admin_tools
        if request_runtime.allow_admin_actions and admin is not None:
            admin_definitions = admin.definitions()
            definitions = (
                tuple(tool for tool in definitions if tool.name != "get_my_capabilities")
                + admin_definitions
            )
            providers.append(
                ChatToolCapabilityProvider(
                    admin_definitions,
                    source=CapabilityTrustSource.ADMIN,
                )
            )
        plugin = self._service._plugin_tools
        if plugin is not None:
            plugin_definitions = plugin.definitions(
                request_runtime,
                web_was_used=self._web_was_used,
            )
            definitions += plugin_definitions
            providers.append(
                ChatToolCapabilityProvider(
                    plugin_definitions,
                    source=CapabilityTrustSource.PLUGIN,
                    plugin_read_only=plugin.is_read_only,
                )
            )
        descriptors = tuple(
            descriptor
            for provider in providers
            for descriptor in provider.descriptors()
            if any(tool.name == descriptor.model_name for tool in definitions)
        )
        self._capabilities = CapabilityRegistry(descriptors)
        selection = ToolSelection(
            mode=self._runtime.tool_mode,
            groups=tuple(ToolGroup(group) for group in sorted(self._runtime.tool_groups)),
        )
        visible = CapabilityPolicyEngine().visible(
            self._capabilities.all(),
            CapabilityPolicyContext(
                authority=AuthorityContext(
                    actor_user_id=self._runtime.actor_user_id,
                    is_superuser=self._runtime.actor_is_superuser,
                ),
                origin=self._runtime.origin,
                tool_selection=selection,
                contains_images=bool(
                    self._runtime.inbound.attachments or self._runtime.inbound.reply_attachments
                ),
                web_was_used=self._web_was_used,
            ),
        )
        visible_names = {descriptor.model_name for descriptor in visible}
        definitions = tuple(tool for tool in definitions if tool.name in visible_names)
        if self._admin_retry_constraint is not None:
            definitions = tuple(
                tool for tool in definitions if tool.name == self._admin_retry_constraint[0]
            )
        return definitions

    def begin_batch(self, calls: tuple[ToolCall, ...], runtime: AgentRuntime) -> None:
        self._batch = list(calls)

    async def execute(self, name: str, arguments_json: str, runtime: AgentRuntime) -> str:
        if not self._batch:
            return json.dumps(
                {"ok": False, "error": "tool_batch_state_missing"}, ensure_ascii=False
            )
        call = self._batch.pop(0)
        if call.function.name != name or call.function.arguments != arguments_json:
            return json.dumps(
                {"ok": False, "error": "tool_batch_state_mismatch"}, ensure_ascii=False
            )
        descriptor = self._capabilities.get(name)
        if descriptor is None:
            return json.dumps({"ok": False, "error": "unknown_capability"})
        is_web_tool = descriptor.group == ToolGroup.WEB.value
        is_admin_tool = descriptor.trust_source is CapabilityTrustSource.ADMIN
        automation = self._service._automation_tools
        is_automation_tool = bool(automation is not None and automation.owns(name))
        plugin = self._service._plugin_tools
        is_plugin_tool = bool(plugin is not None and plugin.owns(name))
        config = self._runtime.runtime_config
        assert config is not None
        mutation_identity = (
            (call.function.name, call.function.arguments) if self._is_mutating_call(call) else None
        )
        if mutation_identity is not None and mutation_identity in self._completed_admin_mutations:
            result = json.dumps(
                {
                    "ok": False,
                    "error": "duplicate_mutation",
                    "detail": "本轮已经成功执行过相同修改，不再重复执行。",
                },
                ensure_ascii=False,
            )
        elif is_web_tool and self._web_calls_used >= config.web.max_calls_per_turn:
            result = json.dumps(
                {
                    "ok": False,
                    "error": "web_tool_limit_exceeded",
                    "detail": (
                        f"本轮最多执行 {config.web.max_calls_per_turn} 次联网工具，"
                        "请根据已有结果回答。"
                    ),
                },
                ensure_ascii=False,
            )
        elif descriptor.group == ToolGroup.ONEBOT.value and self._web_was_used:
            result = json.dumps(
                {
                    "ok": False,
                    "error": "web_onebot_isolation",
                    "detail": "使用外部网页内容后，本轮不允许执行 OneBot 管理操作。",
                },
                ensure_ascii=False,
            )
        elif self._admin_retry_constraint is not None and not self._matches_retry(
            call,
            self._admin_retry_constraint,
        ):
            result = json.dumps(
                {
                    "ok": False,
                    "error": "retry_scope_violation",
                    "detail": "参数修正只能重试刚才失败的同一个工具和操作。",
                },
                ensure_ascii=False,
            )
            self._tools_closed = True
        elif is_admin_tool and (
            self._service._admin_tools is None or not self._request_runtime().allow_admin_actions
        ):
            result = json.dumps(
                {
                    "ok": False,
                    "error": "permission_denied",
                    "detail": "当前真实消息事件没有管理员工具权限。",
                },
                ensure_ascii=False,
            )
            self._tools_closed = True
        else:
            execution_runtime = self._request_runtime()
            if mutation_identity is not None and execution_runtime.turn_token is not None:
                await self._service._turn_coordinator.mark_mutation_started(
                    execution_runtime.turn_token
                )
            if is_admin_tool:
                assert self._service._admin_tools is not None
                result = await self._service._admin_tools.execute(
                    name,
                    arguments_json,
                    execution_runtime,
                )
            elif is_automation_tool:
                assert automation is not None
                result = await automation.execute(name, arguments_json, execution_runtime)
            elif is_plugin_tool:
                assert plugin is not None
                result = await plugin.execute(
                    name,
                    arguments_json,
                    execution_runtime,
                    web_was_used=self._web_was_used,
                )
            else:
                result = await self._service._tools.execute(
                    name,
                    arguments_json,
                    execution_runtime,
                )
            if contains_internal_capability_payload(result):
                self._capability_was_used = True
            if is_web_tool:
                self._web_calls_used += 1
                self._web_was_used = True
        decoded = self._service._decode_tool_result(result)
        if self._is_mutating_call(call):
            if bool(decoded.get("ok")):
                self._admin_retry_constraint = None
                self._admin_terminal_failure = None
                if mutation_identity is not None:
                    self._completed_admin_mutations.add(mutation_identity)
            elif decoded.get("error") in _ADMIN_RETRYABLE_ERRORS:
                self._admin_terminal_failure = decoded
                self._admin_retry_constraint = self._retry_identity(call)
                if self._admin_retry_constraint is None:
                    self._tools_closed = True
            else:
                self._admin_terminal_failure = decoded
                self._tools_closed = True
        return result

    def finalize(self, content: str, runtime: AgentRuntime) -> str:
        if self._admin_terminal_failure is not None:
            return self._service._admin_failure_text(self._admin_terminal_failure)
        if self._capability_was_used and contains_internal_capability_payload(content):
            return "我已经在本轮内部读取了权限范围，但没有生成合适的简短回答。请再问一次。"
        return content

    def has_visible_effects(self) -> bool:
        """Return whether queued effects can produce a reply without model text."""

        effects = self._runtime.reply_effects or ()
        if any(isinstance(effect, PendingVoiceReplyEffect) for effect in effects):
            # Voice synthesis needs the final model text as its spoken content.
            # Treating the queued voice request itself as visible content can make
            # a successful tool call end in a completely silent turn.
            return False
        return any(
            isinstance(effect, PendingReplyEffect)
            and (
                effect.mode is EmojiReplyMode.EMOJI_ONLY or effect.placement is EmojiPlacement.ONLY
            )
            for effect in effects
        )

    def exhausted(self, runtime: AgentRuntime) -> str:
        if self._admin_terminal_failure is not None:
            return self._service._admin_failure_text(self._admin_terminal_failure)
        return "这次操作的工具调用次数过多，已停止继续执行。请把请求拆小后再试。"

    def _is_mutating_call(self, call: ToolCall) -> bool:
        descriptor = self._capabilities.get(call.function.name)
        return bool(descriptor is not None and descriptor.risk is not CapabilityRisk.READ)

    def _retry_identity(self, call: ToolCall) -> tuple[str, str] | None:
        if not self._is_mutating_call(call):
            return None
        try:
            arguments = json.loads(call.function.arguments)
        except json.JSONDecodeError:
            return None
        if not isinstance(arguments, dict):
            return None
        operation = next(
            (
                arguments[key]
                for key in ("action", "key", "change_id", "automation_id", "id", "name")
                if key in arguments
            ),
            call.function.name,
        )
        if not isinstance(operation, (str, int)) or isinstance(operation, bool):
            return None
        return call.function.name, str(operation)

    def _matches_retry(self, call: ToolCall, expected: tuple[str, str]) -> bool:
        return self._retry_identity(call) == expected

    def _request_runtime(self) -> ToolRuntime:
        return replace(
            self._runtime,
            allow_generic_onebot=(self._runtime.allow_generic_onebot and not self._web_was_used),
            allow_admin_actions=(self._runtime.allow_admin_actions and not self._web_was_used),
        )


class ChatService:
    """Answer with cross-scope person memory and an event-bound Agent runtime."""

    def __init__(
        self,
        *,
        settings: Settings,
        provider: ModelCompleter | None = None,
        model_executor: ModelExecutor | None = None,
        concurrency: ConcurrencyManager,
        ledger: EventLedgerRepository,
        people: PeopleRepository,
        memories: MemoryRepository,
        tools: AgentToolService,
        relationships: RelationshipRepository,
        web_sources: WebSearchSourceRepository,
        runtime_config: RuntimeConfigService,
        time_service: TimeContextService,
        source_policy: SourceDisplayPolicy | None = None,
        source_renderer: SourceRenderer | None = None,
        context_assembler: ContextAssembler | None = None,
        prompt_composer: PromptComposer | None = None,
        turn_coordinator: ConversationTurnCoordinator | None = None,
        reply_sequence: ReplySequenceManager | None = None,
        emoji_effects: EmojiReplyEffectService | None = None,
        speech_effects: VoiceReplyEffectService | None = None,
        event_publisher: LifecycleEventPublisher | None = None,
    ) -> None:
        self._settings = settings
        models = require_model_executor(
            model_executor,
            provider=provider,
            model=settings.llm_model or "fake",
        )
        self._models = models
        self._concurrency = concurrency
        self._ledger = ledger
        self._people = people
        self._memories = memories
        self._relationships = relationships
        self._tools = tools
        self._web_sources = web_sources
        self._source_policy = source_policy or SourceDisplayPolicy()
        self._source_renderer = source_renderer or SourceRenderer()
        self._runtime_config = runtime_config
        self._agent_runner = AgentRunner(models, concurrency)
        self._admin_tools: AdminToolService | None = None
        self._automation_tools: AutomationToolProvider | None = None
        self._plugin_tools: PluginToolProvider | None = None
        self._time = time_service
        self._context_assembler = context_assembler or ContextAssembler(
            settings=settings,
            ledger=self._ledger,
            people=self._people,
            memories=self._memories,
            relationships=self._relationships,
            time_service=self._time,
        )
        self._prompt_composer = prompt_composer or PromptComposer(settings)
        self._turn_coordinator = turn_coordinator or ConversationTurnCoordinator(
            cancel_replies_on_new_message=settings.reply_sequence_cancel_on_new_message,
            interrupt_autonomous_on_new_message=(
                settings.planner_interrupt_autonomous_on_new_message
            ),
        )
        self._reply_sequence = reply_sequence or ReplySequenceManager(self._turn_coordinator)
        self._emoji_effects = emoji_effects
        self._speech_effects = speech_effects
        self._event_publisher = event_publisher

    def set_admin_tools(self, service: AdminToolService) -> None:
        """Attach privileged tools to this same Agent loop without a second router."""

        self._admin_tools = service

    def set_automation_tools(self, service: AutomationToolProvider) -> None:
        """Attach owner-scoped scheduling tools without introducing a second Agent."""

        self._automation_tools = service

    def set_plugin_tools(self, service: PluginToolProvider) -> None:
        """Attach approved plugin tools without a parallel chat router."""

        self._plugin_tools = service

    def configure_runtime_controls(self, runtime: RuntimeConfigSnapshot) -> None:
        """Apply HOT controls shared by the Agent and Planner prompt pipeline."""

        self._prompt_composer.configure_plugin_limits(runtime)

    def set_event_publisher(self, publisher: LifecycleEventPublisher) -> None:
        """Attach the host notification bus without changing reply control flow."""

        self._event_publisher = publisher

    async def respond(
        self,
        inbound: InboundMessage,
        identity: ConversationIdentity,
        profile: UserProfileSnapshot,
        content: str,
        sender: OutboundSender,
        *,
        autonomous: bool = False,
        runtime_snapshot: RuntimeConfigSnapshot | None = None,
        visual_observation: VisualObservation | None = None,
        visual_input_present: bool = False,
        visual_failure: bool = False,
        planned_turn: PlannedTurn | None = None,
        turn_token: TurnToken | None = None,
    ) -> int:
        """Run one ordered Agent turn and return the sent message count."""

        async with self._concurrency.conversation(identity.key):
            runtime_config = runtime_snapshot or await self._runtime_config.snapshot(
                user_id=inbound.sender.user_id,
                group_id=inbound.group_id,
            )
            if not visual_input_present and self._source_policy.standalone_request(content):
                sources = await self._web_sources.latest(identity.key)
                source_text = self._source_renderer.render(
                    sources,
                    maximum=runtime_config.web.extract_max_results,
                )
                reply = source_text or "当前对话中没有可提供的联网来源。"
                result = await sender.send(OutboundMessage(text=reply))
                await self._record_outbound(inbound, reply, result)
                return 1

            source_display_requested = self._source_policy.requested(content)
            messages = await self._build_messages(
                inbound,
                identity,
                profile,
                content,
                runtime_config,
                visual_observation=visual_observation,
                visual_failure=visual_failure,
                planned_turn=planned_turn,
            )
            gateway = (
                cast(OneBotToolGateway, sender)
                if callable(getattr(sender, "call_api", None))
                else None
            )
            runtime = ToolRuntime(
                inbound=inbound,
                gateway=gateway,
                allow_generic_onebot=(
                    not autonomous
                    and not visual_input_present
                    and inbound.sender.user_id in self._settings.superusers
                ),
                allow_admin_actions=(
                    not autonomous
                    and not visual_input_present
                    and inbound.sender.user_id in self._settings.superusers
                ),
                allow_automation=(not autonomous and not visual_input_present),
                conversation_key=identity.key,
                trigger_message_id=inbound.message_id,
                source_display_requested=source_display_requested,
                actor_user_id=inbound.sender.user_id,
                actor_is_superuser=inbound.sender.user_id in self._settings.superusers,
                current_group_id=inbound.group_id,
                mentioned_user_ids=inbound.mentioned_user_ids,
                runtime_config=runtime_config,
                origin=(TurnOrigin.AUTONOMOUS_GROUP if autonomous else TurnOrigin.USER_MESSAGE),
                tool_mode=(
                    planned_turn.plan.tool_mode if planned_turn is not None else ToolMode.INHERIT
                ),
                tool_groups=(
                    frozenset(group.value for group in planned_turn.plan.tool_selection.groups)
                    if planned_turn is not None
                    else frozenset(group.value for group in ToolGroup)
                ),
                turn_token=turn_token,
                reply_effects=[],
                voice_tool_authorized=(
                    planned_turn is not None
                    and planned_turn.plan.voice.agent_tool is VoiceAgentToolPolicy.REQUIRED
                ),
            )
            if turn_token is not None:
                async with self._turn_coordinator.track(turn_token, "generation"):
                    response_text = await self._run_agent(identity.key, messages, runtime)
            else:
                response_text = await self._run_agent(identity.key, messages, runtime)
            sources = await self._web_sources.for_trigger(
                conversation_key=identity.key,
                trigger_message_id=inbound.message_id,
            )
            response_text = self._source_renderer.sanitize_model_text(response_text, sources)
            if not response_text and not runtime.reply_effects:
                response_text = "已完成联网查询，但模型没有生成可用的正文。"
            rendered = clean_model_output(
                response_text,
                max_characters=self._settings.max_output_characters,
            )
            effects = runtime.reply_effects or []
            emoji_effects = [effect for effect in effects if isinstance(effect, PendingReplyEffect)]
            queued_voice = next(
                (effect for effect in effects if isinstance(effect, PendingVoiceReplyEffect)),
                None,
            )
            if (
                planned_turn is not None
                and planned_turn.plan.emoji.mode is not EmojiReplyMode.NONE
                and not emoji_effects
            ):
                emoji_effects.append(
                    PendingReplyEffect(
                        mode=planned_turn.plan.emoji.mode,
                        placement=planned_turn.plan.emoji.placement,
                        goal=planned_turn.plan.emoji.goal,
                        emotion=planned_turn.plan.emoji.emotion,
                        source="planner",
                    )
                )
            prepared_effects: list[tuple[PendingReplyEffect, OutboundMessage]] = []
            if self._emoji_effects is not None:
                for effect in emoji_effects[: runtime_config.emoji.max_effects_per_reply]:
                    prepared = await self._emoji_effects.prepare(
                        effect,
                        inbound=inbound,
                        response_text=rendered,
                        runtime=runtime_config,
                    )
                    if prepared is not None:
                        prepared_effects.append((effect, prepared))
            prepared_voice: PreparedVoiceReply | None = None
            if (
                planned_turn is not None
                and turn_token is not None
                and self._speech_effects is not None
            ):
                prepared_voice = await self._speech_effects.prepare(
                    inbound=inbound,
                    response_text=rendered,
                    runtime=runtime_config,
                    token=turn_token,
                    mode=planned_turn.plan.voice.mode,
                    style_hint=(
                        queued_voice.style_hint
                        if queued_voice is not None
                        else planned_turn.plan.voice.style_hint
                    ),
                    language_hint=(
                        queued_voice.language_hint
                        if queued_voice is not None
                        else planned_turn.plan.voice.language.value
                    ),
                    profile_id=queued_voice.profile_id if queued_voice is not None else "",
                )
            if not rendered and not prepared_effects and prepared_voice is None:
                # A failed optional media effect must never turn a planned reply
                # into silence. AgentRunner normally prevents this, while this
                # guard also covers selectors/synthesizers that decline an effect.
                rendered = "我在，刚才没有生成可用的回复。"
            if planned_turn is not None and turn_token is not None:
                if source_display_requested:
                    source_text = self._source_renderer.render(
                        sources,
                        maximum=runtime_config.web.extract_max_results,
                    )
                    if source_text:
                        rendered = clean_model_output(
                            f"{rendered}\n\n{source_text}",
                            max_characters=self._settings.max_output_characters,
                        )

                async def record_chunk(message: OutboundMessage, result: Any) -> None:
                    await self._record_outbound_message(inbound, message, result)
                    if message.media and self._emoji_effects is not None:
                        await self._emoji_effects.record_success(
                            message,
                            inbound=inbound,
                            source="reply_effect",
                        )
                    if message.media and self._speech_effects is not None:
                        await self._speech_effects.record_success(message)

                async def record_failure(message: OutboundMessage, _error: Exception) -> None:
                    if message.media and self._emoji_effects is not None:
                        await self._emoji_effects.record_failure(
                            message,
                            source="reply_effect",
                        )
                    if message.media and self._speech_effects is not None:
                        await self._speech_effects.record_failure(message)

                before = tuple(
                    message
                    for effect, message in prepared_effects
                    if effect.placement is EmojiPlacement.BEFORE_TEXT
                )
                after = tuple(
                    message
                    for effect, message in prepared_effects
                    if effect.placement is not EmojiPlacement.BEFORE_TEXT
                )
                if prepared_voice is not None:
                    after = (*after, prepared_voice.message)
                suppress_text = bool(prepared_effects) and any(
                    effect.mode is EmojiReplyMode.EMOJI_ONLY
                    or effect.placement is EmojiPlacement.ONLY
                    for effect, _message in prepared_effects
                )
                if prepared_voice is not None:
                    suppress_text = suppress_text or prepared_voice.suppress_text

                sequence = await self._reply_sequence.send(
                    text=rendered,
                    plan=planned_turn.plan,
                    runtime=runtime_config,
                    token=turn_token,
                    sender=sender,
                    record_outbound=record_chunk,
                    record_failure=record_failure,
                    before_messages=before,
                    after_messages=after,
                    suppress_text=suppress_text,
                )
                return sequence.sent_messages
            chunks = self._render_chunks(rendered, runtime_config) if rendered else ()
            legacy_messages = [
                message
                for effect, message in prepared_effects
                if effect.placement is EmojiPlacement.BEFORE_TEXT
            ]
            suppress_text = bool(prepared_effects) and any(
                effect.mode is EmojiReplyMode.EMOJI_ONLY or effect.placement is EmojiPlacement.ONLY
                for effect, _message in prepared_effects
            )
            if not suppress_text:
                legacy_messages.extend(OutboundMessage(text=chunk) for chunk in chunks)
            legacy_messages.extend(
                message
                for effect, message in prepared_effects
                if effect.placement is not EmojiPlacement.BEFORE_TEXT
            )
            for index, outbound in enumerate(legacy_messages):
                if len(legacy_messages) > 1 and index > 0:
                    delay = random.uniform(
                        runtime_config.reply.delay_min_seconds,
                        runtime_config.reply.delay_max_seconds,
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
                try:
                    result = await sender.send(outbound)
                except Exception:
                    if outbound.media and self._emoji_effects is not None:
                        await self._emoji_effects.record_failure(
                            outbound,
                            source="reply_effect",
                        )
                    raise
                await self._record_outbound_message(inbound, outbound, result)
                if outbound.media and self._emoji_effects is not None:
                    await self._emoji_effects.record_success(
                        outbound,
                        inbound=inbound,
                        source="reply_effect",
                    )
            sent_count = len(legacy_messages)
            if source_display_requested:
                source_text = self._source_renderer.render(
                    sources,
                    maximum=runtime_config.web.extract_max_results,
                )
                if source_text:
                    result = await sender.send(OutboundMessage(text=source_text))
                    await self._record_outbound(inbound, source_text, result)
                    sent_count += 1
            return sent_count

    async def _build_messages(
        self,
        inbound: InboundMessage,
        identity: ConversationIdentity,
        profile: UserProfileSnapshot,
        content: str,
        runtime: RuntimeConfigSnapshot,
        *,
        visual_observation: VisualObservation | None = None,
        visual_failure: bool = False,
        planned_turn: PlannedTurn | None = None,
    ) -> tuple[ChatMessage, ...]:
        context = await self._context_assembler.assemble(
            inbound=inbound,
            identity=identity,
            profile=profile,
            content=content,
            runtime=runtime,
        )
        return self._prompt_composer.compose(
            inbound=inbound,
            context=context,
            runtime=runtime,
            visual_observation=visual_observation,
            visual_failure=visual_failure,
            planned_turn=planned_turn,
        )

    async def _run_agent(
        self,
        conversation_key: str,
        initial_messages: tuple[ChatMessage, ...],
        runtime: ToolRuntime,
    ) -> str:
        config = runtime.runtime_config
        if config is None:
            config = await self._runtime_config.snapshot(
                user_id=runtime.inbound.sender.user_id,
                group_id=runtime.inbound.group_id,
            )
            runtime = replace(runtime, runtime_config=config)
        current_time = await self._time.current(runtime.inbound.sender.user_id)
        result = await self._agent_runner.run(
            initial_messages,
            AgentRuntime(
                origin=runtime.origin,
                actor_user_id=runtime.actor_user_id,
                actor_is_superuser=runtime.actor_is_superuser,
                delegated_authority=None,
                conversation_key=conversation_key,
                current_group_id=runtime.current_group_id,
                bot_user_id=runtime.inbound.bot_user_id,
                gateway=runtime.gateway,
                runtime_config=config,
                current_time=current_time,
                allowed_capabilities=frozenset(),
                max_tool_calls=config.agent.max_tool_calls,
                max_model_requests=config.agent.max_model_requests,
            ),
            _ChatAgentBackend(self, runtime),
        )
        return result.text

    @staticmethod
    def _decode_tool_result(value: str) -> dict[str, object]:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return {"ok": False, "error": "invalid_tool_result"}
        return payload if isinstance(payload, dict) else {"ok": False}

    @staticmethod
    def _admin_failure_text(result: dict[str, object]) -> str:
        detail = str(result.get("detail") or result.get("error") or "未知错误")
        return f"操作未完成：{detail}"

    def _render_chunks(
        self,
        rendered: str,
        runtime: RuntimeConfigSnapshot,
    ) -> tuple[str, ...]:
        messages: tuple[str, ...] = (rendered,)
        if runtime.reply.daily_split_enabled:
            messages = split_daily_chat_sentences(
                rendered,
                max_characters=runtime.reply.daily_split_max_characters,
                max_messages=runtime.reply.daily_split_max_messages,
            )
        return tuple(
            chunk
            for message in messages
            for chunk in split_qq_message(
                message,
                limit=runtime.reply.max_qq_message_chars,
            )
        )

    async def _record_outbound(
        self,
        inbound: InboundMessage,
        content: str,
        send_result: Any,
        *,
        reply_to_message_id: str | None = None,
    ) -> None:
        await self._record_outbound_message(
            inbound,
            OutboundMessage(text=content, reply_to_message_id=reply_to_message_id),
            send_result,
        )

    async def _record_outbound_message(
        self,
        inbound: InboundMessage,
        message: OutboundMessage,
        send_result: Any,
    ) -> None:
        """Persist text and ledger-safe media metadata after confirmed delivery."""

        message_id: str | None = None
        if isinstance(send_result, str | int):
            message_id = str(send_result)
        elif isinstance(send_result, dict):
            raw_id = send_result.get("message_id") or send_result.get("id")
            if raw_id is not None:
                message_id = str(raw_id)
        platform_message_id = message_id or f"out-{uuid.uuid4()}"
        media_segments = tuple(self._ledger_media_segment(media) for media in message.media)
        content = self._ledger_content(message)
        await self._ledger.append(
            bot_user_id=inbound.bot_user_id or "unknown-bot",
            platform_message_id=platform_message_id,
            scope_type=inbound.scope_type,
            sender_user_id=inbound.bot_user_id or "unknown-bot",
            direction="outbound",
            content=content,
            segments=(
                *(
                    ({"type": "reply", "data": {"id": message.reply_to_message_id}},)
                    if message.reply_to_message_id
                    else ()
                ),
                *(({"type": "text", "data": {"text": message.text}},) if message.text else ()),
                *media_segments,
            ),
            group_id=inbound.group_id,
            private_peer_user_id=(
                inbound.sender.user_id if inbound.scope_type is ScopeType.PRIVATE else None
            ),
            reply_to_message_id=message.reply_to_message_id,
            sender_is_bot=True,
        )
        await publish_notification(
            self._event_publisher,
            EventName.REPLY_SENT,
            {
                "trigger_message_id": inbound.message_id,
                "platform_message_id": platform_message_id,
                "scope_type": inbound.scope_type.value,
                "character_count": len(content),
                "recorded": True,
            },
        )

    async def record_confirmed_outbound(
        self,
        inbound: InboundMessage,
        message: OutboundMessage,
        send_result: Any,
    ) -> None:
        """Share the same ledger boundary with deterministic media commands."""

        await self._record_outbound_message(inbound, message, send_result)

    @staticmethod
    def _ledger_content(message: OutboundMessage) -> str:
        """Return only user-visible or spoken content, never internal voice metadata."""

        spoken_text = next((media.spoken_text for media in message.media if media.spoken_text), "")
        visual_descriptions = " ".join(
            f"[表情：{media.summary or '图片表情'}]"
            for media in message.media
            if media.kind is not AttachmentKind.AUDIO
        )
        return message.text or spoken_text or visual_descriptions

    @staticmethod
    def _ledger_media_segment(media: OutboundMedia) -> dict[str, object]:
        if media.kind is AttachmentKind.AUDIO:
            return {
                "type": "record",
                "data": {
                    "summary": media.summary[:2000],
                    "mime_type": media.mime_type,
                    "duration_milliseconds": media.duration_milliseconds,
                    "profile_id": media.voice_profile_id or "",
                    "reference_key": media.voice_reference_key or "",
                    "target_language": media.voice_language or "",
                    "generation_id": media.generation_id,
                },
            }
        return {
            "type": "image",
            "data": {
                "emoji_id": media.emoji_id or "",
                "summary": media.summary[:2000],
                "mime_type": media.mime_type,
                "animated": media.animated,
            },
        }
