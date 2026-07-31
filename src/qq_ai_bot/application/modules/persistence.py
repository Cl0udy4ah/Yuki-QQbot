"""Persistence module with an explicit immutable repository bundle."""

from __future__ import annotations

from dataclasses import dataclass

from qq_ai_bot.admin.config_service import RuntimeConfigService
from qq_ai_bot.application.lifecycle import LifecycleRegistry
from qq_ai_bot.config import Settings
from qq_ai_bot.emoji.repository import EmojiRepository
from qq_ai_bot.memory.repository import MemoryFactRepository, MemoryJobRepository
from qq_ai_bot.memory.service import MemoryFactService
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.persistence.repositories import (
    AgentActionRepository,
    ConversationRepository,
    EmojiDescriptionRepository,
    EventLedgerRepository,
    GroupSettingsRepository,
    MediaAnalysisRepository,
    PrivateUserSettingsRepository,
    ProcessedEventRepository,
    RelationshipJobRepository,
    RelationshipRepository,
    UserProfileRepository,
    WebSearchSourceRepository,
)
from qq_ai_bot.planner.repository import PlannerRepository
from qq_ai_bot.speech.preference_repository import VoicePreferenceRepository
from qq_ai_bot.speech.repository import SpeechGenerationRepository, VoiceProfileRepository


@dataclass(frozen=True, slots=True)
class PersistenceBundle:
    database: Database
    runtime_config: RuntimeConfigService
    conversations: ConversationRepository
    groups: GroupSettingsRepository
    private_users: PrivateUserSettingsRepository
    people: UserProfileRepository
    processed_events: ProcessedEventRepository
    ledger: EventLedgerRepository
    memories: MemoryFactService
    memory_jobs: MemoryJobRepository
    agent_actions: AgentActionRepository
    web_sources: WebSearchSourceRepository
    media_analyses: MediaAnalysisRepository
    emoji_descriptions: EmojiDescriptionRepository
    emoji_repository: EmojiRepository
    planner_runs: PlannerRepository
    voice_preferences: VoicePreferenceRepository
    voice_profiles: VoiceProfileRepository
    speech_generations: SpeechGenerationRepository
    relationships: RelationshipRepository
    relationship_jobs: RelationshipJobRepository


class PersistenceModule:
    def __init__(
        self,
        settings: Settings,
        *,
        lifecycle: LifecycleRegistry,
        database: Database | None = None,
        runtime_config: RuntimeConfigService | None = None,
    ) -> None:
        self._settings = settings
        self._lifecycle = lifecycle
        self._database = database
        self._runtime_config = runtime_config

    def build(self) -> PersistenceBundle:
        settings = self._settings
        database = self._database or Database(settings.database_url)
        self._lifecycle.register("database", close=database.close)
        runtime_config = self._runtime_config or RuntimeConfigService(
            settings=settings,
            database=database,
        )
        initial = {
            "initial_affection": settings.relationship_initial_affection,
            "initial_trust": settings.relationship_initial_trust,
        }
        memory_repository = MemoryFactRepository(database)
        return PersistenceBundle(
            database=database,
            runtime_config=runtime_config,
            conversations=ConversationRepository(database),
            groups=GroupSettingsRepository(database),
            private_users=PrivateUserSettingsRepository(database, **initial),
            people=UserProfileRepository(database, **initial),
            processed_events=ProcessedEventRepository(database),
            ledger=EventLedgerRepository(database),
            memories=MemoryFactService(memory_repository),
            memory_jobs=MemoryJobRepository(database),
            agent_actions=AgentActionRepository(database),
            web_sources=WebSearchSourceRepository(database),
            media_analyses=MediaAnalysisRepository(database),
            emoji_descriptions=EmojiDescriptionRepository(database),
            emoji_repository=EmojiRepository(database),
            planner_runs=PlannerRepository(database),
            voice_preferences=VoicePreferenceRepository(database),
            voice_profiles=VoiceProfileRepository(database),
            speech_generations=SpeechGenerationRepository(database),
            relationships=RelationshipRepository(
                database,
                **initial,
                trust_cap_offset=settings.trust_affection_cap_offset,
                max_affection_auto_delta=settings.affection_max_auto_delta,
                max_trust_auto_delta=settings.trust_max_auto_delta,
            ),
            relationship_jobs=RelationshipJobRepository(
                database,
                max_attempts=settings.relationship_max_attempts,
            ),
        )
