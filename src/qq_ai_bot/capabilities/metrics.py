"""Low-cardinality in-process Tool Kernel metrics."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass(slots=True)
class ToolKernelMetrics:
    invocations: Counter[tuple[str, str, bool]] = field(default_factory=Counter)
    refreshes: Counter[tuple[str, bool]] = field(default_factory=Counter)
    selected_for_turn: Counter[tuple[str, str]] = field(default_factory=Counter)
    schema_tokens: Counter[tuple[str, str]] = field(default_factory=Counter)

    def record_invocation(self, provider_id: str, tool_name: str, ok: bool) -> None:
        self.invocations[(provider_id, tool_name, ok)] += 1

    def record_refresh(self, provider_id: str, ok: bool) -> None:
        self.refreshes[(provider_id, ok)] += 1

    def record_selection(self, provider_id: str, tool_name: str, schema_tokens: int) -> None:
        self.selected_for_turn[(provider_id, tool_name)] += 1
        self.schema_tokens[(provider_id, tool_name)] += max(0, schema_tokens)
