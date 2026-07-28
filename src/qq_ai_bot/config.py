"""Environment-driven application settings with safe defaults."""

from __future__ import annotations

import re
from functools import cached_property
from pathlib import Path
from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv_set(value: str) -> frozenset[str]:
    return frozenset(item.strip() for item in value.split(",") if item.strip())


class Settings(BaseSettings):
    """Configuration loaded from environment variables and an optional .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    app_host: str = "0.0.0.0"
    app_port: int = 8080
    log_level: str = "INFO"
    log_message_content: bool = False

    onebot_access_token: str = ""
    superusers_csv: str = Field(default="", validation_alias="SUPERUSERS")
    allowed_private_users_csv: str = Field(default="", validation_alias="ALLOWED_PRIVATE_USERS")
    enabled_groups_csv: str = Field(default="", validation_alias="ENABLED_GROUPS")
    ignored_bot_users_csv: str = Field(default="", validation_alias="IGNORED_BOT_USERS")
    ai_prefix: str = "!ai"

    llm_provider: str = "openai"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = 2
    llm_temperature: float = 0.7
    llm_max_output_tokens: int = 8192
    llm_thinking_enabled: bool | None = None
    system_prompt: str = (
        "你是一个运行在 QQ 中的 AI 助手。请只输出给用户的最终回答，不要输出隐藏的推理过程。"
        "不要声称执行了未实际成功的工具、代码、命令或文件访问。"
        "只有联网工具实际成功时，才能说明已经搜索或读取网页。"
    )
    system_prompt_file: Path | None = None

    database_url: str = "sqlite+aiosqlite:///./data/qq_ai_bot.db"
    processed_event_ttl_seconds: int = 86400
    processed_event_cleanup_seconds: int = 3600
    max_context_characters: int = 12000

    global_llm_concurrency: int = 4
    per_user_requests_per_minute: int = 10
    per_group_requests_per_minute: int = 30
    max_input_characters: int = 4000
    max_output_characters: int = 12000
    max_qq_message_chars: int = 1800
    split_daily_chat_sentences: bool = True
    daily_chat_split_max_characters: int = 240
    daily_chat_split_max_messages: int = 4
    daily_chat_message_delay_min_seconds: float = 3.0
    daily_chat_message_delay_max_seconds: float = 5.0
    group_memory_max_entries: int = 100

    observe_enabled_groups: bool = True
    autonomous_group_chat_enabled: bool = True
    autonomous_silence_seconds: float = 3.0
    autonomous_confidence_threshold: float = 0.2
    autonomous_cooldown_seconds: int = 20
    autonomous_max_per_hour: int = 30
    recent_history_tool_limit: int = 20
    local_context_event_limit: int = 30
    related_people_limit: int = 5
    person_memory_max_entries: int = 100
    person_group_memory_max_entries: int = 50
    preference_max_entries: int = 30
    memory_batch_seconds: float = 30.0
    memory_batch_trigger_count: int = 10
    memory_batch_max_events: int = 20
    agent_max_tool_calls: int = 12
    agent_max_model_requests: int = 12
    agent_tool_result_max_characters: int = 32000

    # Planner-first conversation orchestration.  The legacy autonomous confidence,
    # cooldown and hourly limit settings remain readable for 1.x compatibility but
    # are not used by the 1.6 Planner pipeline.
    planner_enabled: bool = True
    planner_model: str = ""
    planner_direct_enabled: bool = True
    planner_group_enabled: bool = True
    planner_group_debounce_seconds: float = 3.0
    planner_preferred_messages: int = 3
    planner_temperature: float = 0.1
    planner_max_output_tokens: int = 512
    planner_timeout_seconds: float = 20.0
    planner_confidence_threshold: float = 0.2
    planner_reply_necessity_threshold: int = 0
    planner_max_pending_messages: int = 8
    planner_recent_presence_window_seconds: int = 300
    planner_max_wait_seconds: int = 60
    planner_interrupt_autonomous_on_new_message: bool = True
    planner_record_runs: bool = True
    reply_sequence_cancel_on_new_message: bool = True
    reply_plan_hard_max_messages: int = 10

    # Local in-process plugins.  Approval is API governance, not a Python sandbox.
    plugin_system_enabled: bool = False
    plugin_directory: Path = Path("plugins")
    plugin_api_version: str = "1.0"
    plugin_hook_timeout_seconds: float = 3.0
    plugin_start_timeout_seconds: float = 10.0
    plugin_stop_timeout_seconds: float = 10.0
    plugin_max_prompt_fragment_characters: int = 2000
    plugin_max_prompt_characters_per_plugin: int = 4000
    plugin_max_total_prompt_characters: int = 8000
    plugin_background_task_limit: int = 4
    plugin_failure_disable_threshold: int = 3
    plugin_http_max_response_bytes: int = 2_097_152
    plugin_http_timeout_seconds: float = 15.0
    plugin_ai_session_max_history_messages: int = 200

    relationship_enabled: bool = True
    relationship_initial_affection: int = 50
    relationship_initial_trust: int = 50
    relationship_batch_seconds: float = 60.0
    relationship_batch_trigger_count: int = 5
    relationship_batch_max_turns: int = 10
    relationship_max_attempts: int = 3
    relationship_confidence_threshold: float = 0.75
    affection_max_auto_delta: int = 2
    trust_max_auto_delta: int = 2
    # Zero deliberately means unlimited, preserving the 1.2 relationship behavior.
    relationship_daily_positive_cap: int = 0
    relationship_daily_negative_cap: int = 0
    trust_affection_cap_offset: int = 10
    conflict_preference_min_gap: int = 15

    web_enabled: bool = False
    tavily_api_key: str = Field(default="", repr=False)
    web_search_depth: str = "advanced"
    web_search_max_results: int = 5
    web_extract_max_results: int = 3
    web_timeout_seconds: float = 20.0
    web_max_retries: int = 1
    web_global_concurrency: int = 4
    web_max_calls_per_turn: int = 3
    web_tool_result_max_characters: int = 16000
    web_source_retention_days: int = 7
    web_source_max_runs_per_conversation: int = 10

    vision_enabled: bool = False
    vision_provider: str = "qwen"
    vision_base_url: str = ""
    vision_api_key: str = Field(default="", repr=False)
    vision_model: str = "qwen3.7-plus"
    vision_timeout_seconds: float = 120.0
    vision_max_retries: int = 1
    vision_global_concurrency: int = 4
    vision_queue_max_pending: int = 32
    vision_queue_timeout_seconds: float = 120.0
    vision_media_download_timeout_seconds: float = 120.0
    vision_allow_private_urls: bool = False
    vision_max_output_tokens: int = 8192
    vision_thinking_enabled: bool = False
    vision_thinking_budget: int = 6144
    vision_low_confidence_retry_threshold: float = 0.65
    vision_max_images_per_turn: int = 5
    vision_max_frames_per_turn: int = 16
    vision_gif_max_frames: int = 8
    vision_max_download_bytes: int = 20_971_520
    vision_max_prepared_bytes: int = 16_777_216
    vision_max_dimension: int = 4096
    vision_max_pixels: int = 16_777_216
    vision_per_user_requests_per_minute: int = 20
    vision_per_group_requests_per_minute: int = 60
    vision_analysis_retention_days: int = 7

    # Persistent emoji collection and reply effects. Recognition reuses the
    # configured VisionProvider; no second visual client or review pipeline exists.
    emoji_enabled: bool = True
    emoji_collection_enabled: bool = True
    emoji_collection_mode: str = "likely"
    emoji_collect_private: bool = True
    emoji_collect_group: bool = True
    emoji_auto_adopt_enabled: bool = True
    emoji_auto_adopt_min_confidence: float = 0.78
    emoji_pool_capacity: int | None = None
    emoji_replacement_mode: str = "score"
    emoji_selector_enabled: bool = True
    emoji_selector_candidate_count: int = 6
    emoji_max_effects_per_reply: int = 1
    emoji_near_duplicate_enabled: bool = True
    emoji_near_duplicate_distance: int = 6
    emoji_same_emoji_cooldown_seconds: int = 300
    emoji_scope_repeat_cooldown_seconds: int = 60
    emoji_cache_retention_days: int = 30
    emoji_worker_batch_size: int = 10
    emoji_worker_poll_seconds: float = 2.0
    emoji_worker_lease_seconds: int = 120
    emoji_worker_max_attempts: int = 3
    emoji_worker_retry_delay_seconds: float = 30.0
    emoji_analysis_version: str = "emoji-v1"
    emoji_storage_root: Path = Path("data/emoji")
    emoji_preview_max_dimension: int = 512

    automation_enabled: bool = False
    default_timezone: str = "Asia/Shanghai"
    automation_poll_seconds: float = 2.0
    automation_lease_seconds: int = 120
    automation_max_active_per_superuser: int = 50
    automation_max_active_per_user: int = 10
    automation_max_steps: int = 16
    automation_max_llm_calls_per_run: int = 5
    automation_max_tool_calls_per_run: int = 16
    automation_max_messages_per_run: int = 10
    automation_max_runtime_seconds: int = 600
    automation_min_interval_seconds: int = 60
    automation_default_misfire_grace_seconds: int = 1800
    automation_max_consecutive_failures: int = 3
    automation_run_retention_days: int = 30

    @field_validator(
        "app_port",
        "llm_max_output_tokens",
        "processed_event_ttl_seconds",
        "processed_event_cleanup_seconds",
        "max_context_characters",
        "global_llm_concurrency",
        "per_user_requests_per_minute",
        "per_group_requests_per_minute",
        "max_input_characters",
        "max_output_characters",
        "max_qq_message_chars",
        "daily_chat_split_max_characters",
        "daily_chat_split_max_messages",
        "group_memory_max_entries",
        "autonomous_cooldown_seconds",
        "autonomous_max_per_hour",
        "recent_history_tool_limit",
        "local_context_event_limit",
        "related_people_limit",
        "person_memory_max_entries",
        "person_group_memory_max_entries",
        "preference_max_entries",
        "memory_batch_trigger_count",
        "memory_batch_max_events",
        "agent_max_tool_calls",
        "agent_max_model_requests",
        "agent_tool_result_max_characters",
        "planner_preferred_messages",
        "planner_max_output_tokens",
        "planner_max_pending_messages",
        "planner_recent_presence_window_seconds",
        "planner_max_wait_seconds",
        "reply_plan_hard_max_messages",
        "plugin_max_prompt_fragment_characters",
        "plugin_max_prompt_characters_per_plugin",
        "plugin_max_total_prompt_characters",
        "plugin_background_task_limit",
        "plugin_failure_disable_threshold",
        "plugin_http_max_response_bytes",
        "plugin_ai_session_max_history_messages",
        "relationship_batch_trigger_count",
        "relationship_batch_max_turns",
        "relationship_max_attempts",
        "affection_max_auto_delta",
        "trust_max_auto_delta",
        "web_search_max_results",
        "web_extract_max_results",
        "web_global_concurrency",
        "web_max_calls_per_turn",
        "web_tool_result_max_characters",
        "web_source_retention_days",
        "web_source_max_runs_per_conversation",
        "vision_max_retries",
        "vision_global_concurrency",
        "vision_queue_max_pending",
        "vision_max_output_tokens",
        "vision_thinking_budget",
        "vision_max_images_per_turn",
        "vision_max_frames_per_turn",
        "vision_gif_max_frames",
        "vision_max_download_bytes",
        "vision_max_prepared_bytes",
        "vision_max_dimension",
        "vision_max_pixels",
        "vision_per_user_requests_per_minute",
        "vision_per_group_requests_per_minute",
        "vision_analysis_retention_days",
        "emoji_selector_candidate_count",
        "emoji_max_effects_per_reply",
        "emoji_cache_retention_days",
        "emoji_worker_batch_size",
        "emoji_worker_lease_seconds",
        "emoji_worker_max_attempts",
        "emoji_preview_max_dimension",
        "automation_lease_seconds",
        "automation_max_active_per_superuser",
        "automation_max_active_per_user",
        "automation_max_steps",
        "automation_max_llm_calls_per_run",
        "automation_max_tool_calls_per_run",
        "automation_max_messages_per_run",
        "automation_max_runtime_seconds",
        "automation_min_interval_seconds",
        "automation_default_misfire_grace_seconds",
        "automation_max_consecutive_failures",
        "automation_run_retention_days",
    )
    @classmethod
    def _positive_integer(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be greater than zero")
        return value

    @field_validator(
        "llm_timeout_seconds",
        "web_timeout_seconds",
        "vision_timeout_seconds",
        "vision_queue_timeout_seconds",
        "vision_media_download_timeout_seconds",
        "emoji_worker_poll_seconds",
        "automation_poll_seconds",
        "planner_timeout_seconds",
        "plugin_hook_timeout_seconds",
        "plugin_start_timeout_seconds",
        "plugin_stop_timeout_seconds",
        "plugin_http_timeout_seconds",
    )
    @classmethod
    def _positive_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be greater than zero")
        return value

    @field_validator(
        "emoji_same_emoji_cooldown_seconds",
        "emoji_scope_repeat_cooldown_seconds",
        "emoji_worker_retry_delay_seconds",
    )
    @classmethod
    def _non_negative_emoji_delay(cls, value: float) -> float:
        if value < 0:
            raise ValueError("must not be negative")
        return value

    @field_validator("llm_max_retries", "web_max_retries")
    @classmethod
    def _non_negative_retries(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must not be negative")
        return value

    @field_validator("planner_reply_necessity_threshold")
    @classmethod
    def _non_negative_planner_necessity(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must not be negative")
        return value

    @field_validator(
        "relationship_daily_positive_cap",
        "relationship_daily_negative_cap",
    )
    @classmethod
    def _non_negative_relationship_cap(cls, value: int) -> int:
        if value < 0 or value > 100:
            raise ValueError("must be between zero and 100")
        return value

    @field_validator(
        "daily_chat_message_delay_min_seconds",
        "daily_chat_message_delay_max_seconds",
        "autonomous_silence_seconds",
        "planner_group_debounce_seconds",
        "memory_batch_seconds",
        "relationship_batch_seconds",
    )
    @classmethod
    def _non_negative_delay(cls, value: float) -> float:
        if value < 0:
            raise ValueError("must not be negative")
        return value

    @model_validator(mode="after")
    def _validate_daily_chat_delay_range(self) -> Self:
        if self.daily_chat_message_delay_min_seconds > self.daily_chat_message_delay_max_seconds:
            raise ValueError(
                "DAILY_CHAT_MESSAGE_DELAY_MIN_SECONDS must not exceed "
                "DAILY_CHAT_MESSAGE_DELAY_MAX_SECONDS"
            )
        return self

    @field_validator(
        "autonomous_confidence_threshold",
        "planner_temperature",
        "planner_confidence_threshold",
        "relationship_confidence_threshold",
        "vision_low_confidence_retry_threshold",
        "emoji_auto_adopt_min_confidence",
    )
    @classmethod
    def _probability(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("must be between zero and one")
        return value

    @field_validator("web_search_depth")
    @classmethod
    def _web_search_depth(cls, value: str) -> str:
        normalized = value.casefold()
        if normalized not in {"basic", "advanced"}:
            raise ValueError("WEB_SEARCH_DEPTH must be basic or advanced")
        return normalized

    @field_validator("emoji_collection_mode")
    @classmethod
    def _emoji_collection_mode(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in {"metadata_only", "likely", "all_images"}:
            raise ValueError("EMOJI_COLLECTION_MODE must be metadata_only, likely, or all_images")
        return normalized

    @field_validator("emoji_replacement_mode")
    @classmethod
    def _emoji_replacement_mode(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in {"off", "score", "llm", "hybrid"}:
            raise ValueError("EMOJI_REPLACEMENT_MODE must be off, score, llm, or hybrid")
        return normalized

    @field_validator("emoji_near_duplicate_distance")
    @classmethod
    def _emoji_near_duplicate_distance(cls, value: int) -> int:
        if not 0 <= value <= 64:
            raise ValueError("EMOJI_NEAR_DUPLICATE_DISTANCE must be between zero and 64")
        return value

    @field_validator("emoji_pool_capacity")
    @classmethod
    def _emoji_pool_capacity(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("EMOJI_POOL_CAPACITY must be positive when configured")
        return value

    @field_validator("default_timezone")
    @classmethod
    def _valid_default_timezone(cls, value: str) -> str:
        normalized = value.strip()
        try:
            ZoneInfo(normalized)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("DEFAULT_TIMEZONE must be a valid IANA timezone") from exc
        return normalized

    @field_validator(
        "relationship_initial_affection",
        "relationship_initial_trust",
        "trust_affection_cap_offset",
        "conflict_preference_min_gap",
    )
    @classmethod
    def _relationship_score_or_gap(cls, value: int) -> int:
        if not 0 <= value <= 100:
            raise ValueError("must be between zero and 100")
        return value

    @model_validator(mode="after")
    def _validate_memory_limits(self) -> Self:
        if self.person_memory_max_entries > 100:
            raise ValueError("PERSON_MEMORY_MAX_ENTRIES must not exceed 100")
        if self.group_memory_max_entries > 100:
            raise ValueError("GROUP_MEMORY_MAX_ENTRIES must not exceed 100")
        if self.related_people_limit > 5:
            raise ValueError("RELATED_PEOPLE_LIMIT must not exceed 5")
        if self.person_group_memory_max_entries > 50:
            raise ValueError("PERSON_GROUP_MEMORY_MAX_ENTRIES must not exceed 50")
        if self.preference_max_entries > 30:
            raise ValueError("PREFERENCE_MAX_ENTRIES must not exceed 30")
        if self.memory_batch_max_events > 20:
            raise ValueError("MEMORY_BATCH_MAX_EVENTS must not exceed 20")
        if self.agent_max_tool_calls > 16:
            raise ValueError("AGENT_MAX_TOOL_CALLS must not exceed 16")
        if self.relationship_batch_max_turns > 10:
            raise ValueError("RELATIONSHIP_BATCH_MAX_TURNS must not exceed 10")
        if self.affection_max_auto_delta > 2:
            raise ValueError("AFFECTION_MAX_AUTO_DELTA must not exceed 2")
        if self.trust_max_auto_delta > 2:
            raise ValueError("TRUST_MAX_AUTO_DELTA must not exceed 2")
        if self.web_search_max_results > 5:
            raise ValueError("WEB_SEARCH_MAX_RESULTS must not exceed 5")
        if self.web_extract_max_results > 3:
            raise ValueError("WEB_EXTRACT_MAX_RESULTS must not exceed 3")
        if self.web_max_retries > 1:
            raise ValueError("WEB_MAX_RETRIES must not exceed 1")
        if self.web_max_calls_per_turn > 3:
            raise ValueError("WEB_MAX_CALLS_PER_TURN must not exceed 3")
        if self.web_tool_result_max_characters > 16000:
            raise ValueError("WEB_TOOL_RESULT_MAX_CHARACTERS must not exceed 16000")
        if self.web_source_max_runs_per_conversation > 10:
            raise ValueError("WEB_SOURCE_MAX_RUNS_PER_CONVERSATION must not exceed 10")
        if self.vision_max_images_per_turn > 5:
            raise ValueError("VISION_MAX_IMAGES_PER_TURN must not exceed 5")
        if self.vision_gif_max_frames > 8:
            raise ValueError("VISION_GIF_MAX_FRAMES must not exceed 8")
        if self.vision_max_frames_per_turn > 16:
            raise ValueError("VISION_MAX_FRAMES_PER_TURN must not exceed 16")
        if self.vision_max_download_bytes > 20 * 1024 * 1024:
            raise ValueError("VISION_MAX_DOWNLOAD_BYTES must not exceed 20 MB")
        if self.vision_max_retries > 1:
            raise ValueError("VISION_MAX_RETRIES must not exceed 1")
        if self.vision_thinking_budget > 32768:
            raise ValueError("VISION_THINKING_BUDGET must not exceed 32768")
        if self.automation_max_steps > 16:
            raise ValueError("AUTOMATION_MAX_STEPS must not exceed 16")
        if self.automation_max_llm_calls_per_run > 5:
            raise ValueError("AUTOMATION_MAX_LLM_CALLS_PER_RUN must not exceed 5")
        if self.automation_max_tool_calls_per_run > 16:
            raise ValueError("AUTOMATION_MAX_TOOL_CALLS_PER_RUN must not exceed 16")
        if self.automation_max_messages_per_run > 10:
            raise ValueError("AUTOMATION_MAX_MESSAGES_PER_RUN must not exceed 10")
        if self.automation_min_interval_seconds < 60:
            raise ValueError("AUTOMATION_MIN_INTERVAL_SECONDS must be at least 60")
        if self.planner_reply_necessity_threshold > 100:
            raise ValueError("PLANNER_REPLY_NECESSITY_THRESHOLD must not exceed 100")
        if self.planner_group_debounce_seconds > 60:
            raise ValueError("PLANNER_GROUP_DEBOUNCE_SECONDS must not exceed 60")
        if self.planner_preferred_messages > 20:
            raise ValueError("PLANNER_PREFERRED_MESSAGES must not exceed 20")
        if self.planner_max_pending_messages > 100:
            raise ValueError("PLANNER_MAX_PENDING_MESSAGES must not exceed 100")
        if self.planner_max_wait_seconds > 300:
            raise ValueError("PLANNER_MAX_WAIT_SECONDS must not exceed 300")
        if self.reply_plan_hard_max_messages > 20:
            raise ValueError("REPLY_PLAN_HARD_MAX_MESSAGES must not exceed 20")
        if self.plugin_max_total_prompt_characters <= self.plugin_max_prompt_fragment_characters:
            raise ValueError(
                "PLUGIN_MAX_TOTAL_PROMPT_CHARACTERS must exceed "
                "PLUGIN_MAX_PROMPT_FRAGMENT_CHARACTERS"
            )
        if (
            self.plugin_max_prompt_characters_per_plugin
            < self.plugin_max_prompt_fragment_characters
        ):
            raise ValueError(
                "PLUGIN_MAX_PROMPT_CHARACTERS_PER_PLUGIN must be at least "
                "PLUGIN_MAX_PROMPT_FRAGMENT_CHARACTERS"
            )
        return self

    @field_validator("plugin_api_version")
    @classmethod
    def _valid_plugin_api_version(cls, value: str) -> str:
        normalized = value.strip()
        if re.fullmatch(r"[1-9][0-9]*\.[0-9]+", normalized) is None:
            raise ValueError("PLUGIN_API_VERSION must use major.minor format")
        return normalized

    @field_validator("plugin_directory")
    @classmethod
    def _safe_plugin_directory(cls, value: Path) -> Path:
        path = Path(value)
        resolved = path.resolve(strict=False)
        sensitive = {Path(resolved.anchor), Path.home().resolve(strict=False)}
        if resolved in sensitive:
            raise ValueError("PLUGIN_DIRECTORY must not point to a filesystem root or home")
        lowered = {part.casefold() for part in resolved.parts}
        if {"windows", "system32"}.issubset(lowered):
            raise ValueError("PLUGIN_DIRECTORY must not point to a system directory")
        return path

    @model_validator(mode="after")
    def _validate_web_configuration(self) -> Self:
        if self.web_enabled and not self.tavily_api_key:
            raise ValueError("TAVILY_API_KEY is required when WEB_ENABLED=true")
        return self

    @model_validator(mode="after")
    def _validate_vision_configuration(self) -> Self:
        if self.vision_enabled:
            missing = [
                name
                for name, value in (
                    ("VISION_BASE_URL", self.vision_base_url),
                    ("VISION_API_KEY", self.vision_api_key),
                    ("VISION_MODEL", self.vision_model),
                )
                if not value.strip()
            ]
            if missing:
                raise ValueError(f"{', '.join(missing)} required when VISION_ENABLED=true")
        return self

    @model_validator(mode="after")
    def _load_system_prompt_file(self) -> Self:
        """Load a UTF-8 prompt file when explicitly configured."""

        if self.system_prompt_file is None:
            return self
        try:
            prompt = self.system_prompt_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"cannot read SYSTEM_PROMPT_FILE: {self.system_prompt_file}") from exc
        if not prompt:
            raise ValueError("SYSTEM_PROMPT_FILE must not be empty")
        self.system_prompt = prompt
        return self

    @cached_property
    def superusers(self) -> frozenset[str]:
        return _csv_set(self.superusers_csv)

    @cached_property
    def allowed_private_users(self) -> frozenset[str]:
        return _csv_set(self.allowed_private_users_csv) | self.superusers

    @cached_property
    def enabled_groups(self) -> frozenset[str]:
        return _csv_set(self.enabled_groups_csv)

    @cached_property
    def ignored_bot_users(self) -> frozenset[str]:
        return _csv_set(self.ignored_bot_users_csv)

    @property
    def sqlite_path(self) -> Path | None:
        """Return the local SQLite path without exposing it in user-facing output."""

        prefix = "sqlite+aiosqlite:///"
        if not self.database_url.startswith(prefix):
            return None
        return Path(self.database_url.removeprefix(prefix))

    @property
    def llm_configured(self) -> bool:
        """Whether enough configuration exists to make LLM requests."""

        if self.llm_provider.casefold() == "fake":
            return True
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def web_configured(self) -> bool:
        """Whether controlled web search is enabled with provider credentials."""

        return bool(self.web_enabled and self.tavily_api_key)

    @property
    def vision_configured(self) -> bool:
        """Whether vision is enabled with all required provider configuration."""

        return bool(
            self.vision_enabled
            and self.vision_base_url.strip()
            and self.vision_api_key.strip()
            and self.vision_model.strip()
        )

    @property
    def planner_configured(self) -> bool:
        """Whether Planner may use the already configured main LLM provider."""

        return bool(self.planner_enabled and self.llm_configured)
