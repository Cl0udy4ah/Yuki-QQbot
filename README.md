# Yuki-QQbot

## 启动项目

> **升级提示：**1.8.2 会执行非破坏性 Alembic `0017`，增加人物语音偏好和 Planner 语音决策观测字段，并清理旧版误写入普通正文的内部 TTS 描述；结构化语音元数据、`0015`/`0016` 的本地声线、生成记录和其他现有数据均会保留。若从 1.0 之前直接升级，仍会经过不可逆的 `0005` 数据重建。始终先备份 `data/`。

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

语音功能默认关闭，不影响原有纯文字启动。准备好本地 GenieData 和声线后，使用：

```bash
docker compose --profile speech up -d --build
```

停止服务：

```bash
docker compose down
```

不要添加 `-v`，否则可能删除持久化数据。NapCat WebUI 地址为 <http://127.0.0.1:6099/webui/>。

## 项目定位

Yuki-QQbot 1.8.2 是基于 Python 3.12、NoneBot2、OneBot v11、NapCatQQ、SQLite 和 OpenAI-compatible Chat Completions API 的人物中心 QQ Agent。

- QQ 号字符串是人物的全局唯一身份。
- 当前消息发送者的 QQ 是否属于 `SUPERUSERS`，是唯一管理员凭证。
- 同一 QQ 的私聊、不同群成员关系和人物记忆关联到同一个人。
- 群号区分群；已启用群的全部消息都会被观察并永久写入事件账本。
- 私聊默认向所有 QQ 开放；`/ai private <QQ> off` 用于阻止指定用户。
- 个人记忆可以在私聊与群聊间自然复用，群记忆和群成员记忆仍按群隔离。
- 机器人支持 DeepSeek 普通/思考模式的多轮工具调用。
- 可选使用 Qwen3.7-Plus 作为独立视觉前端，动态思考并识别图片、虚构角色、图片表情、动态表情和回复图片；DeepSeek 仍是唯一主聊天模型并负责最终回复。
- 可选接入 Tavily 受控联网搜索，由后端严格控制来源保存、隔离和显示。
- 每个 QQ 拥有独立、持久化的好感度和信任度，关系阶段会自然影响 Yuki 的语气。
- 关系分数不会改变程序权限；只有当前真实发送者属于 `SUPERUSERS` 才能获得管理员工具。
- 超级管理员可以用自然语言管理注册配置、关系、记忆、偏好、群和私聊准入。
- 运行时配置保存在 SQLite，不修改 `.env`；所有修改都有脱敏审计，配置覆盖可安全回滚。
- 每轮聊天获得后端可信当前时间；每个 QQ 可保存独立 IANA 时区，历史消息按本地时间显示。
- 普通用户和超级管理员都可以用自然语言创建自己的持久化自动化任务；普通用户严格限于本人和当前群，超级管理员可显式委托现有管理员与 OneBot 能力。
- 默认启用 Planner-first 会话：后端先做确定性回复必要性评分，再生成受约束 `TurnPlan`，规划回复/等待/沉默、工具上限、消息条数和发送节奏。
- 新消息可以中断过期的自主 Planner、自主生成和尚未发送的旧分句；已经开始的修改型业务操作不会被自动取消。
- 提供 Plugin API v1、独立 `yuki_plugin_sdk`、Manifest/批准/权限/事件/Prompt/PlannerSignal 扩展点和无网络测试 SDK；插件系统默认关闭。
- 插件可以创建与主聊天账本、人物记忆分离的持久或临时 AI 会话，适合骰子跑团等连续任务；插件拿不到模型隐藏推理，也不能伪造超级管理员。
- 内置持久化表情系统会按配置观察图片、保存原图与静态预览、复用 Qwen 视觉分类、自动采用合格表情，并由 Planner 或 Agent 在正常回复序列中选择发送。
- 可选启用完全本地的 Genie-TTS 2.0.2 Worker，使用部署者自行准备的 GPT-SoVITS V2/V2ProPlus ONNX 声线和多参考风格发送 QQ `record`，不调用云端 TTS。

### 当前架构约束

- 正常聊天、管理员自然语言操作、联网和自动化创建继续使用同一个聊天 Agent；Planner 只规划，不能执行工具或产生权限。插件独立 AI 会话只服务插件任务，不是第二套管理员人格或主聊天路由。
- `ContextAssembler` 统一装配人物、群、关系和近期事件，并用 `MAX_CONTEXT_CHARACTERS` 限制动态上下文总量；当前消息优先保留，低优先级旧资料先裁剪。
- `PromptComposer` 集中生成后端可信的时间、权限、关系、视觉和联网规则，业务服务不再各自拼接一套运行说明。
- 持久化仓储按人物与访问、事件账本、记忆、关系、媒体和联网来源分域实现；`persistence.repositories` 仅作为稳定兼容门面，不再承载全部 SQL 逻辑。
- `/ai` 确定性命令由 `CommandService` 调度并绕过 Planner；普通聊天由 `ReplyNecessityScorer → PlannerService → AgentRunner → ReplySequenceManager` 协作，`MessageProcessor` 继续负责准入、观察、账本、视觉和最终异常边界。
- 运行时配置注册表只负责查找、别名和类型转换；热更新、仅影响未来、需重启、受保护/密钥配置分别维护在独立声明目录中。
- 相关人物按批次读取，避免群聊中按 QQ 串行查询多组资料；群名片仍严格按当前群号隔离。
- SQLite 使用 WAL 和有限等待支持多个后台 Worker；部署仍定位于单 Bot、小型服务器，未来需要多进程横向扩展时再迁移 PostgreSQL。
- GitHub Actions 会在推送和 PR 时执行 Ruff、严格 mypy、pytest、Echo 示例插件契约测试、Alembic 全新安装和 Docker 构建。

本版本不识别用户发来的语音，也不处理视频、PDF 和普通文件，不实现 ASR、实时语音通话、VAD 或 WebRTC。已启用群里未触发 Yuki 的图片可以按 `EMOJI_COLLECTION_MODE` 进入独立后台表情候选流程；这不会触发聊天回复、人物记忆、关系评价或管理员操作。普通聊天视觉理解仍只处理当前真实消息或回复中的图片。

## 持久化表情系统

表情系统默认启用，视觉分类复用现有 `VISION_*` Provider。不存在第二套视觉客户端，也没有表情审核队列或审核模型调用：分类为表情后进入 `recognized`，满足 `EMOJI_AUTO_ADOPT_MIN_CONFIDENCE` 时直接进入 `adopted`。

- 状态：`candidate → recognized → adopted`；普通照片进入 `rejected`，管理员可 `ban`，文件丢失时标记 `missing`。
- 自动收集：`metadata_only` 只看 OneBot 明确表情字段；`likely` 还接受表情相关元数据；`all_images` 接受作用域内全部图片作为候选。
- 去重与文件：SHA-256 完全去重；可选 dHash 只标识近似候选，不会误删。原图保存到 `data/emoji/original/`，第一帧 WebP 预览保存到 `data/emoji/preview/`；GIF/WebP 原动画保持不变。
- 回复：Planner 只输出语义目标、情绪、模式和位置，不能指定文件或表情 ID；核心先粗排，再可选用候选拼图做视觉精排。发送可以位于文字前、文字后或仅发表情，并服从新消息取消与发送成功后计数。
- 隔离：OCR、描述、插件和网页都不能执行命令、改变关系或写人物记忆；数据库和日志不保存图片 Base64。

常用命令（仅真实 `SUPERUSERS`）：

