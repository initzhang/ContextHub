# 01 — 统一存储范式：对外文件语义，对内智能存储

## 核心理念

借鉴 OpenViking 的"一切皆文件"范式作为 **对外接口**——Agent 通过 `ctx://` URI 读写上下文，感知到的是文件语义。但对内不再用文件系统存储，而是按数据特性路由到合适的存储引擎：

```
对外接口：ctx:// URI + 文件语义（read/write/list/search）
对内存储：PG（元数据 + 内容 + pgvector 向量索引） + CatalogConnector（外部数据源）
```

### 为什么不用文件系统

ContextHub 的核心价值在变更传播、多 Agent 协作、权限治理——这些需要事务一致性、关系查询、事件通知。文件系统是"哑"存储，这些能力全部要在应用层重建。PG 原生提供：

| 能力 | PG 机制 | 文件系统替代方案 |
|------|---------|-----------------|
| 变更传播 | `LISTEN/NOTIFY` + 触发器 | 自建事件队列 + 轮询 |
| 依赖关系查询 | `JOIN` + 递归 CTE | 解析 `.deps.json` 文件 |
| 事务一致性 | ACID 事务 | 无（内容和元数据可能不一致） |
| 权限隔离 | 行级安全策略（RLS） | 应用层路径前缀检查 |
| 版本管理 | 行版本 + 历史表 | 文件复制 + 命名约定 |
| 审计日志 | 触发器自动记录 | 手动写 append-only 文件 |

### 设计原则

- **URI 是逻辑地址，不是物理路径**：`ctx://datalake/prod/orders` 不对应磁盘上的某个目录，而是 PG `contexts` 表中的一行
- **元数据和内容同库**：L0/L1/L2 内容存在 PG TEXT 列中（TOAST 自动处理大文本），与元数据在同一个事务中更新
- **结构化数据拆表存**：数据湖表的 DDL、血缘、查询模板等不塞进一个 TEXT blob，而是拆解为独立的 PG 表（详见 03-datalake-management.md）

## URI 命名空间

URI 作为 Agent 的统一寻址接口保持不变：

```
ctx://
├── datalake/{catalog}/{db}/{table}/      # 数据湖表
├── resources/{project}/                  # 文档资源
├── team/                                 # 共享空间（根 = 全组织）
│   ├── memories/                         # 全组织共享记忆
│   │   ├── business_rules/
│   │   └── data_dictionary/
│   ├── skills/                           # 全组织共享 skills
│   ├── engineering/                      # 工程部（子团队）
│   │   ├── memories/
│   │   ├── skills/
│   │   ├── backend/                      # 后端组（子子团队）
│   │   └── data/                         # 数据组
│   └── sales/                            # 销售部
├── agent/{agent_id}/                     # Agent 私有空间
│   ├── memories/
│   │   ├── cases/
│   │   └── patterns/
│   └── skills/
└── user/{user_id}/                       # 用户空间
    └── memories/
        ├── profile/
        ├── preferences/
        ├── entities/
        └── events/
```

## PG 核心表结构

### contexts 表（通用上下文）

