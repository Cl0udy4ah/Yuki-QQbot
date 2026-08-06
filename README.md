<div align="center">

<p>
  <img src="img/Yuki_2.png" alt="Yuki" width="280">
</p>

<h1>Yuki-QQbot</h1>

<p>
  面向个人部署的 QQ AI Agent
</p>

<p>
  <img src="https://img.shields.io/badge/Version-3.4.4-blue" alt="Version">
  <img src="https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white" alt="Python Version">
  <img src="https://img.shields.io/badge/NoneBot2-OneBot%20v11-green" alt="NoneBot2">
  <img src="https://img.shields.io/badge/Deploy-Docker%20Compose-2496ED?logo=docker&logoColor=white" alt="Docker Compose">
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/License-MIT-yellow" alt="MIT License">
  </a>
</p>

<p>
  <a href="#-主要功能">主要功能</a>
  ·
  <a href="#-快速开始">快速开始</a>
  ·
  <a href="#-文档">文档</a>
  ·
  <a href="#-开发">开发</a>
</p>

</div>

---

Yuki 是一个纯用 Codex vibe coding 开发、面向个人部署的 QQ AI Agent。它通过 NapCatQQ 接入 QQ，使用 Planner、Agent、长期记忆、工具系统和插件系统完成聊天、检索、自动化与外部服务调用。

> **当前版本：3.4.4**
>
> 主 Prompt、历史窗口和工具定义现在保持稳定顺序与分块边界，提高缓存复用率；输出清理器
> 同时兼容省略消息 ID 的发送者身份头，避免内部元数据偶尔出现在 QQ 回复中。

## ✨ 主要功能

- **自然对话**：支持私聊、群聊、多轮上下文和思考模型。
- **Planner + Agent**：先规划是否回复、调用哪些能力，再由同一个 Agent 完成工具调用与回答。
- **Memory V2**：按人物、群、群内身份和 Yuki 自身保存长期事实；Yuki 可通过统一变更回执自主
  创建、纠正、争议、合并或恢复记忆，并记录证据、来源、有效期和版本链。
- **混合 RAG**：在人物与群硬隔离后，结合 SQLite FTS 与可选 Qwen Embedding 检索相关记忆。
- **关系系统**：为每个 QQ 保存独立的好感度、信任度和关系阶段。
- **自动化任务**：用户可以通过自然语言创建持久化提醒和周期任务。
- **统一工具内核**：Core、Admin、Automation、Plugin 与 MCP 工具统一交给 Planner 和 Agent 调用。
- **MCP Client**：支持 stdio 与 Streamable HTTP，可接入麦当劳、网易云音乐等 MCP Server。
- **插件系统**：提供 Plugin API 1.1、独立 SDK、权限、事件、Prompt、Planner Signal、静态直达绑定、后台服务与持久通知扩展点。
- **GitHub 仓库管家**：可选 GitHub Monitor 支持多仓库、多 QQ 目标、事件过滤、中文 Push/Release 卡片、去重推送和 Yuki 自然点评。
- **多模态扩展**：可选图片理解、表情系统、DeepSeek 原生或 Tavily 联网搜索和本地 Genie-TTS 语音回复。
- **运行时管理**：支持管理员自然语言配置、权限审计、健康检查和数据库迁移。

---

## 🧰 技术栈

- Python 3.12
- NoneBot2
- OneBot v11 / NapCatQQ
- SQLite / SQLAlchemy / Alembic
- Pydantic
- OpenAI-compatible Chat Completions / Responses API，建议使用 DeepSeek
- MCP Python SDK
- Docker Compose
- 可选 DeepSeek 原生联网、Tavily、Qwen Vision、Qwen Embedding 与 Genie-TTS

---

## 🏗️ 架构概览

实线表示当前轮同步路径，虚线表示持久化、后台 Worker 或下一轮回流。模型只能通过经过授权的
工具和服务访问外部能力，不能直接读写数据库或调用 OneBot。

