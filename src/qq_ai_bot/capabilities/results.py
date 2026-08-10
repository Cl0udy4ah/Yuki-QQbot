"""Uniform tool results and model-facing result budgeting."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from qq_ai_bot.capabilities.models import CapabilityDescriptor, CapabilityEffect


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    ok: bool
    data: Any = None
    error: str | None = None
    public_message: str | None = None
    retryable: bool = False
    mutation_committed: bool | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """Provider-neutral result returned by every ToolBinding."""

    ok: bool
    data: Any = None
    content: tuple[dict[str, Any], ...] = ()
    error_code: str | None = None
    public_message: str | None = None
    retryable: bool = False
    mutation_committed: bool | None = None
    provider_id: str = ""
    tool_name: str = ""
    metadata: dict[str, Any] | None = None

    def model_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        return {key: value for key, value in payload.items() if value not in (None, (), "")}


class ToolArtifactWriter(Protocol):
    def configure_retention(self, retention_seconds: int) -> None: ...

    async def write_artifact(
        self,
        *,
        provider_id: str,
        tool_name: str,
        content: str,
        media_type: str,
        retention_seconds: int | None = None,
    ) -> str: ...

    async def read(
        self,
        handle_id: str,
        *,
        offset: int = 0,
        limit: int = 8000,
        query: str = "",
    ) -> dict[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class BudgetedToolResult:
    text: str
    artifact_id: str | None = None
    truncated: bool = False


class ToolResultBudgeter:
    """Bound all tool output without losing the complete value when artifacts are enabled."""

    def __init__(
        self,
        *,
        max_characters: int | None,
        item_limit: int | None = None,
        artifacts: ToolArtifactWriter | None = None,
        artifact_retention_seconds: int | None = None,
    ) -> None:
        if max_characters is not None and max_characters <= 0:
            raise ValueError("tool result budget must be positive or null")
        if item_limit is not None and item_limit <= 0:
            raise ValueError("tool result item limit must be positive or null")
        if artifact_retention_seconds is not None and artifact_retention_seconds <= 0:
            raise ValueError("artifact retention must be positive or null")
        self._max_characters = max_characters
        self._item_limit = item_limit
        self._artifacts = artifacts
        self._artifact_retention_seconds = artifact_retention_seconds

    async def render(self, result: ToolExecutionResult) -> BudgetedToolResult:
        payload = result.model_payload()
        text = json.dumps(payload, ensure_ascii=False, default=str)
        item_overflow = (
            self._item_limit is not None and _largest_collection(payload) > self._item_limit
        )
        character_overflow = self._max_characters is not None and len(text) > self._max_characters
        if not item_overflow and not character_overflow:
            return BudgetedToolResult(text=text)
        artifact_id: str | None = None
        recursive_artifact_read = (
            result.provider_id == "artifacts"
            and result.tool_name == "read_tool_artifact"
        )
        if self._artifacts is not None and not recursive_artifact_read:
            artifact_id = await self._artifacts.write_artifact(
                provider_id=result.provider_id,
                tool_name=result.tool_name,
                content=text,
                media_type="application/json",
                retention_seconds=self._artifact_retention_seconds,
            )
        summary = _bounded_payload(payload, item_limit=self._item_limit)
        important = _important_fields(payload)
        if important:
            summary["important_fields"] = important
        summary["truncated"] = True
        summary["original_characters"] = len(text)
        if artifact_id:
            summary["artifact_handle"] = artifact_id
        rendered = json.dumps(summary, ensure_ascii=False, default=str)
        if self._max_characters is not None and len(rendered) > self._max_characters:
            minimal = {
                "ok": result.ok,
                "truncated": True,
                "original_characters": len(text),
                "artifact_handle": artifact_id,
                "public_message": result.public_message,
                "important_fields": important or None,
            }
            rendered = json.dumps(
                {key: value for key, value in minimal.items() if value is not None},
                ensure_ascii=False,
            )
        return BudgetedToolResult(
            text=rendered,
            artifact_id=artifact_id,
            truncated=True,
        )


def normalize_legacy_result(
    value: object,
    *,
    provider_id: str,
    tool_name: str,
) -> ToolExecutionResult:
    """Convert old string/dict tool results into the kernel result contract."""

    payload: object = value
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return ToolExecutionResult(
                ok=True,
                data=value,
                provider_id=provider_id,
                tool_name=tool_name,
            )
    if isinstance(payload, dict):
        raw = dict(payload)
        ok = bool(raw.pop("ok", True))
        error = raw.pop("error_code", raw.pop("error", None))
        public = raw.pop("public_message", raw.pop("detail", None))
        committed_value = raw.pop("mutation_committed", None)
        committed = None if committed_value is None else bool(committed_value)
        retryable = bool(raw.pop("retryable", False))
        data = raw.pop("data", raw if raw else None)
        return ToolExecutionResult(
            ok=ok,
            data=data,
            error_code=str(error) if error is not None else None,
            public_message=str(public) if public is not None else None,
            retryable=retryable,
            mutation_committed=False if not ok else committed,
            provider_id=provider_id,
            tool_name=tool_name,
        )
    return ToolExecutionResult(
        ok=True,
        data=payload,
        provider_id=provider_id,
        tool_name=tool_name,
    )


def resolve_mutation_commit(
    result: ToolExecutionResult,
    descriptor: CapabilityDescriptor,
) -> bool:
    """Resolve one provider-neutral commit state from result and capability effect."""

    if not result.ok:
        return False
    if result.mutation_committed is not None:
        return result.mutation_committed
    if descriptor.effect in {
        CapabilityEffect.READ_STATE,
        CapabilityEffect.EXTERNAL_READ,
        CapabilityEffect.REPLY_EFFECT,
    }:
        return False
    if descriptor.effect in {
        CapabilityEffect.WRITE_STATE,
        CapabilityEffect.PLATFORM_MUTATE,
        CapabilityEffect.PLATFORM_SEND,
    }:
        return True
    return False


def _largest_collection(value: object) -> int:
    if isinstance(value, dict):
        return max((len(value), *(_largest_collection(item) for item in value.values())))
    if isinstance(value, (list, tuple)):
        return max((len(value), *(_largest_collection(item) for item in value)))
    return 0


def _bounded_payload(value: object, *, item_limit: int | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"summary": str(value)[:1000]}
    limit = item_limit

    def bounded(item: object) -> object:
        if isinstance(item, list):
            selected = item if limit is None else item[:limit]
            return {
                "total_items": len(item),
                "items": [bounded(value) for value in selected],
            }
        if isinstance(item, tuple):
            return bounded(list(item))
        if isinstance(item, dict):
            pairs = list(item.items())
            selected_pairs = pairs if limit is None else pairs[:limit]
            return {str(key): bounded(child) for key, child in selected_pairs}
        if isinstance(item, str) and len(item) > 1000:
            return f"{item[:1000]}…"
        return item

    return {str(key): bounded(item) for key, item in value.items()}


def _important_fields(value: object) -> dict[str, object]:
    """Project identifiers, status, errors, and URLs before lossy truncation."""

    important: dict[str, object] = {}

    def visit(item: object, path: tuple[str, ...]) -> None:
        if len(important) >= 64:
            return
        if isinstance(item, dict):
            for raw_key, child in item.items():
                key = str(raw_key)
                visit(child, (*path, key))
            return
        if isinstance(item, (list, tuple)):
            for index, child in enumerate(item[:20]):
                visit(child, (*path, str(index)))
            return
        if not path:
            return
        key = path[-1].casefold()
        is_important = (
            "url" in key or key == "id" or key.endswith("id") or "status" in key or "error" in key
        )
        if not is_important:
            return
        field_path = ".".join(path)
        important[field_path] = item[:2000] if isinstance(item, str) else item

    visit(value, ())
    return important
