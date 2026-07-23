# QQ AI Bot

一个面向单账号、单进程部署的 QQ AI 聊天机器人。项目使用 Python 3.12、NoneBot2、OneBot v11、异步 SQLAlchemy 和 OpenAI-compatible Chat Completions API；个人 QQ 由外部 NapCatQQ 容器登录，本仓库不复制、修改或内置 NapCat 源码。

项目的重要功能和配置变更记录在 [CHANGELOG.md](CHANGELOG.md)。

> **重要风险提示**：NapCat 属于个人 QQ 协议端，不等同于腾讯官方 QQ Bot。个人账号自动化可能受到平台规则、风控、协议变更和封号风险影响。请只使用自己的账号，控制频率，遵守适用法律、腾讯平台规则和 NapCat 许可。本项目不处理验证码、不绕过登录验证、不保存 QQ 密码；登录必须由用户在 WebUI 手动扫码完成。

## 架构

```text
QQ / NTQQ
  ↓
NapCatQQ（外部容器，手动扫码）
  ↓ OneBot v11 反向 WebSocket
NoneBot2 接入层
  ↓
消息标准化 → 触发/权限 → 去重 → 当前用户资料 → 限流/会话锁
  ↓                         ↓                    ↓
SQLite 会话历史与身份资料 ← Chat Service → LLM Provider
  ↓
回复清理/分段 → OneBot v11 → QQ
```

代码分层如下：

- `domain/`：`InboundMessage`、`OutboundMessage`、`ConversationIdentity` 等平台无关模型。
- `adapters/onebot/`：唯一接触 OneBot Event 的标准化与发送层；不下载附件。
- `services/`：触发策略、会话、命令、限流、去重、并发和回复渲染。
- `llm/`：抽象 `LLMProvider`、真实 OpenAI-compatible 实现和离线 Fake 实现。
- `persistence/`：异步 SQLAlchemy 模型与显式事务仓储。
- `plugins/ai_chat/`：很薄的 NoneBot matcher，只负责连接适配器和业务服务。

同一 `conversation_key` 使用独立 `asyncio.Lock` 顺序处理，不同会话可并行；所有模型请求还受全局 `asyncio.Semaphore` 限制。MVP 明确是单进程架构。多实例部署前，必须把会话锁、限流与去重迁移到 Redis 等共享基础设施。

## 官方 QQ Bot 与个人 QQ 协议端

| 项目 | 腾讯官方 QQ Bot | 本项目使用的 NapCatQQ |
|---|---|---|
| 身份 | 官方开放平台机器人 | 用户自己的个人 QQ |
| 登录 | AppID/Token 等官方凭据 | NapCat WebUI 手动扫码 |
| 协议与支持 | 官方 API 和审核规则 | OneBot v11 社区生态，受 QQ 客户端变更影响 |
| 风险 | 主要是开放平台配额与审核 | 还包括个人账号风控、掉线和协议兼容风险 |

若业务可以使用官方机器人，应优先评估官方方案。本项目的领域层不依赖 OneBot，后续可新增官方适配器而不重写会话与 LLM 逻辑。

## 系统要求

