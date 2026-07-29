"""Administrative CLI for migrations, NapCat config, and local Plugin API v1."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import cast
from uuid import uuid4

from alembic import command
from alembic.config import Config

from qq_ai_bot import __version__
from qq_ai_bot.admin.models import RuntimeConfigSnapshot
from qq_ai_bot.config import Settings
from qq_ai_bot.domain.messages import ChatMessage, ChatTool
from qq_ai_bot.model_runtime import (
    ModelInvocationRepository,
    ModelProfileCatalog,
    ModelTask,
    load_model_profile_catalog,
)
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.planner.prompt import PLANNER_SYSTEM_PROMPT
from qq_ai_bot.plugin_host.discovery import PluginDiscovery
from qq_ai_bot.plugin_host.manifest import load_manifest
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository
from qq_ai_bot.prompting import (
    CORE_CONTRACT,
    PromptChannel,
    PromptCompiler,
    PromptContribution,
    PromptProgram,
    PromptStability,
    PromptTrust,
    measure_tool_schemas,
)
from qq_ai_bot.speech.cache import SpeechCache
from qq_ai_bot.speech.genie_client import GenieWorkerClient
from qq_ai_bot.speech.paths import SpeechPathPolicy
from qq_ai_bot.speech.profiles import VoiceProfileService
from qq_ai_bot.speech.provider import SpeechSynthesisRequest
from qq_ai_bot.speech.repository import SpeechGenerationRepository, VoiceProfileRepository
from qq_ai_bot.speech.service import GenieTTSProvider, SpeechService
from yuki_plugin_sdk.testing.contract import run_plugin_contract_tests


def _init_database(settings: Settings) -> None:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    command.upgrade(config, "head")


def _render_napcat_config(settings: Settings, output: Path) -> None:
    """Atomically render only the OneBot client config, never QQ credentials."""

    target_url = os.getenv("NAPCAT_REVERSE_WS_URL", "ws://bot:8080/onebot/v11/ws")
    payload = {
        "network": {
            "httpServers": [],
            "httpSseServers": [],
            "httpClients": [],
            "websocketServers": [],
            "websocketClients": [
                {
                    "enable": True,
                    "name": "qq-ai-bot-rws",
                    "url": target_url,
                    "reportSelfMessage": False,
                    "messagePostFormat": "array",
                    "token": settings.onebot_access_token,
                    "debug": False,
                    "heartInterval": 30000,
                    "reconnectInterval": 30000,
                }
            ],
            "plugins": [],
        },
        "musicSignUrl": "",
        "enableLocalFile2Url": False,
        "parseMultMsg": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)


def _add_plugin_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    plugin = subparsers.add_parser("plugin", help="管理本地可信 Plugin API v1 插件")
    commands = plugin.add_subparsers(dest="plugin_command", required=True)
    commands.add_parser("list")
    commands.add_parser("discover")
    for name in ("inspect", "permissions", "approve", "enable", "disable", "doctor"):
        parser = commands.add_parser(name)
        parser.add_argument("plugin_id")
    validate = commands.add_parser("validate")
    validate.add_argument("path", type=Path)
    docs = commands.add_parser("docs")
    docs.add_argument("path", type=Path)
    test = commands.add_parser("test")
    test.add_argument("path", type=Path)


def _add_speech_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    speech = subparsers.add_parser("speech", help="管理本地 Genie-TTS 语音")
    commands = speech.add_subparsers(dest="speech_command", required=True)
    commands.add_parser("status")
    genie = commands.add_parser("genie")
    genie.add_subparsers(dest="genie_command", required=True).add_parser("doctor")
    profile = commands.add_parser("profile")
    profiles = profile.add_subparsers(dest="profile_command", required=True)
    profiles.add_parser("list")
    for action in ("inspect", "reload", "enable", "disable", "set-default"):
        item = profiles.add_parser(action)
        item.add_argument("profile_id")
    imported = profiles.add_parser("import")
    imported.add_argument("source_directory", type=Path)
    reference = commands.add_parser("reference")
    references = reference.add_subparsers(dest="reference_command", required=True)
    listed = references.add_parser("list")
    listed.add_argument("profile_id")
    disabled = references.add_parser("disable")
    disabled.add_argument("profile_id")
    disabled.add_argument("reference_key")
    added = references.add_parser("add")
    added.add_argument("profile_id")
    added.add_argument("source", type=Path)
    test = commands.add_parser("test")
    test.add_argument("profile_id")
    test.add_argument("text")
    cache = commands.add_parser("cache")
    cache.add_subparsers(dest="cache_command", required=True).add_parser("cleanup")
    worker = commands.add_parser("worker")
    worker.add_subparsers(dest="worker_command", required=True).add_parser("restart")


_PROMPT_SCENARIOS = (
    "direct-text",
    "group-mention",
    "autonomous-group",
    "admin",
    "web",
    "vision",
    "emoji",
    "speech",
    "plugin",
)


def _add_diagnostics_parsers(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    prompt = subparsers.add_parser("prompt", help="检查脱敏 Prompt 与 Token 指标")
    prompt_commands = prompt.add_subparsers(dest="prompt_command", required=True)
    inspect = prompt_commands.add_parser("inspect")
    inspect.add_argument("scenario", choices=_PROMPT_SCENARIOS)
    prompt_commands.add_parser("compare")

    model = subparsers.add_parser("model", help="检查模型档案、路由和调用统计")
    model_commands = model.add_subparsers(dest="model_command", required=True)
    model_commands.add_parser("routes")
    model_commands.add_parser("profiles")
    model_commands.add_parser("stats")


def _model_catalog(settings: Settings) -> ModelProfileCatalog:
    return load_model_profile_catalog(
        settings.model_profiles_file,
        legacy_provider=settings.llm_provider,
        legacy_base_url=settings.llm_base_url,
        legacy_model=settings.llm_model,
        legacy_timeout_seconds=settings.llm_timeout_seconds,
        legacy_max_retries=settings.llm_max_retries,
        legacy_temperature=settings.llm_temperature,
        legacy_max_output_tokens=settings.llm_max_output_tokens,
        legacy_thinking_enabled=settings.llm_thinking_enabled,
        environment={
            "LLM_BASE_URL": settings.llm_base_url,
            "LLM_MODEL": settings.llm_model,
            "LLM_FLASH_BASE_URL": settings.llm_flash_base_url,
            "LLM_FLASH_MODEL": settings.llm_flash_model,
        },
    )


def _prompt_diagnostic(settings: Settings, scenario: str) -> dict[str, object]:
    catalog = _model_catalog(settings)
    task = ModelTask.PLANNER if scenario == "autonomous-group" else ModelTask.CHAT_AGENT
    route, profile = catalog.routes[task], catalog.profiles[catalog.routes[task].profile_id]
    dynamic_payloads: dict[str, object] = {
        "time": {"timezone": "Asia/Shanghai", "local": "2000-01-01T12:00:00+08:00"},
        "scene": {"kind": "group" if "group" in scenario else "private"},
    }
    if scenario == "admin":
        dynamic_payloads["authority"] = {"role": "superuser", "source": "real_event"}
    if scenario == "vision":
        dynamic_payloads["vision"] = {"observations": ["synthetic visual observation"]}
    if scenario == "speech":
        dynamic_payloads["speech"] = {"available": True, "requested": True}
    if scenario == "plugin":
        dynamic_payloads["plugins"] = [{"id": "example", "data": "synthetic"}]
    if scenario != "autonomous-group":
        dynamic_payloads["plan"] = {"decision": "reply"}

    if scenario == "autonomous-group":
        contributions = [
            PromptContribution(
                id="planner.contract",
                channel=PromptChannel.INVARIANT,
                trust=PromptTrust.CORE,
                priority=100,
                stability=PromptStability.STATIC,
                content=PLANNER_SYSTEM_PROMPT,
                required=True,
            )
        ]
    else:
        contributions = [
            PromptContribution(
                id="core.persona",
                channel=PromptChannel.PERSONA,
                trust=PromptTrust.CORE,
                priority=100,
                stability=PromptStability.STATIC,
                content=settings.system_prompt,
                required=True,
            ),
            PromptContribution(
                id="core.contract",
                channel=PromptChannel.INVARIANT,
                trust=PromptTrust.CORE,
                priority=90,
                stability=PromptStability.STATIC,
                content=CORE_CONTRACT,
                required=True,
            ),
        ]
    contributions.extend(
        PromptContribution(
            id=f"runtime.{key}",
            channel=PromptChannel.RUNTIME,
            trust=PromptTrust.TRUSTED,
            priority=50,
            payload=value,
            required=key in {"time", "scene"},
        )
        for key, value in dynamic_payloads.items()
    )
    history = (
        ()
        if scenario == "autonomous-group"
        else (
            ChatMessage(role="user", content="这是脱敏的历史消息。"),
            ChatMessage(role="assistant", content="这是脱敏的历史回复。"),
            ChatMessage(role="user", content="请回应当前合成场景。"),
        )
    )
    compiled = PromptCompiler().compile(
        PromptProgram(contributions=tuple(contributions)),
        history=history,
    )
    tools, groups = _scenario_tools(scenario)
    tool_metrics = measure_tool_schemas(tools, groups=groups)
    metrics = compiled.metrics
    return {
        "scenario": scenario,
        "static_prefix_characters": metrics.static_characters,
        "dynamic_envelope_characters": metrics.dynamic_characters,
        "history_characters": metrics.history_characters,
        "tool_schema_characters": tool_metrics.schema_characters,
        "estimated_tokens": metrics.estimated_tokens + tool_metrics.estimated_tokens,
        "contribution_ids": [item.id for item in compiled.selected],
        "tool_count": tool_metrics.tool_count,
        "tool_group_characters": tool_metrics.group_characters,
        "route_task": task.value,
        "model_profile": route.profile_id,
        "model": profile.model,
        "usage_available": True,
        "stable_prefix_hash": metrics.stable_prefix_hash,
        "fixture": "sanitized_predefined_scenario",
    }


def _scenario_tools(scenario: str) -> tuple[tuple[ChatTool, ...], dict[str, str]]:
    selected = {
        "direct-text": ("get_person_memories",),
        "group-mention": ("get_group_memories", "get_person_memories"),
        "autonomous-group": (),
        "admin": ("admin_execute_action", "admin_set_config", "call_onebot_api"),
        "web": ("web_search", "read_webpage"),
        "vision": ("get_person_memories",),
        "emoji": ("send_emoji",),
        "speech": ("send_voice",),
        "plugin": ("plugin_example",),
    }[scenario]
    group_by_name = {
        "get_person_memories": "memory",
        "get_group_memories": "memory",
        "admin_execute_action": "admin",
        "admin_set_config": "config",
        "call_onebot_api": "onebot",
        "web_search": "web",
        "read_webpage": "web",
        "send_emoji": "emoji",
        "send_voice": "speech",
        "plugin_example": "plugin",
    }
    tools = tuple(
        ChatTool(
            name=name,
            description="Predefined diagnostic capability schema.",
            parameters={"type": "object", "properties": {}},
        )
        for name in selected
    )
    return tools, group_by_name


async def _model_stats(settings: Settings) -> int:
    database = Database(settings.database_url)
    repository = ModelInvocationRepository(database)
    try:
        rows = {
            task.value: (await repository.stats(task=task)).model_dump(mode="json")
            for task in ModelTask
        }
        rows["all"] = (await repository.stats()).model_dump(mode="json")
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    finally:
        await database.close()


def _prompt_comparison(settings: Settings) -> dict[str, object]:
    baseline = {
        "direct-text": {"total_characters": 22413, "estimated_tokens": 5604},
        "group-mention": {"total_characters": 22422, "estimated_tokens": 5606},
        "autonomous-group": {"total_characters": 4509, "estimated_tokens": 1128},
        "admin": {"total_characters": 27451, "estimated_tokens": 6863},
        "web": {"total_characters": 22413, "estimated_tokens": 5604},
        "vision": {"total_characters": 22886, "estimated_tokens": 5722},
        "emoji": {"total_characters": 22413, "estimated_tokens": 5604},
        "speech": {"total_characters": 22751, "estimated_tokens": 5688},
        "plugin": {"total_characters": 22413, "estimated_tokens": 5604},
    }
    comparisons: dict[str, object] = {}
    for scenario in _PROMPT_SCENARIOS:
        current = _prompt_diagnostic(settings, scenario)
        current_characters = (
            cast(int, current["static_prefix_characters"])
            + cast(int, current["dynamic_envelope_characters"])
            + cast(int, current["history_characters"])
            + cast(int, current["tool_schema_characters"])
        )
        old = baseline[scenario]
        comparisons[scenario] = {
            "baseline": old,
            "current": {
                "total_characters": current_characters,
                "estimated_tokens": current["estimated_tokens"],
            },
            "character_reduction_percent": round(
                (1 - current_characters / old["total_characters"]) * 100,
                1,
            ),
        }
    return {
        "baseline_commit": "c4a0910a39a69501a0a49226a64f2cfb6e7a682d",
        "fixture": "sanitized_predefined_scenario",
        "scenarios": comparisons,
    }


async def _speech_command(settings: Settings, args: argparse.Namespace) -> int:
    paths = SpeechPathPolicy(settings.speech_root)
    database = Database(settings.database_url)
    profiles = VoiceProfileRepository(database)
    generations = SpeechGenerationRepository(database)
    cache = SpeechCache(repository=generations, paths=paths)
    client = GenieWorkerClient(
        settings.speech_socket_path,
        request_timeout_seconds=settings.speech_worker_request_timeout_seconds,
    )
    provider = GenieTTSProvider(
        client=client,
        profiles=profiles,
        generations=generations,
        cache=cache,
        paths=paths,
    )
    service = SpeechService(
        provider=provider,
        generations=generations,
        cache=cache,
        paths=paths,
        profiles=profiles,
    )
    profile_service = VoiceProfileService(repository=profiles, paths=paths, loader=client)
    try:
        action = str(args.speech_command)
        if action == "status":
            health = await service.health()
            print(
                json.dumps(
                    {
                        "enabled": settings.speech_enabled,
                        "worker_connected": health.connected,
                        "worker_ready": health.ready,
                        "worker_busy": health.busy,
                        "loaded_profile": health.loaded_profile_id,
                        "japanese_frontend_available": (
                            health.japanese_frontend_available
                        ),
                        "japanese_frontend_version": health.japanese_frontend_version,
                        "japanese_frontend_signature": (
                            health.japanese_frontend_signature
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if health.available else 1
        if action == "genie":
            print(json.dumps(await profile_service.doctor(), ensure_ascii=False, indent=2))
            return 0
        if action == "profile":
            operation = str(args.profile_command)
            if operation == "list":
                rows = await profile_service.list_profiles()
                print(
                    json.dumps(
                        [
                            {
                                "profile_id": row.profile_id,
                                "display_name": row.display_name,
                                "enabled": row.enabled,
                                "default": row.is_default,
                            }
                            for row in rows
                        ],
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            if operation == "import":
                profile_row = await profile_service.import_profile(Path(args.source_directory))
            elif operation == "reload":
                profile_row = await profile_service.reload_profile(str(args.profile_id))
            elif operation == "enable":
                profile_row = await profile_service.enable_profile(str(args.profile_id))
            elif operation == "disable":
                profile_row = await profile_service.disable_profile(str(args.profile_id))
            elif operation == "set-default":
                profile_row = await profile_service.activate_profile(str(args.profile_id))
            else:
                selected_profile = await profile_service.get_profile(str(args.profile_id))
                if selected_profile is None:
                    print("profile not found")
                    return 1
                profile_row = selected_profile
            print(
                json.dumps(
                    {
                        "profile_id": profile_row.profile_id,
                        "display_name": profile_row.display_name,
                        "provider": profile_row.provider,
                        "model_version": profile_row.engine_model_version.value,
                        "language": profile_row.language,
                        "supported_languages": profile_row.supported_languages,
                        "default_style": profile_row.default_style,
                        "enabled": profile_row.enabled,
                        "default": profile_row.is_default,
                        "source": profile_row.source,
                        "references": len(profile_row.references),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if action == "reference":
            operation = str(args.reference_command)
            if operation == "add":
                reference_row = await profile_service.add_reference(
                    str(args.profile_id), Path(args.source)
                )
                print(f"added: {reference_row.reference_key}")
                return 0
            profile_id = str(args.profile_id)
            if operation == "disable":
                reference_row = await profile_service.disable_reference(
                    profile_id, str(args.reference_key)
                )
                print(f"disabled: {reference_row.reference_key}")
                return 0
            selected_profile = await profile_service.get_profile(profile_id)
            if selected_profile is None:
                print("profile not found")
                return 1
            print(
                json.dumps(
                    [
                        {
                            "reference_key": ref.reference_key,
                            "style": ref.style,
                            "aliases": ref.aliases,
                            "language": ref.language,
                            "enabled": ref.enabled,
                            "priority": ref.priority,
                        }
                        for ref in selected_profile.references
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if action == "test":
            runtime = (await _runtime_snapshot(settings, database)).speech
            result = await service.synthesize(
                SpeechSynthesisRequest(
                    request_id=str(uuid4()),
                    profile_id=str(args.profile_id),
                    style_hint="",
                    text=str(args.text),
                    split_sentence=runtime.split_sentence,
                    conversation_key="cli:speech-test",
                    trigger_event_id=None,
                    turn_token=None,
                ),
                runtime=runtime,
            )
            print(
                json.dumps(
                    {
                        "generation_id": result.generation_id,
                        "profile_id": result.profile_id,
                        "reference_key": result.reference_key,
                        "target_language": result.target_language,
                        "duration_milliseconds": result.duration_milliseconds,
                        "cache_hit": result.cache_hit,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if action == "cache":
            runtime = (await _runtime_snapshot(settings, database)).speech
            print(await service.cleanup(runtime=runtime))
            return 0
        if action == "worker":
            await client.shutdown()
            print("worker restart requested")
            return 0
        return 1
    finally:
        await service.close()
        await database.close()


async def _runtime_snapshot(settings: Settings, database: Database) -> RuntimeConfigSnapshot:
    from qq_ai_bot.admin.config_service import RuntimeConfigService

    runtime = RuntimeConfigService(settings=settings, database=database)
    await runtime.initialize()
    return await runtime.snapshot()


async def _plugin_command(settings: Settings, args: argparse.Namespace) -> int:
    action = str(args.plugin_command)
    if action in {"validate", "test", "docs"}:
        path = Path(args.path)
        if action == "validate":
            manifest = load_manifest(path, yuki_version=__version__)
            print(json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2))
            return 0
        if action == "test":
            report = await run_plugin_contract_tests(path, yuki_version=__version__)
            print(report.model_dump_json(indent=2))
            return 0 if report.passed else 1
        await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
        target = path / "plugin-api-v1-reference.md"
        await asyncio.to_thread(
            target.write_text,
            (
                "# Yuki Plugin API v1\n\n"
                "由 `qq-ai-bot-cli plugin docs` 生成。完整手册位于 "
                "`docs/plugin-development/`。\n"
            ),
            encoding="utf-8",
        )
        print(target)
        return 0

    database = Database(settings.database_url)
    repository = PluginInstallationRepository(database)
    try:
        if action == "discover":
            discovery = PluginDiscovery(
                settings.plugin_directory,
                yuki_version=__version__,
                plugin_api=settings.plugin_api_version,
            )
            records = discovery.discover()
            for discovered in records:
                discovered_manifest = discovered.manifest
                if discovered_manifest is None:
                    print(f"invalid: {discovered.record.directory}: {discovered.record.detail}")
                    continue
                await repository.upsert_discovered(
                    plugin_id=discovered_manifest.id,
                    name=discovered_manifest.name,
                    version=discovered_manifest.version,
                    plugin_api=discovered_manifest.plugin_api,
                    yuki_requires=discovered_manifest.yuki_requires,
                    manifest_hash=discovered_manifest.manifest_hash,
                    entrypoint=discovered_manifest.entrypoint,
                    requested_permissions=(item.value for item in discovered_manifest.permissions),
                )
                print(f"discovered: {discovered_manifest.id}")
            return 0
        if action == "list":
            for item in await repository.list_all():
                print(
                    f"{item.plugin_id}\t{item.version}\t{item.status}\t"
                    f"enabled={str(item.enabled).lower()}"
                )
            return 0
        plugin_id = str(args.plugin_id)
        row = await repository.get(plugin_id)
        if row is None:
            print("plugin not found")
            return 1
        if action == "approve":
            updated = await repository.approve(plugin_id)
        elif action == "enable":
            updated = await repository.set_enabled(plugin_id, enabled=True)
        elif action == "disable":
            updated = await repository.set_enabled(plugin_id, enabled=False)
        else:
            updated = row
        if updated is None:
            print("plugin update failed")
            return 1
        row = updated
        if action == "doctor":
            root = settings.plugin_directory / plugin_id
            manifest_ok = False
            try:
                manifest_ok = (
                    load_manifest(root, yuki_version=__version__).manifest_hash == row.manifest_hash
                )
            except Exception:
                pass
            print(
                json.dumps(
                    {
                        "plugin_id": plugin_id,
                        "manifest_ok": manifest_ok,
                        "status": row.status,
                        "enabled": row.enabled,
                        "approval_current": bool(row.approved_at),
                        "failure_count": row.failure_count,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if manifest_ok else 1
        if action == "permissions":
            print("requested:", ", ".join(row.requested_permissions) or "none")
            print("approved:", ", ".join(row.approved_permissions) or "none")
        else:
            print(json.dumps(asdict(row), ensure_ascii=False, default=str, indent=2))
        return 0
    finally:
        await database.close()


def main() -> None:
    """Run one explicit administrative subcommand."""

    parser = argparse.ArgumentParser(prog="qq-ai-bot-cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="运行 Alembic 数据库迁移")
    render = subparsers.add_parser("render-napcat-config", help="生成 NapCat OneBot 配置")
    render.add_argument("--output", type=Path, required=True)
    _add_plugin_parser(subparsers)
    _add_speech_parser(subparsers)
    _add_diagnostics_parsers(subparsers)
    args = parser.parse_args()
    settings = Settings()
    if args.command == "init-db":
        _init_database(settings)
    elif args.command == "render-napcat-config":
        _render_napcat_config(settings, args.output)
    elif args.command == "plugin":
        raise SystemExit(asyncio.run(_plugin_command(settings, args)))
    elif args.command == "speech":
        raise SystemExit(asyncio.run(_speech_command(settings, args)))
    elif args.command == "prompt":
        if args.prompt_command == "inspect":
            result = _prompt_diagnostic(settings, str(args.scenario))
        else:
            result = _prompt_comparison(settings)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "model":
        if args.model_command == "stats":
            raise SystemExit(asyncio.run(_model_stats(settings)))
        catalog = _model_catalog(settings)
        if args.model_command == "routes":
            result = {
                task.value: {
                    "profile_id": route.profile_id,
                    "required_capabilities": sorted(
                        item.value for item in route.required_capabilities
                    ),
                }
                for task, route in catalog.routes.items()
            }
        else:
            result = {
                profile_id: profile.model_dump(mode="json")
                for profile_id, profile in catalog.profiles.items()
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
