"""Closed vocabularies for the Memory V2 domain."""

from enum import StrEnum


class MemoryScopeType(StrEnum):
    PERSON = "person"
    PERSON_GROUP = "person_group"
    GROUP = "group"


class MemoryKind(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    EPISODE = "episode"


class MemorySourceType(StrEnum):
    AUTOMATIC = "automatic"
    EXPLICIT = "explicit"
    REBUILD = "rebuild"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    INVALIDATED = "invalidated"


class MemoryEvidenceRelation(StrEnum):
    SELF_STATEMENT = "self_statement"
    EXPLICIT_COMMAND = "explicit_command"
    CORRECTION = "correction"
    REBUILD = "rebuild"


class MemoryJobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
