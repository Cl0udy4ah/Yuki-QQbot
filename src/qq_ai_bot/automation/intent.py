"""Deterministic intent guards for future-triggered automation requests."""

from __future__ import annotations

import re

_NUMBER = r"(?:\d+|[零〇一二两三四五六七八九十百]+)"
_DELAY = re.compile(rf"{_NUMBER}\s*(?:秒钟?|分钟?|分|小时|天)\s*(?:之?后|以后)")
_RECURRENCE = re.compile(
    r"(?:每天|每日|每晚|每早|每周|每星期|每月|每年|工作日|每隔|定期|周期性)"
)
_FUTURE_DAY = re.compile(
    r"(?:今天|今晚|今早|明天|明早|明晚|后天|大后天|下周|下个月|"
    r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]|20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})"
)
_CLOCK = re.compile(
    rf"(?:(?:凌晨|早上|上午|中午|下午|傍晚|晚上)\s*)?{_NUMBER}\s*"
    rf"(?:[:：]\s*\d{{1,2}}|点(?:\s*(?:半|{_NUMBER}\s*分?))?|时)"
)
_VAGUE_FUTURE = re.compile(r"(?:晚点|稍后|过会儿?|等会儿?|有空时|到时候)")
_ACTION = re.compile(
    r"(?:提醒|叫醒|通知|告诉|发给|发送|推送|查询|查一下|查看|检查|下单|点一份|"
    r"预约|创建|执行|运行|清理|删除|更新|同步|备份|关闭|打开|启动|帮我|替我|给我)"
)
_PAST_ONLY = re.compile(r"(?:昨天|前天|上周|上个月|刚才|之前|过去)")
_SUCCESS_CLAIM = re.compile(
    r"(?:设好了|设置好了|安排好了|创建成功|已经创建|已创建|建好了|任务已(?:经)?建立|"
    r"定时(?:已经)?设好|到时候(?:会|就会))"
)


def is_scheduled_automation_request(text: str) -> bool:
    """Return whether the current message commands an action at a future trigger."""

    compact = " ".join(text.casefold().split())
    if not compact or not _ACTION.search(compact):
        return False
    delayed = bool(_DELAY.search(compact))
    recurring = bool(_RECURRENCE.search(compact))
    future_day = bool(_FUTURE_DAY.search(compact))
    clock = bool(_CLOCK.search(compact))
    vague = bool(_VAGUE_FUTURE.search(compact))
    if _PAST_ONLY.search(compact) and not (delayed or recurring or future_day or vague):
        return False
    return delayed or recurring or vague or clock or future_day


def contains_automation_success_claim(text: str) -> bool:
    """Detect only concrete persistence claims, not ordinary future-tense discussion."""

    return bool(_SUCCESS_CLAIM.search(" ".join(text.casefold().split())))


def enforce_creation_claim(
    text: str,
    *,
    scheduled_intent: bool,
    persisted: bool,
) -> str:
    """Prevent a model from claiming a task exists without a persisted tool result."""

    if scheduled_intent and not persisted and contains_automation_success_claim(text):
        return "这个定时任务还没有写入任务列表，不能算创建成功。"
    return text
