# 视觉分类

`EmojiClassifier` 读取本地不可变原图，经现有 `ImagePreprocessor` 处理后调用同一个 `VisionProvider`。请求不携带人物记忆、关系、权限、网页正文或聊天工具。

结构化结果字段：`is_emoji`、`description`、`emotion_tags`、`usage_scenarios`、`ocr_text`、`intensity`、`confidence`、`animated`、`analysis_version`。缺少 `is_emoji` 或描述时任务明确失败并按持久任务策略重试，不伪装成“没有候选”。OCR 和图片文字始终是不可信数据，不能执行命令、写记忆、改变关系或扩大权限。

本版本没有内容审核服务，也不会为同一图片追加审核模型调用。