```sql
CREATE TABLE contexts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    uri             TEXT NOT NULL,           -- ctx://datalake/prod/orders（对外接口用 URI，对内 JOIN 用 UUID）
    context_type    TEXT NOT NULL,           -- 'table_schema' | 'skill' | 'memory' | 'resource'（见 00a §3.1）
    scope           TEXT NOT NULL,           -- 'datalake' | 'team' | 'agent' | 'user'（见 00a §3.2）
    owner_space     TEXT,                    -- 团队路径如 'engineering/backend'（须匹配 teams.path），或 agent_id
    account_id      TEXT NOT NULL,           -- 租户隔离

    -- L0/L1/L2 内容（TOAST 自动处理大文本）
    l0_content      TEXT,                    -- ~100 tokens 摘要
    l1_content      TEXT,                    -- ~2k tokens 概览
    l2_content      TEXT,                    -- 完整内容（非 datalake 类型使用）
    file_path       TEXT,                    -- 长文档文件系统路径（仅 context_type='resource' 的长文档子类型使用，NULL = L2 存 PG）

    -- 元数据
    status          TEXT DEFAULT 'active',   -- active | stale | archived | deleted | pending_review（见 00a §5.1）
    version         INT DEFAULT 1,
    tags            TEXT[],
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    last_accessed_at TIMESTAMPTZ DEFAULT NOW(),  -- 初始值 = 创建时间，避免 NULL 导致生命周期 SQL 失效
    stale_at        TIMESTAMPTZ,             -- 进入 stale 时写入，恢复 active 时清空（见 00a §5.3）
    archived_at     TIMESTAMPTZ,             -- 进入 archived 时写入，恢复 active 时清空
    deleted_at      TIMESTAMPTZ,             -- 进入 deleted 时写入

    -- 热度与质量
    active_count    INT DEFAULT 0,
    adopted_count   INT DEFAULT 0,
    ignored_count   INT DEFAULT 0,

    UNIQUE (account_id, uri)                 -- 租户内唯一，非全局唯一（见 00a §1.1）
);

-- 租户隔离（RLS）
ALTER TABLE contexts ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON contexts
    USING (account_id = current_setting('app.account_id'));

-- 重要：每个请求必须在 PG 连接上设置 app.account_id，否则 RLS 无法生效。
-- 实现位置：db/repository.py 的 acquire_connection() 方法中：
--   await conn.execute("SET LOCAL app.account_id = $1", request_context.account_id)
-- SET LOCAL 作用域限于当前事务，事务结束后自动清除，不会泄漏到连接池的其他使用者。

-- 常用索引
CREATE INDEX idx_contexts_scope ON contexts (scope, context_type);
CREATE INDEX idx_contexts_owner ON contexts (account_id, owner_space);
CREATE INDEX idx_contexts_status ON contexts (status) WHERE status != 'deleted';
```

### dependencies 表（内容间使用依赖）

记录 context 与 context 之间的使用依赖关系。所有边都是"某个 artifact 在创建/生成时引用了另一个 context"。

**不含 Skill 订阅**——订阅是 agent 与 skill 之间的持续关系，主体是 agent（TEXT 标识），不是 context（UUID 行）。订阅见下方 `skill_subscriptions` 表。

```sql
CREATE TABLE dependencies (
    id              SERIAL PRIMARY KEY,
    dependent_id    UUID NOT NULL REFERENCES contexts(id),   -- 依赖方（"我依赖别人"的"我"）
    dependency_id   UUID NOT NULL REFERENCES contexts(id),   -- 被依赖方（"我依赖别人"的"别人"）
    dep_type        TEXT NOT NULL,           -- 'skill_version' | 'table_schema' | 'derived_from'
    pinned_version  INT,                     -- 创建时引用的版本号（仅 skill_version 使用）
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (dependent_id, dependency_id, dep_type)
);

CREATE INDEX idx_deps_dependency ON dependencies (dependency_id);  -- 变更传播时按被依赖方查找所有依赖方
CREATE INDEX idx_deps_dependent ON dependencies (dependent_id);    -- 查某个 context 的所有依赖
```

dep_type 语义：
- `skill_version`：Agent 的某个 case/pattern 依赖 Skill 的特定版本（写入 case 时自动注册，`pinned_version` 记录创建时使用的版本号）
- `table_schema`：依赖某张表的 schema
- `derived_from`：从某个共享 memory 派生

### skill_subscriptions 表（Agent 对 Skill 的订阅）

订阅是 **agent 与 skill 之间的持续关系**，语义与 dependencies 不同：
- **dependency**（使用依赖）：某个 artifact（context 行）在创建时引用了某个 skill version → 当 skill breaking change 时该 artifact 过时
- **subscription**（订阅关系）：某个 agent 持续关注某个 skill → 决定该 agent 读取时看到哪个版本，以及是否收到新版本通知

```sql
CREATE TABLE skill_subscriptions (
    id              SERIAL PRIMARY KEY,
    agent_id        TEXT NOT NULL,                            -- 订阅者（agent 标识）
    skill_id        UUID NOT NULL REFERENCES contexts(id),    -- 被订阅的 Skill
    pinned_version  INT,            -- NULL = floating（跟随 latest），非 NULL = 固定到指定版本
    account_id      TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (agent_id, skill_id)
);

CREATE INDEX idx_subs_skill ON skill_subscriptions (skill_id);      -- 发布新版本时查所有订阅者
CREATE INDEX idx_subs_agent ON skill_subscriptions (agent_id);      -- 查某个 agent 的所有订阅

-- 租户隔离（RLS）
ALTER TABLE skill_subscriptions ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON skill_subscriptions
    USING (account_id = current_setting('app.account_id'));
```

pinned_version 语义：
- `NULL`（floating）：跟随 latest published 版本。新版本发布时收到通知，读取时始终拿到最新版
- 非 `NULL`（pinned）：固定到指定版本。读取时从 `skill_versions` 表获取该版本内容。新版本发布时收到 advisory 通知（"v3 已发布，你仍在 v2"），但不被标记 stale

### change_events 表（替代 Event Log / Propagation Outbox）

`change_events` 是传播系统的唯一持久化事实源。传播引擎是否应该处理某个变更，只看这张表；`LISTEN/NOTIFY` 只负责尽快唤醒处理流程，不承担可靠投递语义。

```sql
CREATE TABLE change_events (
    event_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp       TIMESTAMPTZ DEFAULT NOW(),
    context_id      UUID NOT NULL REFERENCES contexts(id),  -- 发生变更的 context
    account_id      TEXT NOT NULL,           -- 冗余租户标识（从 contexts 写入时带入），供传播引擎创建 tenant-scoped session
    change_type     TEXT NOT NULL,           -- 'created' | 'modified' | 'deleted' | 'version_published'
    actor           TEXT NOT NULL,           -- agent_id | 'system' | 'catalog_sync'
    diff_summary    TEXT,                    -- ~50 tokens
    previous_version TEXT,
    new_version     TEXT,
    metadata        JSONB,
    delivery_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (delivery_status IN ('pending', 'processing', 'retry', 'processed')),
    attempt_count   INT NOT NULL DEFAULT 0,
    next_retry_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claimed_at      TIMESTAMPTZ,
    processed_at    TIMESTAMPTZ,
    last_error      TEXT
);
-- 注意：change_events 不启用 RLS。传播引擎需跨租户扫描 outbox（见 00a §1.3 例外说明）。

CREATE INDEX idx_events_ready
    ON change_events (next_retry_at, timestamp)
    WHERE delivery_status IN ('pending', 'retry');
CREATE INDEX idx_events_processing
    ON change_events (claimed_at)
    WHERE delivery_status = 'processing';
CREATE INDEX idx_events_context ON change_events (context_id);
```

最小状态语义：
- `pending`：事件已持久化，尚未被传播引擎领取
- `processing`：传播引擎已领取；若 worker 崩溃，可由启动补扫/周期补扫回收
- `retry`：上次传播失败，等待 `next_retry_at` 后重试
- `processed`：该事件要求的传播副作用已经全部成功提交

推荐由数据库触发器在 `change_events` 插入时自动发出唤醒通知，避免应用层漏发：

```sql
CREATE OR REPLACE FUNCTION notify_change_event() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('context_changed', NEW.context_id::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_change_events_notify
AFTER INSERT ON change_events
FOR EACH ROW
EXECUTE FUNCTION notify_change_event();
```

`pg_notify` 在事务中调用时会延迟到 commit 后投递，因此仍然与业务写入保持一致性；即使通知丢失，传播引擎也会依靠 outbox 补扫恢复。

> **能力边界**：`audit_log` 和 `access_policies` 都属于明确后置 backlog。它们只冻结未来的数据形状，不进入初始 migration，也不是当前 MVP 协作闭环成立的前提；触发条件与重开入口统一见 `14-adr-backlog-register.md`。

