"""Environment-driven application settings with safe defaults."""

from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Self

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
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 1
    llm_temperature: float = 0.7
    llm_max_output_tokens: int = 1024
    llm_thinking_enabled: bool | None = None
    system_prompt: str = (
        "你是一个运行在 QQ 中的 AI 助手。请只输出给用户的最终回答，不要输出隐藏的推理过程。"
        "不要声称执行了工具、代码、命令、文件访问或网络操作。"
    )
    system_prompt_file: Path | None = None

    database_url: str = "sqlite+aiosqlite:///./data/qq_ai_bot.db"
    processed_event_ttl_seconds: int = 86400
    processed_event_cleanup_seconds: int = 3600
    max_context_messages: int = 30
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
    group_memory_enabled: bool = True
    group_memory_max_entries: int = 100

    observe_enabled_groups: bool = True
    autonomous_group_chat_enabled: bool = True
    autonomous_silence_seconds: float = 8.0
    autonomous_confidence_threshold: float = 0.85
    autonomous_cooldown_seconds: int = 300
    autonomous_max_per_hour: int = 3
    recent_history_tool_limit: int = 20
    local_context_event_limit: int = 30
    related_people_limit: int = 5
    person_memory_max_entries: int = 100
    person_group_memory_max_entries: int = 50
    preference_max_entries: int = 30
    memory_batch_seconds: float = 30.0
    memory_batch_trigger_count: int = 10
    memory_batch_max_events: int = 20
    agent_max_tool_calls: int = 5
    agent_max_model_requests: int = 6
    agent_tool_result_max_characters: int = 32000

    @field_validator(
        "app_port",
        "llm_max_output_tokens",
        "processed_event_ttl_seconds",
        "processed_event_cleanup_seconds",
        "max_context_messages",
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
    )
    @classmethod
    def _positive_integer(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be greater than zero")
        return value

    @field_validator("llm_timeout_seconds")
    @classmethod
    def _positive_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be greater than zero")
        return value

    @field_validator("llm_max_retries")
    @classmethod
    def _non_negative_retries(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must not be negative")
        return value

    @field_validator(
        "daily_chat_message_delay_min_seconds",
        "daily_chat_message_delay_max_seconds",
        "autonomous_silence_seconds",
        "memory_batch_seconds",
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

    @field_validator("autonomous_confidence_threshold")
    @classmethod
    def _probability(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("must be between zero and one")
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
        if self.agent_max_tool_calls > 5:
            raise ValueError("AGENT_MAX_TOOL_CALLS must not exceed 5")
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
