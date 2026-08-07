"""Deterministic attribution and retention policy for extracted memory claims."""

from __future__ import annotations

import re
from dataclasses import dataclass

from qq_ai_bot.memory.enums import (
    MemoryRetention,
    MemoryScopeType,
    MemorySourceStyle,
    MemorySubjectBasis,
)
from qq_ai_bot.memory.extraction import MemoryClaim
from qq_ai_bot.memory.subjects import ResolvedSubject
from qq_ai_bot.persistence.repository_records import EventRecord

_FIRST_PERSON = re.compile(
    r"(?:^|[，。！？；：,.!?;:\s]|记住|保存|记得)(?:我|我的|俺|本人|咱)(?:家|们)?"
)
_SECOND_PERSON_SUBJECT = re.compile(
    r"^[\s，。！？；：,.!?;:@\[\]【】]*(?:你|您)(?:今天|昨天|明天|现在|最近|已经|是|有|会|能|在|喜欢|讨厌|想|叫|姓)"
)
_YUKI_SUBJECT = re.compile(
    r"^[\s，。！？；：,.!?;:@\[\]【】]*(?:Yuki|yuki|机器人)(?:今天|现在|最近|已经|是|有|会|能|在|喜欢|讨厌|想|叫|缺少|完成)"
)
_LEADING_NAMED_OTHER = re.compile(
    r"^[\s，。！？；：,.!?;:@\[\]【】]*(?P<name>[\u4e00-\u9fff·]{2,8}|[A-Za-z][A-Za-z0-9_.-]{1,31})"
    r"\s*(?:不是|没有|不会|不能|住在|来自|负责|擅长|喜欢|讨厌|已经|是|有|爱|想|会|能|在|叫|姓)"
)
_ROLEPLAY = re.compile(r"(?:扮演|角色扮演|每句话.{0,12}(?:喵|结尾)|假装你是)")
_ONE_TURN_INSTRUCTION = re.compile(
    r"(?:这轮|本轮|当前(?:轮|消息)|临时|先).{0,16}(?:回复|回答|称呼|格式|语气|扮演|不要|改成|设置)"
)
_GENERATED_RESULT = re.compile(
    r"(?:获得|增加|减少|掉落|抽到|钓到|伤害|经验|\bXP\b|任务执行|调用结果|状态码).{0,16}(?:\d|成功|失败|点|级|条)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class MemoryPolicyDecision:
    accepted: bool
    reason_code: str
    candidate_type: str | None = None

    @classmethod
    def accept(cls) -> MemoryPolicyDecision:
        return cls(True, "policy_verified")

    @classmethod
    def reject(cls, reason_code: str) -> MemoryPolicyDecision:
        return cls(False, reason_code)

    @classmethod
    def candidate(cls, reason_code: str, candidate_type: str) -> MemoryPolicyDecision:
        return cls(False, reason_code, candidate_type)


class AttributionPolicy:
    """Verify that model-declared subject semantics match trusted metadata and text."""

    @staticmethod
    def evaluate(
        claim: MemoryClaim,
        event: EventRecord,
        resolved: ResolvedSubject | None,
    ) -> MemoryPolicyDecision:
        quote = claim.evidence_quote.strip()
        basis = claim.subject_basis
        if basis is MemorySubjectBasis.ABOUT_YUKI or _YUKI_SUBJECT.search(quote):
            return MemoryPolicyDecision.candidate(
                "self_candidate_requires_agent_judgment",
                "self",
            )
        if basis is MemorySubjectBasis.NAMED_UNRESOLVED and not claim.subject_ref.startswith(
            "named_"
        ):
            return MemoryPolicyDecision.candidate("named_subject_unresolved", "memory")
        if resolved is None:
            return MemoryPolicyDecision.reject("subject_unresolved")
        if claim.scope_type is MemoryScopeType.SELF:
            return MemoryPolicyDecision.reject("self_candidate_requires_agent_judgment")
        if resolved.scope_type is MemoryScopeType.GROUP:
            return (
                MemoryPolicyDecision.accept()
                if basis is MemorySubjectBasis.GROUP
                else MemoryPolicyDecision.reject("group_basis_not_verified")
            )
        subject_is_speaker = resolved.subject_user_id == event.sender_user_id
        if subject_is_speaker:
            if basis is MemorySubjectBasis.ADDRESSED_SECOND_PERSON or _SECOND_PERSON_SUBJECT.search(
                quote
            ):
                return MemoryPolicyDecision.reject("second_person_attributed_to_speaker")
            if basis in {
                MemorySubjectBasis.MENTIONED_SUBJECT,
                MemorySubjectBasis.REPLY_SUBJECT,
                MemorySubjectBasis.GROUP,
            }:
                return MemoryPolicyDecision.reject("speaker_basis_not_verified")
            if basis is MemorySubjectBasis.FIRST_PERSON:
                return (
                    MemoryPolicyDecision.accept()
                    if _FIRST_PERSON.search(quote)
                    else MemoryPolicyDecision.reject("speaker_basis_not_verified")
                )
            if basis is MemorySubjectBasis.OMITTED_SELF:
                if (
                    event.mentioned_user_ids
                    or _SECOND_PERSON_SUBJECT.search(quote)
                    or _YUKI_SUBJECT.search(quote)
                ):
                    return MemoryPolicyDecision.reject("speaker_basis_not_verified")
                # Ordinary Chinese predicates are too ambiguous to infer a named
                # third party here (for example ``最近喜欢猫娘``).  The claim
                # validator performs the stricter, event-aware named-subject
                # check after this deterministic basis check.
                return MemoryPolicyDecision.accept()
            return MemoryPolicyDecision.reject("speaker_basis_not_verified")
        if claim.subject_ref.startswith("mentioned_"):
            if basis not in {
                MemorySubjectBasis.MENTIONED_SUBJECT,
                MemorySubjectBasis.ADDRESSED_SECOND_PERSON,
            }:
                return MemoryPolicyDecision.reject("mentioned_user_is_addressee_not_subject")
            if AttributionPolicy._named_other(quote) and not _SECOND_PERSON_SUBJECT.search(quote):
                return MemoryPolicyDecision.reject("mentioned_user_is_addressee_not_subject")
            return MemoryPolicyDecision.accept()
        if claim.subject_ref == "reply_author":
            if basis not in {
                MemorySubjectBasis.REPLY_SUBJECT,
                MemorySubjectBasis.ADDRESSED_SECOND_PERSON,
            }:
                return MemoryPolicyDecision.reject("reply_subject_not_verified")
            return MemoryPolicyDecision.accept()
        if claim.subject_ref.startswith("named_"):
            return MemoryPolicyDecision.accept()
        return MemoryPolicyDecision.reject("subject_basis_not_verified")

    @staticmethod
    def _named_other(quote: str) -> bool:
        match = _LEADING_NAMED_OTHER.search(quote)
        if match is None:
            return False
        return match.group("name") not in {
            "本人",
            "自己",
            "我们",
            "今天",
            "昨天",
            "明天",
            "现在",
            "最近",
        }


class RetentionPolicy:
    """Keep temporary activity and generated output out of automatic long-term facts."""

    @staticmethod
    def evaluate(
        claim: MemoryClaim,
        event: EventRecord,
        *,
        explicit_request: bool,
    ) -> MemoryPolicyDecision:
        if explicit_request:
            return MemoryPolicyDecision.accept()
        quote = claim.evidence_quote.strip()
        if claim.retention is MemoryRetention.TRANSIENT:
            return MemoryPolicyDecision.reject("transient_not_long_term")
        style = claim.source_style
        if style is MemorySourceStyle.ROLEPLAY or _ROLEPLAY.search(quote):
            return MemoryPolicyDecision.reject("roleplay_not_long_term_preference")
        if style is MemorySourceStyle.INSTRUCTION or _ONE_TURN_INSTRUCTION.search(quote):
            return MemoryPolicyDecision.reject("instruction_not_long_term_preference")
        if style is MemorySourceStyle.GENERATED_RESULT or _GENERATED_RESULT.search(quote):
            return MemoryPolicyDecision.reject("generated_result_not_self_report")
        if style is MemorySourceStyle.QUOTED_TEXT:
            return MemoryPolicyDecision.reject("quoted_text_not_self_report")
        if event.direction != "inbound":
            return MemoryPolicyDecision.reject("generated_result_not_self_report")
        return MemoryPolicyDecision.accept()
