# 声线档案

每个档案位于 `data/speech/voices/<profile_id>/`，包含 `profile.toml`、`model/` 和
`references/`。Manifest 使用严格字段，未知字段、绝对路径、目录逃逸、空模型、缺失参考
音频以及不存在的默认风格都会被拒绝。

```toml
id = "yuki"
display_name = "Yuki"
provider = "genie"
engine_model_version = "v2proplus"
language = "zh"
default_style = "neutral"
enabled = true
source = "user_supplied"
source_note = "本地转换"
license_note = "授权由部署者确认"

[model]
path = "model"

[[references]]
id = "neutral"
style = "neutral"
aliases = ["日常", "平静"]
audio = "references/neutral.wav"
text = "你好。"
language = "zh"
enabled = true
priority = 0
```

导入：`qq-ai-bot-cli speech profile import <目录>`；检查、启停和设默认档案请使用
`speech profile inspect|reload|enable|disable|set-default`。
