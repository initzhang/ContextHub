# 05 — 权限控制与审计日志

## 细粒度权限控制

> **能力边界**：MVP 只实现默认可见性 / 默认写权限。本章是明确后置 backlog 的 owner 文档，定义 `access_policies`、字段脱敏与审计能力；触发条件与重开入口见 `14-adr-backlog-register.md`。

### 默认访问基线（MVP）

在引入显式 ACL 之前，系统先有一套默认访问基线：

- **默认读权限**：来自 `scope` + 团队层级可见性。Agent 默认可读自己的私有空间、所属团队及其祖先团队、根团队共享内容，以及 `datalake` / 组织级 `resources`。
- **默认写权限**：来自所有权与 `team_memberships`。Agent 只能写自己的私有空间，或自己所在团队有写权限的团队路径。
- **跨团队共享**：MVP 只通过 `promote` 把内容写入目标团队路径或共同祖先路径来完成。
- `dependencies` 只记录引用 / 来源 / 传播，本身不授予读权限。

`access_policies` 是对这套默认基线的显式覆盖，而不是取代它的全局白名单系统。

### `access_policies` 叠加层（post-MVP）

权限策略存储在 PG `access_policies` 表中（定义见 01-storage-paradigm.md）：

```sql
-- access_policies 表字段回顾
resource_uri_pattern TEXT    -- 如 'ctx://datalake/prod/*'
principal       TEXT         -- agent_id | team_path | role
effect          TEXT         -- 'allow' | 'deny'
actions         TEXT[]       -- {'read', 'write', 'admin'}
conditions      JSONB        -- 附加条件（如时间窗口、IP 白名单）
field_masks     TEXT[]       -- 需要脱敏的字段路径
priority        INT          -- 数值越大优先级越高
account_id      TEXT         -- 租户隔离
```

### Policy 评估规则

```
评估顺序（从高到低）：
1. 先计算默认访问基线（可见性 / 所有权）
2. 显式 deny 优先（deny-override）：任何匹配的 deny 策略直接拒绝
3. 同级冲突：多条 allow 策略匹配时，取 priority 最高的
4. 无匹配策略：回退到默认访问基线，而不是全局默认 deny
5. 仅当最终结果允许 `read` 时，才应用 `field_masks`
```

### 评估实现（PG 查询）

```sql
-- 查找所有匹配当前请求的策略
SELECT effect, priority, field_masks FROM access_policies
WHERE account_id = $1
  AND $2 LIKE replace(resource_uri_pattern, '*', '%')  -- URI 模式匹配
  AND (principal = $3                                    -- 精确匹配 agent_id
       OR principal = ANY($4))                           -- 匹配 agent 所属的团队路径列表
  AND $5 = ANY(actions)                                  -- 匹配请求的 action
ORDER BY
  CASE WHEN effect = 'deny' THEN 0 ELSE 1 END,          -- deny 优先
  priority DESC                                          -- 同类型按优先级排序
LIMIT 1;
```

上面这条 SQL 只是**策略查找**，不是最终判定。最终判定还需要把“默认访问基线”一起纳入。

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

实现方式：评估时查询 agent 的完整团队路径链（`teams` 表经 `parent_id` 的递归 CTE 自 `team_memberships` 解析出可见团队，再展开为路径前缀列表），对每一层级检查是否有 deny 策略：

```python
async def check_access(self, uri: str, ctx: RequestContext, action: str) -> bool:
    # 0. 先算默认访问基线（MVP 已有能力）
    baseline_allowed = await self.visibility.check_default_access(uri, ctx, action)

    # 获取 agent 可见团队对应的路径链（如 ['engineering/backend', 'engineering', '']），供 principal = ANY($4)
    team_paths = await self.get_visible_team_paths(ctx.agent_id)

    # 查询所有匹配的策略（一次 SQL）
    policies = await self.pg.fetch("""
        SELECT effect, priority, field_masks, principal FROM access_policies
        WHERE account_id = $1
          AND $2 LIKE replace(resource_uri_pattern, '*', '%')
          AND (principal = $3 OR principal = ANY($4))
          AND $5 = ANY(actions)
        ORDER BY CASE WHEN effect = 'deny' THEN 0 ELSE 1 END, priority DESC
    """, ctx.account_id, uri, ctx.agent_id, team_paths, action)

    # deny-override：任何 deny 直接拒绝
    if policies and policies[0]['effect'] == 'deny':
        return False
    # 有显式 allow 则通过（可授予默认不可见资源）
    if policies and policies[0]['effect'] == 'allow':
        return True
    # 无匹配策略：回退到默认访问基线
    return baseline_allowed
```

`get_visible_team_paths` 的实现：由 `team_memberships` 得到 agent 所属 `team_id`，再对 `teams` 用 `parent_id` 做递归 CTE 向上遍历祖先，将每条 team 的路径片段拼成与 `principal` / `resource_uri_pattern` 对齐的字符串列表（不再依赖 `owner_space` 字符串劈分模拟层级）。

### 字段脱敏

```
执行层：Retrieval Engine 返回结果时过滤（不在存储层加密）

流程：
  1. Retrieval Engine 从 PG 读取候选上下文
  2. Auth & ACL 模块先做默认访问基线判定，再评估 AccessPolicy
  3. 若最终允许 `read` 且命中 `field_masks` → 在返回的 L1/L2 内容中替换对应字段为 [MASKED]
  4. Agent 看到的是脱敏后的内容

为什么不在存储层加密：
  - 同一份数据对不同 Agent 的脱敏规则不同
  - 存储层加密意味着每个权限组合都要存一份副本，不现实
```

## 审计日志

> 审计日志是 **post-MVP** 能力。MVP 阶段可先不落 `audit_log`，不影响共享 / 传播 / 可见性语义。

审计日志存储在 PG `audit_log` 表中（定义见 01-storage-paradigm.md），利用 PG 的 ACID 保证审计记录与业务操作的一致性：

```python
# 审计记录在业务事务中一起写入，保证不丢失
async with self.pg.transaction():
    await self.do_business_operation(...)
    await self.pg.execute("""
        INSERT INTO audit_log (actor, action, resource_uri, context_used, result, metadata)
        VALUES ($1, $2, $3, $4, $5, $6)
    """, ctx.agent_id, action, uri, context_uris, 'success', metadata)
```

审计日志字段：
- `actor`：agent_id 或 user_id
- `action`：read | write | delete | search | promote
- `resource_uri`：操作的目标上下文
- `context_used`：本次操作引用了哪些上下文（用于溯源）
- `result`：success | denied | error
- `metadata`：附加信息（JSONB）
