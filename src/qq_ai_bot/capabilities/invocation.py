"""Capability result serialization shared by model tool backends."""

from __future__ import annotations

import json

from qq_ai_bot.capabilities.results import CapabilityResult


def serialize_capability_result(result: CapabilityResult) -> str:
    return json.dumps(
        {
            "ok": result.ok,
            "data": result.data,
            "error": result.error,
            "public_message": result.public_message,
            "retryable": result.retryable,
            "mutation_committed": result.mutation_committed,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
