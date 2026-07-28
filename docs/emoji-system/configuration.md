# 配置

配置按 `user > group > global > .env > 代码默认值` 解析。核心键：

| 组 | 键 |
|---|---|
| 开关/收集 | `emoji.enabled`、`collection_enabled`、`collection_mode`、`collect_private`、`collect_group` |
| 采用/容量 | `auto_adopt_enabled`、`auto_adopt_min_confidence`、`pool_capacity`、`replacement_mode` |
| 选择/冷却 | `selector_enabled`、`selector_candidate_count`、`same_emoji_cooldown_seconds`、`scope_repeat_cooldown_seconds` |
| 去重/维护 | `near_duplicate_enabled`、`near_duplicate_distance`、`cache_retention_days`、`analysis_version` |
| Worker | `worker_batch_size`、`worker_poll_seconds`、`worker_lease_seconds`、`worker_max_attempts`、`worker_retry_delay_seconds` |

`pool_capacity` 未设置表示无限；两个 cooldown 都允许 `0` 表示关闭。`storage_root` 和预览尺寸是启动配置。不存在 `emoji.review_enabled`。
