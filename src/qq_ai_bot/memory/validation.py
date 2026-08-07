"""Backend validation that prevents model-selected identity attribution."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from qq_ai_bot.memory.enums import (
    MemoryAuthority,
    MemoryClaimOperation,
    MemoryEvidenceRelation,
    MemoryKind,
    MemoryRetention,
    MemoryScopeType,
    MemorySourceType,
)
from qq_ai_bot.memory.extraction import MemoryClaim
from qq_ai_bot.memory.models import MemoryEvidenceCreate, MemoryFactCreate
from qq_ai_bot.memory.quality_policy import AttributionPolicy, RetentionPolicy
from qq_ai_bot.memory.subjects import SubjectResolutionContext, SubjectResolver
from qq_ai_bot.memory.temporal import MemoryTemporalResolver
from qq_ai_bot.persistence.repository_records import EventRecord

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_EXPLICIT_MEMORY_PATTERNS = (
    re.compile(r"(?:记住|别忘|请保存|加入记忆)"),
    re.compile(r"^(?:请|要|一定要|以后)?记得(?!.*[吗么？?])"),
)
_MEMORY_OBJECT = (
    r"(?:这条|那条|某条|我的|你的|Yuki的|yuki的|长期)?记忆"
    r"(?!功能|模块|系统|代码|接口|工具|数据库|数据表|实现|逻辑|设计|架构)"
)
_MEMORY_MUTATION_PATTERNS = (
    re.compile(r"(?:记住|别忘|请保存|加入记忆)"),
    re.compile(r"^(?:请|要|一定要|以后)?记得(?!.*[吗么？?])"),
    re.compile(
        r"(?:删除|删掉|移除|清除|忘掉|忘记|撤销|恢复|还原|纠正|更正|修正|修改|更新|"
        rf"合并|调整|改归属).{{0,16}}{_MEMORY_OBJECT}"
    ),
    re.compile(
        rf"{_MEMORY_OBJECT}.{{0,16}}(?:删除|删掉|移除|清除|忘掉|忘记|撤销|恢复|"
        r"还原|纠正|更正|修正|修改|更新|合并|调整|改归属)"
    ),
    re.compile(r"(?:你|Yuki|yuki)?记错了"),
    re.compile(
        r"\b(?:remember|forget|correct|update|delete|remove|restore)\b.{0,24}\bmemor(?:y|ies)\b",
        re.IGNORECASE,
    ),
)
_MEMORY_COMMAND_PREFIX = re.compile(r"^(?:请)?(?:记住|记得|别忘|保存|加入记忆)\s*")
_INTERACTION_MARKERS = (
    "回复",
    "回答",
    "称呼",
    "语音",
    "表情",
    "格式",
    "简短",
    "句号",
    "引用",
    "yuki",
    "机器人",
)
_NAMED_OTHER_PREFIX = re.compile(
    r"^(?:(?:据说|听说|我听说|我觉得|听人说|其实|不过|而且|原来|好像|感觉|话说)\s*)?"
    r"(?P<subject>[\u4e00-\u9fff·]{2,8}|[A-Za-z][A-Za-z0-9_.-]{1,31})\s*"
    r"(?:不是|没有|不会|不能|住在|来自|负责|擅长|喜欢|讨厌|想要|已经|曾经|今年|"
    r"是|有|爱|想|会|能|在|叫|姓|这(?:个|些|种|爱好|习惯|人)|"
    r"那(?:个|些|种|爱好|习惯|人))"
)
_SELF_OR_TOPIC_SUBJECTS = frozenset(
    {
        "本人",
        "自己",
        "咱们",
        "我们",
        "俺们",
        "现在",
        "最近",
        "以后",
        "之前",
        "一直",
        "已经",
        "曾经",
        "平时",
        "通常",
        "目前",
        "今天",
        "昨天",
        "明天",
        "比较",
        "非常",
        "特别",
        "目标",
        "计划",
        "梦想",
        "爱好",
        "工作",
        "职业",
        "生日",
        "家乡",
        "名字",
        "昵称",
        "习惯",
        "口味",
        "偏好",
        "性格",
        "愿望",
        "专业",
        "学校",
        "公司",
    }
)


@dataclass(frozen=True, slots=True)
class ValidatedMemoryClaim:
    operation: MemoryClaimOperation
    fact: MemoryFactCreate
    evidence: MemoryEvidenceCreate
    subject_is_speaker: bool
    occurred_at: datetime
    subject_basis: str = ""
    retention: str = ""
    source_style: str = ""


@dataclass(frozen=True, slots=True)
class MemoryClaimValidationResult:
    """A content-free explanation of one claim validation decision."""

    claim: ValidatedMemoryClaim | None
    reason_code: str
    candidate_type: str | None = None
    raw_claim: MemoryClaim | None = None

    @property
    def ok(self) -> bool:
        return self.claim is not None


class _MemoryClaimRejected(ValueError):
    """Internal control flow carrying a stable, content-free reason code."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class _MemoryClaimCandidate(_MemoryClaimRejected):
    def __init__(self, reason_code: str, candidate_type: str) -> None:
        super().__init__(reason_code)
        self.candidate_type = candidate_type


