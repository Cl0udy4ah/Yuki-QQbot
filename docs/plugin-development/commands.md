# 确定性插件命令

命令不经过 Planner 或 LLM，适合状态查询、开关和参数明确的操作。需要 `command.register`。

```python
from yuki_plugin_sdk.models import PermissionLevel, StrictModel
from yuki_plugin_sdk.registrar import CommandMetadata, CommandRegistration
from yuki_plugin_sdk.results import CommandResult


class StatusArguments(StrictModel):
    verbose: bool = False


async def status(arguments: StatusArguments) -> CommandResult:
    text = "插件运行正常"
    if arguments.verbose:
        text += "；无待处理任务"
    return CommandResult(text=text)


registrar.register_command(
    CommandRegistration(
        metadata=CommandMetadata(
            name="status",
            description="查看插件状态",
            short_alias="demo-status",
            permission=PermissionLevel.USER,
        ),
        argument_model=StatusArguments,
        handler=status,
    )
)
```

本地名匹配 `[a-z][a-z0-9_-]{0,63}`，短别名最多 32 字符。核心 `/ai` 名称和别名（如 `help`、`status`、`memory`、`plugin`、`stop`）保留，冲突会使注册失败。

命令参数同样要求 `extra='forbid'` 并由 Host 做类型校验。`CommandResult.text` 最多 12000 字符；失败时使用 `ok=False` 和稳定 `error_code`，不要在错误详情放 Secret 或原始外部响应。

命令只在当前真实消息上下文执行，不能从参数伪造超级管理员或跨群目标。需要发送、配置写入等操作时仍须相应 Facade 权限。