### audit_log 表（post-MVP）

```sql
CREATE TABLE audit_log (
    id              BIGSERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ DEFAULT NOW(),
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,           -- 'read' | 'write' | 'delete' | 'search' | 'promote'
    resource_uri    TEXT,
    context_used    TEXT[],                  -- 本次操作引用了哪些上下文
    result          TEXT NOT NULL,           -- 'success' | 'denied' | 'error'
    metadata        JSONB
);
```

### access_policies 表（post-MVP）

```sql
CREATE TABLE access_policies (
    id              SERIAL PRIMARY KEY,
    resource_uri_pattern TEXT NOT NULL,      -- 如 'ctx://datalake/prod/*'
    principal       TEXT NOT NULL,           -- agent_id | team_path | role
    effect          TEXT NOT NULL,           -- 'allow' | 'deny'
    actions         TEXT[] NOT NULL,         -- {'read', 'write', 'admin'}
    conditions      JSONB,                  -- 附加条件
    field_masks     TEXT[],                 -- 需要脱敏的字段路径
    priority        INT DEFAULT 0,
    account_id      TEXT NOT NULL
);
```

## 向量索引层

L0 摘要被向量化后存入 PG 的 pgvector 列，用于语义检索。元数据、内容、向量索引全部在同一个 PG 实例中，消除了跨系统双写的一致性问题。

| 数据 | 存储位置 | 说明 |
|------|----------|------|
| URI、元数据、L0/L1/L2 内容、状态、版本 | PG 结构化列 | 权威数据源，支持事务 |
| L0 embedding | PG `l0_embedding` 列（pgvector `vector` 类型） | HNSW 索引加速检索，与内容同库存储 |

### pgvector 列定义

```sql
-- 在 contexts 表中添加 embedding 列
ALTER TABLE contexts ADD COLUMN l0_embedding vector(1536);

-- HNSW 索引（推荐，支持近似最近邻搜索）
CREATE INDEX idx_contexts_l0_embedding ON contexts
    USING hnsw (l0_embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

向量检索直接在 PG 中完成，可与标量过滤在同一查询中组合：

```sql
SELECT id, uri, l0_content, context_type,
       l0_embedding <=> $1 AS distance
FROM contexts
WHERE account_id = $2
  AND status = 'active'
  AND context_type = ANY($3)
ORDER BY l0_embedding <=> $1
LIMIT 20;
```

**与 OpenViking 的区别：** OpenViking 将 L0/L1/L2 三个层级都入向量库（通过 `level` 字段区分）。ContextHub 只对 L0 做向量化——L1/L2 内容在同一 PG 表的其他列中，通过 URI 直接读取。原因：向量检索的目的是找到相关上下文（L0 足够），精排和详情加载直接查 PG（更快、更一致）。

**与独立向量库方案的区别：** 早期设计考虑过 ChromaDB/Milvus 作为独立向量库，但引入了双写一致性问题（PG 写成功但向量库写失败）和额外基础设施运维。ContextHub 只向量化 L0 摘要（~100 tokens/条，万级规模），pgvector 的 HNSW 索引完全胜任。统一到 PG 后消除了跨系统双写问题，架构最简。注意：embedding 生成需调用外部 API（如 OpenAI text-embedding-3-small），因此与内容写入不在同一事务中——内容先写入，embedding 异步回填。`EmbeddingReconciler` 定时检测缺失的 embedding 并补写，保证最终一致性。在 embedding 就绪前，新写入的 context 可通过 URI 直接访问，但不会出现在向量检索结果中。

### 检索流程

> **职责边界**：检索由独立的 `RetrievalService` 执行，不属于 `ContextStore`。

```
用户问题："上个月销售额是多少？"

