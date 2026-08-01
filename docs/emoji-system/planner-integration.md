# Planner 与回复序列

`TurnPlan.emoji` 只有：

- `intent`：当前消息是否明确索要表情
- `mode`：`none/optional/preferred/emoji_only`
- `placement`：`before_text/after_text/only`
- `goal`：期望的社交作用
- `emotion`：目标情绪

Schema 禁止 `emoji_id`、路径和 URL。表情是 Planner 原生回复效果，不再作为 Chat Agent 工具重复暴露。`emoji_only` 会把工具范围和记忆上下文模式同时收敛为 `none`，由 ChatService 跳过上下文装配、Embedding 与 Agent，直接解析效果；其他模式在正文生成后解析。回复完成条件是至少存在一个可见输出，因此已计划的 `preferred/emoji_only` 表情允许正文为空；纯文字和需要正文合成的语音仍不会把空响应当作成功。ReplySequenceManager 按“前置表情 → 文本消息 → 后置表情”发送；选择失败时提供文字降级。新消息可以取消尚未发送的效果，已经发送成功的内容保留在账本中。
