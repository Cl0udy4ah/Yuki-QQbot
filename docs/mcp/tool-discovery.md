# 工具发现与选择

`tools/list` 结果会转为稳定的 MCP metadata 并缓存。配置哈希一致且 TTL 有效时，重启后可直接用于
目录检索；哈希变化后旧缓存不用于执行，下一次连接重新发现。

选择模式：

- `all`：向主 Agent 提供 Planner scope 内全部完整工具
- `catalog`：只使用本地名称、描述、标签、scope 和请求文本粗选
- `hybrid`：本地粗选后使用 `ModelTask.TOOL_SELECTION` 的 Flash 档案精排
- `gateway`：不直接注入远程工具，只提供 `mcp_gateway`

Flash 只收到请求、Planner intent、候选名、短描述、tags 和 provider，不接收 JSON Schema；后端
会拒绝目录外返回值。两个 Server 的同名工具通过 Server ID 隔离。
