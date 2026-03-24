# 07 — 上下文质量反馈与生命周期管理

> **能力边界**：本章是明确后置 backlog 的 owner 文档。`context_feedback`、`lifecycle_policies` 及其相关服务 / 路由不进入初始 migration，也不属于当前 MVP 协作闭环的必做项；触发条件与重开入口见 `14-adr-backlog-register.md`。

## 质量反馈闭环

### 问题

当前系统是开环的：提供上下文给 Agent，但不知道这些上下文是否真的有用。没有反馈信号，检索质量无法迭代优化。

### (1) 隐式反馈采集

从 Agent 的后续行为中推断上下文质量，不要求显式评分：

```python
@dataclass
class ContextFeedback:
    context_uri: str        # 被检索到的上下文 URI
    session_id: str
    retrieved_at: datetime
    outcome: str            # adopted | ignored | corrected | irrelevant
    metadata: dict

# 推断规则：
# - adopted:    Agent 在后续生成中引用了该上下文的内容（文本相似度 > 阈值）
# - ignored:    上下文被检索返回但 Agent 未在生成中使用
# - corrected:  Agent 使用后被用户纠正
# - irrelevant: Agent 显式跳过
```

> **观测通道说明**：判断 `adopted` vs `ignored` 需要知道 Agent 的生成输出。ContextHub 作为后端中间件无法直接获取 Agent 输出。
> - **如后续先做最小版本**：可先支持显式反馈——Agent 通过 `contexthub_feedback` tool 主动报告 adopted/ignored。
> - **后续增强**：通过 OpenClaw 插件的 `afterTurn` 方法，将 Agent 输出摘要 + 本轮检索的 context URI 列表一起发送给 ContextHub feedback API，由服务端做文本相似度对比推断 adopted/ignored。
> - **遗留问题**：OpenClaw `afterTurn` 是否能获取 Agent 完整输出？实现时需调研确认。

反馈记录存入 PG：

> **实现边界**：下表只冻结未来表形状，避免语义漂移；不要求在初版代码骨架中创建。

```sql
CREATE TABLE context_feedback (
    id              BIGSERIAL PRIMARY KEY,
    context_id      UUID NOT NULL REFERENCES contexts(id),
    session_id      TEXT NOT NULL,
    retrieved_at    TIMESTAMPTZ DEFAULT NOW(),
    outcome         TEXT NOT NULL,          -- 'adopted' | 'ignored' | 'corrected' | 'irrelevant'
    metadata        JSONB
);

CREATE INDEX idx_feedback_context ON context_feedback (context_id);
```

### (2) 反馈信号回写

融入热度评分机制。`adopted_count` 和 `ignored_count` 直接存在 `contexts` 表中，每次反馈时原子更新：

```sql
-- 反馈为 adopted 时
UPDATE contexts SET adopted_count = adopted_count + 1 WHERE id = $1;
-- 反馈为 ignored 时
UPDATE contexts SET ignored_count = ignored_count + 1 WHERE id = $1;
```

综合评分计算：

```python
# 原始: score = sigmoid(log1p(active_count)) * exponential_decay(updated_at)
# 新增: quality_score = adopted_count / (adopted_count + ignored_count + 1)
# 综合: final_score = score * (0.5 + 0.5 * quality_score)

# 效果：
# - 高检索量 + 高采纳率 → 分数高（优质上下文）
# - 高检索量 + 低采纳率 → 分数被压低（噪音上下文）
# - 低检索量 → quality_score 趋近 0.5（数据不足，不惩罚）
```

### (3) 低质量上下文报告

定期生成（如每周），通过 PG 聚合查询：

```sql
-- 高检索 + 低采纳的上下文（噪音候选）
SELECT id, uri, active_count, adopted_count, ignored_count,
       adopted_count::float / NULLIF(adopted_count + ignored_count, 0) AS adoption_rate
FROM contexts
WHERE active_count > 10
  AND adopted_count::float / NULLIF(adopted_count + ignored_count, 0) < 0.2
ORDER BY active_count DESC;
```

---

## 生命周期管理

### 上下文状态机

> **权威定义**：状态枚举和时间戳字段见 00a §5.1 和 §5.3。