1. 意图分析 → TypedQuery: {query: "月度销售额统计", context_type: "table_schema", scope: "datalake"}
2. `RetrievalService` 执行 pgvector 检索（L0 embedding 相似度 + 标量过滤 context_type, owner_space, account_id）→ top-K URI
3. 从同一 PG 读取候选的 L1 内容 → Rerank
4. 按需从 PG 加载 L2 / 关联的结构化数据（DDL、血缘、查询模板）
```

## 关系：用 PG 表存储

关系存储在 PG 的 `dependencies`、`skill_subscriptions` 和 `table_relationships` 表中（替代 `.relations.json` 和 `.deps.json` 文件）。

| 关系类型 | PG 表 | 查询方式 |
|----------|-------|----------|
| 上下文使用依赖（Skill 版本、表 schema、派生） | `dependencies` | `SELECT * FROM dependencies WHERE dependency_id = $1` |
| Skill 订阅（Agent → Skill 的持续关系） | `skill_subscriptions` | `SELECT * FROM skill_subscriptions WHERE skill_id = $1` |
| 表间 JOIN 关系 | `table_relationships`（见 03） | SQL JOIN |
| 数据血缘 | `lineage`（见 03） | 递归 CTE 遍历 |

**优势：**
- 变更传播时查依赖方：一条 SQL，不需要遍历文件系统
- 血缘图遍历：PG 递归 CTE 原生支持多跳查询
- 事务保证：依赖注册和内容更新在同一个事务中，不会出现"内容更新了但依赖没注册"的不一致

## 可见性与权限规则

> **权威约束**：可见性继承方向、两层访问模型、默认可见性规则见 00a §4。

### 可见性（子读父，PG 查询实现）

继承方向：子团队 Agent 可见所有祖先团队内容；父团队成员**不能**默认看到子团队内容（见 00a §4.1）。

```
Agent 所属团队路径: team/engineering/backend, team/data/analytics（支持多团队归属）

该 Agent 可见的上下文（从私有到全局）:
  1. ctx://agent/{self}/              ← 私有空间（scope=agent）
  2. ctx://user/{user_id}/            ← 所服务用户的记忆（scope=user）
  3. ctx://team/engineering/backend/  ← 所属团队 A（scope=team）
  4. ctx://team/engineering/          ← 团队 A 的上级（scope=team，自动继承）
  5. ctx://team/data/analytics/       ← 所属团队 B（scope=team）
  6. ctx://team/data/                 ← 团队 B 的上级（scope=team，自动继承）
  7. ctx://team/                      ← 根团队 = 全组织（scope=team，自动继承）
  8. ctx://datalake/                  ← 数据湖（scope=datalake，默认可见；post-MVP 可再叠加 ACL deny）
```

注：`ctx://resources/` 下的内容 scope 为 `team`，owner_space 为根团队路径，因此对同租户所有 Agent 默认可见（通过第 7 条继承）。如需限制访问，post-MVP 使用 ACL deny 规则。

实现方式：通过 `teams` 表的 `parent_id` 递归 CTE 展开 Agent 所属团队及其所有祖先，再通过 `owner_space` 匹配：

```sql
-- 递归展开 Agent 所属团队及其所有祖先
WITH RECURSIVE visible_teams AS (
    -- 基础：Agent 直接所属的团队
    SELECT t.id, t.path, t.parent_id
    FROM teams t
    JOIN team_memberships tm ON t.id = tm.team_id
    WHERE tm.agent_id = $1
    UNION ALL
    -- 递归：所有祖先团队
    SELECT t.id, t.path, t.parent_id
    FROM teams t
    JOIN visible_teams vt ON t.id = vt.parent_id
)
SELECT path FROM visible_teams;
```

```sql
-- 可见性查询（支持多团队归属 + 层级继承）
-- $visible_paths 由上述递归 CTE 生成
SELECT * FROM contexts
WHERE account_id = $1
  AND (
    owner_space = ANY($visible_paths)                -- 所有可见团队（含祖先链）
    OR scope = 'datalake'                            -- 数据湖默认可见（post-MVP 可再叠加 ACL）
    OR (scope = 'agent' AND owner_space = $agent_id) -- 私有空间
  );
```

