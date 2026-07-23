"""Resolve mentioned members without exposing platform ids to the model."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.exc import SQLAlchemyError

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.domain.memories import MentionedMember
from qq_ai_bot.domain.messages import InboundMessage
from qq_ai_bot.persistence.repositories import UserProfileRepository
from qq_ai_bot.services.user_profiles import sanitize_profile_name

logger = logging.getLogger(__name__)

_MAX_MENTIONED_MEMBERS = 5


@dataclass(frozen=True, slots=True)
class GroupMemberResolution:
    """One transport-resolved group member."""

    user_id: str
    nickname: str = ""
    group_card: str = ""


class GroupMemberResolver(Protocol):
    """Resolve only members explicitly mentioned in the current message."""

    async def resolve_members(
        self,
        message: InboundMessage,
    ) -> tuple[GroupMemberResolution, ...]:
        """Return platform metadata for explicitly mentioned members."""


class GroupMemberService:
    """Create safe group-local identities for explicitly mentioned members."""

    def __init__(self, profiles: UserProfileRepository) -> None:
        self._profiles = profiles

    async def resolve(
        self,
        message: InboundMessage,
        resolver: GroupMemberResolver | None,
    ) -> tuple[MentionedMember, ...]:
        """Resolve bounded mentions after trigger policy has accepted the message."""

        if (
            message.scope_type is not ScopeType.GROUP
            or message.group_id is None
            or not message.mentioned_user_ids
        ):
            return ()

        requested_ids = message.mentioned_user_ids[:_MAX_MENTIONED_MEMBERS]
        resolved_by_id: dict[str, GroupMemberResolution] = {}
        if resolver is not None:
            try:
                resolutions = await resolver.resolve_members(message)
                resolved_by_id = {item.user_id: item for item in resolutions}
            except Exception as exc:
                logger.warning(
                    "group_member_resolve_failed exception_category=%s",
                    type(exc).__name__,
                )

        members: list[MentionedMember] = []
        for index, user_id in enumerate(requested_ids, start=1):
            resolution = resolved_by_id.get(user_id)
            display_name = ""
            if resolution is not None:
                display_name = sanitize_profile_name(resolution.group_card or resolution.nickname)
            if not display_name:
                try:
                    stored = await self._profiles.get(
                        user_id=user_id,
                        group_id=message.group_id,
                    )
                    if stored is not None:
                        # A stored global nickname may originate in private chat; only reuse
                        # the exact group's card here.
                        display_name = stored.group_card
                except (OSError, RuntimeError, SQLAlchemyError) as exc:
                    logger.warning(
                        "mentioned_member_profile_read_failed exception_category=%s",
                        type(exc).__name__,
                    )
            reference = hashlib.sha256(f"{message.group_id}\x1f{user_id}".encode()).hexdigest()[:12]
            members.append(
                MentionedMember(
                    placeholder=f"提及成员{index}",
                    reference=f"member_{reference}",
                    display_name=display_name or f"被提及成员{index}",
                )
            )
        return tuple(members)
