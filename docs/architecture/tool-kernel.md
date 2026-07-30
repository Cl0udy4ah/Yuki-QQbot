# Tool Kernel

Yuki 2.1 把工具来源和执行方式分开。`ToolProvider` 只贡献
`CapabilityDescriptor`，`ToolBinding` 才持有可执行实现；Planner 和 AgentRunner 不知道工具来自
Python 服务、插件、MCP Session，还是未来的 RPC 进程。

```mermaid
flowchart LR
  C[Core Provider] --> R[ToolProviderRegistry]
  A[Admin Provider] --> R
  U[Automation Provider] --> R
  P[Plugin Provider] --> R
  M[MCP Provider] --> R
  R --> D[UnifiedToolCatalog]
  D --> S[Planner scopes]
  D --> Q[Candidate Selector]
  Q --> B[Schema Budgeter]
  B --> G[AgentRunner]
  G --> I[ToolInvocationCoordinator]
  I --> X[Descriptor.binding]
  X --> O[ToolResultBudgeter]
```

目录项包含 descriptor、provider、scope、简述、tags、可检索文本、Schema Token 估算、可用性和
revision。模型工具名全局去重；远程 MCP 名称规范为 `mcp__<server>__<tool>`。

同一模型响应里的连续 `parallel_safe` 工具可并发执行。修改状态、平台修改、非幂等或语义未知的
工具默认串行；工具结果始终按模型原始 call 顺序回传。调用总数只取运行时
`agent.max_tool_calls`、`agent.max_model_requests` 和 `tooling.max_parallel_calls`。

Core、Admin、Automation 和 Plugin 通过 `InProcessToolBinding` 兼容现有服务；MCP 使用
`MCPToolBinding`。未来只需实现 `RpcToolBinding` 和新的 Provider，无需修改 Planner 或
AgentRunner。
