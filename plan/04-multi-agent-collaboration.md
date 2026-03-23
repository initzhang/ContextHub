# 04 — 多 Agent 协作：记忆/Skill 的隔离、共享与版本管理

## (a) 多层级团队所有权模型

| 范围 | URI 示例 | 可见性 | 写权限 |
|------|----------|--------|--------|
| Private | `ctx://agent/{agent_id}/` | 仅该 Agent | 该 Agent |
| 子团队 | `ctx://team/engineering/backend/` | 该子团队成员 + 上级继承 | 子团队成员 |
| 上级团队 | `ctx://team/engineering/` | 工程部所有成员 | 工程部管理员 |
| 根团队(=全组织) | `ctx://team/` | 所有 Agent | 组织管理员 |

团队结构存储在 PG `teams` + `team_memberships` 表中（定义见 01-storage-paradigm.md），通过 `teams.parent_id` 递归 CTE 实现层级继承。

## (b) Skill 版本管理（简化方案）

Skill 本质是自然语言指令（Markdown），不是代码 API。SemVer 的"兼容性"概念在语义层面无法客观判定。因此不做自动兼容性判断，改为"版本号 + changelog + 手动标记 breaking"。

### PG 表结构

```sql
CREATE TABLE skill_versions (
    skill_id        UUID NOT NULL REFERENCES contexts(id),  -- Skill 对应 contexts 行
    version         INT NOT NULL,           -- 递增版本号
    content         TEXT NOT NULL,          -- Skill 定义（Markdown）
    changelog       TEXT,                   -- 变更说明（~50 tokens）
    is_breaking     BOOLEAN DEFAULT FALSE,  -- 发布者手动标记
    status          TEXT DEFAULT 'draft',   -- 'draft' | 'published' | 'deprecated'
    published_by    TEXT,                   -- 发布者 agent_id
    published_at    TIMESTAMPTZ,
    PRIMARY KEY (skill_id, version)
);

-- skill_subscriptions 已合并到 dependencies 表（见 01-storage-paradigm.md）
-- 订阅通过 dep_type='skill_subscription' 表示：
--   dependent_id = <agent context UUID>（订阅者）
--   dependency_id = skill_id（被订阅的 Skill）
--   pinned_version = NULL（跟随 latest）或具体版本号
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
            INSERT INTO change_events (context_id, change_type, actor, new_version, metadata)
            VALUES ($1, 'version_published', $2, $3, $4)
        """, skill_id, ctx.agent_id, str(new_ver),
             json.dumps({"is_breaking": is_breaking, "changelog": changelog}))

    # 5. 事务提交后触发传播（payload 为 Skill 的 context UUID）
    await self.pg.execute("NOTIFY context_changed, $1", str(skill_id))
```

传播逻辑：
- `is_breaking=True` → 依赖方被标记 STALE（见 06-change-propagation.md）
- `is_breaking=False` → 仅通知订阅者，不标记 STALE

## (c) 记忆共享与提升

```
Agent 私有记忆 → [提升请求] → 审核流程（可选） → 写入目标团队路径
                                                    ↓
                                        [该团队及子团队 Agent 收到通知]
```

> **MVP 阶段**：跳过审核，直接写入。但预留审核接口（`review_status` 字段 + `approve/reject` API），后续可启用。

### 提升流程（PG 事务保证原子性）

```python
async def promote_memory(self, source_uri: str, target_team: str, ctx: RequestContext,
                          skip_review: bool = True):
    """提升记忆到团队共享空间。

    Args:
        skip_review: MVP 阶段默认 True（直接写入）。
                     设为 False 时写入 review_status='pending'，等待审核通过后再激活。
    """
    async with self.pg.transaction():
        # 1. 读取源记忆
        source = await self.pg.fetchrow("SELECT * FROM contexts WHERE uri = $1", source_uri)

        # 2. 构造目标 URI
        target_uri = f"ctx://team/{target_team}/memories/shared_knowledge/{source['uri'].split('/')[-1]}"

        # 3. 写入目标团队路径，取得新行 id
        initial_status = 'active' if skip_review else 'pending_review'
        promoted_id = await self.pg.fetchval("""
            INSERT INTO contexts (uri, context_type, scope, owner_space, account_id,
                l0_content, l1_content, l2_content, status)
            VALUES ($1, 'memory', 'team', $2, $3, $4, $5, $6, $7)
            RETURNING id
        """, target_uri, target_team, ctx.account_id,
             source['l0_content'], source['l1_content'], source['l2_content'], initial_status)

        # 4. 注册 derived_from：提升后的记忆（dependent）依赖原始记忆（dependency），便于来源追踪与变更传播
        await self.pg.execute("""
            INSERT INTO dependencies (dependent_id, dependency_id, dep_type)
            VALUES ($1, $2, 'derived_from')
        """, promoted_id, source['id'])

        # 5. 发出变更事件
        await self.pg.execute("""
            INSERT INTO change_events (context_id, change_type, actor, metadata)
            VALUES ($1, 'created', $2, $3)
        """, promoted_id, ctx.agent_id,
             json.dumps({"promoted_from": source_uri, "review_status": initial_status}))

        # 6. 审计日志
        await self.pg.execute("""
            INSERT INTO audit_log (actor, action, resource_uri, metadata)
            VALUES ($1, 'promote', $2, $3)
        """, ctx.agent_id, target_uri, json.dumps({"from": source_uri, "to_team": target_team}))

async def approve_promotion(self, uri: str, ctx: RequestContext):
    """审核通过：将 pending_review 状态的记忆激活。预留接口，MVP 阶段不调用。"""
    async with self.pg.transaction():
        await self.pg.execute("""
            UPDATE contexts SET status = 'active', updated_at = NOW()
            WHERE uri = $1 AND status = 'pending_review'
        """, uri)
        await self.pg.execute("""
            INSERT INTO audit_log (actor, action, resource_uri, metadata)
            VALUES ($1, 'approve_promotion', $2, '{}')
        """, ctx.agent_id, uri)
    ctx_id = await self.pg.fetchval("SELECT id FROM contexts WHERE uri = $1", uri)
    await self.pg.execute("NOTIFY context_changed, $1", str(ctx_id))

async def reject_promotion(self, uri: str, reason: str, ctx: RequestContext):
    """审核拒绝：删除 pending_review 状态的记忆。预留接口，MVP 阶段不调用。"""
    async with self.pg.transaction():
        rejected_id = await self.pg.fetchval(
            "SELECT id FROM contexts WHERE uri = $1 AND status = 'pending_review'", uri)
        if rejected_id:
            await self.pg.execute("DELETE FROM dependencies WHERE dependent_id = $1", rejected_id)
        await self.pg.execute("DELETE FROM contexts WHERE uri = $1 AND status = 'pending_review'", uri)
        await self.pg.execute("""
            INSERT INTO audit_log (actor, action, resource_uri, metadata)
            VALUES ($1, 'reject_promotion', $2, $3)
        """, ctx.agent_id, uri, json.dumps({"reason": reason}))
```

示例：后端组 Agent 的一个 SQL pattern 提升到工程部共享
```
ctx://agent/backend-bot/memories/cases/sql-pattern-001
  → 提升到 ctx://team/engineering/memories/shared_knowledge/sql-pattern-001
  → 工程部下所有子团队（backend、data 等）的 Agent 可见
  → dependencies 表记录 derived_from 关系，源记忆变更时可传播通知
```
