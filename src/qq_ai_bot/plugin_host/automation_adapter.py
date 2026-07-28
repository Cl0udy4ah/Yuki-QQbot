"""Map approved plugin actions into the one existing automation registry."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, cast

from pydantic import BaseModel

from qq_ai_bot.automation.authority import PermissionLevel
from qq_ai_bot.automation.models import RetryPolicy, RiskClass, TurnOrigin
from qq_ai_bot.automation.registry import (
    AutomationCapability,
    AutomationCapabilityRegistry,
    CapabilityExecutionContext,
    CapabilityHandler,
    CapabilityResult,
)
from qq_ai_bot.plugin_host.extension_registry import ExtensionKind, ExtensionRegistry
from qq_ai_bot.plugin_host.facades import PluginInvocation
from qq_ai_bot.plugin_host.manifest import PluginManifest
from yuki_plugin_sdk.models import PermissionLevel as SdkPermissionLevel
from yuki_plugin_sdk.registrar import AutomationActionRegistration
from yuki_plugin_sdk.results import PluginResult

InvocationScopeFactory = Callable[
    [str, PluginInvocation],
    AbstractAsyncContextManager[object],
]


class PluginAutomationAdapter:
    def __init__(
        self,
        *,
        extensions: ExtensionRegistry,
        automation: AutomationCapabilityRegistry,
        invocation_scope: InvocationScopeFactory | None = None,
    ) -> None:
        self._extensions = extensions
        self._automation = automation
        self._invocation_scope = invocation_scope

    def activate(self, manifest: PluginManifest) -> int:
        self.deactivate(manifest.id)
        count = 0
        for item in self._extensions.list(
            plugin_id=manifest.id,
            kind=ExtensionKind.AUTOMATION_ACTION,
        ):
            registration = cast(AutomationActionRegistration, item.registration)
            name = f"plugin.{manifest.id}.{registration.metadata.name}"
            self._automation.register(
                AutomationCapability(
                    name=name,
                    description=registration.metadata.description,
                    argument_model=registration.input_model,
                    output_schema=registration.output_model.model_json_schema(),
                    required_permission=(
                        PermissionLevel.SUPERUSER
                        if registration.metadata.permission is SdkPermissionLevel.SUPERUSER
                        else PermissionLevel.USER
                    ),
                    risk_class=RiskClass(registration.metadata.risk.value),
                    retry_policy=RetryPolicy(registration.metadata.retry_policy.value),
                    allowed_origins=frozenset(
                        TurnOrigin(origin.value)
                        for origin in registration.metadata.allowed_origins
                        if origin.value in {item.value for item in TurnOrigin}
                    ),
                    schema_version=registration.metadata.schema_version,
                    provider_plugin_id=manifest.id,
                    provider_version=manifest.version,
                    provider_manifest_hash=manifest.manifest_hash,
                    handler=self._handler(manifest.id, registration),
                )
            )
            count += 1
        return count

    def deactivate(self, plugin_id: str) -> int:
        return self._automation.unregister_plugin(plugin_id)

    def _handler(
        self,
        plugin_id: str,
        registration: AutomationActionRegistration,
    ) -> CapabilityHandler:
        async def execute(
            arguments: dict[str, Any],
            context: CapabilityExecutionContext,
        ) -> CapabilityResult:
            model = registration.input_model.model_validate(arguments)
            scope_factory = self._invocation_scope
            if scope_factory is None:
                raise RuntimeError("plugin automation invocation scope is unavailable")
            invocation = _automation_invocation(plugin_id, context)
            async with asyncio.timeout(registration.metadata.timeout_seconds):
                async with scope_factory(plugin_id, invocation):
                    value = await registration.handler(model)
            if isinstance(value, PluginResult):
                if not value.ok:
                    raise RuntimeError(value.error_code or "plugin_automation_failed")
                data = dict(value.data)
            elif isinstance(value, BaseModel):
                validated = registration.output_model.model_validate(
                    value.model_dump(mode="python")
                )
                data = validated.model_dump(mode="json")
            else:
                validated = registration.output_model.model_validate(value)
                data = validated.model_dump(mode="json")
            return CapabilityResult(data=data)

        return execute


def _automation_invocation(
    plugin_id: str,
    context: CapabilityExecutionContext,
) -> PluginInvocation:
    """Project a validated scheduled capability context into plugin authority."""

    authority = context.authority
    if authority.origin is not TurnOrigin.SCHEDULED_AUTOMATION:
        raise RuntimeError("plugin automation requires scheduled authority")
    if authority.actor_user_id != context.creator_user_id:
        raise RuntimeError("plugin automation creator does not match trusted authority")
    if authority.bot_user_id != context.bot_user_id:
        raise RuntimeError("plugin automation bot does not match trusted authority")
    delegated = authority.delegated_authority
    if delegated is None:
        raise RuntimeError("plugin automation requires delegated authority")
    if delegated.bot_user_id != authority.bot_user_id:
        raise RuntimeError("plugin automation delegation belongs to another bot")
    if delegated.current_group_id != context.current_group_id:
        raise RuntimeError("plugin automation group does not match delegated authority")
    return PluginInvocation(
        plugin_id=plugin_id,
        origin=TurnOrigin.SCHEDULED_AUTOMATION,
        actor_user_id=authority.actor_user_id,
        bot_user_id=authority.bot_user_id,
        delegated_authority=delegated,
        allowed_capabilities=authority.allowed_capabilities,
        web_was_used=context.web_was_used,
    )


__all__ = ["InvocationScopeFactory", "PluginAutomationAdapter"]
