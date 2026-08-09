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
        if self._artifacts is not None:
            artifact_id = await self._artifacts.write_artifact(
                provider_id=result.provider_id,
                tool_name=result.tool_name,
                content=text,
                media_type="application/json",
                retention_seconds=self._artifact_retention_seconds,
            )
        summary_item_limit = self._item_limit or 5
        summary = _bounded_payload(payload, item_limit=summary_item_limit)
        summary["truncated"] = True
        summary["original_characters"] = len(text)
        if artifact_id:
            summary["artifact_handle"] = artifact_id
        rendered = _json_dumps(summary)
        if self._max_characters is not None and len(rendered) > self._max_characters:
            rendered = _minimal_summary(
                result,
                original_characters=len(text),
                artifact_id=artifact_id,
                important_fields=_important_fields(
                    payload,
                    item_limit=summary_item_limit,
                ),
                max_characters=self._max_characters,
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


def _bounded_payload(value: object, *, item_limit: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"summary": str(value)[:1000]}

    def bounded(item: object, *, compact_record: bool = False) -> object:
        if isinstance(item, list):
            selected = item[:item_limit]
            return {
                "total_items": len(item),
                "shown_items": len(selected),
                "items": [bounded(value, compact_record=True) for value in selected],
            }
        if isinstance(item, tuple):
            return bounded(list(item))
        if isinstance(item, dict):
            pairs = _compact_record_pairs(item) if compact_record else item.items()
            return {
                str(key): bounded(
                    child,
                    compact_record=compact_record and isinstance(child, dict),
                )
                for key, child in pairs
            }
        if isinstance(item, str) and len(item) > 1000:
            return f"{item[:1000]}…"
        return item

    return {str(key): bounded(item) for key, item in value.items()}


def _compact_record_pairs(value: dict[object, object]) -> list[tuple[object, object]]:
    ranked: list[tuple[int, int, object, object]] = []
    for index, (raw_key, item) in enumerate(value.items()):
        rank = _record_field_rank(str(raw_key), item)
        if rank is not None:
            ranked.append((rank, index, raw_key, item))
    if sum(rank < 20 for rank, _index, _key, _item in ranked) >= 3:
        ranked = [field for field in ranked if field[0] < 20]
    ranked.sort(key=lambda field: (field[0], field[1]))
    return [(key, item) for _rank, _index, key, item in ranked[:12]]


def _record_field_rank(value: str, item: object) -> int | None:
    if item in (None, "", [], {}, "[redacted]"):
        return None
    key = value.casefold().replace("-", "_")
    if key in {"id", "title", "name", "label", "status", "state"}:
        return 0
    if key in {"category", "feed", "source", "author", "owner", "disabled", "starred"}:
        return 1
    if "url" in key or key in {"uri", "link", "href"}:
        return 2
    if key in {
        "published",
        "published_at",
        "updated",
        "updated_at",
        "created",
        "created_at",
        "changed_at",
        "started_at",
        "finished_at",
        "completed_at",
    }:
        return 3
    if key in {"language", "reading_time", "tags"}:
        return 4
    if key.endswith("_id") or key.endswith("id"):
        return 5
    if any(part in key for part in ("price", "amount", "currency", "count", "total")):
        return 6
    if key in {"content", "body", "html", "raw", "text", "blob", "data", "icon"}:
        return None
    if isinstance(item, (str, int, float, bool)):
        return 20
    return None


def _important_fields(value: object, *, item_limit: int) -> dict[str, object]:
    """Project counts and common record identity fields before lossy truncation."""

    collected: list[tuple[int, str, object]] = []

    def visit(item: object, path: tuple[str, ...]) -> None:
        if len(collected) >= 256:
            return
        if isinstance(item, dict):
            for raw_key, child in item.items():
                key = str(raw_key)
                visit(child, (*path, key))
            return
        if isinstance(item, (list, tuple)):
            collection_path = ".".join(path) or "result"
            collected.append((0, f"{collection_path}.total_items", len(item)))
            collected.append(
                (0, f"{collection_path}.shown_items", min(len(item), item_limit))
            )
            for index, child in enumerate(item[:item_limit]):
                visit(child, (*path, str(index)))
            return
        if not path:
            return
        priority = _field_priority(path[-1])
        if priority is None or item in (None, "", [], {}):
            return
        field_path = ".".join(path)
        collected.append(
            (priority, field_path, item[:500] if isinstance(item, str) else item)
        )

    visit(value, ())
    collected.sort(key=lambda field: (field[0], field[1]))
    return {path: item for _priority, path, item in collected[:128]}


def _field_priority(value: str) -> int | None:
    key = value.casefold().replace("-", "_")
    if key in {
        "total",
        "total_count",
        "count",
        "item_count",
        "page_count",
        "has_more",
        "next_cursor",
    }:
        return 0
    if "error" in key or key in {"ok", "status", "state", "disabled", "starred"}:
        return 1
    if key in {
        "title",
        "name",
        "label",
        "summary",
        "description",
        "author",
        "language",
    }:
        return 2
    if key == "id" or key.endswith("_id") or key.endswith("id"):
        return 3
    if "url" in key or key in {"uri", "link", "href"}:
        return 4
    if key.endswith(("_at", "_time", "_date")) or key in {
        "published",
        "updated",
        "created",
    }:
        return 5
    if any(part in key for part in ("price", "amount", "currency")):
        return 6
    return None


def _minimal_summary(
    result: ToolExecutionResult,
    *,
    original_characters: int,
    artifact_id: str | None,
    important_fields: dict[str, object],
    max_characters: int,
) -> str:
    base: dict[str, object] = {
        "ok": result.ok,
        "truncated": True,
        "original_characters": original_characters,
    }
    if result.tool_name:
        base["tool_name"] = result.tool_name
    if artifact_id:
        base["artifact_handle"] = artifact_id
    if result.public_message:
        base["public_message"] = result.public_message[:500]

    selected: dict[str, object] = {}
    for path, item in important_fields.items():
        candidate = {**base, "important_fields": {**selected, path: item}}
        if len(_json_dumps(candidate)) > max_characters:
            continue
        selected[path] = item
    if selected:
        base["important_fields"] = selected
    rendered = _json_dumps(base)
    if len(rendered) <= max_characters:
        return rendered

    tiny = {"ok": result.ok, "truncated": True}
    tiny_rendered = _json_dumps(tiny)
    return tiny_rendered if len(tiny_rendered) <= max_characters else "{}"


def _json_dumps(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )
