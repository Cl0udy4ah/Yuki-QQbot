# 模型转换

运行时 Worker 只加载 ONNX，不安装 PyTorch。转换使用独立的
`tools/genie_model_converter/` 环境：

```bash
cd tools/genie_model_converter
uv run --with . yuki-genie-convert --pth voice.pth --ckpt voice.ckpt \
  --output ./yuki-profile --profile-id yuki --display-name Yuki \
  --model-version v2proplus --language zh \
  --reference-audio neutral.wav --reference-text "你好。"
```

工具调用 Genie-TTS 2.0.2 官方 `convert_to_onnx`，仅接受 V2/V2ProPlus。完成后审阅
`profile.toml`，再通过主项目 CLI 导入。生产镜像、Bot 和 Worker 均不包含转换依赖。
