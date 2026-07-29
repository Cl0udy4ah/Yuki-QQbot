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
        match="daily chat minimum delay must not exceed",
    ):
        Settings.model_validate(
            {
                "daily_chat_message_delay_min_seconds": 3,
                "daily_chat_message_delay_max_seconds": 1,
            }
        )


def test_planner_and_plugin_defaults_are_domain_validated_without_arbitrary_caps() -> None:
    settings = Settings()
    assert settings.planner_group_debounce_seconds == 3
    assert settings.planner_preferred_messages == 3
    assert settings.planner_confidence_threshold == 0.2
    assert settings.planner_reply_necessity_threshold == 0
    assert settings.planner_max_pending_messages == 8
    assert settings.reply_plan_hard_max_messages == 10
    assert not settings.plugin_system_enabled
    assert settings.plugin_api_version == "1.0"
    assert settings.plugin_ai_session_max_history_messages == 200

    assert Settings.model_validate({"planner_reply_necessity_threshold": 101})
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        Settings.model_validate({"planner_reply_necessity_threshold": -1})
    assert Settings.model_validate({"planner_group_debounce_seconds": 0})
    assert Settings.model_validate({"planner_group_debounce_seconds": 61})
    assert Settings.model_validate({"planner_preferred_messages": 21})
    assert Settings.model_validate({"reply_plan_hard_max_messages": 21})
    with pytest.raises(ValidationError, match="PLUGIN_API_VERSION"):
        Settings.model_validate({"plugin_api_version": "v1"})
    with pytest.raises(ValidationError, match="total plugin prompt budget"):
        Settings.model_validate(
            {
                "plugin_max_prompt_fragment_characters": 2000,
                "plugin_max_total_prompt_characters": 2000,
            }
        )


def test_memory_limits_are_configurable_positive_values() -> None:
    assert Settings.model_validate({"group_memory_max_entries": 100})
    assert Settings.model_validate({"person_group_memory_max_entries": 500})
    with pytest.raises(ValidationError, match="greater than 0"):
        Settings.model_validate({"person_group_memory_max_entries": 0})


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


def test_web_limits_are_configurable_and_search_depth_is_validated() -> None:
    assert Settings.model_validate({"web_extract_max_results": 4})
    assert Settings.model_validate({"web_max_calls_per_turn": 4})
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
    assert Settings.model_validate({"affection_max_auto_delta": 3})
    assert Settings.model_validate({"relationship_batch_max_turns": 11})
    with pytest.raises(ValidationError, match="less than or equal to 1"):
        Settings.model_validate({"relationship_confidence_threshold": 1.1})


def test_vision_defaults_are_safe_and_api_key_is_hidden() -> None:
    settings = Settings.model_validate(
        {
            "vision_enabled": False,
            "vision_api_key": "vision-sensitive-test-value",
        }
    )

    assert not settings.vision_enabled
    assert not settings.vision_configured
    assert settings.vision_provider == "qwen"
    assert settings.vision_model == "qwen3.7-plus"
    assert settings.vision_timeout_seconds == 120
    assert settings.vision_global_concurrency == 4
    assert settings.vision_queue_max_pending == 32
    assert settings.vision_queue_timeout_seconds == 120
    assert settings.vision_media_download_timeout_seconds == 120
    assert settings.vision_max_output_tokens == 8192
    assert not settings.vision_thinking_enabled
    assert settings.vision_thinking_budget == 6144
    assert settings.vision_low_confidence_retry_threshold == 0.65
    assert settings.vision_max_images_per_turn == 5
    assert settings.vision_max_frames_per_turn == 16
    assert settings.vision_gif_max_frames == 8
    assert settings.vision_max_download_bytes == 20_971_520
    assert settings.vision_max_prepared_bytes == 16_777_216
    assert settings.vision_max_dimension == 4096
    assert settings.vision_max_pixels == 16_777_216
    assert settings.vision_per_user_requests_per_minute == 20
    assert settings.vision_per_group_requests_per_minute == 60
    assert "vision-sensitive-test-value" not in repr(settings)


def test_vision_enabled_requires_complete_provider_configuration() -> None:
    with pytest.raises(ValidationError, match=r"VISION_BASE_URL.*VISION_API_KEY"):
        Settings.model_validate(
            {
                "vision_enabled": True,
                "vision_base_url": "",
                "vision_api_key": "",
                "vision_model": "qwen3.7-plus",
            }
        )

    settings = Settings.model_validate(
        {
            "vision_enabled": True,
            "vision_base_url": "https://dashscope.example/v1",
            "vision_api_key": "secret",
            "vision_model": "qwen3.7-plus",
        }
    )
    assert settings.vision_configured


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("vision_max_prepared_bytes", 0),
        ("vision_timeout_seconds", 0),
        ("vision_queue_max_pending", 0),
        ("vision_queue_timeout_seconds", 0),
        ("vision_media_download_timeout_seconds", 0),
        ("vision_max_retries", 0),
        ("vision_low_confidence_retry_threshold", 1.1),
    ],
)
def test_vision_numeric_domain_constraints_are_validated(field: str, value: int | float) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({field: value})


def test_vision_operational_limits_have_no_hidden_upper_clamp() -> None:
    settings = Settings.model_validate(
        {
            "vision_max_images_per_turn": 25,
            "vision_gif_max_frames": 40,
            "vision_max_frames_per_turn": 50,
            "vision_max_download_bytes": 128 * 1024 * 1024,
            "vision_max_retries": 4,
            "vision_thinking_budget": 65536,
        }
    )
    assert settings.vision_max_images_per_turn == 25
    assert settings.vision_thinking_budget == 65536
