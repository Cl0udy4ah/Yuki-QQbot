"""Apply Planner-authorized persistent voice preferences."""

from __future__ import annotations

from qq_ai_bot.automation.models import TurnOrigin
from qq_ai_bot.speech.models import VoicePreferenceDuration, VoiceReplyPlan
from qq_ai_bot.speech.preference_repository import (
    PersonSpeechPreference,
    VoicePreferenceRepository,
)


class VoicePreferenceService:
    """Persist only explicit, person-authored, future-facing mode changes."""

    def __init__(self, repository: VoicePreferenceRepository) -> None:
        self._repository = repository

    async def apply(
        self,
        voice: VoiceReplyPlan,
        *,
        user_id: str,
        source_message_id: str,
        origin: TurnOrigin,
    ) -> PersonSpeechPreference | None:
        change = voice.preference_change
        if (
            change is None
            or change.duration is not VoicePreferenceDuration.PERSISTENT
            or origin is not TurnOrigin.USER_MESSAGE
        ):
            return None
        return await self._repository.set(
            user_id,
            change.mode,
            source_message_id=source_message_id,
        )


__all__ = ["VoicePreferenceService"]
