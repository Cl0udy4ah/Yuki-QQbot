"""One strict structured-output path for multiple model tasks."""

from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from qq_ai_bot.domain.messages import ChatMessage, ChatRequest, ChatTool
from qq_ai_bot.model_runtime.executor import ModelExecutor
from qq_ai_bot.model_runtime.models import ModelTask, StructuredOutputMode

OutputT = TypeVar("OutputT", bound=BaseModel)


class StructuredTaskError(RuntimeError):
    """A structured request did not produce exactly one valid result."""


class StructuredTaskRunner:
    """Render schema from Pydantic and validate the provider result exactly once."""

    def __init__(self, models: ModelExecutor) -> None:
        self._models = models

    async def run(
        self,
        *,
        task: ModelTask,
        instruction: str,
        structured_input: BaseModel | dict[str, Any] | list[Any],
        output_model: type[OutputT],
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        mode: StructuredOutputMode | None = None,
        allow_text_json: bool = False,
    ) -> OutputT:
        if not instruction.strip():
            raise ValueError("structured task instruction must not be empty")
        effective_mode = mode or self._models.structured_output_mode(task)
        if effective_mode is StructuredOutputMode.TEXT_JSON and not allow_text_json:
            raise ValueError("text_json mode must be explicitly enabled for this task")
        if isinstance(structured_input, BaseModel):
            payload: Any = structured_input.model_dump(
                mode="json",
                exclude_none=True,
                exclude_defaults=True,
                exclude_computed_fields=True,
            )
        else:
            payload = structured_input
        schema = output_model.model_json_schema()
        tools: tuple[ChatTool, ...] = ()
        tool_choice: str | None = None
        response_format: dict[str, object] | None = None
        if effective_mode is StructuredOutputMode.FUNCTION_TOOL:
            tools = (
                ChatTool(
                    name="emit_result",
                    description="Return the validated task result.",
                    parameters=schema,
                ),
            )
            tool_choice = "required"
        elif effective_mode is StructuredOutputMode.JSON_SCHEMA:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "emit_result",
                    "strict": True,
                    "schema": schema,
                },
            }
        response = await self._models.execute(
            task,
            ChatRequest(
                messages=(
                    ChatMessage(role="system", content=instruction.strip()),
                    ChatMessage(
                        role="user",
                        content=json.dumps(
                            payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    ),
                ),
                model=self._models.model_name(task),
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                thinking_enabled=False,
                tools=tools,
                tool_choice=tool_choice,
                response_format=response_format,
            ),
        )
        try:
            if effective_mode is StructuredOutputMode.FUNCTION_TOOL:
                if len(response.tool_calls) != 1:
                    raise StructuredTaskError(
                        "structured task must return exactly one emit_result call"
                    )
                call = response.tool_calls[0]
                if call.function.name != "emit_result":
                    raise StructuredTaskError("structured task returned an unknown function")
                decoded = json.loads(call.function.arguments)
            else:
                decoded = json.loads(response.content.strip())
                if not isinstance(decoded, dict):
                    raise StructuredTaskError("structured text result must be one object")
            return output_model.model_validate(decoded)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise StructuredTaskError("structured task returned an invalid result") from exc
