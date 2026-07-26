# Yuki-QQbot

## 启动项目

> **升级提示：**1.4.1 新增非破坏性迁移 `0010`，只给 `chat_events` 增加精简图片观察字段，以便后续聊天继续理解近期图片；不会删除或改写人物、聊天正文、记忆、联网来源、关系或运行时配置。视觉缓存版本仍为 `vision-observation-v3`，不会复用 1.4.0 的旧识别结果。若从 1.0 之前直接升级，仍会经过不可逆的 `0005` 数据重建；始终先备份 `data/`。

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

Yuki-QQbot 1.4.1 是基于 Python 3.12、NoneBot2、OneBot v11、NapCatQQ、SQLite 和 OpenAI-compatible Chat Completions API 的人物中心 QQ Agent。

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

本版本只处理当前真实消息或其回复中的图片，不处理视频、语音、PDF 和普通文件，也不会主动回溯群历史中的任意旧图片。已启用群里未触发 Yuki 的普通图片只写入原有事件账本，不下载、不分析，也不会因此触发自主发言。

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

## 1.x 数据模型

`0005` 会创建以下主要数据：

| 表 | 作用 |
|---|---|
| `people` | 以 QQ `user_id` 为主键的人物 |
| `person_aliases` | QQ 昵称和各群历史称呼 |
| `groups` | 群名、启用状态和自主参与设置 |
| `memberships` | `(user_id, group_id)` 当前群名片与活跃时间 |
| `chat_events` | 永久保存收发消息、消息段、回复关系和时间；`0010` 增加与原始事件关联的精简图片摘要 |
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

消息到达后的顺序是：

