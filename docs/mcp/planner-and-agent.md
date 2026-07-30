# Planner 与 Agent

PlannerInput 的 `available_tool_scopes` 是动态紧凑目录。Planner 只能从中选择 scope，未知 scope 会使
该计划明确失败；2.0 的 `groups` 和 `tool_mode` 仍可解析，但内部统一为 scopes。

主 Agent 只看到经能力策略、Candidate Selector 和 Schema Budgeter 选中的完整工具。一次响应可调用
多个 Provider 的工具，后续模型请求可使用整批结果继续调用。MCP 不具有额外固定调用上限，统一使用
Agent 和 Tooling 运行时配置。

图片、网页、真实管理员身份、DelegatedAuthority、重复修改和 TurnCoordinator 规则仍在统一能力
边界执行，远程文本不能扩大权限。
