# Task: ContextHub Phase 0 + Phase 1 基础设施

你正在从零构建 ContextHub 项目的基础设施层。这是第一个实现任务，后续所有模块都依赖你的产出。

## 1. 项目背景（一句话）

ContextHub 是面向 toB 多 Agent 协作的上下文管理中间件，底层用 PostgreSQL + pgvector 统一存储。

## 2. 你的交付物

按以下顺序创建文件：

### 2.1 项目骨架
- `pyproject.toml` — Python 3.12+, 依赖: fastapi, uvicorn, asyncpg, pgvector, alembic, pydantic, pydantic-settings, httpx (dev)
- `docker-compose.yml` — PostgreSQL 16 + pgvector 扩展, 端口 5432, 数据库名 contexthub
- `Dockerfile` — 本地开发镜像，能安装项目依赖并执行 alembic / Python 命令
- `.env.example` — DATABASE_URL, API_KEY, EMBEDDING_MODEL 等
- `alembic.ini` + `alembic/env.py` — 异步 migration 配置，使用 asyncpg
- `src/contexthub/__init__.py` — 最小 package scaffold，确保 `from contexthub...` 可导入

### 2.2 配置
- `src/contexthub/config.py` — 使用 pydantic-settings 的 Settings 类

### 2.3 数据模型（Pydantic）
- `src/contexthub/models/context.py`
- `src/contexthub/models/request.py`
- `src/contexthub/models/team.py`
- `src/contexthub/models/skill.py`
- `src/contexthub/models/memory.py`
- `src/contexthub/models/datalake.py`

### 2.4 数据库层
- `src/contexthub/db/pool.py` — asyncpg 连接池创建
- `src/contexthub/db/repository.py` — PgRepository + ScopedRepo（见下方冻结规则）

### 2.5 初始 Migration
- `alembic/versions/001_initial_schema.py` — 所有核心表 + 索引 + RLS + trigger + seed data
- migration 必须显式创建扩展：
  - `CREATE EXTENSION IF NOT EXISTS vector`
  - `CREATE EXTENSION IF NOT EXISTS pgcrypto`

## 3. 冻结规则（必须严格遵守）

### 3.1 DB 执行模型

这是整个项目最重要的约束。所有后续模块都依赖这个模式：

```python
class ScopedRepo:
    """Request-scoped 数据库执行器。所有 SQL 都必须通过它执行。"""
    def __init__(self, conn: asyncpg.Connection):
        self._conn = conn

    async def fetch(self, sql: str, *args) -> list[asyncpg.Record]: ...
    async def fetchrow(self, sql: str, *args) -> asyncpg.Record | None: ...
    async def fetchval(self, sql: str, *args) -> Any: ...
    async def execute(self, sql: str, *args) -> str: ...

class PgRepository:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    @asynccontextmanager
    async def session(self, account_id: str) -> AsyncIterator[ScopedRepo]:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL app.account_id = $1", account_id)
                yield ScopedRepo(conn)
```

冻结规则：
- `SET LOCAL app.account_id` 只允许在 `PgRepository.session()` 内执行
- 任何 service/store 禁止自行调用 `pool.acquire()`
- middleware 不执行任何 SQL

### 3.2 租户隔离

- `contexts.uri` 唯一性: `UNIQUE (account_id, uri)`，不是全局唯一
- `teams.path` 唯一性: `UNIQUE (account_id, path)`
- 所有面向 Agent/用户请求的表启用 RLS: `contexts`, `teams`, `skill_subscriptions`
- 例外: `change_events` 不启用 RLS（传播引擎需跨租户扫描）
- RLS 策略: `USING (account_id = current_setting('app.account_id'))`

### 3.3 类型系统

`context_type` 只有 4 个值: `table_schema`, `skill`, `memory`, `resource`
`scope` 只有 4 个值: `datalake`, `team`, `agent`, `user`
`contexts.status` 只有 5 个值: `active`, `stale`, `archived`, `deleted`, `pending_review`
`skill_versions.status` 只有 3 个值: `draft`, `published`, `deprecated`

## 4. 完整表结构

