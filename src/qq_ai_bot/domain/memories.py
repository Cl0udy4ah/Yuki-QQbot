"""Group-scoped shared memory projections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class GroupMemory:
    """One extracted fact available only inside its source group."""

    id: int
    group_id: str
    memory_key: str
    content: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class GroupMemoryUpsert:
    """A validated fact update produced by the memory extractor."""

    memory_key: str
    content: str


@dataclass(frozen=True, slots=True)
class MentionedMember:
    """Opaque, group-local identity for one member mentioned in a message."""

    placeholder: str
    reference: str
    display_name: str