状态存储在 `contexts.status` 列中，每次状态转换同步写入对应的时间戳列：

```
                              审核通过
          提升请求 ──→ pending_review ──→ active  ←── 被访问/更新时重置
          创建 ──────────────────────→ active
                                         │ 标记过时(变更传播) 或 超过 N 天未访问
                                         ▼
                                       stale   ←── stale_at = NOW()
                                         │ 超过 M 天仍为 stale 且未被访问
                                         ▼
                                      archived ←── archived_at = NOW()，从向量索引中移除
                                         │ 超过 K 天（可选）
                                         ▼
                                      deleted  ←── deleted_at = NOW()

注：pending_review 仅用于记忆提升审核流程（MVP 阶段跳过，直接进入 active）。
```

状态转换通过 PG 操作实现：
- `active → stale`：`UPDATE contexts SET status = 'stale', stale_at = NOW() WHERE id = $1`
- `stale → active`：`UPDATE contexts SET status = 'active', stale_at = NULL, last_accessed_at = NOW() WHERE id = $1`
- `stale → archived`：`UPDATE contexts SET status = 'archived', archived_at = NOW(), l0_embedding = NULL WHERE id = $1`
- `archived → active`：重新生成 embedding 回填 `l0_embedding` + `UPDATE contexts SET status = 'active', archived_at = NULL WHERE id = $1`

### 生命周期策略配置

> **实现边界**：`lifecycle_policies` 为 post-MVP 配置表，不进入初始 migration。

```sql
CREATE TABLE lifecycle_policies (
    context_type    TEXT NOT NULL,       -- 'resource' | 'memory' | 'skill'
    scope           TEXT NOT NULL,       -- 'agent' | 'team' | 'datalake'
    stale_after_days INT DEFAULT 0,     -- 未访问 N 天后标记为 stale（0 = 不自动标记）
    archive_after_days INT DEFAULT 0,   -- stale 状态持续 M 天后归档
    delete_after_days INT DEFAULT 0,    -- 归档后 K 天删除（0 = 永不删除）
    PRIMARY KEY (context_type, scope)
);

-- 默认策略
INSERT INTO lifecycle_policies VALUES
    ('memory', 'agent',    90, 30, 180),
    ('memory', 'team',     0,  60, 0),
    ('resource','datalake', 0,  0,  0),
    ('skill',  'team',     0,  90, 0);
```

### 定时生命周期任务

```sql
-- 标记过期的 active 上下文为 stale
UPDATE contexts c SET status = 'stale', stale_at = NOW()
FROM lifecycle_policies lp
WHERE c.context_type = lp.context_type AND c.scope = lp.scope
  AND c.status = 'active'
  AND lp.stale_after_days > 0
  AND c.last_accessed_at < NOW() - (lp.stale_after_days || ' days')::interval;

-- 归档过期的 stale 上下文（基于 stale_at 计时）
UPDATE contexts c SET status = 'archived', archived_at = NOW(), l0_embedding = NULL
FROM lifecycle_policies lp
WHERE c.context_type = lp.context_type AND c.scope = lp.scope
  AND c.status = 'stale'
  AND lp.archive_after_days > 0
  AND c.stale_at < NOW() - (lp.archive_after_days || ' days')::interval;
```

### 湖表同步删除

`CatalogConnector.detect_changes()` 检测到表被删除 → 事务内处理：

```python
async def handle_table_deleted(self, table_uri: str):
    context_id = await self.pg.fetchval("SELECT id FROM contexts WHERE uri = $1", table_uri)
    async with self.pg.transaction():
        # 1. 归档该表的 context（目录侧按 URI 定位）
        await self.pg.execute("UPDATE contexts SET status = 'archived' WHERE uri = $1", table_uri)
        # 2. 发出变更事件（同一事务内；trigger 会在 commit 后自动 NOTIFY，无需手动调用）
        await self.pg.execute("""
            INSERT INTO change_events (context_id, account_id, change_type, actor)
            VALUES ($1, $2, 'deleted', 'catalog_sync')
        """, context_id, ctx.account_id)
    # 3. 事务提交后，change_events trigger 自动发出 NOTIFY → 传播引擎标记依赖方 stale
```
