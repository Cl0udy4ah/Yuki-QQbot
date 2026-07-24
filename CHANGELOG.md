# 更新日志

## 1.0.0 - 2026-07-25

### 破坏性变更

- 新增不可逆 Alembic `0005`：删除 1.0 之前的会话、资料、权限和记忆数据，重建人物中心 schema。
- QQ 号字符串成为人物的全局唯一身份；`SUPERUSERS` 环境变量成为唯一管理员来源。
- 所有私聊默认准入，`/ai private <QQ> off|on` 改为阻止/恢复指定用户。
- 已启用群开始观察并永久保存未触发消息；禁用群只处理超级管理员启用命令。
- `/ai new` 改为写上下文切点，不再删除历史。
- `/ai forgetme` 改为彻底删除当前 QQ 的人物、记忆、成员关系和可归属聊天事件。

### 人物、事件与记忆

- 新增 `people`、`person_aliases`、`groups`、`memberships`。
- 新增永久 `chat_events` 事件账本，保存机器人账号、消息 ID、QQ、群号、方向、文本、消息段 JSON、回复关系和时间。
- 新增 FTS5 `trigram` 索引；三字及以上使用全文检索，短词使用带范围限制的 `LIKE`。
- 新增 `person_memories`、`group_memories`、`person_group_memories` 和 `person_preferences`。
- 新增持久化 `memory_jobs`，每 30 秒或累计 10 条批量提炼，单批最多 20 条，失败最多重试 3 次。
- 回答上下文支持当前人物跨私聊/群聊记忆、当前群记忆、成员群记忆和最多 5 位相关人物。
- 保留图片、表情、语音、视频、文件和转发消息段元数据；本版不下载媒体、不接视觉模型。

### QQ Agent

- 扩展 Chat Completions 类型：`tools`、`tool_choice`、`tool_calls`、`tool_call_id`、`reasoning_content`。
- 支持 DeepSeek 普通和思考模式多轮工具调用；思考模式中间轮原样回传 `reasoning_content`。
- 每轮最多 5 次工具、6 次模型请求；未知工具、无效 JSON 和超限循环返回工具错误。
- 新增 `get_recent_chat_history`：每次直接调用 NapCat 当前私聊/群历史接口，并把未见消息补入账本。
- 新增 `search_chat_history`、`get_person_memories`、`get_group_memories`。
- 当前直接消息发送者属于 `SUPERUSERS` 时开放 `call_onebot_api(action, params)`，可调用全部 OneBot action，无 denylist 和二次确认。
- 通用 OneBot 调用新增最小审计，不在普通日志保存完整工具结果。

### 群聊自主参与

- 新增 8 秒静默批次和最多 20 条候选上下文。
- 仅对回复机器人、提到机器人、群提问或记忆相关内容进入模型参与判断。
- 默认置信度阈值 `0.85`、冷却 300 秒、每小时最多 3 次。
- 两次自主发言之间必须有新的人类消息；自主判断轮不开放通用 OneBot 工具。
- 保留普通消息发送、日常分句和 3–5 秒随机间隔。

### 命令

- 新增 `/ai memory list|add|update|delete`。
- 新增 `/ai preference list|set|delete`。
- 超级管理员可在操作名后增加 `user <QQ号>` 管理任意人物。
- `/ai whoami` 新增已知别名、人物记忆数和群成员关系数。
- 命令处理与 AI 工具共用业务仓储，不通过模型生成 `/ai ...` 再回灌。

### 文档与质量

- README 开头提供启动命令和 1.0 数据清空警告。
- 更新 `.env.example`、架构、命令、Agent 工具、配置、部署与升级说明。
- 新增人物、账本、FTS、记忆、工具权限、删除和破坏性迁移测试。

## 0.1.0 - 2026-07-23

- 初始 Python 3.12、NoneBot2、OneBot v11、NapCatQQ、SQLite 和 OpenAI-compatible LLM 项目。
- 支持私聊、群聊触发、管理员开关、SQLite 会话、身份资料、群记忆、分句发送和 Docker Compose 部署。
