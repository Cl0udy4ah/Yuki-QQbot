"""Behavior and contract tests for the bundled NetEase music card plugin."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from yuki_plugin_sdk.context import MCPFacade
from yuki_plugin_sdk.models import CurrentMessage, JsonValue
from yuki_plugin_sdk.plugin import Plugin
from yuki_plugin_sdk.registrar import ToolRegistration
from yuki_plugin_sdk.results import PluginResult
from yuki_plugin_sdk.testing import FakePluginContext, run_plugin_contract_tests

PLUGIN_ROOT = Path(__file__).parents[2] / "plugins" / "io.github.yuanyeyoutao.netease-music-card"


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


def _share_output(result: object, output_model: type[Any]) -> Any:
    if isinstance(result, PluginResult):
        return output_model.model_validate(result.data["result"])
    return result


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


async def _running_named_tool(
    tool_name: str,
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
    tool = next(item for item in registrar.tools if item.metadata.name == tool_name)
    return plugin, tool, context, mcp


def _album(
    album_id: str,
    title: str,
    artist: str,
    *,
    cover_url: str = "https://example.com/album.jpg",
) -> dict[str, Any]:
    return {
        "id": album_id,
        "name": title,
        "artists": [{"id": "9", "name": artist}],
        "cover_url": cover_url,
        "publish_date": "2018-03-21",
        "canonical_url": f"https://music.163.com/#/album?id={album_id}",
    }


def _album_detail_result(
    album: dict[str, Any],
    *,
    tracks: list[dict[str, Any]],
) -> PluginResult:
    return PluginResult(
        data={
            "result": {
                "ok": True,
                "data": cast(
                    JsonValue,
                    {
                        **album,
                        "size": len(tracks),
                        "tracks": tracks,
                        "track_page": {
                            "page": 1,
                            "page_size": 50,
                            "total": len(tracks),
                            "has_more": False,
                        },
                    },
                ),
                "provider_id": "mcp.netease_music",
                "tool_name": "get_album",
            }
        }
    )


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
    output = _share_output(result, tool.output_model)

    assert output.status == "sent"
    assert output.selected is not None and output.selected.song_id == "200"
    assert context.onebot.music_cards == [("netease", "200")]
    assert mcp.calls[0][0:2] == ("netease_music", "music_search")
    assert mcp.calls[0][2]["query"] == "晴天 周杰伦"
    await plugin.stop()


async def test_ambiguous_search_returns_choices_without_sending() -> None:
    plugin, tool, context, _mcp = await _running_tool(
        [_mcp_result([_song("100", "晴天", "歌手甲"), _song("200", "晴天", "歌手乙")])]
    )

    result = await tool.handler(tool.input_model.model_validate({"query": "晴天"}))
    output = _share_output(result, tool.output_model)

    assert output.status == "selection_required"
    assert [item.song_id for item in output.candidates] == ["100", "200"]
    assert isinstance(result, PluginResult) and result.mutation_committed is False
    assert context.onebot.music_cards == []
    await plugin.stop()


async def test_selected_song_id_is_verified_before_sending() -> None:
    plugin, tool, context, mcp = await _running_tool(
        [_mcp_result([_song("200", "晴天", "周杰伦")], field="songs")]
    )

    result = await tool.handler(tool.input_model.model_validate({"song_id": "200"}))
    output = _share_output(result, tool.output_model)

    assert output.status == "sent"
    assert context.onebot.music_cards == [("netease", "200")]
    assert mcp.calls == [
        (
            "netease_music",
            "get_songs",
            {"song_ids": ["200"], "detail_level": "summary"},
        )
    ]
    await plugin.stop()


async def test_artist_only_query_selects_top_song_and_sends() -> None:
    plugin, tool, context, _mcp = await _running_tool(
        [
            _mcp_result(
                [
                    _song("523567", "夢のつづき", "玉置浩二"),
                    _song("524331", "行かないで", "玉置浩二"),
                ]
            )
        ]
    )

    result = await tool.handler(tool.input_model.model_validate({"query": "玉置浩二"}))
    output = _share_output(result, tool.output_model)

    assert output.status == "sent"
    assert output.selected is not None and output.selected.song_id == "523567"
    assert context.onebot.music_cards == [("netease", "523567")]
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


async def test_album_search_fetches_tracks_and_sends_current_scene_card() -> None:
    album = _album("242154493", "中国有弹舌", "MC赵小六")
    plugin, tool, context, mcp = await _running_named_tool(
        "share_netease_album",
        [
            _mcp_result([album]),
            _album_detail_result(
                album,
                tracks=[
                    _song("1001", "弹舌第一式", "MC赵小六"),
                    _song("1002", "弹舌第二式", "MC赵小六"),
                ],
            ),
        ],
    )

    result = await tool.handler(
        tool.input_model.model_validate({"query": "中国有弹舌", "artist": "MC赵小六"})
    )
    output = _share_output(result, tool.output_model)

    assert output.status == "sent"
    assert output.selected is not None
    assert output.selected.album_id == "242154493"
    assert output.track_count == 2
    assert [track.song_id for track in output.tracks] == ["1001", "1002"]
    assert context.onebot.custom_music_cards == [
        {
            "url": "https://y.music.163.com/m/album?id=242154493",
            "image": "https://example.com/album.jpg",
            "title": "中国有弹舌",
            "singer": "MC赵小六",
            "content": "网易云专辑 · 2 首 · 2018-03-21",
        }
    ]
    assert mcp.calls == [
        (
            "netease_music",
            "music_search",
            {
                "query": "中国有弹舌 MC赵小六",
                "category": "album",
                "page": 1,
                "page_size": 10,
                "detail_level": "summary",
            },
        ),
        (
            "netease_music",
            "get_album",
            {
                "album_id": "242154493",
                "include_tracks": True,
                "track_page": 1,
                "track_page_size": 50,
            },
        ),
    ]
    await plugin.stop()


async def test_ambiguous_album_search_returns_ids_without_sending() -> None:
    plugin, tool, context, _mcp = await _running_named_tool(
        "share_netease_album",
        [
            _mcp_result(
                [
                    _album("101", "弹舌合集", "MC赵小六"),
                    _album("102", "弹舌合集", "另一位歌手"),
                ]
            )
        ],
    )

    result = await tool.handler(tool.input_model.model_validate({"query": "弹舌合集"}))
    output = _share_output(result, tool.output_model)

    assert output.status == "selection_required"
    assert [item.album_id for item in output.candidates] == ["101", "102"]
    assert isinstance(result, PluginResult) and result.mutation_committed is False
    assert context.onebot.custom_music_cards == []
    await plugin.stop()


async def test_album_id_skips_search_but_still_fetches_details() -> None:
    album = _album("241937735", "弹舌合集", "MC赵小六")
    plugin, tool, context, mcp = await _running_named_tool(
        "share_netease_album",
        [_album_detail_result(album, tracks=[_song("2001", "弹舌", "MC赵小六")])],
    )

    result = await tool.handler(tool.input_model.model_validate({"album_id": "241937735"}))
    output = _share_output(result, tool.output_model)

    assert output.status == "sent"
    assert output.selected is not None
    assert output.selected.album_id == "241937735"
    assert [call[1] for call in mcp.calls] == ["get_album"]
    assert len(context.onebot.custom_music_cards) == 1
    await plugin.stop()


async def test_current_quoted_title_overrides_hallucinated_id_and_is_idempotent() -> None:
    album = _album("242154493", "中国有弹舌", "MC赵小六")
    plugin, tool, context, mcp = await _running_named_tool(
        "share_netease_album",
        [
            _mcp_result([album]),
            _album_detail_result(
                album,
                tracks=[_song("2608077229", "中国有弹舌", "MC赵小六")],
            ),
        ],
    )
    context.messages.current = CurrentMessage(
        message_id="1768884190",
        sender_user_id="2186567848",
        scope_type="private",
        text="发一张 MC赵小六《中国有弹舌》的专辑卡片",
        received_at=datetime.now(UTC),
    )
    arguments = tool.input_model.model_validate({"album_id": "144008945"})

    first = _share_output(await tool.handler(arguments), tool.output_model)
    second = _share_output(await tool.handler(arguments), tool.output_model)

    assert first.status == "sent"
    assert first.selected is not None and first.selected.album_id == "242154493"
    assert second.status == "sent"
    assert "已经发送成功" in second.message
    assert [call[1] for call in mcp.calls] == ["music_search", "get_album"]
    assert len(context.onebot.custom_music_cards) == 1
    await plugin.stop()


async def test_single_fuzzy_album_result_is_not_sent_as_an_exact_match() -> None:
    plugin, tool, context, mcp = await _running_named_tool(
        "share_netease_album",
        [_mcp_result([_album("144008945", "夜空", "赵小北")])],
    )

    result = await tool.handler(tool.input_model.model_validate({"query": "中国有弹舌"}))
    output = _share_output(result, tool.output_model)

    assert output.status == "selection_required"
    assert output.candidates[0].title == "夜空"
    assert [call[1] for call in mcp.calls] == ["music_search"]
    assert context.onebot.custom_music_cards == []
    await plugin.stop()
