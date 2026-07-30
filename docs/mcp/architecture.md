# MCP 架构

MCP 是 Tool Kernel 的一个 Provider，不是第二套 Agent。配置、连接、缓存、目录、Binding、结果
归一化和运维命令分别由 `mcp/config.py`、`connection.py`、`manager.py`、`provider.py`、
`binding.py`、`result_normalizer.py` 和 `admin.py` 负责。

配置启用的 Server 被视为可信工具来源，不需要 MCP Tool 逐项审批；远程返回内容仍是外部资料，
不会授予权限。MCP Tool 与 Plugin Tool 使用同一个 Planner、能力策略、AgentRunner、调用协调器和
结果预算器。

普通聊天没有选择 MCP scope 时不注入 MCP Schema，也不会为了健康检查连接 lazy Server。