上面这条 SQL 返回的是**默认可见 candidate set**，也是 MVP 阶段的完整读路径。post-MVP 如果支持“显式 allow 访问默认不可见资源”，`search` / `ls` 不能只依赖这条查询，还需要把 ACL allow 命中的 URI 并入候选集，再统一做 deny / mask 裁决。

### 写权限

| 范围 | 谁可以写 |
|------|----------|
| `ctx://agent/{id}/` | 该 Agent 自己 |
| `ctx://team/.../` 某层级 | 该层级的成员（或管理员） |
| `ctx://team/` 根 | 组织管理员 |

### 跨团队共享

- **MVP 冻结方案**：通过 `promote` 把内容写入目标团队路径，或写入双方共同可见的祖先路径。共享后的内容拥有新的 `uri` / `owner_space`，同时用 `dependencies(dep_type='derived_from')` 记录来源，便于溯源与传播。
- `dependencies` 不是权限机制。单独建立一条 dependency 边，不会让其他团队自动获得读权限。
- **post-MVP**：在显式 ACL allow 落地后，可增加“reference + ACL”的窄范围共享方式；届时有效读权限 = 默认可见性 ∪ 显式 allow，再由显式 deny 覆盖。

### 团队层级定义

```sql
CREATE TABLE teams (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    path        TEXT NOT NULL,              -- 'engineering/backend'
    parent_id   UUID REFERENCES teams(id),  -- 指向 'engineering' 的 UUID（根团队 parent_id = NULL）
    display_name TEXT,                       -- '后端工程团队'
    account_id  TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (account_id, path)               -- 租户内唯一，非全局唯一（见 00a §1.2）
);

CREATE INDEX idx_teams_parent ON teams (parent_id);
CREATE INDEX idx_teams_account ON teams (account_id);

-- 租户隔离（RLS）
ALTER TABLE teams ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON teams
    USING (account_id = current_setting('app.account_id'));
```

### Agent 多团队归属

```sql
CREATE TABLE team_memberships (
    agent_id    TEXT NOT NULL,
    team_id     UUID NOT NULL REFERENCES teams(id),  -- FK 约束确保只能加入已注册的团队
    role        TEXT DEFAULT 'member',  -- 'member' | 'admin'
    access      TEXT DEFAULT 'read_write', -- 'read_write' | 'read_only'
    is_primary  BOOLEAN DEFAULT FALSE,  -- 主团队（写入共享记忆的默认目标）
    PRIMARY KEY (agent_id, team_id)
);
```

- 主团队（primary）：Agent 写入共享记忆的默认目标
- 附属团队（secondary）：只读访问其他团队的共享上下文
- 类似 Unix 的主组 + 附属组

## ctx:// URI 路由层

Agent 看到的是 `ctx://` URI 和文件语义操作，ContextHub 内部将其路由到 PG：

> **实现说明**：下方代码块只表达存储语义和职责边界。真正的 request-scoped `ScopedRepo` / `db` 显式下传模型以 `10-code-architecture.md` 为准。

