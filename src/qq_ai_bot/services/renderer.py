"""Safe plain-text cleanup and Unicode-preserving QQ message splitting."""

from __future__ import annotations

import re

from qq_ai_bot.llm.base import LLMEmptyResponseError

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)]\((?:https?://|mailto:)[^)]+\)")
_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s+")
_HORIZONTAL_RULE = re.compile(r"(?m)^\s*[-*_]{3,}\s*$")
_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？!?；;])")


def sanitize_input(text: str) -> str:
    """Normalize line endings and remove unsafe control characters."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _CONTROL_CHARACTERS.sub("", normalized)
    return "\n".join(line.rstrip() for line in normalized.splitlines()).strip()


def clean_model_output(text: str, *, max_characters: int) -> str:
    """Validate and simplify complex Markdown while preserving code fences."""

    cleaned = sanitize_input(text)
    if not cleaned:
        raise LLMEmptyResponseError("model returned empty content")
    cleaned = _MARKDOWN_LINK.sub(r"\1", cleaned)
    cleaned = _HEADING.sub("", cleaned)
    cleaned = _HORIZONTAL_RULE.sub("", cleaned)
    cleaned = cleaned.strip()
    if not cleaned:
        raise LLMEmptyResponseError("model returned empty content")
    return cleaned[:max_characters]


def _split_hard(text: str, limit: int) -> list[str]:
    return [text[index : index + limit] for index in range(0, len(text), limit)]


def _split_sentence(text: str, limit: int) -> list[str]:
    sentences = [part for part in _SENTENCE_BOUNDARY.split(text) if part]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_hard(sentence, limit))
        elif len(current) + len(sentence) <= limit:
            current += sentence
        else:
            chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks


def split_qq_message(text: str, *, limit: int) -> tuple[str, ...]:
    """Split by paragraphs, then sentences, then Python Unicode code points."""

    if not text:
        return ()
    paragraphs = re.split(r"(\n\s*\n)", text)
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if not paragraph:
            continue
        if len(paragraph) > limit:
            if current.strip():
                chunks.append(current.strip())
            current = ""
            chunks.extend(
                part.strip() for part in _split_sentence(paragraph, limit) if part.strip()
            )
        elif len(current) + len(paragraph) <= limit:
            current += paragraph
        else:
            if current.strip():
                chunks.append(current.strip())
            current = paragraph
    if current.strip():
        chunks.append(current.strip())
    return tuple(chunks)
