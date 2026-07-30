"""Behavior and contract tests for the bundled NetEase music card plugin."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from yuki_plugin_sdk.context import MCPFacade
from yuki_plugin_sdk.models import JsonValue
from yuki_plugin_sdk.plugin import Plugin
from yuki_plugin_sdk.registrar import ToolRegistration
from yuki_plugin_sdk.results import PluginResult
from yuki_plugin_sdk.testing import FakePluginContext, run_plugin_contract_tests

PLUGIN_ROOT = (
    Path(__file__).parents[2] / "plugins" / "io.github.yuanyeyoutao.netease-music-card"
)


class RecordingRegistrar:
    def __init__(self) -> None:
        self.tools: list[ToolRegistration] = []

    def register_tool(self, registration: ToolRegistration) -> None:
        self.tools.append(registration)


class StubMCP:
    def __init__(self, responses: list[PluginResult]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, Mapping[str, JsonValue]]] = []

    async def status(self) -> Mapping[str, JsonValue]:
        return {"enabled": True}

    async def list_servers(self) -> tuple[Mapping[str, JsonValue], ...]:
        return ()

    async def search_tools(self, query: str) -> tuple[Mapping[str, JsonValue], ...]:
        return ()

    async def call(
        self,
        server_id: str,
        tool_name: str,
        arguments: Mapping[str, JsonValue],
    ) -> PluginResult:
        self.calls.append((server_id, tool_name, arguments))
        return self.responses.pop(0)


def _load_module() -> ModuleType:
    path = PLUGIN_ROOT / "netease_music_card_plugin.py"
    spec = importlib.util.spec_from_file_location("netease_music_card_plugin_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _mcp_result(items: list[dict[str, Any]], *, field: str = "items") -> PluginResult:
    return PluginResult(
        data={
            "result": {
                "ok": True,
                "data": {field: cast(JsonValue, items)},
                "provider_id": "mcp.netease_music",
                "tool_name": "music_search",
            }
        }
    )


def _song(song_id: str, title: str, artist: str) -> dict[str, Any]:
    return {"id": song_id, "title": title, "artists": [{"id": "9", "name": artist}]}


async def _running_tool(
    responses: list[PluginResult],
) -> tuple[Plugin, ToolRegistration, FakePluginContext, StubMCP]:
    plugin = cast(Plugin, _load_module().NetEaseMusicCardPlugin())
    registrar = RecordingRegistrar()
    await plugin.register(cast(Any, registrar))
    mcp = StubMCP(responses)
    context = FakePluginContext(
        "io.github.yuanyeyoutao.netease-music-card",
        mcp=cast(MCPFacade, mcp),
    )
    await plugin.start(context)
    return plugin, registrar.tools[0], context, mcp


async def test_plugin_passes_host_contract() -> None:
    report = await run_plugin_contract_tests(PLUGIN_ROOT, yuki_version="2.1.1")
    assert report.passed is True


async def test_artist_disambiguates_search_and_sends_native_card() -> None:
    plugin, tool, context, mcp = await _running_tool(
        [
            _mcp_result(
                [
                    _song("100", "晴天", "翻唱歌手"),
                    _song("200", "晴天", "周杰伦"),
                ]
            )
        ]
    )

    arguments = tool.input_model.model_validate({"query": "晴天", "artist": "周杰伦"})
    result = await tool.handler(arguments)

    assert result.status == "sent"
    assert result.selected is not None and result.selected.song_id == "200"
    assert context.onebot.music_cards == [("netease", "200")]
    assert mcp.calls[0][0:2] == ("netease_music", "music_search")
    await plugin.stop()


async def test_ambiguous_search_returns_choices_without_sending() -> None:
    plugin, tool, context, _mcp = await _running_tool(
        [_mcp_result([_song("100", "晴天", "歌手甲"), _song("200", "晴天", "歌手乙")])]
    )

    result = await tool.handler(tool.input_model.model_validate({"query": "晴天"}))

    assert result.status == "selection_required"
    assert [item.song_id for item in result.candidates] == ["100", "200"]
    assert context.onebot.music_cards == []
    await plugin.stop()


async def test_selected_song_id_is_verified_before_sending() -> None:
    plugin, tool, context, mcp = await _running_tool(
        [_mcp_result([_song("200", "晴天", "周杰伦")], field="songs")]
    )

    result = await tool.handler(tool.input_model.model_validate({"song_id": "200"}))

    assert result.status == "sent"
    assert context.onebot.music_cards == [("netease", "200")]
    assert mcp.calls == [
        (
            "netease_music",
            "get_songs",
            {"song_ids": ["200"], "detail_level": "summary"},
        )
    ]
    await plugin.stop()


async def test_mcp_failure_never_sends_a_card() -> None:
    plugin, tool, context, _mcp = await _running_tool(
        [PluginResult(ok=False, error_code="mcp_tool_error", detail="上游接口暂不可用")]
    )

    result = await tool.handler(tool.input_model.model_validate({"query": "晴天"}))

    assert isinstance(result, PluginResult)
    assert result.ok is False
    assert result.error_code == "music_card.mcp_failed"
    assert context.onebot.music_cards == []
    await plugin.stop()
