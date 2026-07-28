# Planner 与回复序列

`TurnPlan.emoji` 只有：

- `mode`：`none/optional/preferred/emoji_only`
- `placement`：`before_text/after_text/only`
- `goal`：期望的社交作用
- `emotion`：目标情绪

Schema 禁止 `emoji_id`、路径和 URL。Agent 的 `send_emoji` 同样只创建 `PendingReplyEffect`，不直接调用 OneBot。ChatService 在生成结束后统一解析效果，ReplySequenceManager 按“前置表情 → 文本消息 → 后置表情”发送；`emoji_only` 选择失败时保留文字降级。新消息可以取消尚未发送的效果，已经发送成功的内容保留在账本中。
