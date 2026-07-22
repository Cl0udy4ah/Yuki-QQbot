"""Administrative commands for migrations and NapCat configuration."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from alembic import command
from alembic.config import Config

from qq_ai_bot.config import Settings


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


def main() -> None:
    """Run one explicit administrative subcommand."""

    parser = argparse.ArgumentParser(prog="qq-ai-bot-cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="运行 Alembic 数据库迁移")
    render = subparsers.add_parser("render-napcat-config", help="生成 NapCat OneBot 配置")
    render.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings()
    if args.command == "init-db":
        _init_database(settings)
    elif args.command == "render-napcat-config":
        _render_napcat_config(settings, args.output)


if __name__ == "__main__":
    main()
