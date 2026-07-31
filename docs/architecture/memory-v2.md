# Memory V2 第一阶段架构

Memory V2 把长期记忆拆成事实、证据和逐事件任务三层，第一阶段优先保证“事实属于谁”，
不做语义检索或历史重建。

## 数据边界

- `memory_facts` 保存版本化事实。作用域只能是当前人物 `person`、当前人物在当前群的
  `person_group`，或当前群 `group`。
- `memory_evidence` 把事实绑定到真实 `chat_events` 和真实发送者。一个事实对同一事件最多一条
  证据，事实或事件删除时按外键级联。
- `memory_jobs` 一行只对应一个入站真人事件。任务可以批量 claim，但提取与提交始终逐事件进行。
- 三个 partial unique index 保证每个主体、kind、memory_key 最多一个 active fact。

## 可信身份映射

模型输入只包含一个 `primary_event`、后端生成的 `available_subjects` 和同一精确会话的少量
`conversation_context`。第一阶段只提供两个引用：

- `speaker`：由后端映射到主事件真实 `sender_user_id`；
- `group`：仅群聊提供，由后端映射到主事件真实 `group_id`。

结构化输出不能包含 QQ 号、群号、事件 ID、证据发送者、状态、替代链或时间字段。验证器拒绝
未知引用、私聊 group claim、错误 scope；证据的事件和发送者始终由后端从主事件填入。
前文只用于消歧，不能单独成为事实来源。

## 事实生命周期

同一作用域、主体、kind 和 memory_key 的规范化内容相同时复用事实并追加证据。内容变化时，
新事实以 `supersedes_id` 指向旧事实，旧事实变为 `superseded`。自动事实不能替代 explicit
事实。事实、证据、替代状态在同一数据库事务内完成，失败不会留下半写入状态。

## 聊天上下文

`ContextAssembler` 只装配三个实体块：

```json
{
  "current_person": {"user_id": "10001", "facts": []},
  "current_person_in_group": {"user_id": "10001", "group_id": "20001", "facts": []},
  "current_group": {"group_id": "20001", "facts": []}
}
```

`related_people` 只含当前群中的显示身份，不含关系和长期事实。每条事实只属于所在实体块，模型
不得把群事实或其他人物资料归给当前人物；没有事实时不得猜测。

## 第一阶段明确不包含

本阶段没有 FTS、BM25、Embedding、向量数据库、Reranker、第三方人物自动记忆、历史全量重建
或兼容旧表。后续设计见 [Memory V2 路线](memory-v2-roadmap.md)。