- 本地开发：Python 3.12、[uv](https://docs.astral.sh/uv/)、Git。
- 容器部署：Docker Engine 24+ 与 Docker Compose v2；Windows 应使用 Linux containers/WSL2。
- NapCat 支持的 Linux 架构与镜像说明以 [NapCat-Docker 官方仓库](https://github.com/NapNeko/NapCat-Docker) 为准。
- 无需也不应向公网开放任何端口。Compose 仅把 WebUI 绑定到 `127.0.0.1:6099`，bot 的 8080 端口只在 Docker 网络内可见。

## 从空目录开始

```bash
git clone <本仓库地址> qq-ai-bot
cd qq-ai-bot
cp .env.example .env
```

Windows PowerShell 将第三行改为：

```powershell
Copy-Item .env.example .env
```

随后编辑 `.env`，至少替换：

- `ONEBOT_ACCESS_TOKEN`：NapCat 与 NoneBot 共用的长随机值。
- `NAPCAT_WEBUI_TOKEN`：独立的 WebUI 长随机值。
- `SUPERUSERS`、`ALLOWED_PRIVATE_USERS`、`ENABLED_GROUPS`。
- `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`。

`.env` 已被 Git 忽略；不要提交它。可用 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 分别生成两个 Token。

较长的系统提示词建议使用 Markdown 文件：

```powershell
Copy-Item config/system_prompt.example.md config/system_prompt.md
```

编辑 `config/system_prompt.md`，然后在 `.env` 中设置：

```dotenv
SYSTEM_PROMPT_FILE=config/system_prompt.md
```

Compose 会把整个 `config/` 目录只读挂载到容器的 `/app/config`。真实的
`config/system_prompt.md` 同时被 Git 和 Docker 构建上下文忽略，仓库只保留
`config/system_prompt.example.md`。文件必须是非空 UTF-8 文本；设置文件路径后，
文件内容优先于 `SYSTEM_PROMPT`。

## 本地开发

```bash
uv sync --all-extras
uv run qq-ai-bot-cli init-db
uv run qq-ai-bot
```

默认监听 `0.0.0.0:8080`。本地 NapCat 的反向 WebSocket 地址为 `ws://127.0.0.1:8080/onebot/v11/ws`；Token 必须与 `.env` 的 `ONEBOT_ACCESS_TOKEN` 一致。

完全离线验证聊天链路时，可在 `.env` 设置：

```dotenv
LLM_PROVIDER=fake
LLM_MODEL=fake-model
```

FakeLLM 不连接真实 QQ 或模型 API，仅用于开发和测试。生产使用 `LLM_PROVIDER=openai`。

## Docker 部署

确认 `.env` 已填写后执行：

```bash
docker compose config
docker compose up -d --build
docker compose ps
docker compose logs -f bot napcat
```

Compose 的行为：

1. bot 启动脚本从环境变量原子生成 `napcat-config/onebot11.json`，其中反向 WebSocket 地址为 `ws://bot:8080/onebot/v11/ws`，Token 来自 `ONEBOT_ACCESS_TOKEN`。
2. bot 执行 `alembic upgrade head` 后启动 NoneBot；健康后 NapCat 才启动。
3. NapCat 在断开时按配置每 30 秒重连。NapCat 未连接或 LLM 暂时不可用不会终止 bot；`/ai status`、`/ai ping` 仍可用。
4. WebUI 只可从部署主机打开：<http://127.0.0.1:6099/webui>。

在远程服务器上不要开放 6099。需要访问时建立 SSH 隧道：

```bash
ssh -L 6099:127.0.0.1:6099 user@server
```

然后在本机打开 `http://127.0.0.1:6099/webui`。

### NapCat WebUI 手动扫码

1. 打开 WebUI，用 `.env` 中的 `NAPCAT_WEBUI_TOKEN` 登录。
2. 进入 QQ 登录页面，选择二维码登录。
3. 使用手机 QQ 扫码并在手机上确认。项目不会自动识别二维码或代替确认。
4. 登录后进入 OneBot/网络配置，确认存在启用的 **WebSocket 客户端（反向 WS）**：
   - URL：`ws://bot:8080/onebot/v11/ws`
   - Token：与 `ONEBOT_ACCESS_TOKEN` 完全一致
   - 消息格式：`array`
   - 上报自身消息：关闭
   - 重连间隔：`30000` 毫秒
5. 查看 `docker compose logs -f napcat bot`，确认反向 WebSocket 已连接。

NapCat 官方文档将 WebSocket 客户端定义为反向 WebSocket；WebUI 端口默认是 6099。界面随版本变化时，以 [NapCat WebUI 配置文档](https://napneko.github.io/config/basic) 为准。

### 持久化目录

| 宿主机目录 | 容器目录 | 内容 |
|---|---|---|
| `./data` | `/app/data` | SQLite 数据库 |
| `./napcat-data` | `/app/.config/QQ` | QQ 登录状态和客户端数据 |
| `./napcat-config` | `/app/napcat/config` | NapCat 与 OneBot 配置 |
| `./napcat-plugins` | `/app/napcat/plugins` | NapCat 插件目录（本项目不安装插件） |

这些目录都已被 Git 忽略。绝不要把 QQ 数据、数据库、日志、生成后的 OneBot 配置或密钥提交到版本库。

## 环境变量

### 核心与权限

| 变量 | 说明 | 默认值 |
|---|---|---|
| `ONEBOT_ACCESS_TOKEN` | 反向 WS 鉴权；双方必须一致 | 空（生产必须设置） |
| `SUPERUSERS` | 逗号分隔的 QQ 号 | 空 |
| `ALLOWED_PRIVATE_USERS` | 私聊白名单；超级用户自动加入 | 空 |
| `ENABLED_GROUPS` | 初始启用群，逗号分隔 | 空 |
| `IGNORED_BOT_USERS` | 已知其他机器人 QQ 号 | 空 |
| `AI_PREFIX` | 群聊自定义触发前缀 | `!ai` |
| `LOG_MESSAGE_CONTENT` | 是否允许正文日志；建议保持关闭 | `false` |

OneBot v11 消息事件没有可靠、统一的“发送者是其他机器人”字段，因此应把已知机器人账号加入 `IGNORED_BOT_USERS`。机器人自身消息会按 `self_id` 拒绝。

`ALLOWED_PRIVATE_USERS` 和 `ENABLED_GROUPS` 是首次部署时的默认值。超级用户通过
管理命令写入的 SQLite 开关优先级更高，因此可以动态新增目标，也可以用 `off`
覆盖 `.env` 中原本启用的用户或群；超级用户自身的私聊权限不能关闭。

### LLM

| 变量 | 说明 | 默认值 |
|---|---|---|
| `LLM_PROVIDER` | `openai` 或 `fake` | `openai` |
| `LLM_BASE_URL` | OpenAI-compatible API 根地址，通常含 `/v1` | `https://api.openai.com/v1` |
| `LLM_API_KEY` | API Key；日志不会记录 | 空 |
| `LLM_MODEL` | 模型名，不在业务代码写死 | 空 |
| `LLM_TIMEOUT_SECONDS` | 连接/读取超时 | `30` |
| `LLM_MAX_RETRIES` | 首次请求之后的最多重试次数 | `1` |
| `LLM_TEMPERATURE` | temperature | `0.7` |
| `LLM_MAX_OUTPUT_TOKENS` | 供应商输出 Token 上限 | `1024` |
| `LLM_THINKING_ENABLED` | 可选思考开关；DeepSeek 建议日常聊天设为 `false` | 不发送 |
| `SYSTEM_PROMPT` | 系统提示词 | 见 `.env.example` |
| `SYSTEM_PROMPT_FILE` | UTF-8 Markdown 提示词文件；设置后优先于 `SYSTEM_PROMPT` | 空 |

只对连接错误、超时和明确的 HTTP 5xx 使用带抖动的指数退避；4xx 不重试。MVP 不发送 tools/functions 字段，不提供 Shell、Python、浏览器、本地文件或任意抓取能力。

DeepSeek V4 默认开启思考模式。QQ 日常聊天可设置 `LLM_THINKING_ENABLED=false`，让 API 直接返回最终回答；删除该变量则保持供应商默认行为。该扩展字段只在变量被显式配置时发送，因此不会影响其他 OpenAI-compatible 服务。

修改 `config/system_prompt.md` 后不需要重建镜像，只需执行
`docker compose up -d --no-deps --force-recreate bot`。为避免旧对话风格继续影响回复，
可在对应 QQ 会话中发送 `/ai new`。

### 容量与安全边界

| 变量 | 说明 | 默认值 |
|---|---|---|
| `MAX_CONTEXT_MESSAGES` | 发送给模型的普通历史消息数 | `30` |
| `MAX_CONTEXT_CHARACTERS` | 上下文字符上限 | `12000` |
| `GLOBAL_LLM_CONCURRENCY` | 全局模型并发 | `4` |
| `PER_USER_REQUESTS_PER_MINUTE` | 每用户每类请求的分钟上限 | `10` |
| `PER_GROUP_REQUESTS_PER_MINUTE` | 每群每类请求的分钟上限 | `30` |
| `MAX_INPUT_CHARACTERS` | 单次清理后输入字符上限 | `4000` |
| `MAX_OUTPUT_CHARACTERS` | 模型回复总字符上限 | `12000` |
| `MAX_QQ_MESSAGE_CHARS` | 单条 QQ 消息字符上限 | `1800` |
| `SPLIT_DAILY_CHAT_SENTENCES` | 是否将短日常回复按句拆成多条普通消息 | `true` |
| `DAILY_CHAT_SPLIT_MAX_CHARACTERS` | 允许按句拆分的回复总字符上限 | `240` |
| `DAILY_CHAT_SPLIT_MAX_MESSAGES` | 单次日常回复最多拆出的消息数 | `4` |
| `DAILY_CHAT_MESSAGE_DELAY_MIN_SECONDS` | 日常分句消息之间的最短等待秒数 | `3` |
| `DAILY_CHAT_MESSAGE_DELAY_MAX_SECONDS` | 日常分句消息之间的最长等待秒数 | `5` |
| `GROUP_MEMORY_ENABLED` | 是否从触发机器人的普通群聊中自动提取共享记忆 | `true` |
| `GROUP_MEMORY_MAX_ENTRIES` | 每个群最多保留的共享事实；最大允许 `50` | `30` |

命令和普通聊天使用不同限流桶。超级用户不会绕过全局 LLM 并发限制。

日常分句使用保守启发式：只有短篇纯文本能够拆成 2–4 个完整句子时才逐句发送；
代码围栏、列表、表格、长回复或句子过多的内容仍按原有 QQ 长度规则发送。关闭
`SPLIT_DAILY_CHAT_SENTENCES` 即可恢复只按长度分段。第一句立即发送，后续每句发送前
会在 `DAILY_CHAT_MESSAGE_DELAY_MIN_SECONDS` 与
`DAILY_CHAT_MESSAGE_DELAY_MAX_SECONDS` 之间随机异步等待；该等待不会阻塞其他会话。

## 消息与会话规则

- 私聊：只响应有效白名单和 `SUPERUSERS`；会话键为 `private:{user_id}`。
- 群聊：只处理已启用群内明确 `@机器人`、以 `/ai` 开头或以 `AI_PREFIX` 开头的消息。
- 有效白名单/群开关优先读取数据库覆盖，未设置覆盖时再使用 `.env` 默认值。
- 默认群会话键为 `group:{group_id}:user:{user_id}`，不同成员不共享历史。
- 数据库已预留 `group:{group_id}:shared` 和 `conversation_mode`，MVP 不提供切换界面。
- 未触发的普通群聊不会进入去重表或消息表，也不会写日志正文。
- 只在允许的私聊或明确触发机器人的群消息中更新发送者身份；普通群聊不查询
  NapCat，也不写入用户资料表。
- 群共享记忆也只处理明确触发机器人的普通聊天；不会监听或持久化未触发消息。
- 支持文本、@后的文本、回复消息文本和 QQ 表情占位符。
- 图片、语音、视频、文件、合并转发、XML/JSON 卡片只记录类型元数据，不下载；仅含这些内容时回复“当前版本暂不支持该消息类型。”

## 命令

| 命令 | 作用 |
|---|---|
| `/ai help` | 显示帮助 |
| `/ai new` | 事务清空当前用户、当前场景的会话 |
| `/ai status` | 显示 OneBot 连接、模型、消息数、处理状态和版本 |
| `/ai stop` | 取消当前会话正在进行的模型请求 |
| `/ai on` | 超级用户在当前群启用 AI |
| `/ai off` | 超级用户在当前群停用 AI |
| `/ai private <QQ号> on\|off` | 超级用户开启或关闭指定 QQ 用户的私聊权限 |
| `/ai group <群号> on\|off` | 超级用户开启或关闭指定群的 AI |
| `/ai ping` | 返回 pong 和内部处理耗时 |
| `/ai whoami` | 查看机器人在当前私聊或当前群识别到的本人身份 |
| `/ai forgetme` | 删除本人的昵称和全部群名片资料；不删除聊天记录 |

状态命令不会显示 API Key、数据库路径、完整系统提示词或 QQ 登录凭据。
`whoami` 不接受目标用户参数，只能查看发送者本人；按当前配置会显示完整 QQ 号。
需要同时清除当前聊天记录时，另行执行 `/ai new`。

`private` 和 `group` 命令可由超级用户在私聊或群聊中执行；即使当前群尚未启用，
超级用户仍可使用管理命令。QQ 号和群号必须是 5–20 位、首位非零的数字，
开关只能是 `on` 或 `off`。命令回复不会复述目标号码，目标也不会被发送给模型。
现有 `/ai on`、`/ai off` 仍是当前群的快捷开关。

## 用户身份与群名片

NapCat 的 OneBot v11 消息事件通常直接提供发送者 QQ 昵称和当前群名片，无需安装插件。
当触发机器人的消息缺少这些字段时，bot 只查询当前发送者：

- 群聊调用 `get_group_member_info(group_id, user_id, no_cache=false)`。
- 私聊调用 `get_stranger_info(user_id, no_cache=false)`。
- 查询失败会退回当前事件和已保存的当前场景资料，不影响聊天；不会拉取完整群成员列表。

资料只保留最新值，不保存修改历史：

| 表 | 主键 | 内容 |
|---|---|---|
| `user_profiles` | `user_id` | 最新 QQ 昵称、首次和最后识别时间 |
| `user_group_profiles` | `(user_id, group_id)` | 用户在该群的最新群名片、首次和最后识别时间 |

同一个用户在不同群的群名片完全分开。模型调用时只临时注入当前场景显示名称：
群聊按“本群群名片 → 当前事件/API 的 QQ 昵称 → 当前用户”选择，私聊按
“QQ 昵称 → 当前用户”选择。该临时系统消息不会写入聊天历史，不包含 QQ 号；
名称按不可信元数据处理，清除控制字符、压平换行并限制长度。群聊不会拿私聊中
保存的昵称作回退，也不会读取其他用户或其他群的名片。

### 群共享记忆

启用 `GROUP_MEMORY_ENABLED` 后，每次明确触发机器人的普通群聊回复完成后，会额外进行
一次模型调用，从当前用户消息中保守提取对未来群聊有持续价值的公开事实。只保存
整理后的短事实，不保存原始聊天、模型回复或未触发的群消息。

- 每条消息最多更新 3 个事实，每群默认最多 30 条；达到上限时自动淘汰最久未更新项。
- 相同事实使用稳定 key 原地更新，明确纠正或撤销时可以更新或删除旧事实。
- 允许记录本群成员称呼、稳定偏好、长期约定和持续事项。
- 禁止记录 QQ 号、联系方式、密码、住址、财务、医疗等敏感信息。
- 记忆查询和注入始终绑定当前 `group_id`，不会进入私聊或其他群。
- 提取失败、返回格式错误或数据库暂时不可用时，只跳过本次记忆更新，不影响已发送回复。

消息中的其他 `@成员` 会转换为 `[提及成员1]` 等占位符；bot 仅查询这些被明确提及的
成员，并向模型提供不含 QQ 号的稳定群内引用和当前群名片。这样模型可以区分
“当前发言者”和“被提及成员”，同时不会把完整 QQ 号发送给模型。

## 健康检查

`GET /healthz` 返回：

```json
{
  "status": "ok",
  "version": "0.1.0",
  "database": "ok",
  "llm_configured": true,
  "onebot_connected": false,
  "uptime_seconds": 12
}
```

它不会请求 LLM，也不返回敏感配置。Compose 中 8080 不映射到宿主机；可在容器内检查：

```bash
docker compose exec bot python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/healthz').read().decode())"
```

## 数据备份与恢复

为了获得 SQLite 与 QQ 登录数据的一致快照，先停止写入：

```bash
docker compose stop bot napcat
```

然后备份 `data/`、`napcat-data/` 和 `napcat-config/` 到受访问控制的离线位置，再启动：

```bash
docker compose up -d
```

恢复时停止服务，将备份放回原路径，确认目录权限后再启动。备份包含聊天历史、QQ 登录状态和 Token，应按敏感数据管理。

## 升级

1. 完成上述备份。
2. 获取新代码，并阅读迁移说明。
3. 执行 `docker compose build --pull bot`。
4. 如需升级 NapCat，先在测试账号验证后修改 `NAPCAT_IMAGE` 为经过验证的标签；`latest` 便于首次部署但不保证可复现。
5. 执行 `docker compose up -d`。bot 启动脚本会自动运行 Alembic 迁移。
6. 检查 `docker compose ps`、`/healthz` 和两项服务日志。

不要删除 `data/` 或 NapCat 持久化目录来“升级”。

## 常见故障

### NapCat 未连接

- 确认已手动扫码且 QQ 在线。
- 查看 `docker compose logs napcat bot`。
- 在 WebUI 检查反向 WS 是否启用，URL 是否为 `ws://bot:8080/onebot/v11/ws`。
- 从 NapCat 容器内使用的是 Docker 服务名 `bot`，不是 `localhost`。

### Access Token 不一致

修改 `.env` 的 `ONEBOT_ACCESS_TOKEN` 后重建/重启 bot；启动脚本会重写通用 OneBot 配置。再在 WebUI 确认当前账号生效的 WebSocket 客户端 Token 一致。不要把 Token 粘贴到日志或问题报告。

### Docker 网络地址错误

执行 `docker compose config`，确认两个服务都在 `qq-ai-bot-network`。反向 WS 地址不要写宿主机 IP，也不要公开 8080。

### LLM 超时或 5xx

- `/ai ping` 与 `/ai status` 应仍可响应。
- 检查 Base URL 是否包含供应商要求的 `/v1`、模型名和账户配额。
- 适度增加 `LLM_TIMEOUT_SECONDS`；不要把 `LLM_MAX_RETRIES` 设得过大。
- 用户只会看到脱敏的暂时不可用提示，详细分类在服务日志中。

### 数据库权限问题

确认 `./data` 可由容器写入。启动脚本会将容器内目录交给 UID 10001 的 bot 用户。Linux 上如果宿主机安全策略阻止 chown，请预先创建目录并设置合适权限。

### QQ 需要重新扫码

打开本机 WebUI 重新手动扫码。不要删除 `napcat-data/`，除非明确希望清除登录状态；不要尝试自动处理验证码。

## 测试与质量检查

所有测试只使用构造的 OneBot 事件、临时 SQLite 和 Fake/Mock LLM，不连接真实 QQ 或真实 API。

```bash
uv sync --all-extras
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
docker compose config
```

测试覆盖私聊/群聊权限、@触发、全部命令、会话与身份隔离、不同群名片、
群共享记忆更新/容量/跨群隔离、未触发消息零采集、NapCat 缺失字段补全、
资料删除、模型身份脱敏、Alembic 升级、
去重、用户/群限流、长回复分段、取消、超时、5xx 有限重试、空回复、附件降级、
数据库重启恢复、发送失败不重试，以及至少十个并发独立会话。

## 隐私与日志

- 默认不记录用户正文或模型完整回复；`LOG_MESSAGE_CONTENT=false`。
- 结构化日志记录事件键、会话键哈希、消息类型、处理器、耗时、结果与异常类别。
- API Key 和 QQ 登录凭据从不进入业务日志。
- 完整 QQ 号只在发送者主动执行 `/ai whoami` 时显示给当前会话，不传给模型，
  也不写入普通业务日志。
- 用户资料没有列表、搜索或代查接口；所有资料读取必须绑定当前发送者，
  群名片还必须同时绑定当前群。
- 用户消息中的控制字符会被清理；链接和代码只作为文本发送给模型，项目不会执行它们。
- 助手回复只有在所有 QQ 分段发送成功后才写入 assistant 历史；发送失败不自动无限重试。

## 已知限制

- 仅单账号、单进程、单副本。
- 不支持图片理解、OCR、语音、RAG、工具调用、MCP、浏览器或代码执行。
- 不主动群发，不批量私聊，不自动加好友、拉群、处理好友请求或管理群。
- 不做流式逐 Token 发送；长文本生成完成后按段落、句子、字符分段。
- 内存限流和锁在进程重启后重置；持久化去重和会话不会重置。
- NapCat 与 QQ 客户端兼容性不由本项目控制。

## 第二阶段路线

1. 新增腾讯官方 QQ Bot 适配器，复用领域和聊天服务。
2. 用 Redis 实现跨实例锁、限流、取消和去重，再支持多副本。
3. 增加管理员审计与共享会话切换，但仍保持最小数据收集。
4. 在单独安全评审后评估多模态或 RAG；默认继续禁用工具执行能力。
