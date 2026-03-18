# 05 — 权限控制与审计日志

## 细粒度权限控制

```python
class AccessPolicy:
    resource_uri_pattern: str   # 如 "ctx://datalake/prod/*"
    principal: str              # agent_id | team_path | role
    effect: str                 # allow | deny
    actions: list[str]          # read | write | admin
    conditions: dict | None     # 附加条件（如时间窗口、IP 白名单）
    field_masks: list[str]      # 需要脱敏的字段路径
    priority: int               # 数值越大优先级越高
```

### Policy 评估规则

```
评估顺序（从高到低）：
1. 显式 deny 优先（deny-override）：任何匹配的 deny 策略直接拒绝
2. 同级冲突：多条 allow 策略匹配时，取 priority 最高的
3. 无匹配策略：默认 deny（白名单模式）
```

### 与团队层级的交互

```
规则：子团队不可放宽父团队的限制

示例：
  ctx://team/ 根团队设置 deny: ctx://datalake/prod/salary/*
  ctx://team/hr/ HR 团队设置 allow: ctx://datalake/prod/salary/*
  → 无效。父团队的 deny 不可被子团队覆盖

实现：评估时从根团队向下遍历，任一层级的 deny 即终止
例外：根团队管理员可以为特定子团队设置"豁免"（exempt）
```

### 字段脱敏

```
执行层：Retrieval Engine 返回结果时过滤（不在存储层加密）

流程：
  1. Retrieval Engine 检索到候选上下文
  2. Auth & ACL 模块评估当前 Agent 的 AccessPolicy
  3. 匹配到 field_masks → 在返回的 L1/L2 内容中替换对应字段为 [MASKED]
  4. Agent 看到的是脱敏后的内容

为什么不在存储层加密：
  - 同一份数据对不同 Agent 的脱敏规则不同
  - 存储层加密意味着每个权限组合都要存一份副本，不现实
```

## 审计日志

```python
class AuditEntry:
    timestamp: datetime
    actor: str              # agent_id 或 user_id
    action: str             # read | write | delete | search | promote
    resource_uri: str
    context_used: list[str] # 本次操作引用了哪些上下文（用于溯源）
    result: str             # success | denied | error
    metadata: dict
```
