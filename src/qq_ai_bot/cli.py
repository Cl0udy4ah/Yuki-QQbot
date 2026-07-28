"""Administrative CLI for migrations, NapCat config, and local Plugin API v1."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path

from alembic import command
from alembic.config import Config

from qq_ai_bot import __version__
from qq_ai_bot.config import Settings
from qq_ai_bot.persistence.database import Database
from qq_ai_bot.plugin_host.discovery import PluginDiscovery
from qq_ai_bot.plugin_host.manifest import load_manifest
from qq_ai_bot.plugin_host.repository import PluginInstallationRepository
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
    args = parser.parse_args()
    settings = Settings()
    if args.command == "init-db":
        _init_database(settings)
    elif args.command == "render-napcat-config":
        _render_napcat_config(settings, args.output)
    elif args.command == "plugin":
        raise SystemExit(asyncio.run(_plugin_command(settings, args)))


if __name__ == "__main__":
    main()
