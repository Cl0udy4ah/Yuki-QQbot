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
    CommandName.WHOAMI: "查看机器人识别到的当前身份",
    CommandName.FORGETME: "删除自己的昵称和全部群名片资料",
    CommandName.PRIVATE: "超级用户开启或关闭指定 QQ 用户的私聊权限",
    CommandName.GROUP: "超级用户开启或关闭指定群的 AI",
    CommandName.MEMORY: "查看、增加、修改或删除人物记忆",
    CommandName.PREFERENCE: "查看、设置或删除交互偏好",
    CommandName.AFFECTION: "查看好感度与信任度，或由超级管理员调整",
    CommandName.CAPABILITIES: "按当前真实 QQ 查看完整权限、可改参数数量与接口范围",
    CommandName.CONFIG: "超级管理员读取、修改、清除或回滚运行时配置",
    CommandName.AUTOMATION: "查看、暂停、恢复、取消或立即运行自己的自动化任务",
    CommandName.VOICE: "查看、测试和管理本地语音声线",
}
