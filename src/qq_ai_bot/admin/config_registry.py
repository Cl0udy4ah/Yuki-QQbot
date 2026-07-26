"""Allowlist registry for every configuration value visible to administrator tools."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from qq_ai_bot.admin.models import (
    ConfigApplyMode,
    ConfigScopeType,
    ConfigSpec,
    ConfigValue,
)
from qq_ai_bot.config import Settings

_G = (ConfigScopeType.GLOBAL,)
_GG = (ConfigScopeType.GLOBAL, ConfigScopeType.GROUP)
_GGU = (ConfigScopeType.GLOBAL, ConfigScopeType.GROUP, ConfigScopeType.USER)
_GU = (ConfigScopeType.GLOBAL, ConfigScopeType.USER)


def _field(name: str) -> Any:
    return lambda settings: getattr(settings, name)


def _constant(value: ConfigValue) -> Any:
    return lambda _settings: value


def _configured(name: str) -> Any:
    return lambda settings: bool(getattr(settings, name, ""))


def _database_password_configured(settings: Settings) -> bool:
    url = settings.database_url
    authority = url.split("://", maxsplit=1)[-1].split("/", maxsplit=1)[0]
    return ":" in authority and "@" in authority


def _max_auto_delta(settings: Settings) -> int:
    # One runtime key safely governs both existing 1.2 dimensions.
    return min(settings.affection_max_auto_delta, settings.trust_max_auto_delta)


def _spec(
    key: str,
    display_name: str,
    description: str,
    *,
    aliases: tuple[str, ...] = (),
    value_type: str,
    minimum: float | None = None,
    maximum: float | None = None,
    choices: tuple[str, ...] = (),
    scopes: tuple[ConfigScopeType, ...] = _G,
    mode: ConfigApplyMode = ConfigApplyMode.HOT,
    env_alias: str | None = None,
    getter: Any,
    settings_fields: tuple[str, ...] = (),
    category: str,
    sensitive: bool = False,
) -> ConfigSpec:
    return ConfigSpec(
        key=key,
        display_name=display_name,
        description=description,
        aliases=aliases,
        value_type=value_type,  # type: ignore[arg-type]
        minimum=minimum,
        maximum=maximum,
        choices=choices,
        allowed_scopes=scopes,
        apply_mode=mode,
        permission="superuser",
        sensitive=sensitive,
        env_alias=env_alias,
        default_getter=getter,
        settings_fields=settings_fields,
        category=category,
    )


def _registered_specs() -> tuple[ConfigSpec, ...]:
    """Return the reviewed allowlist; Settings fields are never reflected automatically."""

    hot = (
        _spec(
            "autonomous.enabled",
            "自主群聊开关",
            "是否允许 Yuki 在已启用群中谨慎主动发言。",
            aliases=("自动插话开关", "自主发言开关"),
            value_type="boolean",
            scopes=_GG,
            env_alias="AUTONOMOUS_GROUP_CHAT_ENABLED",
            getter=_field("autonomous_group_chat_enabled"),
            settings_fields=("autonomous_group_chat_enabled",),
            category="autonomous",
        ),
        _spec(
            "autonomous.silence_seconds",
            "自主发言静默等待",
            "群消息静默多少秒后再判断是否参与。",
            aliases=("自动插话等待", "群聊静默时间"),
            value_type="number",
            minimum=0,
            maximum=300,
            scopes=_GG,
            env_alias="AUTONOMOUS_SILENCE_SECONDS",
            getter=_field("autonomous_silence_seconds"),
            settings_fields=("autonomous_silence_seconds",),
            category="autonomous",
        ),
        _spec(
            "autonomous.confidence_threshold",
            "自主发言置信度阈值",
            "自主群聊判断达到该置信度后才发送消息。",
            aliases=("自动插话置信度",),
            value_type="number",
            minimum=0,
            maximum=1,
            scopes=_GG,
            env_alias="AUTONOMOUS_CONFIDENCE_THRESHOLD",
            getter=_field("autonomous_confidence_threshold"),
            settings_fields=("autonomous_confidence_threshold",),
            category="autonomous",
        ),
        _spec(
            "autonomous.cooldown_seconds",
            "自主发言冷却",
            "同一群两次自主发言之间的最短秒数。",
            aliases=("自动插话间隔", "自主发言间隔"),
            value_type="integer",
            minimum=0,
            maximum=86400,
            scopes=_GG,
            env_alias="AUTONOMOUS_COOLDOWN_SECONDS",
            getter=_field("autonomous_cooldown_seconds"),
            settings_fields=("autonomous_cooldown_seconds",),
            category="autonomous",
        ),
        _spec(
            "autonomous.max_per_hour",
            "每群每小时自动发言次数上限",
            "每个群每小时最多自主发言多少次。",
            aliases=(
                "一小时自动插话次数",
                "每小时自动发言次数",
                "每小时插话上限",
                "自动聊天次数",
                "max per hour",
            ),
            value_type="integer",
            minimum=1,
            maximum=100,
            scopes=_GG,
            env_alias="AUTONOMOUS_MAX_PER_HOUR",
            getter=_field("autonomous_max_per_hour"),
            settings_fields=("autonomous_max_per_hour",),
            category="autonomous",
        ),
        _spec(
            "context.local_event_limit",
            "近期本地事件数量",
            "每轮装配当前场景最近聊天事件的上限。",
            aliases=("上下文消息数量", "近期聊天条数"),
            value_type="integer",
            minimum=1,
            maximum=100,
            scopes=_GGU,
            env_alias="LOCAL_CONTEXT_EVENT_LIMIT",
            getter=_field("local_context_event_limit"),
            settings_fields=("local_context_event_limit",),
            category="context",
        ),
        _spec(
            "context.related_people_limit",
            "相关人物数量",
            "群聊上下文最多装配多少位相关人物。",
            aliases=("相关群友数量",),
            value_type="integer",
            minimum=1,
            maximum=5,
            scopes=_GGU,
            env_alias="RELATED_PEOPLE_LIMIT",
            getter=_field("related_people_limit"),
            settings_fields=("related_people_limit",),
            category="context",
        ),
        _spec(
            "reply.daily_split_enabled",
            "日常回复分句",
            "日常聊天是否按句拆成多条 QQ 消息。",
            aliases=("分句发送", "拆分回复"),
            value_type="boolean",
            scopes=_GGU,
            env_alias="SPLIT_DAILY_CHAT_SENTENCES",
            getter=_field("split_daily_chat_sentences"),
            settings_fields=("split_daily_chat_sentences",),
            category="reply",
        ),
        _spec(
            "reply.daily_split_max_characters",
            "分句最大字符数",
            "触发日常分句处理的最大回复字符数。",
            aliases=("分句字符上限",),
            value_type="integer",
            minimum=20,
            maximum=4000,
            scopes=_GGU,
            env_alias="DAILY_CHAT_SPLIT_MAX_CHARACTERS",
            getter=_field("daily_chat_split_max_characters"),
            settings_fields=("daily_chat_split_max_characters",),
            category="reply",
        ),
        _spec(
            "reply.daily_split_max_messages",
            "分句消息数量上限",
            "一轮日常回复最多拆成多少条消息。",
            aliases=("分句条数",),
            value_type="integer",
            minimum=1,
            maximum=10,
            scopes=_GGU,
            env_alias="DAILY_CHAT_SPLIT_MAX_MESSAGES",
            getter=_field("daily_chat_split_max_messages"),
            settings_fields=("daily_chat_split_max_messages",),
            category="reply",
        ),
        _spec(
            "reply.delay_min_seconds",
            "分句最短延迟",
            "连续发送分句之间的最短随机等待秒数。",
            aliases=("消息最短延迟",),
            value_type="number",
            minimum=0,
            maximum=60,
            scopes=_GGU,
            env_alias="DAILY_CHAT_MESSAGE_DELAY_MIN_SECONDS",
            getter=_field("daily_chat_message_delay_min_seconds"),
            settings_fields=("daily_chat_message_delay_min_seconds",),
            category="reply",
        ),
        _spec(
            "reply.delay_max_seconds",
            "分句最长延迟",
            "连续发送分句之间的最长随机等待秒数。",
            aliases=("消息最长延迟",),
            value_type="number",
            minimum=0,
            maximum=60,
            scopes=_GGU,
            env_alias="DAILY_CHAT_MESSAGE_DELAY_MAX_SECONDS",
            getter=_field("daily_chat_message_delay_max_seconds"),
            settings_fields=("daily_chat_message_delay_max_seconds",),
            category="reply",
        ),
        _spec(
            "reply.max_qq_message_chars",
            "单条 QQ 消息字符上限",
            "发送前对超长文本做最终切分的字符数。",
            aliases=("QQ消息长度",),
            value_type="integer",
            minimum=100,
            maximum=5000,
            scopes=_GGU,
            env_alias="MAX_QQ_MESSAGE_CHARS",
            getter=_field("max_qq_message_chars"),
            settings_fields=("max_qq_message_chars",),
            category="reply",
        ),
        _spec(
            "llm.temperature",
            "模型温度",
            "普通聊天模型采样温度。",
            aliases=("温度", "模型随机度"),
            value_type="number",
            minimum=0,
            maximum=2,
            scopes=_GGU,
            env_alias="LLM_TEMPERATURE",
            getter=_field("llm_temperature"),
            settings_fields=("llm_temperature",),
            category="llm",
        ),
        _spec(
            "llm.max_output_tokens",
            "模型最大输出 Token",
            "普通聊天单次模型请求的最大输出 Token 数。",
            aliases=("最大输出token",),
            value_type="integer",
            minimum=1,
            maximum=32768,
            scopes=_GGU,
            env_alias="LLM_MAX_OUTPUT_TOKENS",
            getter=_field("llm_max_output_tokens"),
            settings_fields=("llm_max_output_tokens",),
            category="llm",
        ),
        _spec(
            "llm.thinking_enabled",
            "模型深度思考开关",
            "是否为普通聊天请求显式启用深度思考。",
            aliases=("深度思考", "思考模式"),
            value_type="boolean",
            scopes=_GGU,
            env_alias="LLM_THINKING_ENABLED",
            getter=_field("llm_thinking_enabled"),
            settings_fields=("llm_thinking_enabled",),
            category="llm",
        ),
        _spec(
            "agent.max_tool_calls",
            "每轮工具调用上限",
            "普通 Agent 每轮最多实际执行的工具次数。",
            aliases=("工具调用次数",),
            value_type="integer",
            minimum=1,
            maximum=5,
            scopes=_GGU,
            env_alias="AGENT_MAX_TOOL_CALLS",
            getter=_field("agent_max_tool_calls"),
            settings_fields=("agent_max_tool_calls",),
            category="agent",
        ),
        _spec(
            "agent.max_model_requests",
            "每轮模型请求上限",
            "包含工具往返在内的模型请求次数上限。",
            aliases=("模型请求次数",),
            value_type="integer",
            minimum=1,
            maximum=12,
            scopes=_GGU,
            env_alias="AGENT_MAX_MODEL_REQUESTS",
            getter=_field("agent_max_model_requests"),
            settings_fields=("agent_max_model_requests",),
            category="agent",
        ),
        _spec(
            "agent.tool_result_max_characters",
            "普通工具结果字符上限",
            "普通 Agent 单个工具结果回传模型前的字符上限。",
            aliases=("工具结果长度",),
            value_type="integer",
            minimum=1000,
            maximum=32000,
            scopes=_GGU,
            env_alias="AGENT_TOOL_RESULT_MAX_CHARACTERS",
            getter=_field("agent_tool_result_max_characters"),
            settings_fields=("agent_tool_result_max_characters",),
            category="agent",
        ),
        _spec(
            "web.search_max_results",
            "联网搜索结果上限",
            "单次联网搜索最多请求的候选结果数。",
            aliases=("搜索结果数量",),
            value_type="integer",
            minimum=1,
            maximum=5,
            scopes=_GGU,
            env_alias="WEB_SEARCH_MAX_RESULTS",
            getter=_field("web_search_max_results"),
            settings_fields=("web_search_max_results",),
            category="web",
        ),
        _spec(
            "web.extract_max_results",
            "网页提取结果上限",
            "单次搜索最多提取正文的结果数。",
            aliases=("网页提取数量",),
            value_type="integer",
            minimum=1,
            maximum=3,
            scopes=_GGU,
            env_alias="WEB_EXTRACT_MAX_RESULTS",
            getter=_field("web_extract_max_results"),
            settings_fields=("web_extract_max_results",),
            category="web",
        ),
        _spec(
            "web.max_calls_per_turn",
            "每轮联网工具上限",
            "普通 Agent 每轮最多执行的联网工具次数。",
            aliases=("联网次数",),
            value_type="integer",
            minimum=1,
            maximum=3,
            scopes=_GGU,
            env_alias="WEB_MAX_CALLS_PER_TURN",
            getter=_field("web_max_calls_per_turn"),
            settings_fields=("web_max_calls_per_turn",),
            category="web",
        ),
        _spec(
            "web.tool_result_max_characters",
            "联网工具结果字符上限",
            "联网工具结果回传模型前的最大字符数。",
            aliases=("联网结果长度",),
            value_type="integer",
            minimum=1000,
            maximum=16000,
            scopes=_GGU,
            env_alias="WEB_TOOL_RESULT_MAX_CHARACTERS",
            getter=_field("web_tool_result_max_characters"),
            settings_fields=("web_tool_result_max_characters",),
            category="web",
        ),
        _spec(
            "relationship.confidence_threshold",
            "关系变化置信度阈值",
            "自动关系变化被接受所需的最低置信度。",
            aliases=("好感变化置信度",),
            value_type="number",
            minimum=0,
            maximum=1,
            scopes=_GGU,
            env_alias="RELATIONSHIP_CONFIDENCE_THRESHOLD",
            getter=_field("relationship_confidence_threshold"),
            settings_fields=("relationship_confidence_threshold",),
            category="relationship",
        ),
        _spec(
            "relationship.max_auto_delta",
            "单次自动关系变化上限",
            "好感度和信任度每次自动变化的绝对值上限。",
            aliases=("单次好感变化", "单次信任变化"),
            value_type="integer",
            minimum=1,
            maximum=2,
            scopes=_GGU,
            getter=_max_auto_delta,
            settings_fields=("affection_max_auto_delta", "trust_max_auto_delta"),
            category="relationship",
        ),
        _spec(
            "relationship.daily_positive_cap",
            "每日正向关系变化上限",
            "每天自动增加的累计上限；0 表示不限制，保持 1.2 行为。",
            aliases=("每日加好感上限",),
            value_type="integer",
            minimum=0,
            maximum=100,
            scopes=_GGU,
            env_alias="RELATIONSHIP_DAILY_POSITIVE_CAP",
            getter=_field("relationship_daily_positive_cap"),
            settings_fields=("relationship_daily_positive_cap",),
            category="relationship",
        ),
        _spec(
            "relationship.daily_negative_cap",
            "每日负向关系变化上限",
            "每天自动减少的累计绝对值上限；0 表示不限制，保持 1.2 行为。",
            aliases=("每日减好感上限",),
            value_type="integer",
            minimum=0,
            maximum=100,
            scopes=_GGU,
            env_alias="RELATIONSHIP_DAILY_NEGATIVE_CAP",
            getter=_field("relationship_daily_negative_cap"),
            settings_fields=("relationship_daily_negative_cap",),
            category="relationship",
        ),
        _spec(
            "relationship.conflict_preference_min_gap",
            "冲突偏好最小差值",
            "无证据冲突中关系权重至少相差多少才形成倾向。",
            aliases=("关系权重差",),
            value_type="integer",
            minimum=0,
            maximum=100,
            scopes=_GGU,
            env_alias="CONFLICT_PREFERENCE_MIN_GAP",
            getter=_field("conflict_preference_min_gap"),
            settings_fields=("conflict_preference_min_gap",),
            category="relationship",
        ),
        _spec(
            "vision.max_images_per_turn",
            "每轮视觉图片上限",
            "每轮最多选择并分析的当前消息或回复图片数量。",
            aliases=("视觉图片上限", "每轮图片数量"),
            value_type="integer",
            minimum=1,
            maximum=5,
            scopes=_GGU,
            env_alias="VISION_MAX_IMAGES_PER_TURN",
            getter=_field("vision_max_images_per_turn"),
            settings_fields=("vision_max_images_per_turn",),
            category="vision",
        ),
        _spec(
            "vision.max_frames_per_turn",
            "每轮视觉总帧数上限",
            "一轮内所有静态和动态图片合计允许发送给视觉模型的帧数。",
            aliases=("视觉总帧数",),
            value_type="integer",
            minimum=1,
            maximum=16,
            scopes=_GGU,
            env_alias="VISION_MAX_FRAMES_PER_TURN",
            getter=_field("vision_max_frames_per_turn"),
            settings_fields=("vision_max_frames_per_turn",),
            category="vision",
        ),
        _spec(
            "vision.gif_max_frames",
            "单张动态图片抽帧上限",
            "每张 GIF 或动态 WEBP 最多均匀抽取的帧数。",
            aliases=("GIF抽帧上限", "动态图片帧数"),
            value_type="integer",
            minimum=1,
            maximum=8,
            scopes=_GGU,
            env_alias="VISION_GIF_MAX_FRAMES",
            getter=_field("vision_gif_max_frames"),
            settings_fields=("vision_gif_max_frames",),
            category="vision",
        ),
        _spec(
            "vision.per_user_requests_per_minute",
            "每用户视觉请求频率",
            "每位用户每分钟允许触发的视觉消息请求数量。",
            aliases=("用户视觉限流",),
            value_type="integer",
            minimum=1,
            maximum=1000,
            scopes=_GGU,
            env_alias="VISION_PER_USER_REQUESTS_PER_MINUTE",
            getter=_field("vision_per_user_requests_per_minute"),
            settings_fields=("vision_per_user_requests_per_minute",),
            category="vision",
        ),
        _spec(
            "vision.per_group_requests_per_minute",
            "每群视觉请求频率",
            "每个群每分钟允许触发的视觉消息请求数量。",
            aliases=("群视觉限流",),
            value_type="integer",
            minimum=1,
            maximum=5000,
            scopes=_GGU,
            env_alias="VISION_PER_GROUP_REQUESTS_PER_MINUTE",
            getter=_field("vision_per_group_requests_per_minute"),
            settings_fields=("vision_per_group_requests_per_minute",),
            category="vision",
        ),
    )
    future = (
        _spec(
            "relationship.initial_affection",
            "新人物初始好感度",
            "只影响之后首次建立关系记录的人物。",
            aliases=("初始好感度",),
            value_type="integer",
            minimum=0,
            maximum=100,
            scopes=_GU,
            mode=ConfigApplyMode.FUTURE_ONLY,
            env_alias="RELATIONSHIP_INITIAL_AFFECTION",
            getter=_field("relationship_initial_affection"),
            settings_fields=("relationship_initial_affection",),
            category="relationship",
        ),
        _spec(
            "relationship.initial_trust",
            "新人物初始信任度",
            "只影响之后首次建立关系记录的人物。",
            aliases=("初始信任度",),
            value_type="integer",
            minimum=0,
            maximum=100,
            scopes=_GU,
            mode=ConfigApplyMode.FUTURE_ONLY,
            env_alias="RELATIONSHIP_INITIAL_TRUST",
            getter=_field("relationship_initial_trust"),
            settings_fields=("relationship_initial_trust",),
            category="relationship",
        ),
        _spec(
            "web.source_retention_days",
            "联网来源保留天数",
            "之后运行的清理任务使用的新保留天数。",
            aliases=("来源保留时间",),
            value_type="integer",
            minimum=1,
            maximum=365,
            scopes=_G,
            mode=ConfigApplyMode.FUTURE_ONLY,
            env_alias="WEB_SOURCE_RETENTION_DAYS",
            getter=_field("web_source_retention_days"),
            settings_fields=("web_source_retention_days",),
            category="web",
        ),
        _spec(
            "web.source_max_runs_per_conversation",
            "每会话联网来源批次上限",
            "之后新保存来源时允许保留的搜索批次数。",
            aliases=("来源批次上限",),
            value_type="integer",
            minimum=1,
            maximum=100,
            scopes=_GGU,
            mode=ConfigApplyMode.FUTURE_ONLY,
            env_alias="WEB_SOURCE_MAX_RUNS_PER_CONVERSATION",
            getter=_field("web_source_max_runs_per_conversation"),
            settings_fields=("web_source_max_runs_per_conversation",),
            category="web",
        ),
        _spec(
            "vision.analysis_retention_days",
            "视觉分析缓存保留天数",
            "之后执行的清理任务使用的新视觉分析缓存保留天数。",
            aliases=("视觉缓存保留时间",),
            value_type="integer",
            minimum=1,
            maximum=365,
            scopes=_G,
            mode=ConfigApplyMode.FUTURE_ONLY,
            env_alias="VISION_ANALYSIS_RETENTION_DAYS",
            getter=_field("vision_analysis_retention_days"),
            settings_fields=("vision_analysis_retention_days",),
            category="vision",
        ),
    )
    restart = (
        _spec(
            "llm.model",
            "模型名称",
            "重启后用于创建长期模型客户端的模型名称。",
            aliases=("聊天模型",),
            value_type="string",
            scopes=_G,
            mode=ConfigApplyMode.RESTART_REQUIRED,
            env_alias="LLM_MODEL",
            getter=_field("llm_model"),
            settings_fields=("llm_model",),
            category="llm",
        ),
        _spec(
            "llm.timeout_seconds",
            "模型超时",
            "重启后模型请求读取超时秒数。",
            aliases=("模型超时时间",),
            value_type="number",
            minimum=1,
            maximum=300,
            scopes=_G,
            mode=ConfigApplyMode.RESTART_REQUIRED,
            env_alias="LLM_TIMEOUT_SECONDS",
            getter=_field("llm_timeout_seconds"),
            settings_fields=("llm_timeout_seconds",),
            category="llm",
        ),
        _spec(
            "llm.max_retries",
            "模型重试次数",
            "重启后模型客户端的最大重试次数。",
            aliases=("模型重试",),
            value_type="integer",
            minimum=0,
            maximum=10,
            scopes=_G,
            mode=ConfigApplyMode.RESTART_REQUIRED,
            env_alias="LLM_MAX_RETRIES",
            getter=_field("llm_max_retries"),
            settings_fields=("llm_max_retries",),
            category="llm",
        ),
        _spec(
            "global.llm_concurrency",
            "全局模型并发",
            "重启后全局同时执行的模型请求数。",
            aliases=("模型并发数",),
            value_type="integer",
            minimum=1,
            maximum=64,
            scopes=_G,
            mode=ConfigApplyMode.RESTART_REQUIRED,
            env_alias="GLOBAL_LLM_CONCURRENCY",
            getter=_field("global_llm_concurrency"),
            settings_fields=("global_llm_concurrency",),
            category="global",
        ),
        _spec(
            "web.global_concurrency",
            "联网全局并发",
            "重启后 Tavily 客户端同时请求数。",
            aliases=("联网并发数",),
            value_type="integer",
            minimum=1,
            maximum=32,
            scopes=_G,
            mode=ConfigApplyMode.RESTART_REQUIRED,
            env_alias="WEB_GLOBAL_CONCURRENCY",
            getter=_field("web_global_concurrency"),
            settings_fields=("web_global_concurrency",),
            category="web",
        ),
        _spec(
            "rate_limit.per_user_per_minute",
            "每用户每分钟请求上限",
            "重启后用户级滑动窗口限流值。",
            aliases=("用户限流",),
            value_type="integer",
            minimum=1,
            maximum=1000,
            scopes=_G,
            mode=ConfigApplyMode.RESTART_REQUIRED,
            env_alias="PER_USER_REQUESTS_PER_MINUTE",
            getter=_field("per_user_requests_per_minute"),
            settings_fields=("per_user_requests_per_minute",),
            category="rate_limit",
        ),
        _spec(
            "rate_limit.per_group_per_minute",
            "每群每分钟请求上限",
            "重启后群级滑动窗口限流值。",
            aliases=("群限流",),
            value_type="integer",
            minimum=1,
            maximum=5000,
            scopes=_G,
            mode=ConfigApplyMode.RESTART_REQUIRED,
            env_alias="PER_GROUP_REQUESTS_PER_MINUTE",
            getter=_field("per_group_requests_per_minute"),
            settings_fields=("per_group_requests_per_minute",),
            category="rate_limit",
        ),
        _spec(
            "vision.enabled",
            "视觉功能开关",
            "重启后决定是否创建并启用视觉服务。",
            aliases=("图片识别开关", "视觉开关"),
            value_type="boolean",
            scopes=_G,
            mode=ConfigApplyMode.RESTART_REQUIRED,
            env_alias="VISION_ENABLED",
            getter=_field("vision_enabled"),
            settings_fields=("vision_enabled",),
            category="vision",
        ),
        _spec(
            "vision.base_url",
            "视觉 API 地址",
            "重启后视觉 Provider 使用的 OpenAI-compatible API 基础地址。",
            aliases=("视觉接口地址",),
            value_type="string",
            scopes=_G,
            mode=ConfigApplyMode.RESTART_REQUIRED,
            env_alias="VISION_BASE_URL",
            getter=_field("vision_base_url"),
            settings_fields=("vision_base_url",),
            category="vision",
        ),
        _spec(
            "vision.model",
            "视觉模型名称",
            "重启后视觉 Provider 使用的模型名称。",
            aliases=("图片识别模型",),
            value_type="string",
            scopes=_G,
            mode=ConfigApplyMode.RESTART_REQUIRED,
            env_alias="VISION_MODEL",
            getter=_field("vision_model"),
            settings_fields=("vision_model",),
            category="vision",
        ),
        _spec(
            "vision.global_concurrency",
            "视觉全局并发",
            "重启后独立视觉请求信号量允许的并发数。",
            aliases=("视觉并发数",),
            value_type="integer",
            minimum=1,
            maximum=32,
            scopes=_G,
            mode=ConfigApplyMode.RESTART_REQUIRED,
            env_alias="VISION_GLOBAL_CONCURRENCY",
            getter=_field("vision_global_concurrency"),
            settings_fields=("vision_global_concurrency",),
            category="vision",
        ),
        _spec(
            "vision.timeout_seconds",
            "视觉请求超时",
            "重启后视觉 Provider 请求的总超时秒数。",
            aliases=("视觉超时时间",),
            value_type="number",
            minimum=1,
            maximum=300,
            scopes=_G,
            mode=ConfigApplyMode.RESTART_REQUIRED,
            env_alias="VISION_TIMEOUT_SECONDS",
            getter=_field("vision_timeout_seconds"),
            settings_fields=("vision_timeout_seconds",),
            category="vision",
        ),
    )
    immutable = (
        _spec(
            "app.host",
            "服务监听地址",
            "仅能通过启动环境修改。",
            value_type="string",
            mode=ConfigApplyMode.IMMUTABLE,
            env_alias="APP_HOST",
            getter=_field("app_host"),
            settings_fields=("app_host",),
            category="app",
        ),
        _spec(
            "app.port",
            "服务监听端口",
            "仅能通过启动环境修改。",
            value_type="integer",
            mode=ConfigApplyMode.IMMUTABLE,
            env_alias="APP_PORT",
            getter=_field("app_port"),
            settings_fields=("app_port",),
            category="app",
        ),
        _spec(
            "database.url",
            "数据库连接",
            "数据库地址不可通过管理员工具读取或修改。",
            value_type="string",
            mode=ConfigApplyMode.IMMUTABLE,
            getter=_configured("database_url"),
            category="database",
            sensitive=True,
        ),
        _spec(
            "superusers",
            "超级管理员列表",
            "唯一权限来源，只能通过启动环境维护。",
            value_type="string",
            mode=ConfigApplyMode.IMMUTABLE,
            getter=lambda settings: bool(settings.superusers),
            category="security",
            sensitive=True,
        ),
        _spec(
            "groups.startup_enabled",
            "启动默认群列表",
            "只作为尚未落库群的启动默认值。",
            value_type="string",
            mode=ConfigApplyMode.IMMUTABLE,
            getter=lambda settings: bool(settings.enabled_groups),
            category="groups",
            sensitive=True,
        ),
    )
    secret = (
        _spec(
            "llm.api_key",
            "LLM API Key",
            "只能确认是否已配置，不能读取或修改。",
            value_type="string",
            mode=ConfigApplyMode.SECRET,
            getter=_configured("llm_api_key"),
            category="secret",
            sensitive=True,
        ),
        _spec(
            "web.tavily_api_key",
            "Tavily API Key",
            "只能确认是否已配置，不能读取或修改。",
            value_type="string",
            mode=ConfigApplyMode.SECRET,
            getter=_configured("tavily_api_key"),
            category="secret",
            sensitive=True,
        ),
        _spec(
            "onebot.access_token",
            "OneBot Access Token",
            "只能确认是否已配置，不能读取或修改。",
            value_type="string",
            mode=ConfigApplyMode.SECRET,
            getter=_configured("onebot_access_token"),
            category="secret",
            sensitive=True,
        ),
        _spec(
            "napcat.webui_token",
            "NapCat WebUI Token",
            "该凭证不进入应用 Settings，只能回答不可访问。",
            value_type="string",
            mode=ConfigApplyMode.SECRET,
            getter=_constant(False),
            category="secret",
            sensitive=True,
        ),
        _spec(
            "database.password",
            "数据库密码",
            "只能确认数据库 URL 是否包含密码，不能读取或修改。",
            value_type="string",
            mode=ConfigApplyMode.SECRET,
            getter=_database_password_configured,
            category="secret",
            sensitive=True,
        ),
        _spec(
            "qq.login_credentials",
            "QQ 登录凭据",
            "QQ 登录态属于 NapCat，应用不能读取或修改。",
            value_type="string",
            mode=ConfigApplyMode.SECRET,
            getter=_constant(False),
            category="secret",
            sensitive=True,
        ),
        _spec(
            "vision.api_key",
            "视觉 API Key",
            "只能确认是否已配置，不能读取或修改。",
            value_type="string",
            mode=ConfigApplyMode.SECRET,
            getter=_configured("vision_api_key"),
            category="secret",
            sensitive=True,
        ),
    )
    return (*hot, *future, *restart, *immutable, *secret)


class ConfigRegistry:
    """Resolve only explicitly reviewed keys and aliases."""

    def __init__(self, specs: Iterable[ConfigSpec] | None = None) -> None:
        selected = tuple(specs) if specs is not None else _registered_specs()
        self._specs = {spec.key: spec for spec in selected}
        if len(self._specs) != len(selected):
            raise ValueError("duplicate runtime configuration key")
        aliases: dict[str, str] = {}
        for spec in selected:
            for alias in (spec.key, *spec.aliases):
                normalized = alias.strip().casefold()
                previous = aliases.get(normalized)
                if previous is not None and previous != spec.key:
                    raise ValueError(f"duplicate runtime configuration alias: {alias}")
                aliases[normalized] = spec.key
        self._aliases = aliases

    def get(self, key_or_alias: str) -> ConfigSpec:
        """Return a spec or reject unknown input before any value is processed."""

        normalized = key_or_alias.strip().casefold()
        key = self._aliases.get(normalized)
        if key is None:
            raise KeyError(key_or_alias)
        return self._specs[key]

    def maybe_get(self, key_or_alias: str) -> ConfigSpec | None:
        try:
            return self.get(key_or_alias)
        except KeyError:
            return None

    def list(self, category: str | None = None) -> tuple[ConfigSpec, ...]:
        """List the stable allowlist, optionally within one category."""

        normalized = category.strip().casefold() if category else None
        return tuple(
            spec
            for spec in self._specs.values()
            if normalized is None or spec.category.casefold() == normalized
        )

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(self._specs)

    @staticmethod
    def convert(spec: ConfigSpec, value: object) -> ConfigValue:
        """Convert untrusted command/tool input into the registered scalar type."""

        if spec.value_type == "boolean":
            if isinstance(value, bool):
                converted: ConfigValue = value
            elif isinstance(value, int) and value in {0, 1}:
                converted = bool(value)
            elif isinstance(value, str):
                token = value.strip().casefold()
                true_values = {"true", "1", "on", "yes", "是", "开启", "启用"}
                false_values = {"false", "0", "off", "no", "否", "关闭", "停用"}
                if token in true_values:
                    converted = True
                elif token in false_values:
                    converted = False
                else:
                    raise ValueError("必须是 true/false、on/off 或开启/关闭")
            else:
                raise ValueError("必须是布尔值")
        elif spec.value_type == "integer":
            if isinstance(value, bool):
                raise ValueError("必须是整数")
            if isinstance(value, int):
                converted = value
            elif isinstance(value, float) and value.is_integer():
                converted = int(value)
            elif isinstance(value, str):
                try:
                    converted = int(value.strip())
                except ValueError as exc:
                    raise ValueError("必须是整数") from exc
            else:
                raise ValueError("必须是整数")
        elif spec.value_type == "number":
            if isinstance(value, bool):
                raise ValueError("必须是数字")
            if isinstance(value, int | float):
                converted = float(value)
            elif isinstance(value, str):
                try:
                    converted = float(value.strip())
                except ValueError as exc:
                    raise ValueError("必须是数字") from exc
            else:
                raise ValueError("必须是数字")
        else:
            if not isinstance(value, str):
                raise ValueError("必须是字符串")
            converted = value.strip()
            if not converted:
                raise ValueError("不能为空")
            if spec.value_type == "enum":
                normalized_choices = {choice.casefold(): choice for choice in spec.choices}
                choice = normalized_choices.get(converted.casefold())
                if choice is None:
                    raise ValueError(f"必须是以下值之一：{', '.join(spec.choices)}")
                converted = choice

        if isinstance(converted, int | float) and not isinstance(converted, bool):
            if isinstance(converted, float) and not math.isfinite(converted):
                raise ValueError("必须是有限数字")
            if spec.minimum is not None and converted < spec.minimum:
                raise ValueError(f"不能小于 {spec.minimum:g}")
            if spec.maximum is not None and converted > spec.maximum:
                raise ValueError(f"不能大于 {spec.maximum:g}")
        return converted