### contexts 表
```sql
CREATE TABLE contexts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    uri             TEXT NOT NULL,
    context_type    TEXT NOT NULL CHECK (context_type IN ('table_schema', 'skill', 'memory', 'resource')),
    scope           TEXT NOT NULL CHECK (scope IN ('datalake', 'team', 'agent', 'user')),
    owner_space     TEXT,
    account_id      TEXT NOT NULL,
    l0_content      TEXT,
    l1_content      TEXT,
    l2_content      TEXT,
    file_path       TEXT,
    status          TEXT DEFAULT 'active' CHECK (status IN ('active', 'stale', 'archived', 'deleted', 'pending_review')),
    version         INT DEFAULT 1,
    tags            TEXT[],
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    last_accessed_at TIMESTAMPTZ DEFAULT NOW(),
    stale_at        TIMESTAMPTZ,
    archived_at     TIMESTAMPTZ,
    deleted_at      TIMESTAMPTZ,
    active_count    INT DEFAULT 0,
    adopted_count   INT DEFAULT 0,
    ignored_count   INT DEFAULT 0,
    l0_embedding    vector(1536),
    UNIQUE (account_id, uri)
);

ALTER TABLE contexts ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON contexts
    USING (account_id = current_setting('app.account_id'));

CREATE INDEX idx_contexts_scope ON contexts (scope, context_type);
CREATE INDEX idx_contexts_owner ON contexts (account_id, owner_space);
CREATE INDEX idx_contexts_status ON contexts (status) WHERE status != 'deleted';
CREATE INDEX idx_contexts_l0_embedding ON contexts
    USING hnsw (l0_embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

### dependencies 表
```sql
CREATE TABLE dependencies (
    id              SERIAL PRIMARY KEY,
    dependent_id    UUID NOT NULL REFERENCES contexts(id),
    dependency_id   UUID NOT NULL REFERENCES contexts(id),
    dep_type        TEXT NOT NULL CHECK (dep_type IN ('skill_version', 'table_schema', 'derived_from')),
    pinned_version  INT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (dependent_id, dependency_id, dep_type)
);

