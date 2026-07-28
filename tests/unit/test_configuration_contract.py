"""Configuration documentation must stay synchronized with typed settings."""

from __future__ import annotations

import re
from pathlib import Path

from qq_ai_bot.config import Settings

_COMPOSE_ONLY_KEYS = {
    "NAPCAT_GID",
    "NAPCAT_IMAGE",
    "NAPCAT_UID",
    "NAPCAT_WEBUI_TOKEN",
}
_COMPOSE_MANAGED_SETTINGS = {
    "APP_HOST",
    "APP_PORT",
    "DATABASE_URL",
}


def test_env_example_matches_typed_settings_and_reviewed_compose_keys() -> None:
    text = Path(".env.example").read_text(encoding="utf-8")
    documented = {
        match.group(1)
        for line in text.splitlines()
        if (match := re.match(r"#?\s*([A-Z][A-Z0-9_]*)=", line)) is not None
    }
    settings_keys = {
        field.validation_alias if isinstance(field.validation_alias, str) else name.upper()
        for name, field in Settings.model_fields.items()
    }

    assert documented - settings_keys == _COMPOSE_ONLY_KEYS
    assert settings_keys - documented == _COMPOSE_MANAGED_SETTINGS
