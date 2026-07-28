"""Resolve a safe Genie target language from Planner intent and actual reply text."""

from __future__ import annotations

import re

from qq_ai_bot.speech.models import SpeechLanguageHint, VoiceProfile

_KANA = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")


def resolve_target_language(profile: VoiceProfile, text: str, hint: str = "auto") -> str:
    """Prefer strong script evidence, then a validated Planner hint, then profile default."""

    supported = set(profile.supported_languages) or {profile.language}
    detected = _detected_language(text)
    if detected is not None and detected in supported:
        return detected
    try:
        requested = SpeechLanguageHint(hint).value
    except ValueError:
        requested = SpeechLanguageHint.AUTO.value
    if requested != SpeechLanguageHint.AUTO.value and requested in supported:
        return requested
    return profile.language


def _detected_language(text: str) -> str | None:
    if _KANA.search(text):
        return "jp"
    if _CJK.search(text):
        return "zh"
    if _LATIN.search(text):
        return "en"
    return None