```mermaid
flowchart TD
    subgraph INBOUND["1. QQ 入站与可信事实"]
        USER["QQ群 / 私聊用户"] --> NAPCAT["NapCatQQ"]
        NAPCAT --> ONEBOT["OneBot v11 反向 WebSocket"]
        ONEBOT --> NONEBOT["NoneBot2 消息入口"]
        NONEBOT --> NORMALIZER["消息标准化<br/>正文、回复、@、图片、群名片、QQ"]
        NORMALIZER --> GUARD["准入与治理<br/>群/私聊策略、去重、限流、权限"]
        GUARD --> LEDGER[("SQLite chat_events<br/>原始消息、身份快照、回复关系")]
        NORMALIZER -.-> PEOPLE[("人物 / 群 / 成员身份目录")]
    end

    GUARD --> ROUTER{"消息路由"}

    subgraph DIRECT["2A. 确定性入口"]
        ROUTER -->|"/ai 管理命令"| COMMANDS["Command Service"]
        ROUTER -->|"已绑定插件命令"| PLUGIN_COMMAND["Plugin Host 命令"]
        COMMANDS --> OUTPUT
        PLUGIN_COMMAND --> OUTPUT
    end

    subgraph PLAN["2B. 普通会话规划"]
        ROUTER -->|"聊天 / 图片 / 回复"| VISION{"存在视觉输入？"}
        VISION -->|"是"| VISION_SERVICE["Vision Service<br/>图片、动图、表情理解与缓存"]
        VISION -->|"否"| PLANNER_CONTEXT
        VISION_SERVICE --> PLANNER_CONTEXT["Planner Context<br/>历史 messages 不含当前消息<br/>current_message 独占本轮输入"]
        LEDGER --> PLANNER_CONTEXT
        TOOL_CATALOG["能力目录<br/>Core、Admin、Automation、Plugin、MCP、Web"] --> PLANNER_CONTEXT
        PLANNER_CONTEXT --> NECESSITY["回复必要性与会话节奏"]
        NECESSITY --> PLANNER["Planner<br/>reply / wait / silent、工具域、记忆深度、表情、语音"]
        PLANNER -->|"silent / wait"| END["本轮静默或等待新消息"]
    end

    subgraph CONTEXT["3. 上下文与长期记忆"]
        LEDGER --> HISTORY["短期历史<br/>逐条发送者、QQ、消息 ID、回复目标"]
        PEOPLE --> SCENE["当前人物、群、关系与时区"]
        MEMORY[("Memory V2<br/>person / person_group / group / self<br/>事实、证据、状态与版本链")]
        MEMORY --> RETRIEVAL["作用域硬过滤<br/>FTS / 可选 Embedding / RRF"]
        HISTORY --> ASSEMBLER["Context Assembler<br/>统一字符预算"]
        SCENE --> ASSEMBLER
        RETRIEVAL --> ASSEMBLER
        VISION_SERVICE --> ASSEMBLER
        PLUGIN_PROMPT["插件 Prompt Fragment"] --> ASSEMBLER
        PLANNER -->|"reply plan"| COMPILER["Prompt Compiler<br/>人格、契约、运行态、计划与历史"]
        ASSEMBLER --> COMPILER
    end

    subgraph AGENT["4. 主 Agent 与统一工具内核"]
        COMPILER --> MODEL["Model Runtime<br/>Chat Completions / Responses API"]
        MODEL <--> RUNNER["AgentRunner<br/>有界多轮工具调用"]
        RUNNER <--> KERNEL["Tool Kernel<br/>Origin、创建者、群、权限与预算"]
        KERNEL --> CORE["Core<br/>历史查询、Memory Change、自我记忆"]
        KERNEL --> ADMIN["Admin / Runtime Config"]
        KERNEL --> AUTO_TOOL["Automation Tool"]
        KERNEL --> PLUGIN_TOOL["Plugin Tool / Facade"]
        KERNEL --> MCP["MCP Client<br/>stdio / Streamable HTTP"]
        KERNEL --> WEB["Web Router<br/>DeepSeek Native / Tavily"]
        CORE --> MUTATION["统一 Memory Mutation Service"]
        MUTATION --> MEMORY
        MODEL --> RESPONSE["最终文本与工具结果"]
    end

    subgraph OUTPUT_FLOW["5. 输出与真实投递回执"]
        RESPONSE --> CLEANER["输出清理器<br/>移除内部身份头与模型标记"]
        CLEANER --> OUTPUT["Reply Sequence<br/>分句、引用与长度边界"]
        PLANNER --> EFFECTS["回复效果计划"]
        EFFECTS --> EMOJI["表情选择与媒体制品"]
        EFFECTS --> SPEECH["Genie-TTS 语音"]
        EMOJI --> OUTPUT
        SPEECH --> OUTPUT
        OUTPUT --> SENDER["OneBot Sender"]
        SENDER --> NAPCAT_OUT["NapCatQQ"]
        NAPCAT_OUT --> RECEIVED["QQ群 / 私聊收到回复"]
        SENDER -.-> RECEIPT[("投递回执<br/>chat_events、Planner / Model / Tool 审计")]
    end

    subgraph BACKGROUND["6. 持久后台循环"]
        LEDGER -.-> MEMORY_JOB["Memory Worker<br/>候选提取、反思与质量治理"]
        MEMORY_JOB -.-> MUTATION
        LEDGER -.-> REL_JOB["Relationship Worker"]
        REL_JOB -.-> PEOPLE
        AUTO_TOOL --> AUTOMATIONS[("Automation Repository<br/>任务、版本与运行记录")]
        AUTOMATIONS -.-> SCHEDULER["Scheduler / Automation Worker"]
        SCHEDULER -.-> SCHEDULED["Scheduled Automation Turn<br/>真实创建者、Origin 与权限"]
        SCHEDULED -.-> PLANNER_CONTEXT
        PLUGIN_TOOL --> PLUGIN_HOST["Plugin Host<br/>权限、配置、私有存储与生命周期"]
        PLUGIN_HOST -.-> PLUGIN_BG["后台 Worker / 外部事件 / Planner Signal"]
        PLUGIN_BG -.-> PLANNER_CONTEXT
        PLUGIN_BG -.-> OUTBOX["Notification Outbox<br/>文本、Push / Release 卡片"]
        OUTBOX -.-> OUTPUT
    end
```

