# Genie-TTS Worker

Yuki 的 Genie-TTS 2.0.2 完全本地 Worker。它只监听 Unix Domain Socket，不启动 HTTP、
不监听 TCP，也不下载 GenieData、角色模型或参考音频。运行前必须由部署者准备
`data/speech/genie_data` 与 `data/speech/voices/<profile_id>`。

```bash
python -m genie_tts_worker \
  --socket data/speech/runtime/genie.sock \
  --data-dir data/speech/genie_data \
  --speech-root data/speech
```

生产环境通过仓库根目录的 `docker compose --profile speech up -d --build` 启动；Worker
使用 `network_mode: none`，音频通过共享目录传递，Socket 只传输长度帧 UTF-8 JSON。
