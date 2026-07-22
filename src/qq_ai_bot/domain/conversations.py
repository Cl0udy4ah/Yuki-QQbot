"""Conversation identities and isolation rules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ScopeType(StrEnum):
    """Supported conversation scopes."""

    PRIVATE = "private"
    GROUP = "group"


class ConversationMode(StrEnum):
    """Group conversation sharing mode."""

    PER_USER = "per_user"
    SHARED = "shared"


@dataclass(frozen=True, slots=True)
class ConversationIdentity:
    """Stable identity used to isolate persisted chat history."""

    key: str
    scope_type: ScopeType
    user_id: str
    group_id: str | None = None
    mode: ConversationMode = ConversationMode.PER_USER

    @classmethod
    def private(cls, user_id: str) -> ConversationIdentity:
        """Create a private conversation identity."""

        return cls(key=f"private:{user_id}", scope_type=ScopeType.PRIVATE, user_id=user_id)

    @classmethod
    def group(
        cls,
        group_id: str,
        user_id: str,
        mode: ConversationMode = ConversationMode.PER_USER,
    ) -> ConversationIdentity:
        """Create an isolated or shared group conversation identity."""

        if mode is ConversationMode.SHARED:
            key = f"group:{group_id}:shared"
            owner_user_id = ""
        else:
            key = f"group:{group_id}:user:{user_id}"
            owner_user_id = user_id
        return cls(
            key=key,
            scope_type=ScopeType.GROUP,
            group_id=group_id,
            user_id=owner_user_id,
            mode=mode,
        )
