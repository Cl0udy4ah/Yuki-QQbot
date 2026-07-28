"""Stable repository imports grouped behind domain-specific implementations."""

from qq_ai_bot.persistence.event_repository import (
    AgentActionRepository,
    ConversationRepository,
    EventLedgerRepository,
    ProcessedEventRepository,
)
from qq_ai_bot.persistence.media_repository import (
    EmojiDescriptionRepository,
    MediaAnalysisRepository,
)
from qq_ai_bot.persistence.memory_repository import MemoryJobRepository, MemoryRepository
from qq_ai_bot.persistence.people_repository import (
    GroupSettingsRepository,
    PeopleRepository,
    PrivateUserSettingsRepository,
    UserProfileRepository,
)
from qq_ai_bot.persistence.relationship_repository import (
    RelationshipJobRepository,
    RelationshipRepository,
)
from qq_ai_bot.persistence.repository_records import (
    EmojiDescriptionRecord,
    EventRecord,
    GroupSetting,
    MediaAnalysisRecord,
    MemoryJobRecord,
    MemoryRecord,
    PreferenceRecord,
    PrivateUserSetting,
    RelationshipEventRecord,
    RelationshipJobRecord,
)
from qq_ai_bot.persistence.web_repository import WebSearchSourceRepository

__all__ = [
    "AgentActionRepository",
    "ConversationRepository",
    "EmojiDescriptionRecord",
    "EmojiDescriptionRepository",
    "EventLedgerRepository",
    "EventRecord",
    "GroupSetting",
    "GroupSettingsRepository",
    "MediaAnalysisRecord",
    "MediaAnalysisRepository",
    "MemoryJobRecord",
    "MemoryJobRepository",
    "MemoryRecord",
    "MemoryRepository",
    "PeopleRepository",
    "PreferenceRecord",
    "PrivateUserSetting",
    "PrivateUserSettingsRepository",
    "ProcessedEventRepository",
    "RelationshipEventRecord",
    "RelationshipJobRecord",
    "RelationshipJobRepository",
    "RelationshipRepository",
    "UserProfileRepository",
    "WebSearchSourceRepository",
]
