# Yuki-QQbot

## 启动项目

> **升级提示：**从 1.1 升级到 1.2 的 Alembic `0007` 只新增关系系统表，并为现有人物补建初始好感度和信任度，不会删除人物、聊天、记忆或联网来源。若从 1.0 之前的版本直接升级，仍会经过不可逆的 `0005` 数据重建；始终先备份 `data/`。

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

Yuki-QQbot 1.2 是基于 Python 3.12、NoneBot2、OneBot v11、NapCatQQ、SQLite 和 OpenAI-compatible Chat Completions API 的人物中心 QQ Agent。

- QQ 号字符串是人物的全局唯一身份。
- 当前消息发送者的 QQ 是否属于 `SUPERUSERS`，是唯一管理员凭证。
- 同一 QQ 的私聊、不同群成员关系和人物记忆关联到同一个人。
- 群号区分群；已启用群的全部消息都会被观察并永久写入事件账本。
- 私聊默认向所有 QQ 开放；`/ai private <QQ> off` 用于阻止指定用户。
- 个人记忆可以在私聊与群聊间自然复用，群记忆和群成员记忆仍按群隔离。
- 机器人支持 DeepSeek 普通/思考模式的多轮工具调用。
- 可选接入 Tavily 受控联网搜索，由后端严格控制来源保存、隔离和显示。
- 每个 QQ 拥有独立、持久化的好感度和信任度，关系阶段会自然影响 Yuki 的语气。
- 关系分数不会改变程序权限；只有当前真实发送者属于 `SUPERUSERS` 才能获得管理员工具。

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

## 1.x 数据模型

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
| `person_relationships` | 每个 QQ 当前好感度、信任度和自动变化时间 |
| `relationship_events` | 自动及管理员手动关系变化审计，不重复保存聊天正文 |
| `relationship_jobs` | 可在重启后继续处理的关系评价任务 |
| `context_resets` | `/ai new` 的上下文切点 |
| `agent_actions` | 通用 OneBot 工具的最小审计记录 |
| `web_search_runs` | 按会话隔离的联网工具运行记录，不保存网页正文 |
| `web_search_sources` | 真实来源的标题、URL、域名、摘要和发布时间 |

消息到达后的顺序是：

```text
准入判断
  → 去重
  → 更新人物/群/成员
  → 写入永久事件账本
  → 记忆任务入队
  → 确定性命令、显式回复或自主参与判断
  → 普通聊天成功发送后，关系评价任务入队
```

`/ai new` 只写上下文切点，不删除永久账本或人物记忆。

`/ai forgetme` 不会把命令和确认回复重新写回账本，并删除：

- 人物、别名、偏好、个人记忆、成员群记忆和成员关系；
- 好感度、信任度、关系变化审计和待处理关系任务；
- 该 QQ 发送的群事件；
- 该 QQ 私聊中的双方事件；
- 以该 QQ 为主体的群记忆、检索索引和后台任务；
- 该 QQ 私聊及各群成员会话中的联网来源记录；
- 其余事件正文中出现的精确 QQ 文本会替换为删除标记。

## 聊天上下文与记忆

每次普通回答会装配：

- 当前用户 QQ、昵称、别名、个人记忆、偏好和关系状态；
- 当前群号、群记忆以及当前用户的成员群记忆；
- 被提及者和最近发言者中最多 5 人的相关记忆与关系状态；
- 当前私聊或当前群最近 30 条本地事件；
- 只有模型主动调用搜索工具时，才加入更早历史。

新事件立即进入账本。后台记忆任务每 30 秒或累计 10 条时唤醒，每批最多 20 条，失败最多重试 3 次。明确添加的记忆标记为 `explicit`，自动提炼不能覆盖它。

## 好感度与信任度

每个 QQ 的初始好感度和信任度均为 `50`，总分始终限制在 `0–100`。自动评价通常不改变分数，常见有效变化为 `±1`，只有明显事件允许 `±2`。置信度低于 `0.75`、普通搜索、命令、重复夸奖、反复示爱、单纯增加消息数量、未触发群观察消息，以及要求 Yuki 直接修改分数的文本都不会加分。

本项目**不设置每日累计增加或降低上限**：同一天可以由多次独立、有效事件累计变化超过旧方案中的 `+3/-5`。单次自动变化上限和 `0–100` 总分边界仍然生效，同一聊天事件只能评价一次。

