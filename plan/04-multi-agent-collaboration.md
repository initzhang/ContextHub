# 04 — 多 Agent 协作：记忆/Skill 的隔离、共享与版本管理

## (a) 多层级团队所有权模型

| 范围 | URI 示例 | 可见性 | 写权限 |
|------|----------|--------|--------|
| Private | `ctx://agent/{agent_id}/` | 仅该 Agent | 该 Agent |
| 子团队 | `ctx://team/engineering/backend/` | backend 成员 | 子团队成员 |
| 上级团队 | `ctx://team/engineering/` | engineering 成员及其子团队成员 | 工程部管理员 |
| 根团队(=全组织) | `ctx://team/` | 所有 Agent | 组织管理员 |

团队结构存储在 PG `teams` + `team_memberships` 表中（定义见 01-storage-paradigm.md），通过 `teams.parent_id` 递归 CTE 实现层级继承。

> 继承方向为**子读父**：子团队成员可见祖先团队内容；父团队成员默认不能读取子团队内容。

## (b) Skill 版本管理（简化方案）

Skill 本质是自然语言指令（Markdown），不是代码 API。SemVer 的"兼容性"概念在语义层面无法客观判定。因此不做自动兼容性判断，改为"版本号 + changelog + 手动标记 breaking"。

### 版本不可变性原则（见 00a §6）

- `published` 状态的版本行，其 `content`、`changelog`、`is_breaking` 字段**不可修改**。
- `draft` 状态的版本可修改。`published → deprecated` 是允许的转换，但内容不变。
- 通过 URI 读取 Skill 始终返回最新 `published` 版本。历史版本通过 API 参数 `?version=N` 读取。

### PG 表结构

```sql
CREATE TABLE skill_versions (
    skill_id        UUID NOT NULL REFERENCES contexts(id),  -- Skill 对应 contexts 行
    version         INT NOT NULL,           -- 递增版本号
    content         TEXT NOT NULL,          -- Skill 定义（Markdown）；published 后不可变（见 00a §6.1）
    changelog       TEXT,                   -- 变更说明（~50 tokens）；published 后不可变
    is_breaking     BOOLEAN DEFAULT FALSE,  -- 发布者手动标记；published 后不可变
    status          TEXT DEFAULT 'draft',   -- 'draft' | 'published' | 'deprecated'（见 00a §5.2）
    published_by    TEXT,                   -- 发布者 agent_id
    published_at    TIMESTAMPTZ,
    PRIMARY KEY (skill_id, version)
);

-- 订阅关系存储在独立的 skill_subscriptions 表（见 01-storage-paradigm.md）
-- 订阅主体是 agent（TEXT 标识），不是 context（UUID 行），因此不适合放在 dependencies 表中（见 00a §2.2, §6.4）
```

### 发布流程

```python
async def publish_skill_version(self, skill_id: UUID, content: str,
                                 changelog: str, is_breaking: bool, ctx: RequestContext):
    async with self.pg.transaction():
        # 1. 获取当前最大版本号
        max_ver = await self.pg.fetchval(
            "SELECT COALESCE(MAX(version), 0) FROM skill_versions WHERE skill_id = $1", skill_id)
        new_ver = max_ver + 1

        # 2. 插入新版本
        await self.pg.execute("""
            INSERT INTO skill_versions (skill_id, version, content, changelog, is_breaking, status, published_by, published_at)
            VALUES ($1, $2, $3, $4, $5, 'published', $6, NOW())
        """, skill_id, new_ver, content, changelog, is_breaking, ctx.agent_id)

        # 3. 更新 contexts 表的 L0/L1（当前版本内容）
        await self.pg.execute("""
            UPDATE contexts SET l0_content = $1, l1_content = $2, l2_content = $3,
                version = $4, updated_at = NOW()
            WHERE id = $5
        """, generate_l0(content), generate_l1(content), content, new_ver, skill_id)

        # 4. 发出变更事件（同一事务内）
        await self.pg.execute("""
            INSERT INTO change_events (context_id, account_id, change_type, actor, new_version, metadata)
            VALUES ($1, $2, 'version_published', $3, $4, $5)
        """, skill_id, ctx.account_id, ctx.agent_id, str(new_ver),
             json.dumps({"is_breaking": is_breaking, "changelog": changelog}))

    # 5. 事务提交后，change_events 的 AFTER INSERT trigger 自动发出 PG NOTIFY 'context_changed'
    #    无需应用层手动调用（见 01-storage-paradigm.md trigger 定义）
```

