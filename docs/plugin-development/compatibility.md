# 兼容性

Yuki 产品版本、Plugin API、Feature 和各 Schema 版本相互独立。

## Plugin API

- 当前 `1.0`；同一主版本内保持向后兼容。
- 新字段优先为可选并提供默认值。
- 删除字段前至少两个次版本标记 deprecated。
- 主版本不一致时拒绝加载。
- 插件不得依赖 `_` 开头属性、Host 类或数据库表结构。

Manifest 同时使用：

```toml
plugin_api = "1.0"
yuki_requires = ">=1.6.0,<3.0"
```

## Feature 探测

```python
if ctx.features.has("planner.signal.v1"):
    ...
ctx.features.require("plugin.agent_session.v1")
```

1.6.0 默认 Feature：

- `message.normalized.v1`
- `prompt.fragment.v1`
- `planner.signal.v1`
- `automation.action.v1`
- `plugin.agent_session.v1`

不要因为 Yuki 版本“看起来足够新”就假设部署者启用了某 Feature。

## Schema 版本

工具、自动化 Action 和 Event 各自声明 Schema 版本。改变必填字段、类型或语义属于不兼容变更，应新建组件名或提升 Schema 并迁移；旧 Automation 不会自动套用新 Schema。
