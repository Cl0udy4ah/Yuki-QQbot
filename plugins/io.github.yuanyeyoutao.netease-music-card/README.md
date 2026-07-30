# 网易云音乐卡片插件

该插件不直接连接网易云，也不把 QQ 发送能力放进 MCP Server。它通过 Yuki 的
`ctx.mcp` 调用 `netease_music` 的只读搜索接口，再通过 `ctx.onebot.send_music_card`
向当前真实私聊或群聊发送 OneBot `music` 消息段。

这样划分后，MCP Server 仍可被其他客户端复用；插件没有任意 OneBot action 权限，
不能指定任意发送目标，只能在用户当前触发的会话内发送卡片。

## 权限

- `tool.register`：向主 Agent 注册 `share_netease_music`
- `mcp.call`：调用已由 Host 配置的 `netease_music`
- `onebot.send`：通过当前会话限定的音乐卡片 Facade 发送

## 使用示例

- “给我发一张周杰伦《晴天》的网易云音乐卡片”
- “分享一下《夜曲》”
- “发一首玉置浩二的歌”（仅指定歌手时发送网易云排序首位的匹配歌曲）
- 重名时先选择 Yuki 给出的候选，Yuki 再按 `song_id` 精确发送

插件不会在仅询问歌曲信息、歌词或歌手资料时主动发送卡片。
查询候选或未命中不会被计为已发送，因此 Yuki 可以在同一轮补充歌手、修正关键词或使用候选
`song_id` 继续完成发送。
