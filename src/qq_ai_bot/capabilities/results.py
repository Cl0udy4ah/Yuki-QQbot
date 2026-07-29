"""Uniform capability result returned before model-side serialization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    ok: bool
    data: Any = None
    error: str | None = None
    public_message: str | None = None
    retryable: bool = False
    mutation_committed: bool = False
