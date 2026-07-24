# Yuki-QQbot

## 启动项目

> **1.0 升级警告：**首次运行 Alembic `0005` 会永久清空 1.0 之前的会话、资料、权限和记忆表，再创建人物中心数据库。先备份 `data/`。

已经配置好 `.env` 并完成 NapCat 扫码时，在仓库根目录执行：

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f bot napcat
```

看到 NapCat 的反向 WebSocket 已连接后即可在 QQ 测试。`Ctrl+C` 只退出日志查看，不会停止容器。之后日常启动只需：

```bash
docker compose up -d
```

停止服务：

```bash
docker compose down
```

不要添加 `-v`，否则可能删除持久化数据。NapCat WebUI 地址为 <http://127.0.0.1:6099/webui/>。

## 项目定位

Yuki-QQbot 1.0 是基于 Python 3.12、NoneBot2、OneBot v11、NapCatQQ、SQLite 和 OpenAI-compatible Chat Completions API 的人物中心 QQ Agent。

- QQ 号字符串是人物的全局唯一身份。
- 当前消息发送者的 QQ 是否属于 `SUPERUSERS`，是唯一管理员凭证。
- 同一 QQ 的私聊、不同群成员关系和人物记忆关联到同一个人。
- 群号区分群；已启用群的全部消息都会被观察并永久写入事件账本。
- 私聊默认向所有 QQ 开放；`/ai private <QQ> off` 用于阻止指定用户。
- 个人记忆可以在私聊与群聊间自然复用，群记忆和群成员记忆仍按群隔离。
- 机器人支持 DeepSeek 普通/思考模式的多轮工具调用。

本版本不下载或识别图片、语音、视频和文件，但会保存其 OneBot 消息段元数据，为后续多模态版本保留基础。

## 首次配置

复制环境变量模板：

```powershell
# Windows PowerShell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

```bash
# Linux / macOS
test -f .env || cp .env.example .env
```

至少填写：

```dotenv
ONEBOT_ACCESS_TOKEN=一段长随机值
NAPCAT_WEBUI_TOKEN=另一段长随机值
SUPERUSERS=你的QQ号

LLM_PROVIDER=openai
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=你的DeepSeek密钥
LLM_MODEL=deepseek-v4-pro
LLM_THINKING_ENABLED=false
```

机器人账号不填在 `.env`。它由 NapCat WebUI 中实际扫码登录的 QQ 决定。

长系统提示词建议放在不提交到 Git 的 Markdown 文件：

```powershell
Copy-Item config/system_prompt.example.md config/system_prompt.md
```

然后设置：

```dotenv
SYSTEM_PROMPT_FILE=config/system_prompt.md
```

修改提示词后无需重建镜像：

```bash
docker compose up -d --no-deps --force-recreate bot
```

## 1.0 数据模型

`0005` 会创建以下主要数据：

| 表 | 作用 |
|---|---|
| `people` | 以 QQ `user_id` 为主键的人物 |
| `person_aliases` | QQ 昵称和各群历史称呼 |
| `groups` | 群名、启用状态和自主参与设置 |
| `memberships` | `(user_id, group_id)` 当前群名片与活跃时间 |
| `chat_events` | 永久保存收发消息、消息段、回复关系和时间 |
| `chat_events_fts` | FTS5 `trigram` 全文索引 |
| `person_memories` | 跨私聊和群聊的人物事实，最多 100 条 |
| `group_memories` | 群共同事实，最多 100 条 |
| `person_group_memories` | 某人在某群的称呼、关系和习惯，最多 50 条 |
| `person_preferences` | 机器人交互偏好，最多 30 条 |
| `memory_jobs` | 持久化后台记忆任务 |
| `context_resets` | `/ai new` 的上下文切点 |
| `agent_actions` | 通用 OneBot 工具的最小审计记录 |

消息到达后的顺序是：

```text
准入判断
  → 去重
  → 更新人物/群/成员
  → 写入永久事件账本
  → 记忆任务入队
  → 确定性命令、显式回复或自主参与判断
```

