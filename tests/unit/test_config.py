"""Settings tests for external system prompt files."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from qq_ai_bot.config import Settings


def test_system_prompt_file_overrides_inline_prompt(tmp_path: Path) -> None:
    prompt_file = tmp_path / "system_prompt.md"
    prompt_file.write_text("# Role\n\nExternal prompt\n", encoding="utf-8")

    settings = Settings.model_validate(
        {
            "system_prompt": "inline prompt",
            "system_prompt_file": prompt_file,
        }
    )

    assert settings.system_prompt == "# Role\n\nExternal prompt"


def test_system_prompt_file_must_exist(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="cannot read SYSTEM_PROMPT_FILE"):
        Settings.model_validate(
            {
                "system_prompt_file": tmp_path / "missing.md",
            }
        )


def test_system_prompt_file_must_not_be_empty(tmp_path: Path) -> None:
    prompt_file = tmp_path / "empty.md"
    prompt_file.write_text(" \n", encoding="utf-8")

    with pytest.raises(ValidationError, match="SYSTEM_PROMPT_FILE must not be empty"):
        Settings.model_validate({"system_prompt_file": prompt_file})


def test_daily_chat_delay_range_must_be_ordered() -> None:
    with pytest.raises(
        ValidationError,
        match="DAILY_CHAT_MESSAGE_DELAY_MIN_SECONDS must not exceed",
    ):
        Settings.model_validate(
            {
                "daily_chat_message_delay_min_seconds": 3,
                "daily_chat_message_delay_max_seconds": 1,
            }
        )


def test_v1_memory_limits_accept_group_hundred_and_reject_member_over_fifty() -> None:
    assert Settings.model_validate({"group_memory_max_entries": 100})
    with pytest.raises(ValidationError, match="PERSON_GROUP_MEMORY_MAX_ENTRIES"):
        Settings.model_validate({"person_group_memory_max_entries": 51})


def test_web_enabled_requires_tavily_key_and_hides_it_from_repr() -> None:
    with pytest.raises(ValidationError, match="TAVILY_API_KEY"):
        Settings.model_validate({"web_enabled": True, "tavily_api_key": ""})

    settings = Settings.model_validate(
        {
            "web_enabled": True,
            "tavily_api_key": "tvly-sensitive-test-value",
        }
    )
    assert settings.web_configured
    assert "tvly-sensitive-test-value" not in repr(settings)


def test_web_limits_are_bounded() -> None:
    with pytest.raises(ValidationError, match="WEB_EXTRACT_MAX_RESULTS"):
        Settings.model_validate({"web_extract_max_results": 4})
    with pytest.raises(ValidationError, match="WEB_MAX_CALLS_PER_TURN"):
        Settings.model_validate({"web_max_calls_per_turn": 4})
    with pytest.raises(ValidationError, match="WEB_SEARCH_DEPTH"):
        Settings.model_validate({"web_search_depth": "unbounded"})


def test_relationship_defaults_have_no_daily_caps_and_keep_single_turn_bounds() -> None:
    settings = Settings()
    assert settings.relationship_initial_affection == 50
    assert settings.relationship_initial_trust == 50
    assert settings.affection_max_auto_delta == 2
    assert settings.trust_max_auto_delta == 2
    assert not hasattr(settings, "affection_daily_positive_cap")
    assert not hasattr(settings, "affection_daily_negative_cap")
    assert not hasattr(settings, "trust_daily_positive_cap")
    assert not hasattr(settings, "trust_daily_negative_cap")


def test_relationship_configuration_is_validated() -> None:
    with pytest.raises(ValidationError, match="AFFECTION_MAX_AUTO_DELTA"):
        Settings.model_validate({"affection_max_auto_delta": 3})
    with pytest.raises(ValidationError, match="RELATIONSHIP_BATCH_MAX_TURNS"):
        Settings.model_validate({"relationship_batch_max_turns": 11})
    with pytest.raises(ValidationError, match="between zero and one"):
        Settings.model_validate({"relationship_confidence_threshold": 1.1})
