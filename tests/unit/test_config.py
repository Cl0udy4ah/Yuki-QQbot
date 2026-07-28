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


def test_planner_and_plugin_defaults_are_bounded() -> None:
    settings = Settings()
    assert settings.planner_enabled
    assert settings.planner_group_debounce_seconds == 8
    assert settings.planner_preferred_messages == 3
    assert settings.planner_reply_necessity_threshold == 80
    assert settings.reply_plan_hard_max_messages == 10
    assert not settings.plugin_system_enabled
    assert settings.plugin_api_version == "1.0"
    assert settings.plugin_ai_session_max_history_messages == 200

    with pytest.raises(ValidationError, match="PLANNER_REPLY_NECESSITY_THRESHOLD"):
        Settings.model_validate({"planner_reply_necessity_threshold": 101})
    assert Settings.model_validate({"planner_group_debounce_seconds": 0})
    with pytest.raises(ValidationError, match="PLANNER_GROUP_DEBOUNCE_SECONDS"):
        Settings.model_validate({"planner_group_debounce_seconds": 61})
    assert Settings.model_validate({"planner_preferred_messages": 20})
    with pytest.raises(ValidationError, match="PLANNER_PREFERRED_MESSAGES"):
        Settings.model_validate({"planner_preferred_messages": 21})
    assert Settings.model_validate({"reply_plan_hard_max_messages": 20})
    with pytest.raises(ValidationError, match="REPLY_PLAN_HARD_MAX_MESSAGES"):
        Settings.model_validate({"reply_plan_hard_max_messages": 21})
    with pytest.raises(ValidationError, match="PLUGIN_API_VERSION"):
        Settings.model_validate({"plugin_api_version": "v1"})
    with pytest.raises(ValidationError, match="PLUGIN_MAX_TOTAL_PROMPT_CHARACTERS"):
        Settings.model_validate(
            {
                "plugin_max_prompt_fragment_characters": 2000,
                "plugin_max_total_prompt_characters": 2000,
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
    ("field", "value", "message"),
    [
        ("vision_max_images_per_turn", 6, "VISION_MAX_IMAGES_PER_TURN"),
        ("vision_gif_max_frames", 9, "VISION_GIF_MAX_FRAMES"),
        ("vision_max_frames_per_turn", 17, "VISION_MAX_FRAMES_PER_TURN"),
        ("vision_max_download_bytes", 20 * 1024 * 1024 + 1, "VISION_MAX_DOWNLOAD_BYTES"),
        ("vision_max_prepared_bytes", 0, "greater than zero"),
        ("vision_timeout_seconds", 0, "greater than zero"),
        ("vision_queue_max_pending", 0, "greater than zero"),
        ("vision_queue_timeout_seconds", 0, "greater than zero"),
        ("vision_media_download_timeout_seconds", 0, "greater than zero"),
        ("vision_max_retries", 0, "greater than zero"),
        ("vision_max_retries", 2, "VISION_MAX_RETRIES"),
        ("vision_thinking_budget", 32769, "VISION_THINKING_BUDGET"),
        ("vision_low_confidence_retry_threshold", 1.1, "between zero and one"),
    ],
)
def test_vision_numeric_limits_are_validated(field: str, value: int | float, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings.model_validate({field: value})