```text
准入判断
  → 去重
  → 更新人物/群/成员
  → 写入永久事件账本
  → 记忆任务入队
  → 已触发且含图片时，按需解析、预处理并调用独立视觉前端
  → 确定性命令，或进入同一个正常聊天 Agent
  → 当前真实发送者是超级管理员时，为该 Agent 动态增加管理员工具
  → 显式回复或自主参与判断
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

1.4.1 采用前后分离的双模型流程：

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
- QQ 商城表情或图片表情仍以真实图片观察为准，消息段的 `summary` 只作为不可信提示。
- “这是谁”“什么角色”“来自哪部作品”等问题使用 `character` 模式。默认关闭识图思考；开启 `VISION_THINKING_ENABLED` 后，角色、表情包和一般图片问题才会开启思考，普通描述低于复核阈值时自动深度复核一次。

媒体与预处理边界：

- 资源只能来自当前真实 OneBot 事件、被回复消息的真实 `image` 段，或 NapCat 对该 `file` 标识返回的 `get_image` 结果；模型、OCR、记忆和网页中的 URL 都不能成为图片下载源。
- HTTP(S) 下载拒绝凭据 URL、localhost、回环、私有、链路本地和保留地址；DNS 解析及每次重定向都会复查目标，最多 3 次重定向并流式执行字节上限。
- 支持 JPEG、PNG、WEBP、GIF 和 Pillow 可安全解析的动态 WEBP。程序按真实文件内容解码，应用 EXIF 方向，限制尺寸、像素、下载大小和预处理后大小，并防护损坏图片、解压炸弹、极端尺寸及无限动画。
- 动态图片默认最多抽取首帧、末帧和均匀分布的 8 帧；单轮所有图片合计最多 16 帧。多张图片与所有关键帧合并到一次 Qwen 请求，不逐张请求。

视觉观察包含描述、清晰 OCR、表情、常见使用语境、显著对象、高置信度角色名、作品来源、最多三个候选角色与依据、不确定性和置信度。它作为外部不可信 JSON 注入 DeepSeek：图片文字不能成为系统指令、管理员命令、工具参数或可信用户消息。只要本轮含当前图片或回复图片，后端会关闭运行时配置、关系、记忆、偏好、群/私聊准入和 `call_onebot_api` 等所有写入型管理员能力；联网、聊天历史及人物/群记忆等只读能力仍可使用。超级管理员若要修改系统，应另发一条纯文本消息。

成功识别后，后端会把最多 6000 字符、纯文本 JSON 形式的精简观察写入原始 `chat_event.visual_summary`。当前场景之后的近期上下文会恢复这段摘要，因此用户下一条再问“刚才图片里是什么”时，DeepSeek 仍能取得识图结果。摘要明确标记为外部不可信资料，不包含原图、Base64、临时路径或隐藏推理，也不会伪装成用户原话。

视觉观察、OCR 和表情含义不会自动写入长期人物/群记忆，也不会进入关系评价或改变好感度/信任度；它只随近期原始事件上下文提供。视觉 API 失败时，图片加文字仍按真实文字继续聊天；纯图片只返回一次简短的重新发送提示。

### 缓存、限流与隐私

- `media_analyses` 按 `content_hash + analysis_mode + question_hash + model + prompt_version` 唯一缓存；`vision-observation-v3` 还把思考开关、预算、复核阈值和预处理限制绑定到缓存变体，默认保留 7 天。
- 缓存只保存经过字段长度约束的结构化观察及必要元数据，不保存原图、Base64、临时文件、隐藏推理或 API Key；事件删除时关联缓存级联删除，过期记录由现有清理任务移除。
- Qwen 使用独立的并发信号量及用户/群限流，不占用 DeepSeek 的全局并发槽。相同内容、问题、模型和缓存版本的并发请求通过 single-flight 合并为一次 Provider 调用；缓存命中和合并跟随请求不重复消耗视觉 API 限额。
- 视觉流水线默认最多运行 4 个请求、等待 32 个请求，排队最长 120 秒；排队超时与 Qwen HTTP 请求超时分开统计，队列满时立即自然降级，避免请求无限堆积。
- 日志只记录脱敏会话哈希、队列等待时间、排队/运行数量、图片/帧/字节计数、内容哈希前 12 位、模型、耗时、缓存或 single-flight 命中状态和错误类别，不记录完整图片 URL、签名参数、原始图片、Base64、完整 OCR 或私聊图片内容。

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

这里的“任意 action”是独立的通用全接口网关：开放范围以当前 NapCat/OneBot 实际提供的全部公开 action 为准，不受权限目录中 18 项应用业务接口数量限制。能力目录是给 Yuki 的内部工具数据，不会原样发给用户或写入聊天账本；Yuki 读取后只输出自然语言结论或继续执行具体操作。

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
| `user` | 已启用，所有普通 QQ | 16 项本人确定性自助接口，其中 7 项会修改本人上下文、记忆、偏好或可归属数据；不能修改运行时配置 |
| `trusted` | 仅预留，当前不可分配 | 供未来介于普通用户与管理员之间的权限扩展 |
| `moderator` | 仅预留，当前不可分配 | 供未来群管理能力扩展 |
| `superuser` | 已启用，来自 `.env` 的 `SUPERUSERS` | 56 项可修改配置、12 项受保护配置、18 项管理员业务接口（14 项修改型），以及 1 个可调用全部 NapCat/OneBot 公开 action 的通用网关 |

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
| HOT | `context.local_event_limit`、`context.related_people_limit` |
| HOT | `reply.daily_split_enabled`、`reply.daily_split_max_characters`、`reply.daily_split_max_messages`、`reply.delay_min_seconds`、`reply.delay_max_seconds`、`reply.max_qq_message_chars` |
| HOT | `llm.temperature`、`llm.max_output_tokens`、`llm.thinking_enabled` |
| HOT | `agent.max_tool_calls`、`agent.max_model_requests`、`agent.tool_result_max_characters` |
| HOT | `web.search_max_results`、`web.extract_max_results`、`web.max_calls_per_turn`、`web.tool_result_max_characters` |
| HOT | `relationship.confidence_threshold`、`relationship.max_auto_delta`、`relationship.daily_positive_cap`、`relationship.daily_negative_cap`、`relationship.conflict_preference_min_gap` |
| HOT | `vision.max_images_per_turn`、`vision.max_frames_per_turn`、`vision.gif_max_frames`、`vision.thinking_enabled`、`vision.thinking_budget`、`vision.low_confidence_retry_threshold`、`vision.per_user_requests_per_minute`、`vision.per_group_requests_per_minute` |
| FUTURE_ONLY | `relationship.initial_affection`、`relationship.initial_trust`、`web.source_retention_days`、`web.source_max_runs_per_conversation`、`vision.analysis_retention_days` |
| RESTART_REQUIRED | `llm.model`、`llm.timeout_seconds`、`llm.max_retries`、`global.llm_concurrency`、`web.global_concurrency`、`rate_limit.per_user_per_minute`、`rate_limit.per_group_per_minute` |
| RESTART_REQUIRED | `vision.enabled`、`vision.base_url`、`vision.model`、`vision.global_concurrency`、`vision.queue_max_pending`、`vision.queue_timeout_seconds`、`vision.timeout_seconds`、`vision.max_output_tokens` |

不可通过管理员工具修改：

- `app.host`、`app.port`、`database.url`、`superusers`、启动默认 `ENABLED_GROUPS`；
- `LLM_API_KEY`、`TAVILY_API_KEY`、`VISION_API_KEY`、`ONEBOT_ACCESS_TOKEN`、`NAPCAT_WEBUI_TOKEN`、数据库密码和 QQ 登录凭据；
- 系统提示词和任何未在 `ConfigRegistry` 显式登记的 `Settings` 字段。

凭证查询最多返回“已配置/未配置”，不会返回真实内容。审计表保存真实管理员 QQ、触发消息 ID、会话键、能力、目标、脱敏前后状态、成功标记、错误类别和耗时；不保存 API Key、完整网页正文、系统提示词或隐藏推理。回滚只支持配置覆盖，且必须由原操作者执行、当前覆盖仍与原变更的 after 版本一致；记忆删除、关系变化、已发消息和 OneBot 操作不提供通用回滚。

同一次模型响应不能批量混合修改操作，避免只执行一半；一次修改成功后会关闭本轮工具，只允许 Yuki 根据真实结果做最终表述。`memory.list`、`preference.list`、关系查询和配置读取等只读操作不会提前关闭工具，因此可以在当前用户请求明确要求时继续执行对应修改。只读结果中的人物记忆、偏好和历史文本始终是不可信资料，不能自行产生新的修改意图。修改失败时，后端会覆盖模型的成功措辞并明确提示操作未完成。

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
uv run qq-ai-bot
```

