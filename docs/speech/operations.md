# 运维与命令

准备 GenieData 和声线后，在 `.env` 设置 `SPEECH_ENABLED=true`、
`SPEECH_DEFAULT_PROFILE=<id>`，再启动：

```bash
docker compose --profile speech up -d --build
docker compose ps
docker compose logs -f bot genie-tts-worker
qq-ai-bot-cli speech status
qq-ai-bot-cli speech genie doctor
qq-ai-bot-cli speech test <profile_id> "你好，这是语音测试。"
```

QQ 中可用 `/ai voice status|profiles|show|styles|test`；切换、reload、缓存清理仅超级管理员。
缓存清理：`qq-ai-bot-cli speech cache cleanup`，不会删除 profile、模型或 reference。升级前仍
先备份 `data/`，Alembic `0015` 为非破坏性迁移。
