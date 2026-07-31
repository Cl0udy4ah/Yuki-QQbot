# Memory V2 架构

Memory V2 将长期记忆拆为事实、证据、逐事件提取任务和可重建检索索引。关系数据库中的
`memory_facts` 始终是真相来源；`memory_facts_fts` 与 `memory_embeddings` 只负责从当前问题
召回候选，删除后可以由事实表完整重建。

## 数据边界

- `memory_facts` 保存版本化事实，作用域只能是人物 `person`、人物在某群的
  `person_group` 或群 `group`。
- `memory_evidence` 将事实绑定到真实 `chat_events` 和真实发送者；事实或事件删除时按外键
  级联。
- `memory_jobs` 一行只对应一个真人入站事件。任务可以批量领取，但提取和提交始终逐事件进行。
- `memory_facts_fts` 是 Alembic `0021` 创建的 FTS5 `trigram` 外部内容表，只索引
  `content`、`memory_key` 和 `category`，由 INSERT/DELETE/UPDATE 触发器同步。
- `memory_embedding_profiles`、`memory_embeddings` 和 `memory_embedding_jobs` 是 Alembic
  `0022` 创建的派生语义索引。向量按 profile 隔离，事实删除时级联删除。
- 三个 partial unique index 保证每个主体、kind、memory_key 最多一个 active fact。

## 可信身份映射

写入模型只看到一个 `primary_event`、后端生成的 `available_subjects` 和同一精确会话的少量
上下文。自动提取只提供 `speaker` 和群聊中的 `group`；模型不能提交 QQ 号、群号、事件 ID、
状态或替代链。

读取时由 `MemoryTargetResolver` 根据当前真实事件生成目标：私聊只有当前人物；群聊包含当前
人物、当前人物在本群和当前群。只有当前事件真实 `@` 的群成员或被回复消息的真实发送者，才会
新增独立的 `referenced_person` 与 `referenced_person_group`。最近发言者不会成为长期记忆目标，
昵称和模型输出也不能改变主体范围。

## 查询驱动检索

普通聊天调用链为：

```text
当前消息 + 有界回复文本 + Planner 简短 intent
  → MemoryQueryBuilder
  → MemoryTargetResolver
  → 对每个实体分别执行带 scope/user/group/status/valid_until 硬过滤的 FTS SQL
  → MemoryRanker 确定性排序
  → 独立实体块
  → ContextBudgeter
  → mark_used(最终入选 fact IDs)
```

FTS 查询先做 NFKC、casefold、空白压缩和有界词项提取，再由后端生成带引号的 OR 表达式，
不会执行用户输入的 FTS 运算符。两字以内查询只会在已经限定实体作用域的 SQL 内做有界
`LIKE`。无词法命中时不会加载全部事实，只允许当前人物少量 `explicit + preference` 常驻；
“你记得我什么”等集中定义的概览表达使用 `overview`，在每个实体内按重要度、置信度、更新时间
和事实 ID 返回有界结果。

词法与语义候选以确定性 RRF 融合，再结合精确匹配、重要度、置信度、更新时间和稳定事实 ID
确定顺序。一次 relevant 检索只生成一个 query embedding 并复用于全部合法目标；overview 不
生成向量。Provider 故障时只降级当前轮为词法检索，不中断聊天，也不改变事实。完整排序和
降级状态见 [Memory V2 检索](memory-v2-retrieval.md)。

语义候选必须先在 SQL 中按 `scope_type`、`subject_user_id`、`group_id`、active 状态、有效期和
当前 profile 过滤，再在内存中计算余弦相似度。严禁全库向量搜索后反推身份。文档向量异步生成，
正文模板只包含有界的 kind、category、memory_key 和 content，不包含 QQ、群号、昵称、证据、
聊天历史或系统提示词。详细设计与运维见 [Embedding 与混合 RAG](memory-v2-embedding.md)。

## 上下文和已使用时间

`ContextAssembler` 通过 `MemoryContextService` 获取以下互不混合的块：

```json
{
  "current_person": {"user_id": "10001", "facts": []},
  "current_person_in_group": {"user_id": "10001", "group_id": "20001", "facts": []},
  "current_group": {"group_id": "20001", "facts": []},
  "referenced_people": [
    {"user_id": "10002", "group_id": "20001", "person_facts": [], "group_facts": []}
  ]
}
```

`related_people` 可以继续携带最近群友的当前群身份元数据，但不附带其关系或长期事实。
`last_used_at` 只在事实通过最终 `ContextBudgeter` 后一次性更新；候选、被预算删除的事实、管理
列表和索引重建都不会更新它。Core Agent、管理员诊断和 Plugin API v1 的相关搜索均复用同一个
`MemoryRetriever`。

## 索引运维

超级管理员可以使用：

```text
/ai memory search person <QQ号> <query>
/ai memory search group <群号> <query>
/ai memory index status
/ai memory index rebuild
/ai memory embedding status
/ai memory embedding doctor
/ai memory embedding retry
/ai memory embedding rebuild
/ai memory embedding purge-old
```

健康检查仅返回事实数、物理索引行数、缺失数和孤儿数，不记录查询或事实正文。重建只执行
FTS 派生索引 rebuild，不修改事实、证据或状态，也不会在每次启动时自动运行。

## 尚未实现

当前没有向量数据库、模型重排、模糊昵称人物识别、第三方人物自动写入或历史聊天重建。
后续阶段见 [Memory V2 路线](memory-v2-roadmap.md)。
