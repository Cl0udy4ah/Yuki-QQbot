# Planner 语音计划

Planner 输出 `voice.mode`（text、voice、text_and_voice、optional）、`style_hint`、
`language`（auto、zh、jp）和 reason。
它看不到 profile ID、reference ID 或文件路径。后端会根据当前私聊/群聊开关、默认档案、
Worker 健康和可用风格约束计划；不可用时回退文字。

Planner 只会看到当前默认声线公开的目标语言，可以按对话语境自然选择中文或日文；选择 `jp`
时 Agent 应生成日文正文，选择 `zh` 时生成中文正文。实际合成前，后端会按最终文本脚本再次判断：
日语假名和中文汉字是更强的证据，能修正 Planner 与正文偶尔不一致的情况；无法判断时才使用
Planner 提示或档案默认语言。

- `voice`：成功时只发 QQ 语音。
- `text_and_voice`：先发文字，再发语音。
- `optional`：日常交流可语音，失败时自然回退文字。
- `text`：技术回答、代码、长结构内容保持文字。

新消息会通过既有 TurnCoordinator 取消未发送的旧语音，过期轮次的结果不会发送。