CREATE INDEX idx_deps_dependency ON dependencies (dependency_id);
CREATE INDEX idx_deps_dependent ON dependencies (dependent_id);
```

### change_events 表（不启用 RLS）
```sql
CREATE TABLE change_events (
    event_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp       TIMESTAMPTZ DEFAULT NOW(),
    context_id      UUID NOT NULL REFERENCES contexts(id),
    account_id      TEXT NOT NULL,
    change_type     TEXT NOT NULL CHECK (change_type IN ('created', 'modified', 'deleted', 'version_published', 'marked_stale')),
    actor           TEXT NOT NULL,
    diff_summary    TEXT,
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

CREATE INDEX idx_events_ready ON change_events (next_retry_at, timestamp)
    WHERE delivery_status IN ('pending', 'retry');
CREATE INDEX idx_events_processing ON change_events (claimed_at)
    WHERE delivery_status = 'processing';
CREATE INDEX idx_events_context ON change_events (context_id);
```

### change_events trigger
```sql
CREATE OR REPLACE FUNCTION notify_change_event() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('context_changed', NEW.context_id::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_change_events_notify
AFTER INSERT ON change_events
FOR EACH ROW EXECUTE FUNCTION notify_change_event();
```

### teams + team_memberships 表
```sql
CREATE TABLE teams (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    path        TEXT NOT NULL,
    parent_id   UUID REFERENCES teams(id),
    display_name TEXT,
    account_id  TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (account_id, path)
);

CREATE INDEX idx_teams_parent ON teams (parent_id);
CREATE INDEX idx_teams_account ON teams (account_id);

ALTER TABLE teams ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON teams
    USING (account_id = current_setting('app.account_id'));

CREATE TABLE team_memberships (
    agent_id    TEXT NOT NULL,
    team_id     UUID NOT NULL REFERENCES teams(id),
    role        TEXT DEFAULT 'member' CHECK (role IN ('member', 'admin')),
    access      TEXT DEFAULT 'read_write' CHECK (access IN ('read_write', 'read_only')),
    is_primary  BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (agent_id, team_id)
);
```

### skill_versions + skill_subscriptions 表
```sql
CREATE TABLE skill_versions (
    skill_id        UUID NOT NULL REFERENCES contexts(id),
    version         INT NOT NULL,
    content         TEXT NOT NULL,
    changelog       TEXT,
    is_breaking     BOOLEAN DEFAULT FALSE,
    status          TEXT DEFAULT 'draft' CHECK (status IN ('draft', 'published', 'deprecated')),
    published_by    TEXT,
    published_at    TIMESTAMPTZ,
    PRIMARY KEY (skill_id, version)
);

CREATE TABLE skill_subscriptions (
    id              SERIAL PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    skill_id        UUID NOT NULL REFERENCES contexts(id),
    pinned_version  INT,
    account_id      TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (agent_id, skill_id)
);

CREATE INDEX idx_subs_skill ON skill_subscriptions (skill_id);
CREATE INDEX idx_subs_agent ON skill_subscriptions (agent_id);

ALTER TABLE skill_subscriptions ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON skill_subscriptions
    USING (account_id = current_setting('app.account_id'));
```

### Carrier-specific 表（数据湖）
```sql
CREATE TABLE table_metadata (
    context_id      UUID PRIMARY KEY REFERENCES contexts(id),
    catalog         TEXT NOT NULL,
    database_name   TEXT NOT NULL,
    table_name      TEXT NOT NULL,
    ddl             TEXT,
    partition_info  JSONB,
    stats           JSONB,
    sample_data     JSONB,
    stats_updated_at TIMESTAMPTZ
);

CREATE TABLE lineage (
    upstream_id     UUID NOT NULL REFERENCES contexts(id),
    downstream_id   UUID NOT NULL REFERENCES contexts(id),
    transform_type  TEXT,
    description     TEXT,
    PRIMARY KEY (upstream_id, downstream_id)
);

CREATE TABLE table_relationships (
    table_id_a      UUID NOT NULL REFERENCES contexts(id),
    table_id_b      UUID NOT NULL REFERENCES contexts(id),
    join_type       TEXT,
    join_columns    JSONB NOT NULL,
    confidence      FLOAT DEFAULT 1.0,
    PRIMARY KEY (table_id_a, table_id_b)
);

CREATE TABLE query_templates (
    id              SERIAL PRIMARY KEY,
    context_id      UUID NOT NULL REFERENCES contexts(id),
    sql_template    TEXT NOT NULL,
    description     TEXT,
    hit_count       INT DEFAULT 0,
    last_used_at    TIMESTAMPTZ,
    created_by      TEXT
);

CREATE INDEX idx_qt_context ON query_templates (context_id);
```

### Seed Data（验证场景用）
```sql
-- 租户
-- account_id = 'acme' 用于所有验证场景

-- 团队层级
INSERT INTO teams (id, path, parent_id, display_name, account_id) VALUES
  ('00000000-0000-0000-0000-000000000001', '', NULL, '全组织', 'acme'),
  ('00000000-0000-0000-0000-000000000002', 'engineering', '00000000-0000-0000-0000-000000000001', '工程部', 'acme'),
  ('00000000-0000-0000-0000-000000000003', 'engineering/backend', '00000000-0000-0000-0000-000000000002', '后端组', 'acme'),
  ('00000000-0000-0000-0000-000000000004', 'data', '00000000-0000-0000-0000-000000000001', '数据部', 'acme'),
  ('00000000-0000-0000-0000-000000000005', 'data/analytics', '00000000-0000-0000-0000-000000000004', '数据分析组', 'acme');

-- Agent 归属
INSERT INTO team_memberships (agent_id, team_id, role, is_primary) VALUES
  ('query-agent', '00000000-0000-0000-0000-000000000003', 'member', TRUE),
  ('analysis-agent', '00000000-0000-0000-0000-000000000005', 'member', TRUE),
  ('analysis-agent', '00000000-0000-0000-0000-000000000002', 'member', FALSE);
```

## 5. Pydantic 模型要求

`models/context.py`:
- `ContextLevel` enum: L0, L1, L2
- `ContextType` enum: table_schema, skill, memory, resource
- `Scope` enum: datalake, team, agent, user
- `ContextStatus` enum: active, stale, archived, deleted, pending_review
- `Context` dataclass/model 对应 contexts 表
- `CreateContextRequest`, `UpdateContextRequest` 请求模型

`models/request.py`:
- `RequestContext` dataclass: account_id, agent_id, expected_version

`models/team.py`:
- `Team`, `TeamMembership` 模型

`models/skill.py`:
- `SkillVersionStatus` enum: draft, published, deprecated
- `SkillVersion`, `SkillSubscription` 模型

`models/memory.py`:
- `PromoteRequest` 模型

`models/datalake.py`:
- `TableMetadata`, `Lineage`, `TableRelationship`, `QueryTemplate` 模型

## 6. 不要做的事

- 不要创建 `access_policies` 或 `audit_log` 表
- 不要实现任何 service 或 API router（那是后续 Task 的工作）
- 不要添加 plan 中没有的字段或表
- 不要引入 SQLAlchemy ORM（我们直接用 asyncpg raw SQL）
- 不要在 models 里写数据库操作逻辑

## 7. 验证标准

完成后，以下命令应该能成功：
1. `docker-compose up -d` 启动 PG
2. `alembic upgrade head` 创建所有表 + seed data
3. 手动连接 PG 验证: `vector` / `pgcrypto` 扩展存在，表存在、RLS 策略存在、索引存在、trigger 存在、seed data 已写入
4. 代码检查验证执行模型冻结规则：
   - `SET LOCAL app.account_id` 只出现在 `PgRepository.session()` 中
   - 除 `PgRepository.session()` 外，项目中没有其他地方直接调用 `pool.acquire()`
   - middleware 不执行任何 SQL
5. `python -c "from contexthub.db.repository import PgRepository, ScopedRepo"` 能 import
6. `python -c "from contexthub.models.context import ContextType, Scope, ContextLevel"` 能 import
7. `python -c "import contexthub; print(contexthub.__file__)"` 能成功运行
