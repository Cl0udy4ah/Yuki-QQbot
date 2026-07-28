# Planner 语音计划

Planner 输出 `voice.mode`（text、voice、text_and_voice、optional）、`style_hint` 和 reason。
它看不到 profile ID、reference ID 或文件路径。后端会根据当前私聊/群聊开关、默认档案、
Worker 健康和可用风格约束计划；不可用时回退文字。

- `voice`：成功时只发 QQ 语音。
- `text_and_voice`：先发文字，再发语音。
- `optional`：日常交流可语音，失败时自然回退文字。
- `text`：技术回答、代码、长结构内容保持文字。

新消息会通过既有 TurnCoordinator 取消未发送的旧语音，过期轮次的结果不会发送。
