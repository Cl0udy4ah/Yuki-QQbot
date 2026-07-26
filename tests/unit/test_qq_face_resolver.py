"""QQ face mapping stays local and deterministic."""

from qq_ai_bot.services.qq_face_resolver import QQFaceResolver


def test_known_face_uses_local_mapping() -> None:
    resolver = QQFaceResolver(mapping={14: "微笑"})

    assert resolver.resolve("14") == "微笑"
    assert resolver.format_placeholder(14) == "[QQ表情：微笑]"


def test_unknown_face_preserves_id() -> None:
    resolver = QQFaceResolver(mapping={})

    assert resolver.resolve(987654) == "ID 987654"
    assert resolver.format_placeholder("987654") == "[QQ表情：ID 987654]"


def test_missing_mapping_file_fails_closed() -> None:
    resolver = QQFaceResolver("definitely-not-present.json")

    assert resolver.resolve(14) == "ID 14"