传播逻辑（详见 06-change-propagation.md）：

对 **使用依赖**（`dependencies` 表中 `dep_type='skill_version'` 的 artifact）：
- `is_breaking=True` → artifact 被标记 STALE（它的内容是用旧版本生成的，可能已过时）
- `is_breaking=False` → 仅通知，不标记 STALE

对 **订阅者**（`skill_subscriptions` 表）：
- floating 订阅者（`pinned_version IS NULL`）→ 收到通知（读取时自动拿到新版本）
- pinned 订阅者（`pinned_version = N`）→ 收到 advisory 通知（"v3 已发布，你仍在 v2"），不被标记 STALE，读取时仍返回 pin 的版本（见 00a §6.2.2）

## (c) 记忆共享与提升

```
Agent 私有记忆 → promote → 写入目标团队路径
                               ↓
                    [目标团队及其子团队 Agent 可见]
```

> **Session 4 冻结结果**：MVP 的跨团队共享只走 `promote`。`dependencies` 负责记录 `derived_from` 来源与传播关系，但不单独授予跨团队读权限。更窄的“reference + ACL”共享方式被归入明确后置 backlog，触发条件与重开入口见 `14-adr-backlog-register.md`。

### 提升流程（PG 事务保证原子性）

> 下方伪码是 MVP 的 canonical promote 流。审核流和审计 hook 均不属于当前 MVP 主路径；如后续要加，只能作为后置叠加层插入这个流程之前或之后，不能改变当前共享语义，具体分流见 `14-adr-backlog-register.md`。
> 真实代码中的 request-scoped `ScopedRepo` / `db` 执行模型以 `10-code-architecture.md` 为准；这里的伪码只表达业务事务边界。

```python
async def promote_memory(self, source_uri: str, target_team: str, ctx: RequestContext):
    """MVP canonical flow：直接将私有记忆提升到目标团队路径。"""
    async with self.pg.transaction():
        # 1. 读取源记忆
        source = await self.pg.fetchrow("SELECT * FROM contexts WHERE uri = $1", source_uri)

        # 2. 构造目标 URI
        target_uri = f"ctx://team/{target_team}/memories/shared_knowledge/{source['uri'].split('/')[-1]}"

        # 3. 写入目标团队路径，取得新行 id
        promoted_id = await self.pg.fetchval("""
            INSERT INTO contexts (uri, context_type, scope, owner_space, account_id,
                l0_content, l1_content, l2_content, status)
            VALUES ($1, 'memory', 'team', $2, $3, $4, $5, $6, 'active')
            RETURNING id
        """, target_uri, target_team, ctx.account_id,
             source['l0_content'], source['l1_content'], source['l2_content'])

        # 4. 注册 derived_from：提升后的记忆（dependent）依赖原始记忆（dependency），便于来源追踪与变更传播
        await self.pg.execute("""
            INSERT INTO dependencies (dependent_id, dependency_id, dep_type)
            VALUES ($1, $2, 'derived_from')
        """, promoted_id, source['id'])

        # 5. 发出变更事件
        await self.pg.execute("""
            INSERT INTO change_events (context_id, account_id, change_type, actor, metadata)
            VALUES ($1, $2, 'created', $3, $4)
        """, promoted_id, ctx.account_id, ctx.agent_id,
             json.dumps({"promoted_from": source_uri}))
```

后置扩展说明（已分流到 `14-adr-backlog-register.md`）：
- 如未来需要审核流，可在 `promote` 前增加 `pending_review` 状态和 `approve/reject` 路由，但它们不属于当前 MVP canonical path。
- 如未来需要审计日志，可在同一事务中追加 `audit_log` 写入，但它不应成为 promote 语义成立的前提。

示例：后端组 Agent 的一个 SQL pattern 提升到工程部共享
```
ctx://agent/backend-bot/memories/cases/sql-pattern-001
  → 提升到 ctx://team/engineering/memories/shared_knowledge/sql-pattern-001
  → 工程部下所有子团队（backend、data 等）的 Agent 可见
  → dependencies 表记录 derived_from 关系，源记忆变更时可传播通知
```