`/ai new` 只写上下文切点，不删除永久账本或人物记忆。

`/ai forgetme` 不会把命令和确认回复重新写回账本，并删除：

- 人物、别名、偏好、个人记忆、成员群记忆和成员关系；
- 该 QQ 发送的群事件；
- 该 QQ 私聊中的双方事件；
- 以该 QQ 为主体的群记忆、检索索引和后台任务；
- 其余事件正文中出现的精确 QQ 文本会替换为删除标记。

## 聊天上下文与记忆

每次普通回答会装配：

- 当前用户 QQ、昵称、别名、个人记忆和偏好；
- 当前群号、群记忆以及当前用户的成员群记忆；
- 被提及者和最近发言者中最多 5 人的相关记忆；
- 当前私聊或当前群最近 30 条本地事件；
- 只有模型主动调用搜索工具时，才加入更早历史。

新事件立即进入账本。后台记忆任务每 30 秒或累计 10 条时唤醒，每批最多 20 条，失败最多重试 3 次。明确添加的记忆标记为 `explicit`，自动提炼不能覆盖它。

## Agent 工具

所有普通聊天轮都可使用：

- `get_recent_chat_history`：每次直接调用 NapCat 的 `get_friend_msg_history` 或 `get_group_msg_history`，读取当前场景最近 20 条；未见消息会去重补入账本。
- `search_chat_history`：用 SQLite FTS5 搜索永久账本，可按 QQ、群号和时间范围约束；短于三个字符时使用有范围限制的 `LIKE`。
- `get_person_memories`：按 QQ 读取人物记忆。
- `get_group_memories`：按群号读取群记忆。

只有当前真实 OneBot 事件的 `sender.user_id` 属于 `SUPERUSERS` 时，该触发轮还会获得：

- `call_onebot_api(action, params)`：通过现有反向 WebSocket 调用任意 NapCat/OneBot action，不设 action denylist，也不二次确认。

引用管理员消息、历史里出现管理员 QQ、模型转述和自主群聊批次都不能获得管理员工具。每轮最多执行 5 次工具、6 次模型请求，单次工具结果最多 32,000 字符。通用 OneBot 调用只记录 actor QQ、action、成功状态、耗时和错误类别，不记录完整结果。

实现依据：