```python
class ContextStore:
    """对外暴露文件语义，对内路由到 PG"""

    async def read(self, uri: str, level: ContextLevel, ctx: RequestContext) -> str:
        # 1. 默认可见性 / 所有权检查（post-MVP 再叠加 access_policies）
        await self.acl.check_access(uri, ctx, action='read')
        # 2. 从 PG 读取（RLS 自动追加 account_id 过滤，命中 UNIQUE(account_id, uri) 索引）
        row = await self.db.fetchrow(
            "SELECT id, l0_content, l1_content, l2_content FROM contexts WHERE uri = $1", uri)
        content = row[f'l{level.value}_content']
        # 3. post-MVP 字段脱敏（MVP 直接原样返回）
        return await self.acl.apply_field_masks(content, uri, ctx)

    async def write(self, uri: str, level: ContextLevel, content: str, ctx: RequestContext):
        # 列名白名单映射（防止 f-string 拼接 SQL 注入）
        LEVEL_COLUMNS = {0: 'l0_content', 1: 'l1_content', 2: 'l2_content'}
        column = LEVEL_COLUMNS[level.value]

        async with self.db.transaction():
            # 1. 默认写权限检查（post-MVP 再叠加显式 ACL）
            await self.acl.check_access(uri, ctx, action='write')
            # 2. 写入 PG（乐观锁：version 条件防止并发覆盖）
            result = await self.db.execute(f"""
                UPDATE contexts SET {column} = $1, version = version + 1,
                    updated_at = NOW() WHERE uri = $2 AND version = $3
            """, content, uri, ctx.expected_version)
            if result == 'UPDATE 0':
                raise ConcurrentModificationError(f"URI {uri} has been modified by another writer")
            # 3. 发出变更事件（同一事务内，使用 context UUID）
            context_id = await self.db.fetchval("SELECT id FROM contexts WHERE uri = $1", uri)
            await self.db.execute(
                "INSERT INTO change_events (context_id, account_id, change_type, actor) VALUES ($1, $2, 'modified', $3)",
                context_id, ctx.account_id, ctx.agent_id)
        # 4. 无需应用层额外调用 NOTIFY。
        #    change_events 的 AFTER INSERT trigger 会在事务提交后发出唤醒通知；
        #    即使通知丢失，传播引擎也会在启动补扫 / 周期补扫中恢复处理。

class RetrievalService:
    """唯一的语义检索入口。"""

    async def search(self, query: str, ctx: RequestContext, **filters) -> list[Context]:
        # 1. 生成查询 embedding
        query_embedding = await self.embedding_client.embed(query)
        # 2. pgvector 检索 L0 + 标量过滤 → top-K 候选行（含 L1 内容，一次查询完成）
        rows = await self.db.fetch("""
            SELECT *, l0_embedding <=> $1 AS distance FROM contexts
            WHERE account_id = $2 AND status = 'active'
            ORDER BY l0_embedding <=> $1 LIMIT 20
        """, query_embedding, ctx.account_id)
        # 3. Rerank（基于 L1 内容）
        reranked = await self.reranker.rerank(query, rows)
        # 4. 默认可见性过滤（MVP）+ post-MVP ACL / 字段脱敏
        return await self.acl.filter_and_mask(reranked, ctx)
```

## LLM Tool 接口层：文件语义的 tool use 包装

### 问题

LLM（Claude、GPT 等）在 agentic 场景下习惯文件系统式的探索——ls 看目录、cat 读文件、grep 搜内容。这是训练数据中最常见的交互模式。ContextHub 底层用 PG，但暴露给 LLM 的工具接口需要符合这个直觉。

### 两种消费模式

| 模式 | 调用方 | 接口 | 适用场景 |
|------|--------|------|----------|
| SDK 调用 | Agent 编排代码（Python） | `ctx.search()` / `ctx.read()` | 编排代码知道要什么，直接检索 |
| Tool use 探索 | LLM 自主决策 | `ls` / `read` / `grep` / `search` | LLM 需要自主浏览上下文空间 |

两条路径共享同一套 PG 事实源，但职责分流：
- `ls` / `read` / `stat` → `ContextStore`
- `grep` / `search` → `RetrievalService`

### Tool 定义（注册为 LLM function calling 工具）

> **实现说明**：下方 tool 示例只表达“文件语义入口如何映射到 store / retrieval”；真实代码中的依赖注入和 request-scoped DB session 仍以 `10-code-architecture.md` 为准。

