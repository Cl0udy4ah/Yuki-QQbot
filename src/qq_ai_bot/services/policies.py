"""Pure permission, trigger, and command parsing policies."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from qq_ai_bot.config import Settings
from qq_ai_bot.domain.conversations import ConversationMode, ScopeType
from qq_ai_bot.domain.messages import InboundMessage


class CommandName(StrEnum):
    """Supported `/ai` commands."""

    HELP = "help"
    NEW = "new"
    STATUS = "status"
    STOP = "stop"
    ON = "on"
    OFF = "off"
    PING = "ping"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Result of deciding whether and how to handle a message."""

    should_respond: bool
    content: str = ""
    command: CommandName | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class EffectiveGroupPolicy:
    """Effective group state after database overrides."""

    enabled: bool
    require_mention: bool = True
    conversation_mode: ConversationMode = ConversationMode.PER_USER


def _command_and_content(text: str, ai_prefix: str) -> tuple[CommandName | None, str, bool]:
    stripped = text.strip()
    lower = stripped.casefold()
    triggered = False
    remainder = stripped
    if lower == "/ai" or lower.startswith("/ai "):
        triggered = True
        remainder = stripped[3:].strip()
        if not remainder:
            return CommandName.HELP, "", True
        first = remainder.split(maxsplit=1)[0].casefold()
        try:
            return CommandName(first), "", True
        except ValueError:
            return None, remainder, True
    if ai_prefix and (stripped == ai_prefix or stripped.startswith(f"{ai_prefix} ")):
        triggered = True
        remainder = stripped[len(ai_prefix) :].strip()
    return None, remainder, triggered


def evaluate_message(
    message: InboundMessage,
    settings: Settings,
    *,
    group_policy: EffectiveGroupPolicy | None = None,
) -> PolicyDecision:
    """Apply self/bot, allowlist, group, mention, prefix, and command rules."""

    if message.is_self_message or message.sender.is_bot:
        return PolicyDecision(False, reason="bot_message")

    command, content, prefix_triggered = _command_and_content(message.text, settings.ai_prefix)
    is_superuser = message.sender.user_id in settings.superusers

    if message.scope_type is ScopeType.PRIVATE:
        if message.sender.user_id not in settings.allowed_private_users:
            return PolicyDecision(False, reason="private_not_allowed")
        return PolicyDecision(True, content=content, command=command, reason="private_allowed")

    if message.group_id is None:
        return PolicyDecision(False, reason="missing_group_id")
    policy = group_policy or EffectiveGroupPolicy(message.group_id in settings.enabled_groups)

    if command in {CommandName.ON, CommandName.OFF} and is_superuser:
        return PolicyDecision(True, command=command, reason="superuser_group_command")
    if not policy.enabled:
        return PolicyDecision(False, reason="group_disabled")
    if message.mentions_bot or prefix_triggered:
        return PolicyDecision(
            True,
            content=content,
            command=command,
            reason="group_triggered",
        )
    return PolicyDecision(False, reason="group_not_triggered")


def command_requires_superuser(command: CommandName) -> bool:
    """Return whether a command mutates group-wide state."""

    return command in {CommandName.ON, CommandName.OFF}
