"""Lossless, bounded structural reads for oversized tool artifacts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qq_ai_bot.capabilities.results import ToolExecutionResult, ToolResultBudgeter
from qq_ai_bot.mcp.repository import ToolArtifactRepository
from qq_ai_bot.persistence.database import Database


def _menu_payload() -> dict[str, object]:
    return {
        "data": {
            "meals": {
                f"meal-{index:03d}": {
                    "name": "o麦金四件套随心选" if index == 73 else f"套餐 {index}",
                    "code": f"meal-{index:03d}",
                    "price": 28 + index / 10,
                    "description": "菜单说明" * 30,
                }
                for index in range(104)
            },
            "categories": [{"name": f"分类 {index}"} for index in range(15)],
        },
        "traceId": "trace-only",
    }


async def _write_menu(
    database: Database,
    tmp_path: Path,
    *,
    max_characters: int = 800,
) -> tuple[ToolArtifactRepository, str, dict[str, object]]:
    artifacts = ToolArtifactRepository(database, tmp_path / "artifacts", retention_seconds=60)
    rendered = await ToolResultBudgeter(
        max_characters=max_characters,
        artifacts=artifacts,
    ).render(
        ToolExecutionResult(
            ok=True,
            data=_menu_payload(),
            provider_id="mcp.mcd",
            tool_name="query-meals",
        )
    )
    assert rendered.artifact_id is not None
    return artifacts, rendered.artifact_id, json.loads(rendered.text)


@pytest.mark.asyncio
async def test_oversized_json_returns_structure_manifest_without_lossy_preview(
    database: Database,
    tmp_path: Path,
) -> None:
    _artifacts, _handle, manifest = await _write_menu(database, tmp_path)

    assert manifest["mode"] == "json"
    assert manifest["logical_root"] == "data"
    assert manifest["root_type"] == "object"
    assert manifest["children"] == {
        "data": "object[2]",
        "traceId": "string[10]",
    }
    assert manifest["available_operations"] == ["inspect", "get", "search"]
    assert "important_fields" not in manifest


@pytest.mark.asyncio
async def test_inspect_and_get_page_dictionary_keys_not_characters(
    database: Database,
    tmp_path: Path,
) -> None:
    artifacts, handle, _manifest = await _write_menu(database, tmp_path)

    inspected = await artifacts.read(
        handle,
        operation="inspect",
        path=("data", "meals"),
        offset=70,
        limit=5,
    )
    assert inspected is not None
    assert inspected["type"] == "object"
    assert inspected["total_children"] == 104
    children = inspected["children"]
    assert isinstance(children, list)
    assert [child["key"] for child in children] == [
        "meal-070",
        "meal-071",
        "meal-072",
        "meal-073",
        "meal-074",
    ]
    assert inspected["next_offset"] == 75

    meal = await artifacts.read(
        handle,
        operation="get",
        path=("data", "meals", "meal-073"),
        limit=10,
        max_characters=2000,
    )
    assert meal is not None
    value = meal["value"]
    assert isinstance(value, dict)
    assert value["name"] == "o麦金四件套随心选"
    assert value["code"] == "meal-073"


@pytest.mark.asyncio
async def test_search_returns_complete_nearest_object_and_deduplicates_fields(
    database: Database,
    tmp_path: Path,
) -> None:
    artifacts, handle, _manifest = await _write_menu(database, tmp_path)

    searched = await artifacts.read(
        handle,
        operation="search",
        path=("data", "meals"),
        query="meal-073",
        limit=5,
        max_characters=2500,
    )
    assert searched is not None
    matches = searched["matches"]
    assert isinstance(matches, list)
    assert len(matches) == 1
    assert matches[0]["path"] == ["data", "meals", "meal-073"]
    assert tuple(matches[0]["matched_path"]) in {
        ("data", "meals", "meal-073"),
        ("data", "meals", "meal-073", "code"),
    }
    assert matches[0]["value"]["name"] == "o麦金四件套随心选"

    missing = await artifacts.read(
        handle,
        operation="search",
        query="并不存在的套餐",
        limit=5,
    )
    assert missing is not None
    assert missing["matches"] == []
    assert missing["next_offset"] is None


@pytest.mark.asyncio
async def test_large_single_value_returns_structure_instead_of_mid_json_truncation(
    database: Database,
    tmp_path: Path,
) -> None:
    artifacts = ToolArtifactRepository(database, tmp_path / "artifacts", retention_seconds=60)
    handle = await artifacts.write_artifact(
        provider_id="test",
        tool_name="huge",
        content=json.dumps(
            {
                "ok": True,
                "data": {
                    "record": {
                        "name": "huge",
                        "description": "x" * 20_000,
                    }
                },
                "provider_id": "test",
                "tool_name": "huge",
            }
        ),
        media_type="application/json",
    )

    result = await artifacts.read(
        handle,
        operation="get",
        path=("record",),
        max_characters=1000,
    )
    assert result is not None
    assert result["error_code"] == "artifact_value_too_large"
    assert result["path"] == ["record"]
    assert result["value_shape"] == {"type": "object", "child_count": 2}
    assert "value" not in result


@pytest.mark.asyncio
async def test_legacy_text_mode_and_invalid_json_structural_error_remain_explicit(
    database: Database,
    tmp_path: Path,
) -> None:
    artifacts = ToolArtifactRepository(database, tmp_path / "artifacts", retention_seconds=60)
    handle = await artifacts.write_artifact(
        provider_id="test",
        tool_name="plain",
        content="before target after",
        media_type="text/plain",
    )

    page = await artifacts.read(handle, query="target", limit=6)
    assert page is not None
    assert page["mode"] == "text"
    assert page["content"] == "target"

    structured = await artifacts.read(handle, operation="inspect")
    assert structured is not None
    assert structured["error_code"] == "artifact_not_json"
