# 架构

Plugin API v1 把“可声明的扩展”和“可使用的运行时服务”分开：

```text
plugin.toml
  → Discovery（只读 TOML，不导入代码）
  → Compatibility + Manifest hash
  → Administrator approval
  → Loader（本地可信 Python）
  → register(PluginRegistrar)
  → start(bound PluginContext)
  → events / tools / commands / automations / background services
  → stop()
```

## SDK 与 Host

- `yuki_plugin_sdk`：稳定、依赖轻的公开 Protocol、Pydantic 模型、枚举和测试 Fake。
- `qq_ai_bot.plugin_host`：Host 私有实现，负责发现、批准、名称冲突、事件超时、数据隔离和权限校验。
- 插件：只能依赖 SDK。不要导入 `qq_ai_bot`、访问 `_` 开头属性或保存 Host 内部对象。

`register()` 得到的是声明型 `PluginRegistrar`，没有运行时 Facade；`start()` 才得到已绑定 `plugin_id`、真实当前用户/群和已批准权限的 `PluginContext`。

## Planner-first 主聊天

Yuki 1.6.0 的主聊天先形成受后端约束的 `TurnPlan`，再调用原有单一 Agent。Planner 只能决定回复、等待或沉默，并缩小工具与发送计划；它不能授予权限、执行工具或产生最终回复。

插件可以贡献有界 `PlannerSignal`，但信号总和会被 Host 裁剪。确定性 `/ai` 命令仍绕过 Planner；普通聊天统一由 Planner 决策，不再保留旧聊天流程。

## 独立插件 AI 会话

`ctx.agent_sessions` 用于跑团、游戏主持、插件向导等需要独立连续历史的功能：

- 会话键为 `plugin-session:<plugin_id>:<uuid>`；
- 历史只在 `plugin_agent_messages` 中，默认不读取 Yuki 主聊天、人物记忆或群记忆；
- 不写入 `chat_events`；
- 不向插件返回隐藏推理；
- 能力始终取“插件声明 ∩ 管理员批准 ∩ 本轮请求”；
- 插件会话永远不能伪造 `SUPERUSERS`。

这不是第二套 Yuki 人格或管理员路由，而是插件拥有的隔离任务会话。完整用法见 [服务 Facade](service-facades.md)。

## 数据所有权

| 数据 | 所有者 | 插件访问方式 |
|---|---|---|
| 主聊天账本、人物、关系、记忆 | Yuki Core | 对应只读/写 Facade + 权限 |
| 插件配置 | Host，按插件/作用域隔离 | `ctx.config` |
| 插件 Secret | Host/部署者 | `ctx.secrets`，只按名称读取 |
| 插件 KV | 插件命名空间 | `ctx.storage` |
| Planner 记录 | Yuki Core | 无原始数据库访问 |
| 独立插件 AI 历史 | Host，按插件和会话隔离 | `ctx.agent_sessions` |

所有 Facade 都是能力边界，不是 Repository 的别名；插件永远不能获得 SQLAlchemy Session。

