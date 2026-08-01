# Memory V2 质量、审计与治理操作

## 离线质量套件

```bash
uv run qq-ai-bot-cli memory quality validate-dataset
uv run qq-ai-bot-cli memory quality run --suite full
uv run qq-ai-bot-cli memory quality compare
```

结果写入 `artifacts/memory-quality/report.json`、`report.md` 和 `junit.xml`。数据集全部是固定时间
和符号身份的合成数据；默认不允许真实模型或真实 Qwen。真实模型实验只能另行显式设置
`MEMORY_QUALITY_REAL_MODEL_ENABLED=true`，不得成为 CI 或发布门禁结果。

只有维护者明确接受数据集、门禁和实现变化后才运行：

```bash
uv run qq-ai-bot-cli memory quality update-baseline
```

该命令仍会先执行绝对门禁；失败时不会覆盖 baseline。

可选真实模型实验必须显式设置 `MEMORY_QUALITY_REAL_MODEL_ENABLED=true`；如还要测试真实 Qwen
Embedding，再设置 `MEMORY_QUALITY_REAL_EMBEDDING_ENABLED=true`。它仍只处理合成 fixture，
结果写入 `artifacts/memory-quality-real/`，不会覆盖 deterministic report/baseline，也不进入 CI
或发布 merge gate。不要在包含真实数据库内容的自定义 fixture 上启用。

## 合成性能场景

完整场景不会读取配置中的数据库：

```bash
uv run qq-ai-bot-cli memory quality performance
```

默认建立 100 用户、每人 100 facts、10 个群、100,000 条事件和 Fake Embedding，结果写入
`artifacts/memory-quality/performance.json`。只有确认结果可接受时才显式更新 baseline：

```bash
uv run qq-ai-bot-cli memory quality performance --update-baseline
```

这是本机类别下的回归参考，不是跨硬件 SLA。命令只使用临时 SQLite，结束即清理；不会调用
DeepSeek/Qwen，也不会读取 `DATABASE_URL`、真实聊天或真实人物资料。

## 生产审计

生产数据库命令从不隐式读取开发数据库，必须显式给出 URL：

```bash
uv run qq-ai-bot-cli memory audit \
  --database-url sqlite+aiosqlite:///./data/qq_ai_bot.db
```

审计只读、无模型，只输出 `issue_code / severity / count / sample_ids`。它不会输出事实正文、证据
摘录、聊天内容、QQ、群号、向量、密钥或数据库路径。

## 显式 Hygiene

```bash
uv run qq-ai-bot-cli memory hygiene scan \
  --database-url sqlite+aiosqlite:///./data/qq_ai_bot.db
uv run qq-ai-bot-cli memory hygiene apply <fingerprint> \
  --database-url sqlite+aiosqlite:///./data/qq_ai_bot.db
```

apply 会重新扫描，fingerprint 不一致即拒绝。它只能：

- 将来源明确无效的 automatic/rebuild fact 版本化为
  `invalidated / administrator_invalidated`，状态事件原因记录为 `invalid_provenance`；
- 重建缺失或孤立的 FTS 派生索引；
- 为当前 embedding profile 补建缺失 job；
- 清理 terminal rebuild run 的 proposal/item staging，保留 run receipt。

它不会自动处理 explicit 事实、contested 事实、跨目标关系、歧义第三方陈述或需要人工判断的
冲突，也不会物理删除 fact/evidence。启动、健康检查和发布检查都不会隐式执行 apply。

## 正式发布检查

```bash
uv run qq-ai-bot-cli memory release-check
```

该命令组合版本、Alembic head、dataset/baseline/gate hash、最新质量报告和契约快照。加上显式
`--database-url` 时才读取指定数据库并执行 `PRAGMA integrity_check`、外键检查和内容无关审计；
不传时会给出 warning。发布检查永远只读。

## 隐私边界

- fixture 只能使用 manifest 中的符号身份和合成 ID；loader 会拒绝疑似真实 QQ、Secret、浮动
  时间、未知字段和 hash 不一致。
- report、baseline、audit 与 release-check 不保存聊天正文、事实正文、证据摘录、向量、密钥或
  数据库路径；生产 audit 最多显示 20 个内部行 ID。
- 真实模型实验必须显式启用且仍只读取合成 fixture；CI 永远使用 Fake Model/Fake Embedding。
- hygiene 不物理删除事实/证据，不修改 explicit/ambiguous/contested 数据，也不会由启动、
  healthz 或 release-check 自动触发。

## 故障排查与发布清单

- `dataset hash mismatch`：不要改 expected 掩盖失败；审阅 fixture 后重新计算 manifest hash。
- `baseline regression`：先重复运行排除调度噪声；确认实现变化后显式 `update-baseline`，禁止
  降低绝对污染门禁。
- `contract snapshot changed`：审阅领域/Pydantic/Plugin API 差异后显式刷新快照；Plugin API
  主版本必须仍为 `1.0`。
- `fingerprint changed`：数据库在 scan 后已变化，重新 scan 和人工审阅，不要复用旧 fingerprint。
- `production audit` 失败：先备份数据库，只对确定可治理项执行 hygiene；其余保留为人工问题。

发布前还必须完成 Ruff、mypy、全量 pytest、Alembic、Compose 配置、Bot 镜像构建、质量套件、
baseline compare、显式生产 audit 和 release-check。任何 warning 都要如实记录，不能当成已执行
的 pass。
