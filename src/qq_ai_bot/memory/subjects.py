"""Deterministic subject aliases derived from trusted ledger metadata."""

from __future__ import annotations

import re
from dataclasses import dataclass

from qq_ai_bot.domain.conversations import ScopeType
from qq_ai_bot.memory.enums import MemoryScopeType, SelfMemoryVisibility
from qq_ai_bot.memory.extraction import AvailableSubject
from qq_ai_bot.persistence.people_repository import PeopleRepository
from qq_ai_bot.persistence.repository_records import EventRecord

_NAMED_SUBJECT = re.compile(
    r"(?:^|[，。！？；：,.!?;:\s])"
    r"(?P<name>[\u4e00-\u9fff·]{2,8}|[A-Za-z][A-Za-z0-9_.-]{1,31})"
    r"(?=\s*(?:不是|没有|不会|不能|住在|来自|负责|擅长|喜欢|讨厌|想要|已经|"
    r"曾经|今年|是|有|爱|想|会|能|在|叫|姓))"
)
_IGNORED_NAMES = frozenset(
    {
        "我们",
        "你们",
        "他们",
        "自己",
        "今天",
        "昨天",
        "明天",
        "现在",
        "最近",
        "以后",
        "Yuki",
        "yuki",
    }
)


@dataclass(frozen=True, slots=True)
class ResolvedSubject:
    scope_type: MemoryScopeType
    subject_user_id: str | None
    group_id: str | None
    visibility_type: SelfMemoryVisibility | None = None
    visibility_user_id: str | None = None
    visibility_group_id: str | None = None


@dataclass(frozen=True, slots=True)
class SubjectResolutionContext:
    """One event's model aliases and their backend-owned resolutions."""

    available_subjects: tuple[AvailableSubject, ...]
    resolved_subjects: tuple[tuple[str, ResolvedSubject], ...]

    def resolve(self, subject_ref: str) -> ResolvedSubject | None:
        return next((value for key, value in self.resolved_subjects if key == subject_ref), None)


class SubjectResolver:
    """Map model-visible aliases to backend-owned QQ and group identifiers."""

    @staticmethod
    def context(event: EventRecord) -> SubjectResolutionContext:
        scopes = [MemoryScopeType.PERSON]
        if event.scope_type is ScopeType.GROUP:
            scopes.append(MemoryScopeType.PERSON_GROUP)
        available = [
            AvailableSubject(
                subject_ref="speaker",
                display_label="当前发送者",
                allowed_scopes=tuple(scopes),
                relation_to_speaker="self",
            )
        ]
        resolved: list[tuple[str, ResolvedSubject]] = [
            ("speaker:person", ResolvedSubject(MemoryScopeType.PERSON, event.sender_user_id, None))
        ]
        if event.scope_type is ScopeType.GROUP and event.group_id:
            resolved.append(
                (
                    "speaker:person_group",
                    ResolvedSubject(
                        MemoryScopeType.PERSON_GROUP,
                        event.sender_user_id,
                        event.group_id,
                    ),
                )
            )
            available.append(
                AvailableSubject(
                    subject_ref="group",
                    display_label="当前群",
                    allowed_scopes=(MemoryScopeType.GROUP,),
                    relation_to_speaker="current_group",
                )
            )
            resolved.append(
                ("group:group", ResolvedSubject(MemoryScopeType.GROUP, None, event.group_id))
            )
            seen = {event.sender_user_id, event.bot_user_id, ""}
            mention_number = 0
            for user_id in event.mentioned_user_ids:
                if user_id in seen:
                    continue
                seen.add(user_id)
                mention_number += 1
                subject_ref = f"mentioned_{mention_number}"
                available.append(
                    AvailableSubject(
                        subject_ref=subject_ref,
                        display_label=f"被提及成员{mention_number}",
                        allowed_scopes=(MemoryScopeType.PERSON_GROUP,),
                        relation_to_speaker="mentioned_member",
                    )
                )
                resolved.append(
                    (
                        f"{subject_ref}:person_group",
                        ResolvedSubject(MemoryScopeType.PERSON_GROUP, user_id, event.group_id),
                    )
                )
            reply_author = event.reply_sender_user_id or ""
            if reply_author not in seen:
                available.append(
                    AvailableSubject(
                        subject_ref="reply_author",
                        display_label="回复消息作者",
                        allowed_scopes=(MemoryScopeType.PERSON_GROUP,),
                        relation_to_speaker="reply_author",
                    )
                )
                resolved.append(
                    (
                        "reply_author:person_group",
                        ResolvedSubject(
                            MemoryScopeType.PERSON_GROUP,
                            reply_author,
                            event.group_id,
                        ),
                    )
                )
        return SubjectResolutionContext(tuple(available), tuple(resolved))

    @staticmethod
    def available(event: EventRecord) -> tuple[AvailableSubject, ...]:
        return SubjectResolver.context(event).available_subjects

    @staticmethod
    def resolve(
        event: EventRecord,
        *,
        subject_ref: str,
        scope_type: MemoryScopeType,
        context: SubjectResolutionContext | None = None,
    ) -> ResolvedSubject | None:
        if subject_ref == "self" and scope_type is MemoryScopeType.SELF:
            return ResolvedSubject(scope_type, None, None)
        selected = context or SubjectResolver.context(event)
        return selected.resolve(f"{subject_ref}:{scope_type.value}")


class SubjectContextBuilder:
    """Add only uniquely resolved current-group names to the trusted alias set."""

    def __init__(self, people: PeopleRepository | None = None) -> None:
        self._people = people

    async def build(self, event: EventRecord) -> SubjectResolutionContext:
        base = SubjectResolver.context(event)
        if self._people is None or event.scope_type is not ScopeType.GROUP or not event.group_id:
            return base
        names = tuple(
            dict.fromkeys(
                match.group("name")
                for match in _NAMED_SUBJECT.finditer(event.content)
                if match.group("name") not in _IGNORED_NAMES
            )
        )[:3]
        if not names:
            return base
        available = list(base.available_subjects)
        resolved = list(base.resolved_subjects)
        known_user_ids = {value.subject_user_id for _, value in resolved if value.subject_user_id}
        number = 0
        for name in names:
            matches = await self._people.find_group_members_by_exact_name(name, event.group_id)
            if len(matches) != 1 or matches[0] in known_user_ids:
                continue
            number += 1
            ref = f"named_{number}"
            available.append(
                AvailableSubject(
                    subject_ref=ref,
                    display_label=f"当前群唯一成员：{name}",
                    allowed_scopes=(MemoryScopeType.PERSON_GROUP,),
                    relation_to_speaker="unique_group_name",
                )
            )
            resolved.append(
                (
                    f"{ref}:person_group",
                    ResolvedSubject(MemoryScopeType.PERSON_GROUP, matches[0], event.group_id),
                )
            )
            known_user_ids.add(matches[0])
        return SubjectResolutionContext(tuple(available), tuple(resolved))
