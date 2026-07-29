"""Provider-neutral model task, profile, route, and usage models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelTask(StrEnum):
    """Stable business purpose attached to every main-model invocation."""

    CHAT_AGENT = "chat_agent"
    PLANNER = "planner"
    MEMORY_EXTRACTION = "memory_extraction"
    RELATIONSHIP_EVALUATION = "relationship_evaluation"
    EMOJI_REPLACEMENT = "emoji_replacement"
    AUTOMATION_TEXT_GENERATION = "automation_text_generation"
    AUTOMATION_AGENT = "automation_agent"
    PLUGIN_AGENT_SESSION = "plugin_agent_session"
    UTILITY_STRUCTURED = "utility_structured"


class ModelCapability(StrEnum):
    """Features a profile must explicitly advertise."""

    TOOLS = "tools"
    STRUCTURED_OUTPUT = "structured_output"
    REASONING = "reasoning"
    LONG_CONTEXT = "long_context"


class StructuredOutputMode(StrEnum):
    """Supported provider strategies for one validated object."""

    FUNCTION_TOOL = "function_tool"
    JSON_SCHEMA = "json_schema"
    TEXT_JSON = "text_json"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelProfile(_FrozenModel):
    """One named provider endpoint and its defaults."""

    id: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9_.-]+$")
    provider: str = Field(min_length=1)
    base_url: str = ""
    api_key_env: str = ""
    model: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    max_retries: int = Field(ge=0)
    default_temperature: float = Field(ge=0, le=2)
    default_max_output_tokens: int = Field(gt=0)
    thinking_enabled: bool | None = None
    structured_output_mode: StructuredOutputMode = StructuredOutputMode.FUNCTION_TOOL
    capabilities: frozenset[ModelCapability] = frozenset()

    @model_validator(mode="after")
    def _validate_endpoint(self) -> ModelProfile:
        if self.provider.casefold() != "fake" and not self.base_url:
            raise ValueError("base_url is required for non-fake model profiles")
        if self.provider.casefold() != "fake" and not self.api_key_env:
            raise ValueError("api_key_env is required for non-fake model profiles")
        return self


class ModelRoute(_FrozenModel):
    """Bind one business task to one profile and required capabilities."""

    task: ModelTask
    profile_id: str = Field(min_length=1)
    required_capabilities: frozenset[ModelCapability] = frozenset()


class ModelInvocationRecord(_FrozenModel):
    """Content-free usage and latency record returned by the repository."""

    id: int
    task: ModelTask
    profile_id: str
    provider: str
    model: str
    success: bool
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_prompt_tokens: int | None = None
    latency_seconds: float
    error_category: str | None = None
    created_at: datetime


class ModelStats(_FrozenModel):
    """Aggregated model usage without prompts, users, or tool payloads."""

    invocations: int = 0
    successes: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_prompt_tokens: int = 0
    unknown_usage: int = 0
    average_latency_seconds: float = 0