def normalize_memory_text(value: str, *, maximum: int) -> str:
    """Flatten untrusted model/user text before persistence and prompt use."""

    return " ".join(_CONTROL.sub(" ", value).split())[:maximum].strip()


def event_requests_explicit_memory(value: str) -> bool:
    """Return whether the trusted event explicitly asks Yuki to retain a memory."""

    normalized = " ".join(value.split())
    return bool(
        normalized and any(pattern.search(normalized) for pattern in _EXPLICIT_MEMORY_PATTERNS)
    )


def event_requests_memory_mutation(value: str) -> bool:
    """Conservatively detect an explicit request to change durable memory."""

    normalized = " ".join(value.split())
    return bool(
        normalized and any(pattern.search(normalized) for pattern in _MEMORY_MUTATION_PATTERNS)
    )


class MemoryClaimValidator:
    """Turn a claim into trusted persistence input or reject it without guessing."""

    def __init__(
        self,
        resolver: SubjectResolver | None = None,
        temporal: MemoryTemporalResolver | None = None,
        *,
        timezone_name: str = "Asia/Shanghai",
    ) -> None:
        self._resolver = resolver or SubjectResolver()
        self._temporal = temporal or MemoryTemporalResolver()
        self._timezone_name = timezone_name

    def validate(
        self,
        claim: MemoryClaim,
        event: EventRecord,
        *,
        subject_context: SubjectResolutionContext | None = None,
    ) -> tuple[MemoryFactCreate, MemoryEvidenceCreate] | None:
        validated = self.validate_claim(claim, event, subject_context=subject_context)
        if validated is None:
            return None
        return validated.fact, validated.evidence

    def validate_claim(
        self,
        claim: MemoryClaim,
        event: EventRecord,
        *,
        subject_context: SubjectResolutionContext | None = None,
    ) -> ValidatedMemoryClaim | None:
        return self.validate_claim_result(
            claim,
            event,
            subject_context=subject_context,
        ).claim

    def validate_claim_result(
        self,
        claim: MemoryClaim,
        event: EventRecord,
        *,
        subject_context: SubjectResolutionContext | None = None,
    ) -> MemoryClaimValidationResult:
        """Validate one claim while preserving why a rejected claim was dropped."""

        try:
            validated = self._validate_claim(
                claim,
                event,
                subject_context=subject_context,
            )
        except _MemoryClaimCandidate as exc:
            return MemoryClaimValidationResult(
                None,
                exc.reason_code,
                candidate_type=exc.candidate_type,
                raw_claim=claim,
            )
        except _MemoryClaimRejected as exc:
            return MemoryClaimValidationResult(None, exc.reason_code, raw_claim=claim)
        return MemoryClaimValidationResult(validated, "validated", raw_claim=claim)

    def _validate_claim(
        self,
        claim: MemoryClaim,
        event: EventRecord,
        *,
        subject_context: SubjectResolutionContext | None = None,
    ) -> ValidatedMemoryClaim:
        if not event.content.strip():
            raise _MemoryClaimRejected("empty_event")
        raw_quote = claim.evidence_quote.strip()
        if not raw_quote or raw_quote not in event.content:
            raise _MemoryClaimRejected("evidence_quote_not_in_event")
        quote = normalize_memory_text(raw_quote, maximum=500)
        source = normalize_memory_text(event.content, maximum=4000)
        if not quote or quote not in source:
            raise _MemoryClaimRejected("normalized_evidence_not_in_event")
        resolved = self._resolver.resolve(
            event,
            subject_ref=claim.subject_ref,
            scope_type=claim.scope_type,
            context=subject_context,
        )
        attribution = AttributionPolicy.evaluate(claim, event, resolved)
        if attribution.candidate_type is not None:
            raise _MemoryClaimCandidate(attribution.reason_code, attribution.candidate_type)
        if not attribution.accepted:
            raise _MemoryClaimRejected(attribution.reason_code)
        explicit_request = event_requests_explicit_memory(event.content)
        retention = RetentionPolicy.evaluate(
            claim,
            event,
            explicit_request=explicit_request,
        )
        if not retention.accepted:
            raise _MemoryClaimRejected(retention.reason_code)
        if claim.confidence < 0.65 and not explicit_request:
            raise _MemoryClaimCandidate("low_confidence_candidate", "memory")
        if resolved is None:
            raise _MemoryClaimRejected("subject_unresolved")
        key = normalize_memory_text(claim.memory_key, maximum=128)
        category = normalize_memory_text(claim.category, maximum=64)
        content = normalize_memory_text(claim.content, maximum=4000)
        if not key or not category or not content:
            raise _MemoryClaimRejected("incomplete_claim_fields")
        if not event_requests_explicit_memory(event.content) and not self._semantically_anchored(
            content,
            quote,
        ):
            raise _MemoryClaimRejected("semantic_not_anchored")
        kind = claim.kind
        if claim.retention is MemoryRetention.MEANINGFUL_EPISODE:
            kind = MemoryKind.EPISODE
        lowered = f"{key} {category} {content} {quote}".casefold()
        if any(marker in lowered for marker in _INTERACTION_MARKERS):
            kind = MemoryKind.PREFERENCE
        subject_is_speaker = resolved.subject_user_id == event.sender_user_id
        is_third_party = bool(resolved.subject_user_id) and not subject_is_speaker
        if subject_is_speaker and self._appears_to_describe_named_other(quote):
            raise _MemoryClaimRejected("named_other_attributed_to_speaker")
        if is_third_party and resolved.scope_type is not MemoryScopeType.PERSON_GROUP:
            raise _MemoryClaimRejected("third_party_scope_not_person_group")
        source_type = claim.source_type
        if source_type is MemorySourceType.EXPLICIT and not event_requests_explicit_memory(
            event.content
        ):
            source_type = MemorySourceType.AUTOMATIC
        if is_third_party:
            source_type = MemorySourceType.AUTOMATIC
            authority = MemoryAuthority.THIRD_PARTY
        elif source_type is MemorySourceType.EXPLICIT:
            authority = MemoryAuthority.EXPLICIT
        elif resolved.scope_type is MemoryScopeType.GROUP:
            authority = MemoryAuthority.GROUP_REPORT
        else:
            authority = MemoryAuthority.SELF_REPORT
        try:
            temporal = self._temporal.resolve(
                mode=claim.temporal_mode,
                valid_from=claim.valid_from,
                valid_until=claim.valid_until,
                occurred_at=event.occurred_at,
                timezone_name=self._timezone_name,
            )
        except ValueError:
            raise _MemoryClaimRejected("invalid_temporal_value") from None
        fact = MemoryFactCreate(
            scope_type=resolved.scope_type,
            subject_user_id=resolved.subject_user_id,
            group_id=resolved.group_id,
            kind=kind,
            memory_key=key,
            category=category,
            content=content,
            importance=claim.importance,
            confidence=claim.confidence,
            source_type=source_type,
            authority=authority,
            valid_from=temporal.valid_from,
            valid_until=temporal.valid_until,
        )
        relation = self._evidence_relation(
            operation=claim.operation,
            authority=authority,
            source_type=source_type,
        )
        evidence = MemoryEvidenceCreate(
            event_id=event.id,
            source_speaker_user_id=event.sender_user_id,
            relation=relation,
            confidence=claim.confidence,
            authority=authority,
            excerpt=quote,
        )
        return ValidatedMemoryClaim(
            operation=claim.operation,
            fact=fact,
            evidence=evidence,
            subject_is_speaker=subject_is_speaker,
            occurred_at=event.occurred_at,
            subject_basis=claim.subject_basis.value,
            retention=claim.retention.value,
            source_style=claim.source_style.value,
        )

    @staticmethod
    def _appears_to_describe_named_other(quote: str) -> bool:
        """Reject high-confidence named third-person text attributed to the speaker.

        The extractor has no authority to resolve ordinary names.  This deliberately
        narrow guard catches a leading name plus a person-like predicate while keeping
        first-person and common subjectless self-report forms available.
        """

        compact = quote.strip().lstrip("，。！？、,.!?：:；;（）()[]【】 ")
        compact = _MEMORY_COMMAND_PREFIX.sub("", compact).lstrip(
            "，。！？、,.!?：:；;（）()[]【】 "
        )
        matched = _NAMED_OTHER_PREFIX.match(compact)
        if matched is None:
            return False
        subject = matched.group("subject")
        predicate_tail = compact[matched.end("subject") :].lstrip()
        if re.match(r"^(?:都)?(?:叫|称|称呼)我", predicate_tail):
            return False
        return not (subject.startswith(("我", "咱", "俺")) or subject in _SELF_OR_TOPIC_SUBJECTS)

    @staticmethod
    def _semantically_anchored(content: str, quote: str) -> bool:
        """Conservatively reject claims derived from context instead of this event."""

        def compact(value: str) -> str:
            return "".join(char.casefold() for char in value if char.isalnum())

        claim_text = compact(content)
        evidence_text = compact(quote)
        if not claim_text or not evidence_text:
            return False
        if claim_text in evidence_text or evidence_text in claim_text:
            return True
        is_cjk = any("\u4e00" <= char <= "\u9fff" for char in evidence_text)
        width = 2 if is_cjk else 3
        if len(claim_text) < width or len(evidence_text) < width:
            return False
        claim_grams = {
            claim_text[index : index + width] for index in range(len(claim_text) - width + 1)
        }
        evidence_grams = {
            evidence_text[index : index + width] for index in range(len(evidence_text) - width + 1)
        }
        overlap = len(claim_grams & evidence_grams)
        if is_cjk:
            cjk_claim = "".join(char for char in claim_text if "\u4e00" <= char <= "\u9fff")
            if len(cjk_claim) >= 2 and cjk_claim[-2:] in evidence_text:
                return True
        required_overlap = 2
        return overlap >= required_overlap and overlap / min(
            len(claim_grams), len(evidence_grams)
        ) >= (0.2 if is_cjk else 0.3)

    @staticmethod
    def _evidence_relation(
        *,
        operation: MemoryClaimOperation,
        authority: MemoryAuthority,
        source_type: MemorySourceType,
    ) -> MemoryEvidenceRelation:
        if operation is MemoryClaimOperation.RETRACT:
            return MemoryEvidenceRelation.RETRACTION
        if operation is MemoryClaimOperation.CORRECT:
            return MemoryEvidenceRelation.CORRECTION
        if operation is MemoryClaimOperation.CONFIRM:
            return MemoryEvidenceRelation.CONFIRMATION
        if source_type is MemorySourceType.REBUILD:
            return MemoryEvidenceRelation.REBUILD
        if source_type is MemorySourceType.EXPLICIT:
            return MemoryEvidenceRelation.EXPLICIT_COMMAND
        if authority is MemoryAuthority.THIRD_PARTY:
            return MemoryEvidenceRelation.THIRD_PARTY_STATEMENT
        if authority is MemoryAuthority.GROUP_REPORT:
            return MemoryEvidenceRelation.GROUP_STATEMENT
        return MemoryEvidenceRelation.SELF_STATEMENT