关系评价任务只在普通聊天回复成功发送后创建。Worker 默认每 60 秒或累计 5 条唤醒，每批最多 10 个会话，失败最多重试 3 次；每个任务只向评价器提供当前人物最近最多 5 条相关事件，不传完整系统提示词、不开放工具并关闭思考模式。评价失败不会影响已经发送的聊天回复。

关系阶段固定为：

| 好感度 | 阶段 | 主要风格 |
|---:|---|---|
| 0–19 | `GUARDED` | 冷淡、谨慎、保持距离 |
| 20–39 | `DISTANT` | 基本礼貌，很少主动关心 |
| 40–59 | `FRIENDLY` | 正常友好，新人物默认阶段 |
| 60–79 | `CLOSE` | 更温暖，可轻微撒娇、调侃和关心 |
| 80–99 | `AFFECTIONATE` | 私聊和群聊均可明显暧昧和使用亲密称呼 |
| 100 | `BONDED` | 私聊可在用户主动发起后使用高度亲密风格；工作请求仍正常处理 |

信任度独立保存，但有效信任度为：

```text
effective_trust = min(trust_score, affection_score + 10)
relationship_weight = round(0.6 × affection_score + 0.4 × effective_trust)
```

关系权重只用于没有证据、双方说法均无明显逻辑漏洞的冲突。模型必须先检查逻辑、聊天原文、人物记忆、联网结果及其他证据；有证据时始终以证据为准。只有权重差至少 `15` 时才倾向较高者，否则保持不确定。数学、代码、医疗、法律、财务、安全事实及可用工具核实的信息不使用关系权重，群聊中也不得公开其他人的具体分数。

好感度和信任度只改变模型获得的可信关系风格，不参与 `SUPERUSERS` 判断、联网权限、历史与记忆工具注册或 OneBot 管理工具授权。即使好感度达到 `100`，非超级管理员也不会获得 `call_onebot_api`。

## Agent 工具

所有普通聊天轮都可使用：

- `get_recent_chat_history`：每次直接调用 NapCat 的 `get_friend_msg_history` 或 `get_group_msg_history`，读取当前场景最近 20 条；未见消息会去重补入账本。
- `search_chat_history`：用 SQLite FTS5 搜索永久账本，可按 QQ、群号和时间范围约束；短于三个字符时使用有范围限制的 `LIKE`。
- `get_person_memories`：按 QQ 读取人物记忆。
- `get_group_memories`：按群号读取群记忆。

启用联网后，普通聊天轮还可使用：

- `web_search`：搜索当前公开信息，并在一次调用内批量提取最多 3 个网页的查询相关正文。
- `read_webpage`：通过 Tavily Extract 读取用户明确发送或本轮搜索真实返回的网页。

只有当前真实 OneBot 事件的 `sender.user_id` 属于 `SUPERUSERS` 时，该触发轮还会获得：

- `call_onebot_api(action, params)`：通过现有反向 WebSocket 调用任意 NapCat/OneBot action，不设 action denylist，也不二次确认。

引用管理员消息、历史里出现管理员 QQ、模型转述和自主群聊批次都不能获得管理员工具。每轮最多执行 5 次工具、6 次模型请求，其中联网工具最多 3 次。只要本轮执行过联网工具，后续 OneBot 管理工具就会被撤销，网页内容不能触发管理操作。通用 OneBot 调用只记录 actor QQ、action、成功状态、耗时和错误类别，不记录完整结果。

实现依据：