```text
/ai emoji list [candidate|recognized|adopted|rejected|banned|missing]
/ai emoji show|adopt|unadopt|reject|ban|unban|reanalyze <ID>
/ai emoji pin <ID> on|off
/ai emoji group enable|disable
/ai emoji import              # 与当前图片一起发，或回复一张图片
/ai emoji stats|cleanup|doctor
```

自动化注册 `emoji.send` 和 `emoji.send_by_id`。普通用户只能委托发送给本人私聊或任务创建时的当前群；固定 ID 必须在创建任务时明确提供。插件 API 新增 `EmojiFacade`、`emoji.*` 权限、通知事件和 `emoji.selection_signals.v1`；插件只能调整核心候选分数，不能构造候选外 ID。完整设计见 [表情系统文档](docs/emoji-system/architecture.md)。

## 完全本地 QQ 语音

1.8.0 的语音是独立可选服务：主 Bot 通过 Unix Domain Socket 调用无网络、无 HTTP 端口的 Genie Worker；Worker 只加载本地 GenieData、GPT-SoVITS V2/V2ProPlus ONNX 模型和参考音频，输出 32 kHz 单声道 16 位 WAV。主进程在 OneBot Adapter 边界把 WAV 编为 Base64 `record`，NapCat 不需要访问本地路径。

同一声线可声明多种目标语言。Planner 可以按当前语境在中文和日文间自然选择，Agent 会生成对应语言的正文；后端还会根据最终文本中的中文汉字或日语假名再次校验，避免语言提示与实际文本不一致。参考音频的语言独立保存，因此日语参考音频也可以用于合成中文目标文本。

语音意图由 Planner 理解自然语言和当前上下文，不再由后端匹配“语音/文字”等固定短语。用户本轮明确索要语音时，Planner 授权 Agent 使用 `send_voice` 选择语气与语言；工具不能改变 Planner 决定的纯语音、文字加语音或纯文字模式。用户没有明确索要语音时，Agent 看不到该工具，普通聊天是否偶尔发语音完全由 Planner 按人物偏好和 `SPEECH_SPONTANEOUS_FREQUENCY`（默认 0.15）决定。

语音账本只把实际朗读正文交给聊天模型；声线、参考风格、目标语言和生成 ID 仅保存在结构化 `record` 消息段，不会以 `[语音：Yuki 发送了一条语音，声线：…]` 的形式混入 Yuki 的上下文或下一次语音。包含语音的回复中，系统提示词要求自称使用 `ゆき`，避免日语 TTS 把 `Yuki` 读成英文字母；纯文字回复仍可使用 `Yuki`。

每个 QQ 可持久保存 `text_only`、`auto` 或 `prefer_voice` 模式。只有“以后都用文字”“以后可以偶尔发语音”等明确持续语义会更新偏好；“这次用语音说”只影响当前轮。`SPEECH_DEFAULT_MODE` 是尚未保存人物偏好时的全局基线：`text` 对应文字模式，`optional` 对应自动决定，`voice`/`text_and_voice` 对应偏好语音。CPU ONNX 模型可能占用数 GiB，Bot 启动时只同步声线元数据、首次合成时才按需加载模型；Worker 会主动归还空闲堆内存，并在 `SPEECH_WORKER_IDLE_RECYCLE_SECONDS`（默认 300 秒）后由 Compose 自动回收重启；设为 `0` 可关闭空闲回收。

仓库不会下载或附带任何角色模型、Galgame/动漫声线或原始语音，生产 Worker 也不安装 PyTorch。部署者必须确认模型权重和参考音频授权。准备流程、Manifest、转换、Planner、插件、自动化与排障见 [语音文档](docs/speech/architecture.md)。

常用命令：

```text
/ai voice status
/ai voice profiles
/ai voice show <profile_id>
/ai voice styles [profile_id]
/ai voice test <文本>
/ai voice use|reload <profile_id>        # 超级管理员
/ai voice cache cleanup                 # 超级管理员
```

CLI 覆盖 `speech status`、`genie doctor`、profile 导入/检查/启停/设默认、reference 添加/停用、测试、缓存清理和 Worker 重启。模型转换工具位于 `tools/genie_model_converter/`，与生产运行环境完全分离。

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

### 可选：启用图片理解

图片理解默认关闭。使用阿里云百炼 OpenAI-compatible Chat Completions 接口时，在 `.env` 中填写：

```dotenv
VISION_ENABLED=true
VISION_PROVIDER=qwen
VISION_BASE_URL=你的百炼兼容接口基础地址
VISION_API_KEY=你的百炼API密钥
VISION_MODEL=qwen3.7-plus
VISION_THINKING_ENABLED=false
VISION_THINKING_BUDGET=6144
VISION_LOW_CONFIDENCE_RETRY_THRESHOLD=0.65
```

然后重建 Bot 容器：

```bash
docker compose up -d --build --no-deps bot
docker compose logs -f bot
```

`--no-deps` 只替换 Bot，不重建 NapCat，因此会保留当前 QQ 登录容器和登录态。后续代码、提示词或 `.env` 更新也优先使用这种方式；只有 NapCat 本身需要升级或修复时才单独重建 NapCat。

`VISION_ENABLED=true` 时，`VISION_BASE_URL`、`VISION_API_KEY` 和 `VISION_MODEL` 缺一不可。识图思考默认关闭；需要时可把 `VISION_THINKING_ENABLED` 改为 `true`，此时角色、表情包与图片问题会使用思考模式，普通描述结果低于 `VISION_LOW_CONFIDENCE_RETRY_THRESHOLD` 时会自动复核一次。Qwen 只接收本轮选中的图片 data URI 和当前用户的图片问题，不接收完整聊天历史、人物记忆、关系分数、系统提示词、管理员权限或 Agent 工具；DeepSeek 只接收 Qwen 返回的结构化文字观察，不接收图片 URL、Base64 或临时路径。

### 可选：启用持久化自动化

自动化默认关闭。需要让普通用户和超级管理员通过自然语言创建自己的任务时，在 `.env` 中设置：

```dotenv
AUTOMATION_ENABLED=true
DEFAULT_TIMEZONE=Asia/Shanghai
```

然后只重建 Bot：

```bash
docker compose up -d --build --no-deps bot
```

普通用户可以创建提醒、定时生成文本、给自己发私聊，以及在创建消息所在的当前群执行受限任务；只能查看、修改和运行本人任务。超级管理员可以额外委托已登记的管理员业务接口、运行时配置和 NapCat/OneBot 全部公开 action。引用、历史、记忆、网页、OCR 和模型自行生成的 QQ/群号不能扩大目标范围。

### Planner-first 会话

1.6.0 默认启用 Planner。Planner 使用当前主 LLM（`PLANNER_MODEL` 留空）或单独模型，关闭思考、不提供工具，只输出严格结构化计划。DeepSeek 部署可设置 `PLANNER_MODEL=deepseek-v4-flash`，在不改变主聊天模型的情况下缩短计划延迟。Planner 只能缩小本轮工具和回复计划，不能修改配置、记忆、关系、权限或直接发送消息。

```dotenv
PLANNER_ENABLED=true
PLANNER_DIRECT_ENABLED=true
PLANNER_GROUP_ENABLED=true
PLANNER_GROUP_DEBOUNCE_SECONDS=3
PLANNER_PREFERRED_MESSAGES=3
PLANNER_REPLY_NECESSITY_THRESHOLD=0
PLANNER_CONFIDENCE_THRESHOLD=0.2
PLANNER_MAX_PENDING_MESSAGES=8
SPEECH_SPONTANEOUS_FREQUENCY=0.15
REPLY_SEQUENCE_CANCEL_ON_NEW_MESSAGE=true
REPLY_PLAN_HARD_MAX_MESSAGES=10
```

要先用 1.5.2 兼容聊天路径验证升级，可临时设置：

