# Planner 与回复序列

`TurnPlan.emoji` 只有：

- `intent`：当前消息是否明确索要表情
- `mode`：`none/optional/preferred/emoji_only`
- `placement`：`before_text/after_text/only`
- `goal`：期望的社交作用
- `emotion`：目标情绪

Schema 禁止 `emoji_id`、路径和 URL。表情是 Planner 原生回复效果，不再作为 Chat Agent 工具重复暴露。`emoji_only` 会把工具范围和记忆上下文模式同时收敛为 `none`，由 ChatService 跳过上下文装配、Embedding 与 Agent，直接解析效果；其他模式在正文生成后解析。回复完成条件是至少存在一个可见输出，因此已计划的 `preferred/emoji_only` 表情允许正文为空；纯文字和需要正文合成的语音仍不会把空响应当作成功。ReplySequenceManager 按“前置表情 → 文本消息 → 后置表情”发送；选择失败时提供文字降级。新消息可以取消尚未发送的效果，已经发送成功的内容保留在账本中。

3.0.2 起，后端 `EmojiRequestDetector` 只对当前消息中高置信度、独立的发送请求（例如“发个
表情”“来个开心的表情包”）设置可信 hint。PlannerService 据此直接生成 `emoji_only` 计划，
Planner 模型请求、Agent 请求和工具调用均为 0；复杂请求仍交给正常 Planner。

Planner 超时、非法响应或供应商故障分别使用独立原因码。降级计划固定为一条 concise 回复、
`tool_selection=none`，不提供 `request_tools`，不执行 MCP、自动化、管理员或 OneBot 工具；若可信
hint 已证明是独立表情请求，则仍保留纯表情效果。

表情准备或发送失败由后端确定性恢复：optional 只跳过媒体并继续正文，preferred 保留正文并补一
条短说明，emoji-only 只发送失败说明。恢复不会重试原图、自动换图或重新进入 Planner/Agent。
