# 结果预算与 Artifact

`ToolResultBudgeter` 对所有 Provider 生效。结果超过字符/Token 或条目预算时，模型收到合法 JSON
摘要、总量、前若干项和不可猜测的 `artifact_handle`；完整 JSON 写入
`data/tool_artifacts/`，路径不会暴露给模型。

Agent 可用 Core Tool `read_tool_artifact(handle, offset, limit, query)` 分页读取或从关键词位置继续。
Handle 由数据库映射文件并在保留期后清理。关闭 Artifact 时仍返回明确截断摘要，不会把超长原文直接
注入模型。
