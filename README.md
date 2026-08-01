# Yuki-QQbot

一个纯用 codex vibe coding 的面向个人部署的 QQ AI Agent。Yuki 通过 NapCatQQ 接入 QQ，使用 Planner、Agent、长期记忆、工具系统和插件系统完成聊天、检索、自动化与外部服务调用。

当前版本：**3.0.2**

3.0.2 修复群聊表情候选查询与发送确认链路。明确的“发个表情”请求会走 Planner 的确定性
快路径；只有 OneBot 返回真实消息 ID 后，系统才会记录图片已发送，失败时会诚实回退为短文字。

## 主要功能

- **自然对话**：支持私聊、群聊、多轮上下文和思考模型。
- **Planner + Agent**：先规划是否回复、调用哪些能力，再由同一个 Agent 完成工具调用与回答。
- **Memory V2**：按人物、群和群内身份保存长期事实，记录证据、修正、冲突、有效期和版本链。
- **混合 RAG**：在人物与群硬隔离后，结合 SQLite FTS 与可选 Qwen Embedding 检索相关记忆。
- **关系系统**：为每个 QQ 保存独立的好感度、信任度和关系阶段。
- **自动化任务**：用户可以通过自然语言创建持久化提醒和周期任务。
- **统一工具内核**：Core、Admin、Automation、Plugin 与 MCP 工具统一交给 Planner 和 Agent 调用。
- **MCP Client**：支持 stdio 与 Streamable HTTP，可接入麦当劳、网易云音乐等 MCP Server。
- **插件系统**：提供 Plugin API v1、独立 SDK、权限、事件、Prompt、Planner Signal 和后台服务扩展点。
- **多模态扩展**：可选图片理解、表情系统、受控联网搜索和本地 Genie-TTS 语音回复。
- **运行时管理**：支持管理员自然语言配置、权限审计、健康检查和数据库迁移。

## 技术栈

- Python 3.12
- NoneBot2
- OneBot v11 / NapCatQQ
- SQLite / SQLAlchemy / Alembic
- Pydantic
- OpenAI-compatible Chat Completions API，建议deepseek
- MCP Python SDK
- Docker Compose
- 可选 Qwen Vision、Qwen Embedding、Tavily 与 Genie-TTS

## 架构概览

```text
QQ / NapCatQQ
      ↓
NoneBot2 消息入口
      ↓
回复必要性判断 → Planner
      ↓
AgentRunner
      ↓
Tool Kernel
├── Core
├── Admin
├── Automation
├── Plugin
└── MCP
      ↓
回复序列 / 表情 / 语音
```

长期记忆流程：

```text
聊天事件账本
→ 身份安全提取
→ 事实、证据与冲突治理
→ FTS + Embedding 混合检索
→ 按实体分块注入当前对话
```

## 快速开始

### 1. 准备配置

```bash
cp .env.example .env
```

至少填写：

- `ONEBOT_ACCESS_TOKEN`
- `NAPCAT_WEBUI_TOKEN`
- `SUPERUSERS`
- 主模型的 API 地址、密钥和模型名称

### 2. 启动

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

停止：

```bash
docker compose down
```

## 可选能力

以下功能默认可以关闭，不影响基础聊天：

- MCP Server
- Qwen 图片理解
- Qwen Memory Embedding
- Tavily 联网搜索
- 插件系统
- 表情收集与自动回复
- Genie-TTS 本地语音

配置示例见 [`.env.example`](.env.example)。

## 数据与升级

Yuki 使用 SQLite 保存事件、人物、关系、记忆、自动化、插件和运行配置。

> 从 2.x 升级到 3.x 前必须完整备份 `data/`。Memory V2 的首次迁移会删除旧记忆表，但保留聊天事件账本和其他核心数据。

详细步骤见 [Memory V2 升级指南](docs/upgrade-memory-v2.md)。

## 文档

- [Memory V2 架构](docs/architecture/memory-v2.md)
- [记忆检索与混合 RAG](docs/architecture/memory-v2-retrieval.md)
- [受控历史重建](docs/architecture/memory-v2-rebuild.md)
- [插件开发](docs/plugin-development/)
- [Memory 质量与运维](docs/operations/memory-quality.md)
- [完整使用帮助](docs/help.md)
- [版本记录](CHANGELOG.md)
- [完整文档目录](docs/)

## 开发

```bash
uv sync --all-extras
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
```

数据库迁移：

```bash
uv run alembic upgrade head
```