- [DeepSeek Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)
- [DeepSeek 思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)
- [NapCat API 列表](https://napneko.github.io/onebot/api)
- [Tavily Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search)
- [Tavily Extract API](https://docs.tavily.com/documentation/api-reference/endpoint/extract)

## 受控联网搜索

联网默认关闭。申请 Tavily API Key 后，在 `.env` 中设置：

```dotenv
WEB_ENABLED=true
TAVILY_API_KEY=你的Tavily密钥
WEB_SEARCH_DEPTH=advanced
```

然后只重建 Bot 容器：

```bash
docker compose up -d --no-deps --force-recreate bot
```

配置规则：

- `WEB_ENABLED=false` 时不向模型注册联网工具。
- `WEB_ENABLED=true` 但 `TAVILY_API_KEY` 为空时会明确拒绝启动。
- 搜索词最多 400 字符，不会自动拼入完整聊天历史、人物记忆或系统提示词。
- 只接受公开 HTTP(S) URL；拒绝凭据 URL、localhost、私有 IP、链路本地地址和内部 Docker 主机名。
- 搜索结果正文只存在于当轮 LLM 工具上下文，不写入聊天账本或人物/群记忆。
- 数据库只保存实际使用来源的标题、URL、简短摘要和时间，每个会话最多保留最近 10 次，默认清理 7 天前记录。

来源由后端控制：

- 普通联网问题只发送总结，不显示 URL、引用编号或来源列表。
- 明确要求“来源、出处、原文链接、参考资料、引用、网址”等内容时，正文后会再发送一条由后端生成的真实来源消息。
- 下一条只问“来源呢”“链接”“把网址发我”等短追问时，不调用 LLM、不重新搜索，直接读取当前隔离会话最近一次来源。
- 私聊用户之间、不同群之间、同一群的不同成员之间都不能互相读取来源记录。
- 模型自行生成的来源段落或虚构链接不会进入后端来源列表。

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
| `/ai affection show` | 查看本人的好感度、信任度、有效信任度和阶段 |
| `/ai affection history` | 查看本人最近 10 次关系变化 |
| `/ai affection show user <QQ号>` | 超级管理员查看指定人物 |
| `/ai affection history user <QQ号>` | 超级管理员查看指定人物最近 10 次变化 |
| `/ai affection set user <QQ号> <0-100>` | 超级管理员设置好感度 |
| `/ai affection adjust user <QQ号> <-20到20>` | 超级管理员调整好感度 |
| `/ai affection trust user <QQ号> <0-100>` | 超级管理员设置信任度 |
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
| `RELATIONSHIP_ENABLED` | `true` |
| `RELATIONSHIP_INITIAL_AFFECTION` | `50` |
| `RELATIONSHIP_INITIAL_TRUST` | `50` |
| `RELATIONSHIP_BATCH_SECONDS` | `60` |
| `RELATIONSHIP_BATCH_TRIGGER_COUNT` | `5` |
| `RELATIONSHIP_BATCH_MAX_TURNS` | `10` |
| `RELATIONSHIP_MAX_ATTEMPTS` | `3` |
| `RELATIONSHIP_CONFIDENCE_THRESHOLD` | `0.75` |
| `AFFECTION_MAX_AUTO_DELTA` | `2` |
| `TRUST_MAX_AUTO_DELTA` | `2` |
| `TRUST_AFFECTION_CAP_OFFSET` | `10` |
| `CONFLICT_PREFERENCE_MIN_GAP` | `15` |
| `WEB_ENABLED` | `false` |
| `WEB_SEARCH_DEPTH` | `advanced` |
| `WEB_SEARCH_MAX_RESULTS` | `5` |
| `WEB_EXTRACT_MAX_RESULTS` | `3` |
| `WEB_TIMEOUT_SECONDS` | `20` |
| `WEB_MAX_RETRIES` | `1` |
| `WEB_GLOBAL_CONCURRENCY` | `4` |
| `WEB_MAX_CALLS_PER_TURN` | `3` |
| `WEB_TOOL_RESULT_MAX_CHARACTERS` | `16000` |
| `WEB_SOURCE_RETENTION_DAYS` | `7` |
| `WEB_SOURCE_MAX_RUNS_PER_CONVERSATION` | `10` |

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

健康检查不会请求模型或 Tavily，也不会暴露密钥；返回值中的 `web_configured` 表示联网是否已启用并配置密钥：

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

## 1.2 升级步骤

1. 停止写入：`docker compose stop bot napcat`。
2. 完整备份 `data/`、`napcat-data/` 和 `napcat-config/`。
3. 将 `.env.example` 中的 `RELATIONSHIP_*`、`AFFECTION_*`、`TRUST_*` 和 `CONFLICT_*` 配置同步到 `.env`；默认值即可直接使用。
4. 执行 `docker compose up -d --build`；Bot 启动脚本会自动运行 `alembic upgrade head` 到 `0007`。
5. 检查 `docker compose ps`、`/healthz` 和日志。
6. 依次人工验证：`affection show/history`、管理员调整、普通私聊与群聊关系风格、`forgetme` 级联删除，以及原有记忆、联网、历史和 OneBot 工具。

`0007` 可以回退且只删除三张关系系统表，不影响人物、聊天、记忆和联网来源。更早的 `0005` 仍是不可逆的破坏性迁移；需要回退到 1.0 之前时只能停止服务并恢复升级前备份。
