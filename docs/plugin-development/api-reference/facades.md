# PluginContext Facade Reference

下列签名来自 `yuki_plugin_sdk.context`；所有方法都可能因权限、作用域、当前轮安全策略或功能关闭而拒绝。

## Message

```python
get_current() -> CurrentMessage | None
get_reply() -> CurrentMessage | None
get_recent(limit=20) -> tuple[CurrentMessage, ...]
search_history(query, limit=20) -> tuple[CurrentMessage, ...]
send_text(text) -> PluginResult
send_private(user_id, text) -> PluginResult
send_group(group_id, text) -> PluginResult
send_image(*, target_type, target_id, media_reference) -> PluginResult
```

## People / Group

```python
people.get_current() -> Mapping | None
people.get(user_id) -> Mapping | None
people.list_aliases(user_id) -> tuple[str, ...]
people.add_alias(user_id, alias) -> PluginResult

groups.get_current() -> Mapping | None
groups.get(group_id) -> Mapping | None
groups.list_members(group_id, limit=100) -> tuple[Mapping, ...]
groups.get_settings(group_id) -> Mapping
groups.set_setting(group_id, key, value) -> PluginResult
```

## Memory / Relationship

```python
memory.list_person(user_id, limit=20) -> tuple[Mapping, ...]
memory.list_group(group_id, limit=20) -> tuple[Mapping, ...]
memory.search(query, *, scope_type, subject_id, limit=20) -> tuple[Mapping, ...]
memory.add(*, scope_type, subject_id, content, source_type, confidence,
           source_event_ids=()) -> PluginResult
memory.update(memory_id, *, content, confidence=None) -> PluginResult
memory.delete(memory_id) -> PluginResult

relationship.get_current() -> Mapping | None
relationship.get(user_id) -> Mapping | None
relationship.list_events(user_id, limit=20) -> tuple[Mapping, ...]
relationship.adjust(user_id, *, affection_delta=0, trust_delta=0,
                    reason) -> PluginResult
```

## LLM / Agent / AgentSession

```python
llm.generate(instruction, *, max_characters=2000) -> str
llm.generate_with_context(instruction, *, context_profile,
                          max_characters=2000) -> str

agent.run(instruction, *, allowed_capabilities=(),
          max_tool_calls=None, max_model_requests=None) -> PluginResult

agent_sessions.create(CreateAgentSessionRequest) -> AgentSession
agent_sessions.run(RunAgentSessionRequest) -> AgentSessionRunResult
agent_sessions.reset(session_id: UUID) -> AgentSession
agent_sessions.close(session_id: UUID) -> AgentSession
```

## Web / HTTP / Vision / Media

```python
web.search(query) -> PluginResult
web.read(url, question="") -> PluginResult
http.request(method, url, *, headers=None, body=None) -> PluginResult
vision.get_current_observation() -> Mapping | None
vision.analyze_current_media(question="") -> PluginResult
media.get_current() -> tuple[Mapping, ...]
```

## Automation

```python
automation.list_current_owner() -> tuple[Mapping, ...]
automation.create_from_template(template, parameters) -> PluginResult
automation.pause(task_id) -> PluginResult
automation.resume(task_id) -> PluginResult
automation.cancel(task_id) -> PluginResult
```

## Config / Secret / Storage

```python
config.get(key, *, scope_type="global", scope_id="") -> JsonValue
config.set(key, value, *, scope_type="global", scope_id="") -> None
secrets.configured(name) -> bool
secrets.get(name) -> str
storage.get(namespace, key) -> JsonValue
storage.set(namespace, key, value) -> None
storage.delete(namespace, key) -> bool
storage.list(namespace) -> Mapping[str, JsonValue]
storage.compare_and_set(namespace, key, expected, value) -> bool
```

## Scheduler / OneBot / Events

```python
scheduler.create_task(name, runner) -> str
scheduler.cancel(task_id) -> bool
scheduler.sleep_until_stopped() -> None
onebot.send_private(user_id, text) -> PluginResult
onebot.send_group(group_id, text) -> PluginResult
onebot.call_read_action(action, params) -> PluginResult
onebot.call_mutating_action(action, params) -> PluginResult
events.publish(EventEnvelope) -> None
```

`PluginContext` 还提供 `plugin_id`、隔离 `logger`、脱敏 `current` 和 `FeatureRegistry features`。

