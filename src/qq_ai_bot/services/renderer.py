"""Safe plain-text cleanup and Unicode-preserving QQ message splitting."""

from __future__ import annotations

import re

from qq_ai_bot.llm.base import LLMEmptyResponseError

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)]\(((?:https?://|mailto:)[^)]+)\)")
_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s+")
_HORIZONTAL_RULE = re.compile(r"(?m)^\s*[-*_]{3,}\s*$")
_INTERNAL_HISTORY_MARKER = re.compile(
    r"\[(?:(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]) )?"
    r"(?:[01]\d|2[0-3]):[0-5]\d(?: QQ [1-9]\d{4,19})?\]\s*"
)
_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？!?；;])")
_STRUCTURED_OUTPUT = re.compile(r"(?m)^\s*(?:```|~~~|[-*+]\s+|\d+[.)、]\s+|>\s+|\|.*\|\s*$)")
_DAILY_SENTENCE_ENDINGS = frozenset("。！？!?")
_SENTENCE_CLOSERS = frozenset('”’"』】）)]')


def sanitize_input(text: str) -> str:
    """Normalize line endings and remove unsafe control characters."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _CONTROL_CHARACTERS.sub("", normalized)
    return "\n".join(line.rstrip() for line in normalized.splitlines()).strip()


def clean_model_output(text: str, *, max_characters: int) -> str:
    """Validate model text and remove backend-only history annotations."""

    cleaned = sanitize_input(text)
    if not cleaned:
        raise LLMEmptyResponseError("model returned empty content")
    # Recent history is timestamped only so the model can reason about chronology.
    # Treat an echoed marker as an internal annotation, wherever it appears in a
    # generated reply, and never expose it as ordinary QQ message text.
    cleaned = _INTERNAL_HISTORY_MARKER.sub("", cleaned)
    cleaned = _MARKDOWN_LINK.sub(r"\1 (\2)", cleaned)
    cleaned = _HEADING.sub("", cleaned)
    cleaned = _HORIZONTAL_RULE.sub("", cleaned)
    cleaned = cleaned.strip()
    if not cleaned:
        raise LLMEmptyResponseError("model returned empty content")
    return cleaned[:max_characters]


def _plain_sentences(text: str) -> tuple[str, ...]:
    """Split plain conversational prose while preserving sentence punctuation."""

    sentences: list[str] = []
    for raw_line in text.splitlines():
        normalized = re.sub(r"\s+", " ", raw_line).strip()
        if not normalized:
            continue
        start = 0
        index = 0
        while index < len(normalized):
            character = normalized[index]
            boundary = character in _DAILY_SENTENCE_ENDINGS
            end = index + 1
            while end < len(normalized) and (
                normalized[end] in _DAILY_SENTENCE_ENDINGS or normalized[end] in _SENTENCE_CLOSERS
            ):
                end += 1
            if character == ".":
                boundary = end == len(normalized) or normalized[end].isspace()
            if boundary:
                sentence = normalized[start:end].strip()
                if sentence:
                    sentences.append(sentence)
                start = end
                index = end
            else:
                index += 1
        tail = normalized[start:].strip()
        if tail:
            sentences.append(tail)
    return tuple(sentences)


def _group_chat_sentences(sentences: tuple[str, ...], target: int) -> tuple[str, ...]:
    """Group adjacent semantic units without exceeding the requested message count."""

    group_count = min(len(sentences), target)
    groups: list[list[str]] = [[] for _ in range(group_count)]
    for index, sentence in enumerate(sentences):
        group_index = min(index * group_count // len(sentences), group_count - 1)
        groups[group_index].append(sentence)
    return tuple(" ".join(group) for group in groups if group)


def split_daily_chat_sentences(
    text: str,
    *,
    max_characters: int,
    max_messages: int,
) -> tuple[str, ...]:
    """Split short plain chat into sentences, otherwise preserve the original text."""

    if (
        not text
        or len(text) > max_characters
        or "```" in text
        or "~~~" in text
        or _STRUCTURED_OUTPUT.search(text)
    ):
        return (text,) if text else ()
    sentences = _plain_sentences(text)
    if len(sentences) < 2 or max_messages < 2:
        return (text,)
    return _group_chat_sentences(sentences, max_messages)


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