```python
class ContextTools:
    """暴露给 LLM 的 tool use 接口，模拟文件语义，底层全部走 PG"""

    def __init__(self, store: ContextStore, retrieval: RetrievalService):
        self.store = store
        self.retrieval = retrieval

    async def ls(self, path: str) -> list[str]:
        """列出路径下的内容（模拟目录浏览）

        示例：
          ls("ctx://datalake/hive/prod/") → ["orders", "users", "products"]
          ls("ctx://team/")               → ["memories/", "skills/", "engineering/", "sales/"]
          ls("ctx://agent/bot-1/")        → ["memories/", "skills/"]
        """
        prefix = path.rstrip('/') + '/'
        rows = await self.store.db.fetch("""
            SELECT DISTINCT
                split_part(substring(uri FROM length($1) + 1), '/', 1) AS child
            FROM contexts
            WHERE uri LIKE $1 || '%'
              AND uri != $1
              AND account_id = $2
              AND status = 'active'
        """, prefix, self.store.current_ctx.account_id)
        return [r['child'] for r in rows]

    async def read(self, uri: str, level: str = "L1") -> str:
        """读取上下文内容

        level:
          L0 = 一句话摘要（~100 tokens）
          L1 = 概览（schema、字段说明，~2k tokens）
          L2 = 完整详情（DDL、血缘、查询模板）
        """
        lvl = ContextLevel[level]
        return await self.store.read(uri, lvl, self.store.current_ctx)

    async def grep(self, query: str, scope: str = None) -> list[dict]:
        """语义搜索（向量检索 + 精排）

        scope: 'datalake' | 'team' | 'agent' | 'user' | None(全部)
        返回按相关性排序的上下文列表，每项包含 uri + L0 摘要
        """
        results = await self.retrieval.search(query, self.store.current_ctx, scope=scope)
        return [{"uri": r.uri, "abstract": r.l0_content, "type": r.context_type} for r in results]

    async def stat(self, uri: str) -> dict:
        """查看上下文的元信息（不读内容）"""
        row = await self.store.db.fetchrow("""
            SELECT id, uri, context_type, scope, owner_space, status, version,
                   active_count, updated_at
            FROM contexts WHERE uri = $1
        """, uri)
        return dict(row) if row else {"error": "Not found"}
```

### LLM 交互示例

```
User: "帮我看看有哪些数据湖表跟销售相关"

LLM → tool_call: ls("ctx://datalake/hive/prod/")
     ← ["orders", "users", "products", "sales_daily", "refunds"]

LLM → tool_call: read("ctx://datalake/hive/prod/orders", level="L0")
     ← "orders 表 - 存储所有订单交易记录，包含订单金额、状态、时间等"

LLM → tool_call: read("ctx://datalake/hive/prod/sales_daily", level="L0")
     ← "sales_daily 表 - 按天汇总的销售数据，包含日期、渠道、金额"

LLM → tool_call: read("ctx://datalake/hive/prod/orders", level="L1")
     ← "## Schema\n| 字段 | 类型 | 说明 |\n| order_id | BIGINT | 订单ID..."

LLM: "有两张相关表：orders（订单明细，可按时间范围和状态筛选）
      和 sales_daily（日汇总，适合趋势分析）。建议用 sales_daily 做月度统计。"
```

LLM 觉得自己在浏览文件系统，实际上每一步都是 PG 查询。

### 注册为 function calling 工具

```python
# FastAPI 端点 + OpenAI/Claude function calling schema
CONTEXT_TOOLS_SCHEMA = [
    {
        "name": "ls",
        "description": "列出 ctx:// 路径下的子项。类似文件系统的 ls 命令。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "ctx:// 路径，如 ctx://datalake/hive/prod/"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "read",
        "description": "读取上下文内容。level=L0 看摘要，L1 看概览，L2 看完整详情。",
        "parameters": {
            "type": "object",
            "properties": {
                "uri":   {"type": "string", "description": "上下文 URI，如 ctx://datalake/hive/prod/orders"},
                "level": {"type": "string", "enum": ["L0", "L1", "L2"], "default": "L1"}
            },
            "required": ["uri"]
        }
    },
    {
        "name": "grep",
        "description": "语义搜索上下文。输入自然语言查询，返回最相关的上下文列表。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询，如 '月度销售额统计'"},
                "scope": {"type": "string", "enum": ["datalake", "team", "agent", "user"],
                          "description": "限定搜索范围（可选）"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "stat",
        "description": "查看上下文的元信息：类型、状态、版本、热度等。不返回内容。",
        "parameters": {
            "type": "object",
            "properties": {
                "uri": {"type": "string", "description": "上下文 URI"}
            },
            "required": ["uri"]
        }
    }
]
```
