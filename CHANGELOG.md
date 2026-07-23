# 更新日志

本文件记录 QQ AI Bot 的重要功能、行为和配置变化。

## 未发布

### 新增

- 支持通过 `SYSTEM_PROMPT_FILE` 从 UTF-8 Markdown 文件加载长系统提示词。
- 提供 `config/system_prompt.example.md` 示例；Docker Compose 将 `config/` 只读挂载到容器。
- 支持将短篇、纯文本的日常聊天回复按完整句子拆成多条 QQ 消息。
- 新增日常分句配置：
  - `SPLIT_DAILY_CHAT_SENTENCES`
  - `DAILY_CHAT_SPLIT_MAX_CHARACTERS`
  - `DAILY_CHAT_SPLIT_MAX_MESSAGES`
- 支持在日常分句消息之间进行随机异步等待，默认范围为 3–5 秒：
  - `DAILY_CHAT_MESSAGE_DELAY_MIN_SECONDS`
  - `DAILY_CHAT_MESSAGE_DELAY_MAX_SECONDS`
- 补充提示词文件校验、普通消息发送、日常分句和异步延迟的自动化测试。
- 新增 `user_profiles`，以 QQ `user_id` 为稳定主键保存最新昵称。
- 新增 `user_group_profiles`，按 `(user_id, group_id)` 隔离保存不同群的最新群名片。
- 缺少事件资料时，通过 NapCat `get_group_member_info` 或 `get_stranger_info`
  补全当前发送者身份，无需额外插件。
- 新增 `/ai whoami` 查看机器人在当前场景识别到的本人身份。
- 新增 `/ai forgetme` 删除本人的昵称和全部群名片资料。
- 新增 `/ai private <QQ号> on|off`，供超级用户持久化开关指定用户的私聊权限。
- 新增 `/ai group <群号> on|off`，供超级用户持久化开关指定群的 AI。
- 新增 `private_user_settings` 数据表；现有 `group_settings` 同时支持按群号管理。
- 新增 `group_memories`，自动保存每群最多 30 条模型提取的共享事实。
- 支持从明确触发机器人的普通群聊中实时提取、更新和删除群共享记忆。
- 支持解析其他被 `@` 的群成员，以不含 QQ 号的稳定群内引用区分发言者与被提及者。

### 变更

- Bot 的 AI 回复、命令结果、限流通知和错误提示改为普通 QQ 消息，不再引用或回复用户原消息。
- 日常分句只处理不超过 240 字、可拆成 2–4 句的纯文本；代码块、列表、表格、长回复和句子过多的内容继续使用原有长度分段逻辑。
- 日常分句的第一条消息立即发送，后续消息分别随机等待 3–5 秒；异步等待不会阻塞其他会话。
- 设置 `SYSTEM_PROMPT_FILE` 后，文件内容优先于内联 `SYSTEM_PROMPT`；文件不存在、不可读或为空时启动失败。
- 模型请求会临时注入当前用户的显示名称，用于区分用户但不会主动称呼；
  身份上下文不会写入聊天历史，也不包含 QQ 号。
- 数据库中的动态私聊/群聊开关优先于 `.env` 初始白名单，`off` 可以明确覆盖
  环境变量中的启用项；超级用户自身始终保留私聊管理权限。
- 群共享记忆在正常回复后通过独立模型请求更新；提取失败不会影响聊天回复。

### 隐私与安全

- 真实的 `config/system_prompt.md` 不进入 Git 仓库或 Docker 构建上下文。
- `.env`、API Key、QQ 登录数据、聊天数据库和 NapCat 运行目录继续保持忽略。
- 仅对允许的私聊或明确触发机器人的群消息采集身份；普通群聊不查询、不落库。
- 昵称和群名片会清除控制字符、压平换行并限制长度，模型将其视为不可信元数据。
- 群名片严格按用户和群双重隔离；群聊不使用私聊昵称作数据库回退，不读取其他群
  或其他用户资料。
- 完整 QQ 号只在本人主动执行 `/ai whoami` 时显示，不传给模型或普通日志。
- 管理命令仅限 `SUPERUSERS`，严格校验目标号码和 `on|off` 参数；回复不复述目标号码，
  目标号码不会进入模型上下文。
- 群记忆只保存提炼后的公开短事实，不保存原始聊天；仅当前群可读取，私聊和其他群
  无法访问。未触发的普通群聊继续保持零采集。

## 0.1.0 - 2026-07-23

### 新增

- 基于 Python 3.12、NoneBot2 和 OneBot v11 的 QQ AI 聊天机器人。
- 支持私聊、群聊 `@` 触发、群开关、超级用户命令和会话隔离。
- 支持 OpenAI-compatible LLM、DeepSeek 和离线 FakeLLM。
- 支持 SQLite 会话持久化、Alembic 迁移、消息去重、频率限制和并发控制。
- 支持 Docker Compose 部署、NapCat WebUI 扫码登录、健康检查和结构化日志。
- 提供输入清理、输出长度限制、QQ 长消息分段以及安全失败提示。
