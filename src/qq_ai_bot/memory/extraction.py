"""Narrow structured contract for one-event Memory V2 extraction."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.enums import MemoryKind, MemoryScopeType, MemorySourceType


class _ExtractionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PrimaryEvent(_ExtractionModel):
    scope_type: ScopeType
    content: str
    occurred_at: datetime


class AvailableSubject(_ExtractionModel):
    subject_ref: str
    allowed_scopes: tuple[MemoryScopeType, ...]


class MemoryExtractionInput(_ExtractionModel):
    primary_event: PrimaryEvent
    available_subjects: tuple[AvailableSubject, ...]
    conversation_context: tuple[str, ...] = ()


class MemoryClaim(_ExtractionModel):
    subject_ref: str = Field(min_length=1, max_length=32)
    scope_type: MemoryScopeType
    kind: MemoryKind = MemoryKind.FACT
    memory_key: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=4000)
    importance: int = Field(default=3, ge=1, le=5)
    confidence: float = Field(default=0.8, ge=0, le=1)
    source_type: MemorySourceType = MemorySourceType.AUTOMATIC


class MemoryExtractionOutput(_ExtractionModel):
    claims: tuple[MemoryClaim, ...] = ()
