# 表情系统架构

1.7 的表情域复用现有 `MediaResolver → ImagePreprocessor → VisionProvider`，不复制视觉客户端、插件 Host 或调度器。

```text
OneBot 图片事件
  → EmojiCandidateDetector
  → EmojiCollector（原图、预览、SHA-256/dHash）
  → emoji_jobs
  → EmojiWorker → EmojiClassifier → EmojiLifecycleService
  → EmojiRetriever → 可选候选拼图精排
  → PendingReplyEffect → ReplySequenceManager → OneBotSender
```

SQLite 保存资产元数据、作用域、持久任务和成功使用记录；文件系统保存不可变原图与静态预览。Planner 和 Agent 只能表达语义意图，最终资产始终由后端选择。系统没有表情审核队列、审核状态或审核模型调用。

旧 `emoji_descriptions` 继续服务已有 QQ 表情/图片描述缓存；它不参与新资产生命周期，因此不会形成两个表情池状态源。
