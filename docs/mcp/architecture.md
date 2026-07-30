# MCP 架构

MCP 是 Tool Kernel 的一个 Provider，不是第二套 Agent。配置、连接、缓存、目录、Binding、结果
归一化和运维命令分别由 `mcp/config.py`、`connection.py`、`manager.py`、`provider.py`、
`binding.py`、`result_normalizer.py` 和 `admin.py` 负责。

配置启用的 Server 被视为可信工具来源，不需要 MCP Tool 逐项审批；远程返回内容仍是外部资料，
不会授予权限。MCP Tool 与 Plugin Tool 使用同一个 Planner、能力策略、AgentRunner、调用协调器和
结果预算器。

持久化自动化通过 `mcp/automation.py` 的通用桥接层接入。只有 Server 配置中
`yuki.automation.includeTools` 明确列出的远端工具才会注册为
`mcp.<server_id>.<remote_tool_name>`；权限、风险、重试、JSON Schema 和输出 Artifact 随
注册定义进入原有 `AutomationCapabilityRegistry`。直接 DSL 步骤与 `yuki.agent` 使用同一个
Binding 路径，不存在按品牌编写的执行分支。

自然语言创建任务时不会把远端名称直接交给模型拼写。`AutomationCompiler` 在 TaskSpec Schema
中提供模型安全 ID，兼容连字符、下划线和点号差异，再解析为注册表中的真实名称；最终委托快照
只保存本任务明确选择的能力。底层 DSL 仍供插件 SDK 与内部调用使用。

自动化委托快照保存远端工具的完整元数据哈希。`tools/list_changed`、手工 refresh 或重连刷新
目录后，桥接层会原子替换该 Server 的动态定义；Schema 改变时旧快照不再匹配，禁用 Server 或
删除允许项时定义会消失。两种情况都会由既有执行器阻止旧任务，而不是把新能力自动补授给它。

普通聊天没有选择 MCP scope 时不注入 MCP Schema，也不会为了健康检查连接 lazy Server。
