"""Unified capability metadata and policy API."""

from qq_ai_bot.capabilities.models import (
    AuthorityContext,
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityIdempotency,
    CapabilityRisk,
    CapabilityTrustSource,
)
from qq_ai_bot.capabilities.policy import CapabilityPolicyContext, CapabilityPolicyEngine
from qq_ai_bot.capabilities.provider import CapabilityProvider, ChatToolCapabilityProvider
from qq_ai_bot.capabilities.registry import CapabilityRegistry
from qq_ai_bot.capabilities.results import CapabilityResult

__all__ = [
    "AuthorityContext",
    "CapabilityDescriptor",
    "CapabilityEffect",
    "CapabilityIdempotency",
    "CapabilityPolicyContext",
    "CapabilityPolicyEngine",
    "CapabilityProvider",
    "CapabilityRegistry",
    "CapabilityResult",
    "CapabilityRisk",
    "CapabilityTrustSource",
    "ChatToolCapabilityProvider",
]
