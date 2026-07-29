# PlannerSignal

PlannerSignal 允许插件向群聊回复必要性提供一个很小、可观察的建议。它不能直接让 Yuki 发言、调用工具或获得权限。

```python
from yuki_plugin_sdk.models import PlannerSignal
from yuki_plugin_sdk.registrar import PlannerSignalRegistration


async def campaign_signal() -> PlannerSignal | None:
    if not campaign_is_active:
        return None
    return PlannerSignal(
        source_plugin_id="com.example.rpg",
        score_delta=4,
        reason_code="campaign.active",
        summary="当前群正在进行插件主持的跑团",
        confidence=0.9,
    )


registrar.register_planner_signal(
    PlannerSignalRegistration(name="campaign_active", provider=campaign_signal)
)
```

需要 `planner.signal.register`，并应先检查 `ctx.features.has("planner.signal.v1")`。

## 硬限制

- 单个信号 `score_delta` 只能在 `-10..10`。
- `confidence` 为 `0..1`；`summary` 最多 500 字符。
- Host 会验证 `source_plugin_id`、过期时间和真实当前场景。
- 多插件累计调整会再次裁剪；不能绕过必要性阈值、群聊速度、存在感惩罚或小时上限。
- Signal 只影响“是否值得进入 Planner”的建议，Planner 仍可 `silent`/`wait`。
- Signal 只能影响 Planner 决策，不能绕过 Host 权限或直接驱动业务逻辑。

不要把 Signal 当事件总线或状态存储。需要连续状态时使用私有 KV；需要独立叙事历史时使用插件 AI 会话。