---

## 🚀 快速开始

### 1. 准备配置

```bash
cp .env.example .env
```

至少填写：

- `ONEBOT_ACCESS_TOKEN`
- `NAPCAT_WEBUI_TOKEN`
- `SUPERUSERS`
- 主模型的 API 地址、密钥和模型名称

### 2. 启动服务

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f bot napcat
```

完成 NapCat 登录并看到反向 WebSocket 已连接后，即可在 QQ 中测试。

日常启动：

```bash
docker compose up -d
```

停止服务：

```bash
docker compose down
```

---

## 🧩 可选能力

以下功能默认可以关闭，不影响基础聊天：

- MCP Server
- Qwen 图片理解
- Qwen Memory Embedding
- Tavily 联网搜索
- 插件系统
- 表情收集与自动回复
- Genie-TTS 本地语音

配置示例见 [`.env.example`](.env.example)。

---

## 🗃️ 数据与升级

Yuki 使用 SQLite 保存事件、人物、关系、记忆、自动化、插件和运行配置。

> [!IMPORTANT]
> 从 2.x 升级到 3.x 前必须完整备份 `data/`。Memory V2 的首次迁移会删除旧记忆表，但保留聊天事件账本和其他核心数据。

详细步骤见 [Memory V2 升级指南](docs/upgrade-memory-v2.md)。

---

## 📚 文档

- [Memory V2 架构](docs/architecture/memory-v2.md)
- [记忆检索与混合 RAG](docs/architecture/memory-v2-retrieval.md)
- [受控历史重建](docs/architecture/memory-v2-rebuild.md)
- [插件开发](docs/plugin-development/)
- [GitHub Monitor 使用说明](plugins/github-monitor/README.md)
- [Memory 质量与运维](docs/operations/memory-quality.md)
- [完整使用帮助](docs/help.md)
- [版本记录](CHANGELOG.md)
- [完整文档目录](docs/)

---

## 🛠️ 开发

安装依赖并运行检查：

```bash
uv sync --all-extras
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
```

执行数据库迁移：

```bash
uv run alembic upgrade head
```

---

## 📄 开源协议

本项目基于 [MIT License](LICENSE) 开源。