```dotenv
PLANNER_ENABLED=false
PLUGIN_SYSTEM_ENABLED=false
```

Planner 开启时，已启用群由 `planner.group_enabled` 控制是否进入自主规划，并使用
`planner.group_debounce_seconds` 聚合连续消息。旧 `AUTONOMOUS_*` 开关、静默时间、
置信度、冷却和小时上限完全不参与 Planner 会话；只有显式关闭 Planner 时，它们才作为
1.5.2 兼容路径重新生效。

当前默认采用高参与度群聊策略：普通群消息静默约 3 秒后进入 Planner，决策上下文限制为
最近 8 条，必要性门槛为 0，由 Planner 判断是否能自然接话。已启用群中的真实 `@Yuki`、
回复 Yuki 和私聊属于后端强制回复，Planner 不能把它们改成 `silent/wait`；后续普通群消息
也不会抢占正在处理的明确触发。已通过发言门槛的批次如遇 Planner 格式异常，会降级为正常回复而非
沉默。禁用群仍只接受超级管理员的启用命令。

`planner.preferred_messages` 是 `natural_multi` 日常回复的软目标，默认 3 条；内容不足时
不会凑数。非结构化聊天正文中的空行会直接成为两条 QQ 消息的发送边界；代码、表格、步骤
和长篇结构化回答不会逐句拆散。超级管理员可直接对 Yuki 说
“把 Planner 日常回复偏好改成 5 条”或“把单轮发送硬上限改成 15 条”，修改会热生效。

Planner 还可在多人聊天中为指向关系非常明确的回答选择引用消息发送；默认仍是普通发送。
引用目标必须来自当前受限 Planner 上下文中的真实消息 ID，多条回复只在第一条携带引用，避免
连续引用气泡刷屏。

Planner 同时是聊天语音的唯一决策边界：它从语义识别本轮明确索要/拒绝语音、持续人物偏好和
中性的日常表达。Agent 的 `send_voice` 只在明确索要语音的轮次临时出现，并且只能补充风格与
语言；日常主动语音按 `speech.spontaneous_frequency` 和最近 Planner 记录形成频率预算。超级
管理员可直接说“把日常主动语音频率改成 0.25”，以 global/group/user 作用域热更新。

### 可选：启用本地插件

插件系统默认关闭。先阅读 [插件开发手册](docs/plugin-development/index.md) 和 [真实安全边界](docs/plugin-development/security.md)：1.6.0 插件运行在 Yuki 进程内，权限系统是官方 API 的访问治理，不是恶意 Python 沙盒，只能安装管理员完全信任并审阅过源码的插件。

```dotenv
PLUGIN_SYSTEM_ENABLED=true
PLUGIN_DIRECTORY=plugins
PLUGIN_API_VERSION=1.0
```

仓库提供无网络 [`com.example.echo`](examples/plugins/com.example.echo/README.md) 示例：

```bash
mkdir -p plugins
cp -R examples/plugins/com.example.echo plugins/com.example.echo
uv run qq-ai-bot-cli plugin validate plugins/com.example.echo
uv run qq-ai-bot-cli plugin test plugins/com.example.echo
```

通过插件 CLI 发现、审阅权限、批准并启用后重启 Bot。Manifest 任何变化都会使批准失效，必须重新审阅。Docker Compose 将 `./plugins` 只读挂载到 `/app/plugins`，插件热更新和在线下载不属于 1.6.0。

