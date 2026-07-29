# Planner 语音治理

主聊天只保留一条执行链：

```text
MessageProcessor → PlannerService → AgentRunner → ReplySequenceManager
```

没有语音关键词路由，也没有独立语音会话。Planner 输出受严格模型校验的 `voice`：

- `intent`：`explicit_request`、`explicit_opt_out` 或 `neutral`；
- `mode`：`text`、`voice` 或 `text_and_voice`；
- `agent_tool`：`forbidden` 或 `required`；
- `style_hint`、`language` 和不包含路径的简短原因；
- 可选的 `preference_change`：人物模式与 `turn`/`persistent` 时长。

## 明确请求与 Agent 工具

Planner 根据完整自然语言语义和上下文判断明确请求，不依赖固定词表。当前用户本轮确实想听
语音时，后端把 `send_voice` 临时加入同一个 Agent 的工具列表；该工具只能选择公开风格与
`auto/zh/jp`，不能传入模式、profile、模型、参考音频、文件或路径。最终发送模式始终取
Planner 的计划，即使 Agent 或插件排队了不同的回复效果也不能覆盖。

Planner 未授权时 Agent 看不到 `send_voice`，直接伪造调用也会得到
`voice_not_authorized`。这样“用户主动询问时 Agent 可以用工具”和“普通聊天由 Planner 统一
决定”不会互相绕过。

## 日常主动语音

`neutral` 轮次的主动语音由 Planner 结合下列可信上下文决定：

- 人物偏好：`text_only`、`auto` 或 `prefer_voice`；
- `speech.spontaneous_frequency`（环境变量 `SPEECH_SPONTANEOUS_FREQUENCY`）；
- 当前会话最近中性回复轮次与其中的语音轮次；
- 声线、Worker、私聊/群聊开关及可用语言/风格。

后端根据脱敏 `planner_runs` 计算确定性的频率预算；明确索要语音与明确拒绝语音不进入该统计，
聊天正文也不用于计数。`text_only` 或预算不足时，中性计划会被单调收紧为 `text`。频率是上限
预算而不是强制配额，Planner 仍可在允许语音时选择文字。

## 持久人物偏好

`person_speech_preferences` 以 QQ 为主键，只保存一个当前模式、来源消息 ID 和时间。只有用户本人
在真实消息轮中明确表达“以后、默认、切换模式”等持续语义时，Planner 才能生成
`persistent` 修改；只约束当前轮的要求不会落库，自主群聊也不能修改人物偏好。删除人物时该行
通过外键级联删除。

未保存人物偏好时，`SPEECH_DEFAULT_MODE` 作为全局基线：

- `text` → `text_only`；
- `optional` → `auto`；
- `voice` / `text_and_voice` → `prefer_voice`。

## 语言、失败与可观测性

Planner 只会看到当前默认声线公开的目标语言，可以按语境选择中文或日文。合成前仍按最终正文
脚本校验语言，避免把中文正文交给日语 G2P。新消息通过既有 TurnCoordinator 取消未发送的旧
语音；TTS 不可用时按原有文字回退策略处理。

Alembic `0017` 为 `planner_runs` 增加模式、意图、工具策略、简短原因、偏好变化、配置频率和
近期比例。这些字段只用于调试与频率统计，不保存聊天正文、原始 QQ 号、Prompt 或隐藏推理。
