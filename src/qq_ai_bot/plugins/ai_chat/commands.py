"""Public command metadata used by documentation and tests."""

from qq_ai_bot.services.policies import CommandName

COMMAND_DESCRIPTIONS: dict[CommandName, str] = {
    CommandName.HELP: "显示使用说明和命令列表",
    CommandName.NEW: "清空当前用户、当前场景的会话",
    CommandName.STATUS: "显示连接、模型和会话状态",
    CommandName.STOP: "取消当前会话正在进行的 LLM 请求",
    CommandName.ON: "超级用户在当前群启用 AI",
    CommandName.OFF: "超级用户在当前群停用 AI",
    CommandName.PING: "返回 pong 和内部处理耗时",
}
