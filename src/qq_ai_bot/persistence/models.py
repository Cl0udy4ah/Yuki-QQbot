"""SQLAlchemy schema for the person-centric event ledger and memories."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative metadata root."""


class PersonModel(Base):
    """One human identity permanently keyed by a QQ number string."""

    __tablename__ = "people"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    nickname: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_bot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    aliases: Mapped[list[PersonAliasModel]] = relationship(
        back_populates="person",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    memberships: Mapped[list[MembershipModel]] = relationship(
        back_populates="person",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    memories: Mapped[list[PersonMemoryModel]] = relationship(
        back_populates="person",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    preferences: Mapped[list[PersonPreferenceModel]] = relationship(
        back_populates="person",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    relationship_state: Mapped[PersonRelationshipModel | None] = relationship(
        back_populates="person",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    time_setting: Mapped[PersonTimeSettingModel | None] = relationship(
        back_populates="person",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    automations: Mapped[list[AutomationModel]] = relationship(
        back_populates="creator",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class PersonAliasModel(Base):
    """A nickname or group card previously observed for one QQ identity."""

    __tablename__ = "person_aliases"
    __table_args__ = (
        UniqueConstraint("user_id", "group_scope", "alias", name="uq_person_alias_scope"),
        Index("ix_person_aliases_user_last_seen", "user_id", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), nullable=False
    )
    group_scope: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    alias: Mapped[str] = mapped_column(String(128), nullable=False)
    alias_type: Mapped[str] = mapped_column(String(24), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    person: Mapped[PersonModel] = relationship(back_populates="aliases")


class GroupModel(Base):
    """A QQ group and its observation/participation settings."""

    __tablename__ = "groups"

    group_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    require_mention: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    autonomous_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    memberships: Mapped[list[MembershipModel]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    memories: Mapped[list[GroupMemoryModel]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class MembershipModel(Base):
    """One person as known inside one exact group."""

    __tablename__ = "memberships"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[str] = mapped_column(
        ForeignKey("groups.group_id", ondelete="CASCADE"), primary_key=True
    )
    group_card: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    person: Mapped[PersonModel] = relationship(back_populates="memberships")
    group: Mapped[GroupModel] = relationship(back_populates="memberships")


class ChatEventModel(Base):
    """An immutable inbound or outbound QQ message in the permanent ledger."""

    __tablename__ = "chat_events"
    __table_args__ = (
        UniqueConstraint(
            "bot_user_id",
            "platform_message_id",
            name="uq_chat_events_bot_platform_message",
        ),
        Index("ix_chat_events_scope_time", "scope_type", "occurred_at"),
        Index("ix_chat_events_group_time", "group_id", "occurred_at"),
        Index("ix_chat_events_sender_time", "sender_user_id", "occurred_at"),
        Index("ix_chat_events_private_peer_time", "private_peer_user_id", "occurred_at"),
        Index("ix_chat_events_automation", "automation_id", "automation_run_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    platform_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    group_id: Mapped[str | None] = mapped_column(
        ForeignKey("groups.group_id", ondelete="CASCADE"), nullable=True
    )
    private_peer_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), nullable=True
    )
    sender_user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    visual_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    segments_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    reply_to_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    origin: Mapped[str] = mapped_column(String(32), nullable=False, default="user_message")
    automation_id: Mapped[int | None] = mapped_column(
        ForeignKey("automations.id", ondelete="SET NULL"), nullable=True
    )
    automation_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("automation_runs.id", ondelete="SET NULL"), nullable=True
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MediaAnalysisModel(Base):
    """A short-lived structured visual observation without source image data."""

    __tablename__ = "media_analyses"
    __table_args__ = (
        CheckConstraint(
            "analysis_mode IN ('general', 'meme', 'ocr', 'question')",
            name="ck_media_analyses_analysis_mode",
        ),
        CheckConstraint(
            "segment_index >= 0",
            name="ck_media_analyses_segment_index",
        ),
        UniqueConstraint(
            "content_hash",
            "analysis_mode",
            "question_hash",
            "model",
            "prompt_version",
            name="uq_media_analyses_cache_key",
        ),
        Index("ix_media_analyses_content_hash", "content_hash"),
        Index(
            "ix_media_analyses_source_event_segment",
            "source_event_id",
            "segment_index",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_events.id", ondelete="CASCADE"), nullable=True
    )
    segment_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    analysis_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    question_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    observation_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EmojiDescriptionModel(Base):
    """A durable description indexed by a stable QQ emoji identity."""

    __tablename__ = "emoji_descriptions"
    __table_args__ = (
        CheckConstraint(
            "analysis_mode IN ('general', 'meme', 'ocr', 'question')",
            name="ck_emoji_descriptions_analysis_mode",
        ),
        CheckConstraint("hit_count >= 0", name="ck_emoji_descriptions_hit_count"),
        UniqueConstraint(
            "emoji_key",
            "analysis_mode",
            "question_hash",
            "model",
            "prompt_version",
            name="uq_emoji_descriptions_lookup",
        ),
        Index("ix_emoji_descriptions_key", "emoji_key"),
        Index("ix_emoji_descriptions_last_used", "last_used_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    emoji_key: Mapped[str] = mapped_column(String(255), nullable=False)
    analysis_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    question_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    observation_json: Mapped[str] = mapped_column(Text, nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PersonMemoryModel(Base):
    """A durable cross-scope memory about one QQ identity."""

    __tablename__ = "person_memories"
    __table_args__ = (
        UniqueConstraint("user_id", "memory_key", name="uq_person_memories_user_key"),
        Index("ix_person_memories_user_updated", "user_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), nullable=False
    )
    memory_key: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="fact")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False, default="automatic")
    source_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_events.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    person: Mapped[PersonModel] = relationship(back_populates="memories")


class GroupMemoryModel(Base):
    """A durable shared memory about one group."""

    __tablename__ = "group_memories"
    __table_args__ = (
        UniqueConstraint("group_id", "memory_key", name="uq_group_memories_group_key"),
        Index("ix_group_memories_group_updated", "group_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(
        ForeignKey("groups.group_id", ondelete="CASCADE"), nullable=False
    )
    subject_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), nullable=True
    )
    memory_key: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="fact")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False, default="automatic")
    source_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_events.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    group: Mapped[GroupModel] = relationship(back_populates="memories")


class PersonGroupMemoryModel(Base):
    """A durable memory about one person's identity inside one group."""

    __tablename__ = "person_group_memories"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "group_id"],
            ["memberships.user_id", "memberships.group_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "user_id",
            "group_id",
            "memory_key",
            name="uq_person_group_memories_scope_key",
        ),
        Index(
            "ix_person_group_memories_scope_updated",
            "user_id",
            "group_id",
            "updated_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    group_id: Mapped[str] = mapped_column(String(64), nullable=False)
    memory_key: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="fact")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False, default="automatic")
    source_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_events.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PersonPreferenceModel(Base):
    """An explicit or inferred interaction preference for one person."""

    __tablename__ = "person_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", "preference_key", name="uq_person_preferences_user_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), nullable=False
    )
    preference_key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False, default="explicit")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    person: Mapped[PersonModel] = relationship(back_populates="preferences")


class MemoryJobModel(Base):
    """A persistent background job that derives memories from one event."""

    __tablename__ = "memory_jobs"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_memory_jobs_event"),
        Index("ix_memory_jobs_status_next", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("chat_events.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)


class PersonRelationshipModel(Base):
    """Persistent affection and trust scores for one QQ identity."""

    __tablename__ = "person_relationships"
    __table_args__ = (
        CheckConstraint(
            "affection_score >= 0 AND affection_score <= 100",
            name="ck_person_relationships_affection_range",
        ),
        CheckConstraint(
            "trust_score >= 0 AND trust_score <= 100",
            name="ck_person_relationships_trust_range",
        ),
    )

    user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), primary_key=True
    )
    affection_score: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    trust_score: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_automatic_change_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    person: Mapped[PersonModel] = relationship(back_populates="relationship_state")


class RelationshipEventModel(Base):
    """Auditable automatic or administrator-issued relationship change."""

    __tablename__ = "relationship_events"
    __table_args__ = (
        CheckConstraint(
            "change_type IN ('automatic', 'manual')",
            name="ck_relationship_events_change_type",
        ),
        Index("ix_relationship_events_user_created", "user_id", "created_at"),
        Index(
            "uq_relationship_events_automatic_source",
            "source_event_id",
            unique=True,
            sqlite_where=text("source_event_id IS NOT NULL AND change_type = 'automatic'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), nullable=False
    )
    source_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("chat_events.id", ondelete="SET NULL"), nullable=True
    )
    actor_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    change_type: Mapped[str] = mapped_column(String(16), nullable=False)
    affection_before: Mapped[int] = mapped_column(Integer, nullable=False)
    affection_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    affection_after: Mapped[int] = mapped_column(Integer, nullable=False)
    trust_before: Mapped[int] = mapped_column(Integer, nullable=False)
    trust_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    trust_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RelationshipJobModel(Base):
    """Persistent restart-safe relationship evaluation job."""

    __tablename__ = "relationship_jobs"
    __table_args__ = (
        UniqueConstraint("trigger_event_id", name="uq_relationship_jobs_trigger_event"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_relationship_jobs_status",
        ),
        Index("ix_relationship_jobs_status_next", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger_event_id: Mapped[int] = mapped_column(
        ForeignKey("chat_events.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), nullable=False
    )
    conversation_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ContextResetModel(Base):
    """A context cutoff that preserves the permanent event ledger."""

    __tablename__ = "context_resets"

    context_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), nullable=False
    )
    group_id: Mapped[str | None] = mapped_column(
        ForeignKey("groups.group_id", ondelete="CASCADE"), nullable=True
    )
    reset_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProcessedEventModel(Base):
    """Durable idempotency record for incoming OneBot events."""

    __tablename__ = "processed_events"
    __table_args__ = (Index("ix_processed_events_expires_at", "expires_at"),)

    event_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentActionModel(Base):
    """A bounded audit entry for a model-issued OneBot action."""

    __tablename__ = "agent_actions"
    __table_args__ = (Index("ix_agent_actions_actor_created", "actor_user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RuntimeConfigOverrideModel(Base):
    """One validated runtime configuration override at an exact scope."""

    __tablename__ = "runtime_config_overrides"
    __table_args__ = (
        UniqueConstraint(
            "config_key",
            "scope_type",
            "scope_id",
            name="uq_runtime_config_override_scope",
        ),
        CheckConstraint(
            "scope_type IN ('global', 'group', 'user')",
            name="ck_runtime_config_overrides_scope_type",
        ),
        CheckConstraint(
            "(scope_type = 'global' AND scope_id = '') OR "
            "(scope_type IN ('group', 'user') AND scope_id <> '')",
            name="ck_runtime_config_overrides_scope_id",
        ),
        CheckConstraint(
            "value_type IN ('string', 'integer', 'number', 'boolean', 'enum')",
            name="ck_runtime_config_overrides_value_type",
        ),
        CheckConstraint(
            "apply_mode IN ('hot', 'future_only', 'restart_required')",
            name="ck_runtime_config_overrides_apply_mode",
        ),
        CheckConstraint("version >= 1", name="ck_runtime_config_overrides_version"),
        Index(
            "ix_runtime_config_overrides_scope_key",
            "scope_type",
            "scope_id",
            "config_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    config_key: Mapped[str] = mapped_column(String(128), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    value_type: Mapped[str] = mapped_column(String(16), nullable=False)
    apply_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False)


class AdminOperationEventModel(Base):
    """A redacted, append-only audit event for administrator capabilities."""

    __tablename__ = "admin_operation_events"
    __table_args__ = (
        CheckConstraint(
            "duration_seconds >= 0",
            name="ck_admin_operation_events_duration",
        ),
        Index(
            "ix_admin_operation_events_actor_created",
            "actor_user_id",
            "created_at",
        ),
        Index(
            "ix_admin_operation_events_target_created",
            "target_type",
            "target_id",
            "created_at",
        ),
        Index(
            "ix_admin_operation_events_capability_created",
            "capability",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_message_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    conversation_key: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    before_json: Mapped[str] = mapped_column(Text, nullable=False, default="null")
    after_json: Mapped[str] = mapped_column(Text, nullable=False, default="null")
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WebSearchRunModel(Base):
    """One successful Agent web tool call in an isolated conversation."""

    __tablename__ = "web_search_runs"
    __table_args__ = (
        Index(
            "ix_web_search_runs_conversation_created",
            "conversation_key",
            "created_at",
        ),
        Index(
            "ix_web_search_runs_conversation_trigger",
            "conversation_key",
            "trigger_message_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_key: Mapped[str] = mapped_column(String(255), nullable=False)
    trigger_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    partial_failure: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    sources: Mapped[list[WebSearchSourceModel]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class WebSearchSourceModel(Base):
    """Display-safe metadata for one real source used by a web tool."""

    __tablename__ = "web_search_sources"
    __table_args__ = (
        UniqueConstraint("run_id", "url", name="uq_web_search_sources_run_url"),
        Index("ix_web_search_sources_run_ordinal", "run_id", "ordinal"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("web_search_runs.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    snippet: Mapped[str] = mapped_column(Text, nullable=False, default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    run: Mapped[WebSearchRunModel] = relationship(back_populates="sources")


class PersonTimeSettingModel(Base):
    """The preferred IANA timezone for one globally identified person."""

    __tablename__ = "person_time_settings"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), primary_key=True
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    person: Mapped[PersonModel] = relationship(back_populates="time_setting")


class AutomationModel(Base):
    """A validated, persistent declaration of one scheduled automation."""

    __tablename__ = "automations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'paused', 'completed', 'cancelled', 'failed', 'blocked')",
            name="ck_automations_status",
        ),
        CheckConstraint("run_count >= 0", name="ck_automations_run_count"),
        CheckConstraint("consecutive_failures >= 0", name="ck_automations_consecutive_failures"),
        Index("ix_automations_status_next", "status", "next_run_at"),
        Index("ix_automations_creator_updated", "creator_user_id", "updated_at"),
        Index("ix_automations_claim", "claimed_until", "claimed_by"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    creator_user_id: Mapped[str] = mapped_column(
        ForeignKey("people.user_id", ondelete="CASCADE"), nullable=False
    )
    bot_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    schedule_json: Mapped[str] = mapped_column(Text, nullable=False)
    script_json: Mapped[str] = mapped_column(Text, nullable=False)
    script_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    required_capabilities_json: Mapped[str] = mapped_column(Text, nullable=False)
    authority_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_from_message_id: Mapped[str] = mapped_column(String(128), nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_runs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    misfire_grace_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=1800)
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    creator: Mapped[PersonModel] = relationship(back_populates="automations")
    versions: Mapped[list[AutomationVersionModel]] = relationship(
        back_populates="automation",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    runs: Mapped[list[AutomationRunModel]] = relationship(
        back_populates="automation",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class AutomationVersionModel(Base):
    """An immutable script revision for an automation."""

    __tablename__ = "automation_versions"
    __table_args__ = (
        UniqueConstraint("automation_id", "version", name="uq_automation_versions_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    automation_id: Mapped[int] = mapped_column(
        ForeignKey("automations.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    script_json: Mapped[str] = mapped_column(Text, nullable=False)
    script_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    automation: Mapped[AutomationModel] = relationship(back_populates="versions")


class AutomationRunModel(Base):
    """One idempotent scheduled or manual execution attempt."""

    __tablename__ = "automation_runs"
    __table_args__ = (
        UniqueConstraint("automation_id", "scheduled_for", name="uq_automation_runs_scheduled_for"),
        UniqueConstraint("idempotency_key", name="uq_automation_runs_idempotency_key"),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'skipped', 'missed', "
            "'uncertain', 'blocked')",
            name="ck_automation_runs_status",
        ),
        Index("ix_automation_runs_automation_created", "automation_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    automation_id: Mapped[int] = mapped_column(
        ForeignKey("automations.id", ondelete="CASCADE"), nullable=False
    )
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actual_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    steps_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    messages_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_summary_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    automation: Mapped[AutomationModel] = relationship(back_populates="runs")
    step_runs: Mapped[list[AutomationStepRunModel]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class AutomationStepRunModel(Base):
    """A redacted audit record for one executed DSL step."""

    __tablename__ = "automation_step_runs"
    __table_args__ = (Index("ix_automation_step_runs_run_step", "run_id", "step_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("automation_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[str] = mapped_column(String(32), nullable=False)
    capability: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    input_summary_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    output_summary_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)

    run: Mapped[AutomationRunModel] = relationship(back_populates="step_runs")


class MCPServerStateModel(Base):
    """Secret-free lifecycle metadata for one configured MCP server."""

    __tablename__ = "mcp_server_states"

    server_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    transport: Mapped[str] = mapped_column(String(32), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    server_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    server_version: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    server_instructions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MCPToolCacheModel(Base):
    """Cached MCP tools/list metadata; never stores credentials or results."""

    __tablename__ = "mcp_tool_cache"
    __table_args__ = (
        UniqueConstraint("server_id", "remote_tool_name", name="uq_mcp_tool_cache_server_tool"),
        Index("ix_mcp_tool_cache_server", "server_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    server_id: Mapped[str] = mapped_column(String(64), nullable=False)
    remote_tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    compact_description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    input_schema_json: Mapped[str] = mapped_column(Text, nullable=False)
    output_schema_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    annotations_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    metadata_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ToolArtifactModel(Base):
    """Handle metadata for an oversized tool result stored outside SQLite."""

    __tablename__ = "tool_artifacts"
    __table_args__ = (Index("ix_tool_artifacts_expires", "expires_at"),)

    handle_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(512), nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ToolInvocationModel(Base):
    """Content-free audit metrics for all provider-neutral tool executions."""

    __tablename__ = "tool_invocations"
    __table_args__ = (
        CheckConstraint("latency_seconds >= 0", name="ck_tool_invocations_latency"),
        CheckConstraint("result_size >= 0", name="ck_tool_invocations_result_size"),
        Index("ix_tool_invocations_provider_created", "provider_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    latency_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    result_size: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_created: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# Source-compatibility aliases for integrations that only inspect the old profile types.
UserProfileModel = PersonModel
UserGroupProfileModel = MembershipModel
