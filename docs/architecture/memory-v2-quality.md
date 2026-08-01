# Memory V2 质量与治理架构

Memory V2 正式版用四层机制防止“把人记串”：

1. 版本化合成 fixture 描述事件、Fake Model 输出、预期事实、证据、检索、上下文与 rebuild。
2. `MemoryQualityRunner` 为每个 case 复制一个已迁移到 Alembic `0024` 的独立临时 SQLite，
   复用生产 EventExtractor、ClaimProcessor、FactService、FTS、Fake Embedding、Retriever、
   ContextService 和 rebuild 状态机。
3. Evaluator 只做 symbolic stable key 精确比较；Metrics 按固定分母聚合；外部 TOML 门禁与
   baseline comparator 共同阻止绝对质量或相对性能回退。
4. Production Audit 只输出 issue code/count/有限行 ID；Hygiene 需要先 scan，再以完全匹配的
   fingerprint 显式 apply。启动、healthz 与 release-check 都不会自动修复数据库。

合成数据不会包含真实 QQ、群号、聊天、向量、Secret 或临时路径。确定性 CI 只使用
`memory-quality-fake-model-v1` 与 `fake-embedding/local-test/v1`，不调用 DeepSeek、Qwen 或网络。

正式契约由 `config/memory_contracts.toml` 和
`tests/contracts/memory_v2/contracts.json` 冻结。Plugin API 保持 `1.0`，插件只能通过受作用域
限制的 MemoryFacade list/search/add/update/delete；不能访问向量、rebuild、全局 audit、其他人物
证据、质量数据集或 Provider Secret。

## 性能基准

`memory quality performance` 使用独立临时库建立固定的 100 用户、每人 100 facts、10 个群和
100,000 条事件场景；其中包含 person/person_group、contested facts、FTS 和 Fake Embedding。
它测量 rebuild plan、keyset 扫描、混合检索、上下文投影、峰值内存和请求计数。性能报告只保存
场景数量、机器类别和数值，不保存合成正文、ID、向量或临时路径。

大场景结果由显式 `--update-baseline` 保存。CI 的合并门禁继续使用 deterministic suite 的宽松
延迟比例，不拿某台机器的绝对毫秒值约束另一类 runner，也不自动刷新性能 baseline。

运行和故障处理参见 [Memory V2 质量运维](../operations/memory-quality.md)，指标定义参见
[质量指标与分母](memory-v2-quality-metrics.md)。
