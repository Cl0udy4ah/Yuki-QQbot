# MCP 配置

Yuki 只读取 `MCP_CONFIG_PATH` 指定的 UTF-8 JSON，不扫描或导入其他客户端配置。本机运行复制
`.mcp.json.example` 为 `.mcp.json`；Docker 使用已有只读 `config/` 挂载，复制为
`config/mcp.json` 并设置 `MCP_CONFIG_PATH=/app/config/mcp.json`。然后设置
`MCP_ENABLED=true`。

支持 `command`、`args`、`cwd`、`env`、`url`、`headers`、`disabled`、`lifecycle`、
`connectTimeoutSeconds`、`requestTimeoutSeconds`、`reconnectDelaySeconds`、`includeTools`、`excludeTools`，以及
`yuki.scope/summary/tags`。`command` 与 `url` 必须且只能填写一个。

Secret 只能写成 `${ENV_NAME}` 并放在 `.env` 或宿主环境中。Yuki 不把解析后的 Header、Cookie、
环境变量值写入日志、数据库、Prompt 或状态接口。配置示例中的麦当劳地址仅是接入模板，实际 URL
和鉴权方式以服务提供者为准。

`reconnectDelaySeconds` 控制 `keep_alive` / `lazy_keep_alive` 断线后的重试间隔。重试没有
代码内固定次数上限；停用 Server 或关闭应用会取消恢复任务。