插件需要连续独立上下文时可使用 `ctx.agent_sessions`。例如跑团插件可以创建 `durable + current_group` 会话；历史只写 `plugin_agent_messages`，不写主 `chat_events`，默认不注入主聊天或人物记忆，也不返回隐藏推理。详见 [独立 AI 会话](docs/plugin-development/service-facades.md#独立-ai-会话跑团示例)。

## 1.x 数据模型

`0005` 会创建以下主要数据：

| 表 | 作用 |
|---|---|
| `people` | 以 QQ `user_id` 为主键的人物 |
| `person_aliases` | QQ 昵称和各群历史称呼 |
| `groups` | 群名、启用状态和自主参与设置 |
| `memberships` | `(user_id, group_id)` 当前群名片与活跃时间 |
| `chat_events` | 永久保存收发消息、消息段、回复关系和时间；`0010` 增加图片摘要，`0012` 增加自动化来源、任务和运行 ID |
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
| `runtime_config_overrides` | 按 global/group/user 保存显式注册的运行时配置覆盖与版本 |
| `admin_operation_events` | 管理员操作、修改前后值、成功状态与错误类别的脱敏审计 |
| `media_analyses` | `0009` 新增的图片结构化观察缓存，不保存原图、Base64 或隐藏推理 |
| `emoji_descriptions` | `0011` 新增的持久化 QQ 表情值与结构化描述库，不随短期图片缓存过期 |
| `person_time_settings` | `0012` 新增的每个 QQ 的 IANA 时区设置 |
| `automations` | 持久任务、调度、最小委托权限、租约和下一次执行时间 |
| `automation_versions` | 每次脚本修改的不可变版本与稳定哈希 |
| `automation_runs` | 幂等执行记录、资源计数、状态和脱敏结果摘要 |
| `automation_step_runs` | 每个步骤的 capability、时间、状态和脱敏摘要 |
| `planner_runs` | `0013` 新增的 Planner 必要性、计划、降级、中断、耗时和发送计数；只保存脱敏哈希与摘要 |
| `plugin_installations` | 插件 Manifest 哈希、请求/批准权限、状态和失败计数 |
| `plugin_config_values` | 按插件及 global/group/user 作用域保存已校验配置 |
| `plugin_state` | 按插件强制隔离的私有 KV，不用于保存 Secret |
| `plugin_audit_events` | 插件操作的脱敏审计元数据 |
| `plugin_agent_sessions` | 插件独立 AI 会话的模型、指令、上下文策略、批准能力和生命周期 |
| `plugin_agent_messages` | 独立插件 AI 会话的可见正文；不保存隐藏推理，也不混入主聊天账本 |
| `speech_voice_profiles` | `0015` 新增的本地声线档案、校验和、启用和默认状态 |
| `speech_voice_references` | 每个档案的多风格参考元数据与相对路径，不保存音频正文 |
| `speech_generations` | 语音队列、缓存、取消、发送和失败类别；正文只保存哈希 |
| `person_speech_preferences` | `0017` 新增的每个 QQ 的持久语音模式与最后一次明确修改来源 |

消息到达后的顺序是：

```text
准入判断
  → 去重
  → 更新人物/群/成员
  → 写入永久事件账本
  → 记忆任务入队
  → 已触发且含图片时，按需解析、预处理并调用独立视觉前端
  → /ai 与确定性插件命令直接处理
  → 其他轮次由 ReplyNecessityScorer 判断是否值得进入 Planner
  → Planner 生成并由后端裁剪 TurnPlan（reply / wait / silent）
  → reply 才装配上下文并进入同一个正常聊天 Agent
  → 纯文本轮次可按当前真实 QQ 创建或管理本人自动化任务
  → 当前真实发送者是超级管理员时，为该 Agent 动态增加管理员工具
  → ReplySequenceManager 按计划发送，并在新消息到达时停止过期的剩余分句
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
- 该 QQ 的用户级运行时配置覆盖；保留的管理员审计和其他作用域配置会把精确 QQ 替换为删除标记；
- 与被删除事件关联的视觉分析缓存；
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

默认仍然**不设置每日累计增加或降低上限**：`RELATIONSHIP_DAILY_POSITIVE_CAP=0`、`RELATIONSHIP_DAILY_NEGATIVE_CAP=0` 中的 `0` 表示不限额，因此保持 1.2 行为。管理员可以把对应运行时配置改为 `1–100`，让之后的自动评价按 UTC 自然日分别裁剪正向和负向累计变化；单次自动变化上限、`0–100` 总分边界和事件幂等始终生效。

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

## 图片、表情与回复图片理解

1.4.2 采用前后分离的双模型流程：

```text
真实 OneBot image 段
  → MediaResolver（可信来源校验、下载或 get_image）
  → ImagePreprocessor（Pillow 解码、方向修正、缩放、动态抽帧）
  → Qwen3.7-Plus（只生成结构化视觉观察，识图思考默认关闭）
  → 不可信视觉 system message
  → DeepSeek（结合真实用户文本、人格与上下文生成最终 QQ 回复）
```

图片选择与触发规则：

- 当前消息图片优先；当前消息没有图片时才使用被回复消息中的图片，保持原始消息段顺序，默认每轮最多 5 张。
- 私聊中的纯图片、图片加文字和回复图片会进入视觉流程；纯图片使用内部默认观察问题，该问题不会伪装成用户原话写入账本。
- 群聊只有已经满足原有回复条件（例如 `@Yuki`、回复 Yuki 或使用 AI 前缀）时才分析图片；普通未触发群图片和自主群聊批次不下载、不分析。
- OneBot `face` 使用本地 `config/qq_face_map.json` 转为可读文本，未知 ID 保留为 `[QQ表情：ID ...]`；Unicode Emoji 保持普通文本，不调用视觉 API。
- QQ 商城表情或图片表情首次仍以真实图片观察为准，消息段的 `summary` 只作为不可信提示；之后优先复用持久化表情描述库。
- “这是谁”“什么角色”“来自哪部作品”等问题使用 `character` 模式。默认关闭识图思考；开启 `VISION_THINKING_ENABLED` 后，角色、表情包和一般图片问题才会开启思考，普通描述低于复核阈值时自动深度复核一次。

媒体与预处理边界：

- 资源只能来自当前真实 OneBot 事件、被回复消息的真实 `image` 段，或 NapCat 对该 `file` 标识返回的 `get_image` 结果；模型、OCR、记忆和网页中的 URL 都不能成为图片下载源。
- HTTP(S) 下载拒绝凭据 URL、localhost、回环、私有、链路本地和保留地址；DNS 解析及每次重定向都会复查目标，最多 3 次重定向并流式执行字节上限。
- 支持 JPEG、PNG、WEBP、GIF 和 Pillow 可安全解析的动态 WEBP。程序按真实文件内容解码，应用 EXIF 方向，限制尺寸、像素、下载大小和预处理后大小，并防护损坏图片、解压炸弹、极端尺寸及无限动画。
- 动态图片默认最多抽取首帧、末帧和均匀分布的 8 帧；单轮所有图片合计最多 16 帧。多张图片与所有关键帧合并到一次 Qwen 请求，不逐张请求。

视觉观察包含描述、清晰 OCR、表情、常见使用语境、显著对象、高置信度角色名、作品来源、最多三个候选角色与依据、不确定性和置信度。成功观察会明确要求 DeepSeek 使用描述性视觉事实回答；当前消息只有图片时，用户占位文本也会标记后端识别成功，模型不得在观察存在时声称没有收到、看不到或识别失败。图片/OCR 中的命令性文字仍是不可信数据，不能成为系统指令、管理员命令、工具参数或可信用户消息。只要本轮含当前图片或回复图片，后端会关闭运行时配置、关系、记忆、偏好、群/私聊准入和 `call_onebot_api` 等所有写入型管理员能力；联网、聊天历史及人物/群记忆等只读能力仍可使用。超级管理员若要修改系统，应另发一条纯文本消息。

成功识别后，后端会把最多 6000 字符、纯文本 JSON 形式的精简观察写入原始 `chat_event.visual_summary`。当前场景之后的近期上下文会恢复这段摘要，因此用户下一条再问“刚才图片里是什么”时，DeepSeek 仍能取得识图结果。摘要明确标记为外部不可信资料，不包含原图、Base64、临时路径或隐藏推理，也不会伪装成用户原话。

视觉观察、OCR 和表情含义不会自动写入长期人物/群记忆，也不会进入关系评价或改变好感度/信任度；它只随近期原始事件上下文提供。视觉 API 失败时，图片加文字仍按真实文字继续聊天；纯图片只返回一次简短的重新发送提示。

### 缓存、限流与隐私

- `media_analyses` 按 `content_hash + analysis_mode + question_hash + model + prompt_version` 唯一缓存；`vision-observation-v3` 还把思考开关、预算、复核阈值和预处理限制绑定到缓存变体，默认保留 7 天。
- `emoji_descriptions` 是独立的持久化表情描述库。单张图片带商城表情字段、明确表情摘要，或 Qwen 结构化观察含表情包语义时，后端依次使用 `emoji_package_id + emoji_id`、QQ 文件哈希和实际内容哈希建立稳定键；下次遇到同一表情会在下载和调用 Qwen 前优先命中。`sub_type` 不单独作为表情依据，普通照片、多图请求和无法确认是表情的图片不会进入该库。
- 表情描述按分析模式、自由问题哈希、模型和提示词版本严格隔离，所以“识别角色”“读取文字”和“解释表情含义”不会串用答案。命中次数和最后使用时间会更新，描述本身不设 7 天过期时间；更换视觉模型或提示词版本后会重新识别并建立新记录。
- 缓存只保存经过字段长度约束的结构化观察及必要元数据，不保存原图、Base64、临时文件、隐藏推理或 API Key；事件删除时关联缓存级联删除，过期记录由现有清理任务移除。
- Qwen 使用独立的并发信号量及用户/群限流，不占用 DeepSeek 的全局并发槽。相同内容、问题、模型和缓存版本的并发请求通过 single-flight 合并为一次 Provider 调用；缓存命中和合并跟随请求不重复消耗视觉 API 限额。
- 视觉流水线默认最多运行 4 个请求、等待 32 个请求，排队最长 120 秒；QQ 图片下载、排队和 Qwen HTTP 请求分别拥有独立的 120 秒超时，队列满时立即自然降级，避免请求无限堆积。
- 纯图片失败会区分下载超时、NapCat 资源查询失败、下载失败、格式损坏、体积超限、队列繁忙、视觉模型超时和视觉模型不可用，不再把所有问题都描述成“图片不清晰”。
- 日志只记录脱敏会话哈希、队列等待时间、排队/运行数量、图片/帧/字节计数、内容哈希前 12 位、模型、耗时、缓存或 single-flight 命中状态和错误类别，不记录完整图片 URL、签名参数、原始图片、Base64、完整 OCR 或私聊图片内容。

## 可信时间与持久化自动化

普通聊天每轮都会收到后端生成的可信时间对象：`utc`、`local`、`timezone`、`date` 和 `weekday`。数据库执行时间统一保存为 UTC，向用户展示和计算 `once/daily/weekly` 时使用任务保存的 IANA 时区；默认是 `Asia/Shanghai`。`time_get_current`、`time_get_timezone` 和 `time_set_timezone` 只作用于当前真实发送者。

自然语言创建流程如下：

```text
真实普通文本消息
  → 同一个 Yuki Agent 生成 AutomationScript JSON
  → automation_create
  → Schema、时间、来源目标、权限和模板污点校验
  → SQLite 持久化脚本、版本和最小委托权限
  → AutomationWorker 使用数据库租约领取
  → AutomationExecutor 顺序执行已登记 capability
  → 写运行/步骤审计；真实发送消息写回 chat_events
```

Automation DSL v1 的完整结构如下。所有对象均拒绝未声明字段：

```json
{
  "version": 1,
  "name": "任务名称，1–128 字符",
  "timezone": "IANA 时区，例如 Asia/Shanghai",
  "schedule": {
    "type": "after | once | daily | weekly | interval",
    "seconds": "after/interval 使用；interval 不少于 60",
    "local_datetime": "once 使用的本地 ISO 时间",
    "weekdays": "weekly 使用，星期一=1 到星期日=7",
    "hour": "daily/weekly 使用，0–23",
    "minute": "daily/weekly 使用，0–59",
    "timezone": "once/daily/weekly 可覆盖脚本时区"
  },
  "context": {
    "scene": "none | creator_private | current_group",
    "include_relationship": false,
    "include_memories": false,
    "history_limit": 0
  },
  "steps": [
    {
      "id": "[a-z][a-z0-9_]{0,31}",
      "call": "注册表中的固定 capability 名",
      "arguments": {},
      "save_as": "可选的结构化输出别名"
    }
  ],
  "limits": {
    "max_steps": 3,
    "max_llm_calls": 1,
    "max_tool_calls": 3,
    "max_messages": 1,
    "timeout_seconds": 60
  }
}
```

只允许 `$creator_user_id`、`$bot_user_id`、`$automation_id`、`$automation_run_id`、`$scheduled_for`、`$actual_started_at`、`$local_time`、`$current_group_id`，以及 `${step_id.field}` 形式的既有步骤输出。步骤输出可以进入最终消息文本，但不能进入 `user_id`、`group_id`、OneBot action、配置键、管理员 action 或自动化 ID。系统不执行 Python、Shell、JavaScript、`eval`、SQL、文件、Docker 或任意 HTTP 请求。

首批 capability：

| capability | 普通用户 | 超级管理员 | 说明 |
|---|:---:|:---:|---|
| `yuki.generate`、`yuki.agent` | ✓ | ✓ | 受运行次数和上下文声明约束的主模型生成/Agent |
| `onebot.send_private_message` | 仅本人 | ✓ | 主动普通私聊，发送结果写事件账本 |
| `onebot.send_group_message` | 仅创建时当前群 | ✓ | 主动普通群消息 |
| `web.search`、`web.read_page` | ✓ | ✓ | 通过现有受控 Tavily Provider，不开放任意 HTTP |
| `memory.get_person`、`memory.get_group`、`history.search` | 仅本人/当前群 | ✓ | 只读结构记忆和永久账本 |
| `onebot.call_api` | — | ✓ | 全部公开 NapCat/OneBot action，不设 denylist |
| `admin.execute_action` | — | ✓ | 复用关系、记忆、偏好、群和私聊准入业务接口 |
| `config.get`、`config.set` | — | ✓ | 仅显式注册配置；任务不能修改 `automation.*` |

每个任务只保存本脚本实际使用的 capability 及其 Schema 版本。运行时有效权限是“创建时授予的最小集合 ∩ 当前仍登记且版本一致的集合 ∩ 创建者当前权限”；超级管理员后来从 `SUPERUSERS` 移除时，其旧管理员任务会变为 `blocked`，后端新增能力不会自动授予旧任务。普通用户的任务始终保持本人/当前群的后端范围校验。

Worker 默认每 2 秒轮询，用租约防止多实例重复执行，并以 `(automation_id, scheduled_for)` 唯一约束保证幂等。一次性任务在 30 分钟宽限内补执行一次，超出后记为 `missed`；周期任务直接计算下一个未来时刻，不逐条补发。Bot 未连接时在宽限期内保留原计划槽。生成、Agent、联网、记忆和历史读取仅对明确瞬时错误最多重试一次；消息发送、通用 OneBot、配置和管理员修改不重试，发送结果无法确认时记为 `uncertain`。连续失败 3 次后任务进入 `failed`，修改或恢复后才会继续。

## Agent 工具

所有普通聊天轮都可使用：

- `get_my_capabilities`：按当前真实消息发送者 QQ 查询本人完整权限能力、可改参数数量、接口、作用域和生效方式；不接受目标 QQ 或角色参数。
- `get_recent_chat_history`：每次直接调用 NapCat 的 `get_friend_msg_history` 或 `get_group_msg_history`，读取当前场景最近 20 条；未见消息会去重补入账本。
- `search_chat_history`：用 SQLite FTS5 搜索永久账本，可按 QQ、群号和时间范围约束；短于三个字符时使用有范围限制的 `LIKE`。
- `get_person_memories`：按 QQ 读取人物记忆。
- `get_group_memories`：按群号读取群记忆。

启用联网后，普通聊天轮还可使用：

- `web_search`：搜索当前公开信息，并在一次调用内批量提取最多 3 个网页的查询相关正文。
- `read_webpage`：通过 Tavily Extract 读取用户明确发送或本轮搜索真实返回的网页。

只有当前真实 OneBot 事件的 `sender.user_id` 属于 `SUPERUSERS` 时，该触发轮还会获得：

- `call_onebot_api(action, params)`：通过现有反向 WebSocket 调用任意 NapCat/OneBot action，不设 action denylist，也不二次确认。

这里的“任意 action”是独立的通用全接口网关：开放范围以当前 NapCat/OneBot 实际提供的全部公开 action 为准，不受权限目录中 19 项应用业务接口数量限制。能力目录是给 Yuki 的内部工具数据，不会原样发给用户或写入聊天账本；Yuki 读取后只输出自然语言结论或继续执行具体操作。

引用管理员消息、历史里出现管理员 QQ、模型转述和自主群聊批次都不能获得管理员工具。每轮最多执行 5 次工具、6 次模型请求，其中联网工具最多 3 次。只要本轮执行过联网工具，后续 OneBot 管理工具就会被撤销，网页内容不能触发管理操作。通用 OneBot 调用只记录 actor QQ、action、成功状态、耗时和错误类别，不记录完整结果。

1.3 的自然语言管理员能力直接并入上述同一个正常聊天 Agent，不创建第二套路由、隐藏会话或客服人格。只有当前真实发送者 QQ 属于 `SUPERUSERS` 时，该 Agent 的当前工具列表才会额外获得：

- `admin_list_capabilities`
- `admin_get_config`
- `admin_set_config`
- `admin_delete_config_override`
- `admin_execute_action`
- `admin_get_history`
- `admin_rollback_change`

管理员操作与日常聊天共享同一份系统提示词、人物关系、记忆和最近消息，因此 Yuki 在执行任务前后保持同一个人格，也能自然理解“先问目标 QQ、下一条再补 QQ”这样的多轮请求。权限不会从上下文继承：每次真正执行工具时，后端仍重新核对当前 OneBot 事件的真实发送者 QQ；普通用户即使看到管理员历史也得不到 `admin_*` 工具。

管理员提出具体操作时，Yuki 会内部查找配置键/action、读取参数约束，然后继续调用 `admin_set_config` 或 `admin_execute_action`，不会把查询页当作最终回复。业务 action 的 `target`、`user_id`、`group_id`、`value`、`delta`、`memory_id`、`content` 和 `key` 都有显式 schema；安全的参数格式错误允许在同一轮修正后重试。同一轮可以按需多次查询能力目录，也可以先执行 `memory.list` 等只读 action 找到 ID，再继续执行对应修改；它们共同受每轮工具总次数限制。能力查询默认返回内部摘要，具体操作使用 `focused + category/query` 获取局部参数；原始工具 JSON 只存在于当前模型调用中，后端还会拦截误回显，不写入聊天账本。若只缺一个参数，Yuki 直接用正常语气追问，不建立额外待办。

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

在已启用群中，可以只发送一个 `@Yuki` 而不附带文字；该消息会进入正常聊天 Agent，让 Yuki 自然回应。后端只把最小的“仅被提及”上下文交给模型，永久事件账本仍保存真实的空文本消息，不伪造用户发言。

Planner-first 自主参与规则：

- 群消息静默窗口结束后，最多按 `PLANNER_MAX_PENDING_MESSAGES` 组成受限批次；
- 默认必要性门槛为 `0`，非空群聊批次都会交给 Planner 判断是否自然参与；
- 达到阈值后，由 Planner 选择 `reply`、`wait` 或 `silent`，`wait` 最多重新规划一次；
- Planner 以活跃群友为默认倾向，能自然接话、参与玩笑、回应情绪或延续话题时优先发言；
- 真实 `@Yuki`、回复 Yuki 和私聊由后端强制回复，历史活跃度不能降低该优先级；
- 旧置信度、冷却、每小时上限及 `AUTONOMOUS_ENABLED` 不限制 Planner-first 路径；
- 新群消息会中断自主 Planner 和自主生成，但普通观察消息不会中断明确触发的处理轮；
- 自主轮不开放通用 OneBot 管理工具，Planner 本身也没有任何工具；
- 最终回复仍由同一个 Yuki Agent 生成，并使用普通消息与计划内的发送节奏。

设置 `PLANNER_ENABLED=false` 后才恢复 1.5.2 的候选判断、置信度、冷却与每小时上限。

## 命令

| 命令 | 作用 |
|---|---|
| `/ai help` | 显示帮助 |
| `/ai new` | 设置当前用户/场景的新上下文切点 |
| `/ai status` | 显示连接、模型、上下文和版本 |
| `/ai stop` | 取消当前用户/场景的模型请求 |
| `/ai ping` | 连通性检查 |
| `/ai voice status|profiles|show|styles|test` | 查看或使用当前本地声线；管理操作仅超级管理员 |
| `/ai whoami` | 显示 QQ、昵称、本群名片、别名与记忆统计 |
| `/ai forgetme` | 彻底删除当前 QQ 的可归属数据 |
| `/ai memory list` | 查看本人的人物记忆 |
| `/ai memory add <内容>` | 添加明确人物记忆 |
| `/ai memory update <ID> <内容>` | 修改本人的人物记忆 |
| `/ai memory delete <ID>` | 删除本人的人物记忆 |
| `/ai preference list` | 查看本人的交互偏好 |
| `/ai preference set <键> <值>` | 设置交互偏好 |
| `/ai preference delete <键>` | 删除交互偏好 |
| `/ai automation list` | 只列出当前任务，按下次运行时间从 `#1` 重新编号并显示本地时间 |
| `/ai automation completed` | 单独列出已完成、取消、失败或阻塞的历史任务 |
| `/ai automation show <当前编号>` | 查看当前任务、调度与下次执行时间 |
| `/ai automation pause <当前编号>` | 暂停当前任务 |
| `/ai automation resume <当前编号>` | 重新计算时间并恢复当前任务 |
| `/ai automation cancel <当前编号>` | 永久取消当前任务并移入历史 |
| `/ai automation run <当前编号>` | 将当前任务调度为尽快执行 |
| `/ai automation history <当前编号>` | 查看当前任务最近执行状态与错误类别 |
| `/ai affection show` | 查看本人的好感度、信任度、有效信任度和阶段 |
| `/ai affection history` | 查看本人最近 10 次关系变化 |
| `/ai affection show user <QQ号>` | 超级管理员查看指定人物 |
| `/ai affection history user <QQ号>` | 超级管理员查看指定人物最近 10 次变化 |
| `/ai affection set user <QQ号> <0-100>` | 超级管理员设置好感度 |
| `/ai affection adjust user <QQ号> <-20到20>` | 超级管理员调整好感度 |
| `/ai affection trust user <QQ号> <0-100>` | 超级管理员设置信任度 |
| `/ai capabilities [类别]` | 所有用户按当前真实 QQ 查看完整权限、可改参数数量和接口范围 |
| `/ai config list [类别]` | 列出显式注册的配置键和生效方式 |
| `/ai config get <key>` | 读取全局有效值；凭证只显示是否已配置 |
| `/ai config set <key> <value>` | 设置全局数据库覆盖 |
| `/ai config set <key> <value> group current` | 设置当前群覆盖 |
| `/ai config set <key> <value> user <QQ号>` | 设置指定用户覆盖 |
| `/ai config unset <key> [...]` | 删除同一作用域覆盖，恢复较低优先级值 |
| `/ai config history [key]` | 查看当前管理员的配置修改记录 |
| `/ai config rollback <change_id>` | 回滚本人尚未被后续修改覆盖的配置变更 |
| `/ai on` / `/ai off` | 超级管理员启用/停用当前群 |
| `/ai group <群号> on\|off` | 超级管理员启用/停用指定群 |
| `/ai private <QQ号> on\|off` | 超级管理员恢复/阻止指定 QQ 私聊 |

超级管理员可在 memory/preference 的操作名后加 `user <QQ号>`，例如：

```text
/ai memory list user 123456789
/ai preference set user 123456789 reply_style 简短
```

## 自然语言管理与运行时配置

### 统一权限能力目录

当用户问“我能修改什么”“有哪些设置”“我的权限范围”或“能改多少参数”时，Yuki 必须调用后端能力目录，不能根据提示词或聊天记忆猜测。普通用户的正常 Agent 使用只读工具 `get_my_capabilities`，超级管理员的同一个 Agent 使用 `admin_list_capabilities`；确定性诊断入口为 `/ai capabilities [类别]`。三个入口读取同一个 `PermissionCatalogService`，但自然语言工具结果只给当前模型轮内部使用。

权限只从当前真实 OneBot 事件的 `sender.user_id` 解析：

| 等级 | 当前状态 | 能力范围 |
|---|---|---|
| `user` | 已启用，所有普通 QQ | 29 项本人自助接口，其中 14 项可修改本人上下文、记忆、偏好、时区或自动化任务；不能修改运行时配置 |
| `trusted` | 仅预留，当前不可分配 | 供未来介于普通用户与管理员之间的权限扩展 |
| `moderator` | 仅预留，当前不可分配 | 供未来群管理能力扩展 |
| `superuser` | 已启用，来自 `.env` 的 `SUPERUSERS` | 71 项可修改配置、12 项受保护配置、19 项管理员业务接口（15 项修改型），以及 1 个可调用全部 NapCat/OneBot 公开 action 的通用网关 |

能力目录直接遍历现有 `ConfigRegistry` 和 `ActionRegistry`，不会另复制配置键或业务 action。`summary` 只提供计数与类别，`focused` 提供命中项的 ID、别名、说明、类型、范围、作用域和生效方式，`full` 才提供全部 ID。`call_onebot_api(action, params)` 作为独立的 `onebot` 权限类别列出：真实超级管理员在直接触发、非自主群聊的普通 Agent 轮次中可调用全部公开 action，不设 action denylist，也不二次确认；使用网页工具后本轮会撤销网关，但不会缩减 action 范围。目录不读取配置值、API Key、凭证状态或他人权限。`trusted`、`moderator` 只有枚举和展示元数据，在执行层接入相同权限校验前不会被实际授予。

管理员身份只在 `MessageProcessor` 中按当前真实 OneBot 事件验证：

```text
current_event.sender.user_id in 启动时加载的 SUPERUSERS
```

模型输入中的 QQ、引用发送者、`@管理员`、聊天历史、记忆、网页和“我是管理员”等文本都不能授权。配置值经过显式注册表、类型转换、数值范围、允许作用域和交叉字段校验后才会写库。模型不能修改 `.env`、`SUPERUSERS`、密钥或数据库地址，也没有 Shell、Python、文件写入、任意 SQL、任意 HTTP 管理或 Docker 控制工具。

有效配置按以下顺序解析：

```text
用户级数据库覆盖
  > 群级数据库覆盖
  > 全局数据库覆盖
  > .env 启动值
  > 代码默认值
```

配置生效方式：

| 模式 | 行为 |
|---|---|
| `HOT` | 下一条相关消息或下一次自主判断重新生成 `RuntimeConfigSnapshot`，无需重启 |
| `FUTURE_ONLY` | 只在之后创建人物关系、来源记录或清理任务时读取，不追改旧记录 |
| `RESTART_REQUIRED` | 覆盖立即保存为 pending，当前进程继续使用启动值；下次启动先加载覆盖再创建模型客户端、并发器和限流器 |
| `SECRET` | 只返回是否已配置；真实密钥不能通过命令或自然语言工具读取、修改或写入审计正文 |

`/ai status` 会显示待重启配置数。运行时覆盖在容器重建和应用重启后仍保留；`unset` 会恢复同一键的较低优先级值。

首批可修改键：

| 模式 | 配置键 |
|---|---|
| HOT | `autonomous.enabled`、`autonomous.silence_seconds`、`autonomous.confidence_threshold`、`autonomous.cooldown_seconds`、`autonomous.max_per_hour` |
| HOT | `planner.enabled`、`planner.direct_enabled`、`planner.group_enabled`、`planner.group_debounce_seconds`、`planner.confidence_threshold`、`planner.reply_necessity_threshold`、`planner.max_pending_messages`、`planner.recent_presence_window_seconds`、`planner.max_wait_seconds`、`planner.interrupt_autonomous_on_new_message` |
| HOT | `context.local_event_limit`、`context.related_people_limit` |
| HOT | `reply.daily_split_enabled`、`reply.daily_split_max_characters`、`reply.daily_split_max_messages`、`reply.delay_min_seconds`、`reply.delay_max_seconds`、`reply.max_qq_message_chars` |
| HOT | `llm.temperature`、`llm.max_output_tokens`、`llm.thinking_enabled` |
| HOT | `agent.max_tool_calls`、`agent.max_model_requests`、`agent.tool_result_max_characters` |
| HOT | `web.search_max_results`、`web.extract_max_results`、`web.max_calls_per_turn`、`web.tool_result_max_characters` |
| HOT | `relationship.confidence_threshold`、`relationship.max_auto_delta`、`relationship.daily_positive_cap`、`relationship.daily_negative_cap`、`relationship.conflict_preference_min_gap` |
| HOT | `vision.max_images_per_turn`、`vision.max_frames_per_turn`、`vision.gif_max_frames`、`vision.thinking_enabled`、`vision.thinking_budget`、`vision.low_confidence_retry_threshold`、`vision.per_user_requests_per_minute`、`vision.per_group_requests_per_minute` |
| FUTURE_ONLY | `relationship.initial_affection`、`relationship.initial_trust`、`web.source_retention_days`、`web.source_max_runs_per_conversation`、`vision.analysis_retention_days` |
| RESTART_REQUIRED | `llm.model`、`llm.timeout_seconds`、`llm.max_retries`、`global.llm_concurrency`、`web.global_concurrency`、`rate_limit.per_user_per_minute`、`rate_limit.per_group_per_minute` |
| RESTART_REQUIRED | `vision.enabled`、`vision.base_url`、`vision.model`、`vision.global_concurrency`、`vision.queue_max_pending`、`vision.queue_timeout_seconds`、`vision.media_download_timeout_seconds`、`vision.timeout_seconds`、`vision.max_output_tokens` |
| RESTART_REQUIRED | `automation.enabled`、`automation.poll_seconds`、`automation.lease_seconds`、`automation.max_active_per_superuser`、`automation.max_active_per_user`、`automation.max_steps`、`automation.max_llm_calls_per_run`、`automation.max_tool_calls_per_run`、`automation.max_messages_per_run`、`automation.max_runtime_seconds`、`automation.min_interval_seconds`、`automation.default_misfire_grace_seconds`、`automation.max_consecutive_failures`、`automation.run_retention_days` |

不可通过管理员工具修改：

- `app.host`、`app.port`、`database.url`、`superusers`、启动默认 `ENABLED_GROUPS`；
- `LLM_API_KEY`、`TAVILY_API_KEY`、`VISION_API_KEY`、`ONEBOT_ACCESS_TOKEN`、`NAPCAT_WEBUI_TOKEN`、数据库密码和 QQ 登录凭据；
- 系统提示词和任何未在 `ConfigRegistry` 显式登记的 `Settings` 字段。

凭证查询最多返回“已配置/未配置”，不会返回真实内容。审计表保存真实管理员 QQ、触发消息 ID、会话键、能力、目标、脱敏前后状态、成功标记、错误类别和耗时；不保存 API Key、完整网页正文、系统提示词或隐藏推理。回滚只支持配置覆盖，且必须由原操作者执行、当前覆盖仍与原变更的 after 版本一致；记忆删除、关系变化、已发消息和 OneBot 操作不提供通用回滚。

同一聊天轮可以在总工具预算内顺序执行多个不同的修改或人物业务操作，后端会逐项校验权限、参数与真实结果；参数完全相同的重复写入会被拦截，避免模型循环提交同一个动作。`memory.list`、`preference.list`、关系查询和配置读取等只读结果中的人物记忆、偏好和历史文本始终是不可信资料，不能自行产生新的修改意图。修改失败时，后端会覆盖模型的成功措辞并明确提示操作未完成。批量清理旧的低重要度自动记忆应使用原子动作 `memory.prune`，显式记忆不会被该动作删除。

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
| `MAX_CONTEXT_CHARACTERS` | `12000` |
| `RELATED_PEOPLE_LIMIT` | `5` |
| `PERSON_MEMORY_MAX_ENTRIES` | `100` |
| `GROUP_MEMORY_MAX_ENTRIES` | `100` |
| `PERSON_GROUP_MEMORY_MAX_ENTRIES` | `50` |
| `PREFERENCE_MAX_ENTRIES` | `30` |
| `MEMORY_BATCH_SECONDS` | `30` |
| `MEMORY_BATCH_TRIGGER_COUNT` | `10` |
| `MEMORY_BATCH_MAX_EVENTS` | `20` |
| `LLM_TIMEOUT_SECONDS` | `120` |
| `LLM_MAX_RETRIES` | `2` |
| `LLM_MAX_OUTPUT_TOKENS` | `8192` |
| `AGENT_MAX_TOOL_CALLS` | `12`（硬上限 `16`） |
| `AGENT_MAX_MODEL_REQUESTS` | `12` |
| `AGENT_TOOL_RESULT_MAX_CHARACTERS` | `32000` |
| `PLANNER_ENABLED` | `true` |
| `PLANNER_MODEL` | 空；使用 `LLM_MODEL` |
| `PLANNER_DIRECT_ENABLED` | `true` |
| `PLANNER_GROUP_ENABLED` | `true` |
| `PLANNER_GROUP_DEBOUNCE_SECONDS` | `3` |
| `PLANNER_PREFERRED_MESSAGES` | `3`（热配置范围 `1`～`20`） |
| `PLANNER_TEMPERATURE` | `0.1` |
| `PLANNER_MAX_OUTPUT_TOKENS` | `512` |
| `PLANNER_TIMEOUT_SECONDS` | `20` |
| `PLANNER_CONFIDENCE_THRESHOLD` | `0.2` |
| `PLANNER_REPLY_NECESSITY_THRESHOLD` | `0` |
| `PLANNER_MAX_PENDING_MESSAGES` | `8` |
| `PLANNER_RECENT_PRESENCE_WINDOW_SECONDS` | `300` |
| `PLANNER_MAX_WAIT_SECONDS` | `60` |
| `PLANNER_INTERRUPT_AUTONOMOUS_ON_NEW_MESSAGE` | `true` |
| `PLANNER_RECORD_RUNS` | `true` |
| `REPLY_SEQUENCE_CANCEL_ON_NEW_MESSAGE` | `true` |
| `REPLY_PLAN_HARD_MAX_MESSAGES` | `10`（可热更新至 `20`） |
| `PLUGIN_SYSTEM_ENABLED` | `false` |
| `PLUGIN_DIRECTORY` | `plugins` |
| `PLUGIN_API_VERSION` | `1.0` |
| `PLUGIN_HOOK_TIMEOUT_SECONDS` | `3` |
| `PLUGIN_START_TIMEOUT_SECONDS` | `10` |
| `PLUGIN_STOP_TIMEOUT_SECONDS` | `10` |
| `PLUGIN_MAX_PROMPT_FRAGMENT_CHARACTERS` | `2000` |
| `PLUGIN_MAX_PROMPT_CHARACTERS_PER_PLUGIN` | `4000` |
| `PLUGIN_MAX_TOTAL_PROMPT_CHARACTERS` | `8000` |
| `PLUGIN_BACKGROUND_TASK_LIMIT` | `4` |
| `PLUGIN_FAILURE_DISABLE_THRESHOLD` | `3` |
| `PLUGIN_HTTP_MAX_RESPONSE_BYTES` | `2097152` |
| `PLUGIN_HTTP_TIMEOUT_SECONDS` | `15` |
| `PLUGIN_AI_SESSION_MAX_HISTORY_MESSAGES` | `200` |
| `AUTOMATION_ENABLED` | `false` |
| `DEFAULT_TIMEZONE` | `Asia/Shanghai` |
| `AUTOMATION_POLL_SECONDS` | `2` |
| `AUTOMATION_LEASE_SECONDS` | `120` |
| `AUTOMATION_MAX_ACTIVE_PER_SUPERUSER` | `50` |
| `AUTOMATION_MAX_ACTIVE_PER_USER` | `10` |
| `AUTOMATION_MAX_STEPS` | `16` |
| `AUTOMATION_MAX_LLM_CALLS_PER_RUN` | `5` |
| `AUTOMATION_MAX_TOOL_CALLS_PER_RUN` | `16` |
| `AUTOMATION_MAX_MESSAGES_PER_RUN` | `10` |
| `AUTOMATION_MAX_RUNTIME_SECONDS` | `600` |
| `AUTOMATION_MIN_INTERVAL_SECONDS` | `60` |
| `AUTOMATION_DEFAULT_MISFIRE_GRACE_SECONDS` | `1800` |
| `AUTOMATION_MAX_CONSECUTIVE_FAILURES` | `3` |
| `AUTOMATION_RUN_RETENTION_DAYS` | `30` |
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
| `RELATIONSHIP_DAILY_POSITIVE_CAP` | `0`（不限额） |
| `RELATIONSHIP_DAILY_NEGATIVE_CAP` | `0`（不限额） |
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
| `VISION_ENABLED` | `false` |
| `VISION_PROVIDER` | `qwen` |
| `VISION_BASE_URL` | 空（启用时必填） |
| `VISION_API_KEY` | 空（启用时必填、敏感） |
| `VISION_MODEL` | `qwen3.7-plus` |
| `VISION_TIMEOUT_SECONDS` | `120` |
| `VISION_MAX_RETRIES` | `1` |
| `VISION_GLOBAL_CONCURRENCY` | `4` |
| `VISION_QUEUE_MAX_PENDING` | `32` |
| `VISION_QUEUE_TIMEOUT_SECONDS` | `120` |
| `VISION_MEDIA_DOWNLOAD_TIMEOUT_SECONDS` | `120` |
| `VISION_ALLOW_PRIVATE_URLS` | `false`；TUN/Fake-IP 环境可设为 `true`，会解除图片 URL 的本地、私有及保留地址拦截 |
| `VISION_MAX_OUTPUT_TOKENS` | `8192` |
| `VISION_THINKING_ENABLED` | `false` |
| `VISION_THINKING_BUDGET` | `6144` |
| `VISION_LOW_CONFIDENCE_RETRY_THRESHOLD` | `0.65` |
| `VISION_MAX_IMAGES_PER_TURN` | `5` |
| `VISION_MAX_FRAMES_PER_TURN` | `16` |
| `VISION_GIF_MAX_FRAMES` | `8` |
| `VISION_MAX_DOWNLOAD_BYTES` | `20971520` |
| `VISION_MAX_PREPARED_BYTES` | `16777216` |
| `VISION_MAX_DIMENSION` | `4096` |
| `VISION_MAX_PIXELS` | `16777216` |
| `VISION_PER_USER_REQUESTS_PER_MINUTE` | `20` |
| `VISION_PER_GROUP_REQUESTS_PER_MINUTE` | `60` |
| `VISION_ANALYSIS_RETENTION_DAYS` | `7` |

`ALLOWED_PRIVATE_USERS` 仅为旧配置兼容保留，1.0 不再把它当白名单；所有新 QQ 私聊默认准入。

## 本地开发与测试

```bash
uv sync --all-extras
uv run qq-ai-bot-cli init-db
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
uv run pytest -q examples/plugins/com.example.echo/tests
uv run qq-ai-bot
```

Docker 验证：

```bash
docker compose config
docker compose build bot
docker compose up -d
docker compose ps
```

健康检查不会请求 DeepSeek、Planner、Tavily、Qwen 或执行真实自动化，也不会暴露密钥；`planner_enabled/configured/active_requests`、`plugin_system_enabled/running_count`、`web_configured`、`vision_configured`、`automation_worker_running` 和 `active_automation_count` 都只读取本地配置或运行状态：

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

`/ai status` 会同时显示视觉是否启用、视觉模型、是否繁忙以及当前“排队/运行”数量，不显示密钥或完整接口查询参数。

## 1.6 升级步骤

1. 停止 Bot 写入但保持 NapCat 和 QQ 登录态运行：`docker compose stop bot`。
2. 完整备份 `data/`、`napcat-data/` 和 `napcat-config/`。
3. 将 `.env.example` 新增的 `PLANNER_*`、`REPLY_*` 和 `PLUGIN_*` 同步到 `.env`。建议首次升级保留 `PLUGIN_SYSTEM_ENABLED=false`；若要先验证旧聊天路径，可临时设 `PLANNER_ENABLED=false`。
4. 执行 `docker compose up -d --build --no-deps bot`；只重建 Bot，NapCat 不会被替换，Bot 启动脚本会自动运行 `alembic upgrade head` 到 `0014`。
5. 检查 `docker compose ps`、`/healthz` 和日志；确认 Planner 状态及插件运行数没有触发外部探测。
6. 依次人工验证：私聊明确请求、群聊 @、低必要性群消息静默、新消息中断剩余分句、管理员自然语言工具、自动化、视觉、联网、关系和旧命令。
7. 需要插件时再复制已审阅目录，通过 CLI 发现、查看权限、批准并启用；不要直接启用未知第三方 Python 代码。

`0017` 是非破坏性迁移，只新增人物语音偏好和 Planner 语音决策字段；回退会删除这些新增偏好与观测字段，因此应先备份 `data/`。`0014`～`0016` 分别新增持久化表情、本地语音及双语元数据。它们都不会删除聊天正文、人物、记忆、联网来源、关系、既有视觉缓存或自动化数据。更早的 `0005` 仍是不可逆的破坏性迁移；需要回退到 1.0 之前时只能停止服务并恢复升级前备份。