Docker 验证：

```bash
docker compose config
docker compose build bot
docker compose up -d
docker compose ps
```

健康检查不会请求 DeepSeek、Tavily 或 Qwen，也不会暴露密钥；`web_configured` 表示联网已启用且配置完整，`vision_configured` 表示视觉功能已启用且 `BASE_URL`、`API_KEY`、模型均已配置：

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

## 1.4 升级步骤

1. 停止 Bot 写入但保持 NapCat 和 QQ 登录态运行：`docker compose stop bot`。
2. 完整备份 `data/`、`napcat-data/` 和 `napcat-config/`。
3. 将 `.env.example` 的 `VISION_*` 项同步到 `.env`。暂时不用图片理解时保留 `VISION_ENABLED=false`；启用时填写独立的百炼兼容接口地址和密钥，不要复用或替换 DeepSeek 的 `LLM_*` 配置。
4. 执行 `docker compose up -d --build --no-deps bot`；只重建 Bot，NapCat 不会被替换，Bot 启动脚本会自动运行 `alembic upgrade head` 到 `0010`。
5. 检查 `docker compose ps`、`/healthz` 和日志。
6. 依次人工验证：私聊纯图片、图片加文字、QQ 内置表情、动态表情、回复旧图片、群聊 `@Yuki` 图片、未触发群图片不分析，以及图片 OCR 不能执行配置/关系/OneBot 修改；再回归原有文本、记忆、联网、关系和管理员工具。

`0010` 可以回退且只删除 `chat_events.visual_summary` 派生摘要；`0009` 回退只删除 `media_analyses` 视觉缓存。两者都不影响聊天正文、人物、记忆、联网来源、关系和运行时配置。更早的 `0005` 仍是不可逆的破坏性迁移；需要回退到 1.0 之前时只能停止服务并恢复升级前备份。