- [DeepSeek Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)
- [DeepSeek 思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)
- [NapCat API 列表](https://napneko.github.io/onebot/api)

## 群聊观察与自主参与

禁用群只处理超级管理员的启用命令。已启用群的未触发消息会更新人物、成员、账本和记忆任务，但不会阻断其他 NoneBot 插件。

谨慎自主参与的默认规则：

- 群消息静默 8 秒后，最多 20 条组成判断批次；
- 只有回复机器人、提到机器人、向群提问或与已有记忆明显相关时才进入模型判断；
- 判断置信度至少为 `0.85` 才发言；
- 每群自主发言后冷却 300 秒，每小时最多 3 次；
- 两次自主发言之间必须出现新的人类消息；
- 自主发言不开放通用 OneBot 管理工具；
- 仍使用普通消息发送、日常分句和 3–5 秒间隔。

## 命令

| 命令 | 作用 |
|---|---|
| `/ai help` | 显示帮助 |
| `/ai new` | 设置当前用户/场景的新上下文切点 |
| `/ai status` | 显示连接、模型、上下文和版本 |
| `/ai stop` | 取消当前用户/场景的模型请求 |
| `/ai ping` | 连通性检查 |
| `/ai whoami` | 显示 QQ、昵称、本群名片、别名与记忆统计 |
| `/ai forgetme` | 彻底删除当前 QQ 的可归属数据 |
| `/ai memory list` | 查看本人的人物记忆 |
| `/ai memory add <内容>` | 添加明确人物记忆 |
| `/ai memory update <ID> <内容>` | 修改本人的人物记忆 |
| `/ai memory delete <ID>` | 删除本人的人物记忆 |
| `/ai preference list` | 查看本人的交互偏好 |
| `/ai preference set <键> <值>` | 设置交互偏好 |
| `/ai preference delete <键>` | 删除交互偏好 |
| `/ai on` / `/ai off` | 超级管理员启用/停用当前群 |
| `/ai group <群号> on\|off` | 超级管理员启用/停用指定群 |
| `/ai private <QQ号> on\|off` | 超级管理员恢复/阻止指定 QQ 私聊 |

超级管理员可在 memory/preference 的操作名后加 `user <QQ号>`，例如：

```text
/ai memory list user 123456789
/ai preference set user 123456789 reply_style 简短
```

## 新配置默认值

| 环境变量 | 默认值 |
|---|---:|
| `OBSERVE_ENABLED_GROUPS` | `true` |
| `AUTONOMOUS_GROUP_CHAT_ENABLED` | `true` |
| `AUTONOMOUS_SILENCE_SECONDS` | `8` |
| `AUTONOMOUS_CONFIDENCE_THRESHOLD` | `0.85` |
| `AUTONOMOUS_COOLDOWN_SECONDS` | `300` |
| `AUTONOMOUS_MAX_PER_HOUR` | `3` |
| `RECENT_HISTORY_TOOL_LIMIT` | `20` |
| `LOCAL_CONTEXT_EVENT_LIMIT` | `30` |
| `RELATED_PEOPLE_LIMIT` | `5` |
| `PERSON_MEMORY_MAX_ENTRIES` | `100` |
| `GROUP_MEMORY_MAX_ENTRIES` | `100` |
| `PERSON_GROUP_MEMORY_MAX_ENTRIES` | `50` |
| `PREFERENCE_MAX_ENTRIES` | `30` |
| `MEMORY_BATCH_SECONDS` | `30` |
| `MEMORY_BATCH_TRIGGER_COUNT` | `10` |
| `MEMORY_BATCH_MAX_EVENTS` | `20` |
| `AGENT_MAX_TOOL_CALLS` | `5` |
| `AGENT_MAX_MODEL_REQUESTS` | `6` |
| `AGENT_TOOL_RESULT_MAX_CHARACTERS` | `32000` |

`ALLOWED_PRIVATE_USERS` 仅为旧配置兼容保留，1.0 不再把它当白名单；所有新 QQ 私聊默认准入。

## 本地开发与测试

```bash
uv sync --all-extras
uv run qq-ai-bot-cli init-db
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
uv run qq-ai-bot
```

Docker 验证：

```bash
docker compose config
docker compose build bot
docker compose up -d
docker compose ps
```

健康检查不会请求模型或暴露密钥：

```bash
docker compose exec bot python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/healthz').read().decode())"
```

## 服务器部署

- 推荐 2 核、2–4 GB 内存、20 GB SSD 的 Linux 小服务器。
- 不向公网暴露 bot 的 8080 端口。
- NapCat WebUI 只绑定 `127.0.0.1:6099`；远程访问使用 SSH 隧道：

  ```bash
  ssh -L 6099:127.0.0.1:6099 user@server
  ```

- 定期离线备份 `data/`、`napcat-data/` 和 `napcat-config/`。
- NapCat 是个人 QQ 协议端，不等同于腾讯官方 QQ Bot，存在协议变化、风控和封号风险；请控制频率并使用你有权控制的账号。

## 1.0 升级步骤

1. 停止写入：`docker compose stop bot napcat`。
2. 完整备份 `data/`、`napcat-data/` 和 `napcat-config/`。
3. 明确认可旧业务数据全部废弃。
4. 执行 `docker compose up -d --build`；bot 启动脚本会自动运行 `alembic upgrade head`。
5. 检查 `docker compose ps`、`/healthz` 和日志。
6. 依次人工验证：私聊、两个群、改昵称/群名片、记忆命令、历史工具和 `forgetme`。

版本 `0005` 是不可逆的破坏性迁移；需要回退时只能停止服务并恢复升级前备份。
