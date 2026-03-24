# 10 — 代码架构设计

基于 `00a-canonical-invariants.md` 和 `01-09` 的冻结决议，本文档把实现边界、依赖方向、请求级数据库执行模型、API 分层和实施顺序写成唯一的 canonical 版本。

本文件的目标只有一个：实现者拿到文档后，不再需要自己做任何产品级架构决策。

---

## 一、实现边界与默认假设

- **MVP Core**：验证“私有写入 → 晋升共享 → 跨 Agent 复用 → Skill 更新 → 下游 stale / advisory 感知 → retry/recovery”这条横向闭环。
- **Carrier-Specific**：数据湖元数据、`sql-context` 组装、企业数据分析 / Text-to-SQL demo 是首个垂直载体，用来制造可复现的共享、版本和传播场景，但不主导 MVP 的主实施顺序。
- **Post-MVP Reserved**：显式 ACL allow/deny/mask、审计日志、反馈闭环、生命周期管理，以及对应路由与表结构，全部明确后置，不进入初始代码骨架和初始 migration。触发条件与 owner 见 `14-adr-backlog-register.md`。

---

## 二、项目结构

### 2.1 MVP Core + Carrier-Specific 骨架

```text
contexthub/
├── pyproject.toml
├── alembic.ini
├── alembic/
│   ├── env.py
│   └── versions/
│       └── 001_initial_schema.py
├── src/
│   └── contexthub/
│       ├── __init__.py
│       ├── main.py
│       ├── config.py
│       │
│       ├── models/
│       │   ├── context.py
│       │   ├── request.py
│       │   ├── team.py
│       │   ├── skill.py
│       │   ├── memory.py
│       │   └── datalake.py              # carrier-specific
│       │
│       ├── db/
│       │   ├── pool.py
│       │   ├── repository.py            # PgRepository.session() + ScopedRepo
│       │   └── queries/
│       │       ├── contexts.py
│       │       ├── dependencies.py
│       │       ├── events.py
│       │       ├── skills.py
│       │       ├── teams.py
│       │       └── datalake.py          # carrier-specific
│       │
│       ├── store/
│       │   └── context_store.py         # read/write/ls/stat（不含 search）
│       │
│       ├── services/
│       │   ├── acl_service.py           # 默认可见性 / 默认写权限
│       │   ├── context_service.py       # 通用 CRUD 编排
│       │   ├── memory_service.py        # promote / derived_from / 团队共享
│       │   ├── skill_service.py         # publish / subscribe / read_resolved
│       │   ├── retrieval_service.py     # 唯一 search owner
│       │   ├── indexer_service.py       # L0/L1 生成 + embedding 更新
│       │   ├── propagation_engine.py    # change_events 消费 / retry / sweep
│       │   ├── catalog_sync_service.py  # carrier-specific
│       │   └── reconciler_service.py    # carrier-specific：embedding 补写
│       │
│       ├── propagation/
│       │   ├── base.py
│       │   ├── skill_dep_rule.py
│       │   ├── subscription_notify_rule.py
│       │   ├── table_schema_rule.py
│       │   ├── derived_memory_rule.py
│       │   ├── complex_rule.py
│       │   └── registry.py
│       │
│       ├── retrieval/
│       │   ├── router.py
│       │   ├── vector_strategy.py
│       │   ├── rerank.py
│       │   ├── tree_strategy.py         # phase 2 optional
│       │   └── keyword_strategy.py      # phase 2 optional
│       │
│       ├── generation/
│       │   ├── base.py
│       │   ├── table_schema.py
│       │   ├── skill.py
│       │   ├── memory.py
│       │   └── resource.py
│       │
│       ├── connectors/
│       │   ├── base.py                  # carrier-specific
│       │   └── mock_connector.py        # carrier-specific
│       │
│       ├── llm/
│       │   ├── base.py
│       │   ├── openai_client.py
│       │   └── factory.py
│       │
│       └── api/
│           ├── deps.py
│           ├── middleware.py            # 认证 / RequestContext 注入；不执行 SET LOCAL
│           └── routers/
│               ├── contexts.py
│               ├── search.py
│               ├── memories.py
│               ├── skills.py
│               ├── tools.py
│               └── datalake.py          # carrier-specific
│
├── sdk/
│   ├── pyproject.toml
│   └── src/contexthub_sdk/
│       ├── __init__.py
│       ├── client.py
│       ├── models.py
│       └── exceptions.py
│
├── plugins/
│   └── openclaw/
│       ├── pyproject.toml
│       ├── plugin.py
│       └── tools.py                     # search / store / promote / skill_publish
│
└── tests/
    ├── conftest.py
    ├── test_visibility.py
    ├── test_memory_promote.py
    ├── test_skill_resolution.py
    ├── test_retrieval.py
    └── test_propagation.py
```

### 2.2 Post-MVP Reserved（不进入初始 skeleton / 初始 migration）

- `models/access.py`
- `services/audit_service.py`
- `services/feedback_service.py`
- `services/lifecycle_service.py`
- `api/routers/feedback.py`
- `api/routers/admin.py`
- `db/queries/access.py`
- `db/queries/audit.py`
- `db/queries/feedback.py`
- `db/queries/lifecycle.py`
- 对应 PG 表：`access_policies`、`audit_log`、`context_feedback`、`lifecycle_policies`

模块边界原则：
- `store/` 只负责 URI 路由、默认读写权限检查后的 `read/write/ls/stat`，**不承担 search**
- `services/retrieval_service.py` 是唯一检索入口，统一承载 `/search`、tool `grep` 和 carrier-specific `sql-context`
- `db/` 只提供 request-scoped `ScopedRepo` 执行能力和 SQL 常量，不做业务判断
- `api/` 只做协议转换、身份解析、依赖注入，不在 middleware 中做 `SET LOCAL`
- `sdk/` 只封装 HTTP + typed models，不依赖 server 内部模块
- `plugins/` 只做 OpenClaw 适配，不持有产品级语义

---

## 三、模块边界与 Canonical Owner

| 模块 | Canonical Owner | 明确不负责 |
|------|-----------------|------------|
| `ContextStore` | `ctx://` URI 到 PG 行的 `read/write/ls/stat` 路由；乐观锁；默认 ACL 检查后的读写落库 | 不做 search；不做版本解析；不自行开连接 |
| `RetrievalService` | 唯一 search owner；embedding、pgvector 检索、L1 rerank、默认可见性过滤；carrier-specific `sql-context` 组装 | 不负责通用写入；不负责 Skill 版本解析 |
| `SkillService` | Skill 版本发布、订阅、`read_resolved()` 版本解析 | 不负责通用 search；不持有传播 outbox |
| `MemoryService` | promote、`derived_from` 注册、团队共享路径写入 | 不负责审核流；不负责 audit_log |
| `ACLService` | MVP 的默认可见性 / 默认写权限；明确后置 backlog 的显式 ACL overlay 挂载点（见 `14-adr-backlog-register.md`） | 不持有 canonical truth；不自行开连接 |
| `PropagationEngine` | `change_events` 消费、delivery 状态机、retry / sweep / 幂等副作用调度 | 不负责业务内容生成本身；不依赖通知表 |
| `PgRepository` | request-scoped `ScopedRepo` session factory | 不是全局 SQL executor；不允许跨请求复用事务上下文 |
| `SDK` | 对外 typed client；把 `ctx.read()` / `ctx.search()` / `ctx.memory.promote()` 映射到 HTTP API | 不实现服务端语义 |
| `OpenClaw Plugin` | 注册 tools；`assemble` 注入 recall；`afterTurn` auto-capture；委托 compaction | 不在插件层重复实现版本解析、传播或 ACL 规则 |

Canonical truth 归属：
- `contexts` / `dependencies` / `change_events` / `skill_versions` / `skill_subscriptions` 的事实源都在 PG
- `SkillService` 是唯一版本解析 owner
- `RetrievalService` 是唯一检索 owner
- `PropagationEngine` 是唯一传播消费 owner
- `ContextStore` 是唯一 URI 文件语义 owner（仅限 `read/write/ls/stat`）

依赖方向：

```text
routers
  ├── contexts / memories / skills -> ContextService / MemoryService / SkillService
  │                                   -> ContextStore -> ScopedRepo
  ├── search / tools.grep           -> RetrievalService -> retrieval strategies -> ScopedRepo
  └── datalake                      -> CatalogSyncService / RetrievalService -> ScopedRepo

ContextService / MemoryService / SkillService
  -> ACLService / ContextStore / IndexerService

PropagationEngine
  -> PgRepository.session(account_id) -> rules -> ContextStore / SkillService / IndexerService
```

---

## 四、数据库执行模型（强冻结）

### 4.1 唯一允许的模式

每个 HTTP 请求、每个传播事件、每次租户 sweep，都必须显式创建一个 request-scoped / work-item-scoped `ScopedRepo`。所有 SQL 都在这个 `ScopedRepo` 上执行。

唯一 canonical 形态：

```python
@dataclass
class RequestContext:
    account_id: str
    agent_id: str
    expected_version: int | None = None


class ScopedRepo:
    def __init__(self, conn: asyncpg.Connection):
        self._conn = conn

    async def fetch(self, sql: str, *args): ...
    async def fetchrow(self, sql: str, *args): ...
    async def fetchval(self, sql: str, *args): ...
    async def execute(self, sql: str, *args): ...


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
- `BEGIN + SET LOCAL app.account_id + COMMIT/ROLLBACK` 只允许在 `PgRepository.session(account_id)` 内执行
- request 内所有 SQL 必须走同一个 `ScopedRepo`
- `ContextStore`、`RetrievalService`、`SkillService`、`ACLService`、`IndexerService` 等内部模块**禁止**自己调用 `pool.acquire()` 或 `repo.session()`
- middleware **不执行**任何 RLS SQL；它只做认证和 `RequestContext` 注入
- `LISTEN` 长连接和业务 SQL 连接分离；`LISTEN` 连接不承担租户查询

### 4.2 Request 级依赖注入

```python
# api/deps.py
async def get_request_context(
    x_account_id: str = Header(..., alias="X-Account-Id"),
    x_agent_id: str = Header(..., alias="X-Agent-Id"),
    if_match: int | None = Header(None, alias="If-Match"),
) -> RequestContext:
    return RequestContext(
        account_id=x_account_id,
        agent_id=x_agent_id,
        expected_version=if_match,
    )


async def get_db(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
) -> AsyncIterator[ScopedRepo]:
    async with request.app.state.repo.session(ctx.account_id) as db:
        yield db
```

HTTP 路径的唯一调用方式：

```python
@router.post("/contexts", status_code=201)
async def create_context(
    body: CreateContextRequest,
    ctx: RequestContext = Depends(get_request_context),
    db: ScopedRepo = Depends(get_db),
    svc: ContextService = Depends(get_context_service),
):
    return await svc.create(db, body, ctx)
```

### 4.3 后台任务的唯一调用方式

```python
class PropagationEngine:
    async def process_event(self, event: ChangeEvent):
        async with self.repo.session(event.account_id) as db:
            await self._claim_event(db, event.id)
            await self._dispatch_rules(db, event)
            await self._finish_event(db, event.id)


class ReconcilerService:
    async def reconcile_account(self, account_id: str):
        async with self.repo.session(account_id) as db:
            ...
```

后台任务冻结规则：
- 一个 event / 一次租户 sweep / 一次 catalog sync 处理，对应一个显式 `ScopedRepo`
- worker 不能持有跨租户复用的事务上下文
- `change_events` 的 `account_id` 必须足以驱动 tenant-scoped session 选择

---

## 五、依赖注入与服务装配

所有 service 在 lifespan 中一次性装配；DB session 不在构造函数中注入，而由 `get_db` 依赖在请求级提供。

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()

    pool = await create_pool(settings)
    repo = PgRepository(pool)
    llm_client = create_llm_client(settings)
    embedding_client = create_embedding_client(settings)
    catalog_connector = create_catalog_connector(settings)

    acl_service = ACLService()
    content_generator = ContentGenerator(llm_client)
    context_store = ContextStore(acl_service)
    indexer_service = IndexerService(content_generator, embedding_client)
    retrieval_router = RetrievalRouter.default()
    retrieval_service = RetrievalService(retrieval_router, embedding_client, acl_service)

    context_service = ContextService(context_store, indexer_service)
    memory_service = MemoryService(context_store, indexer_service)
    skill_service = SkillService(context_store, indexer_service)
    catalog_sync_service = CatalogSyncService(catalog_connector, indexer_service, llm_client)

    rule_registry = PropagationRuleRegistry.default(indexer_service, llm_client)
    propagation_engine = PropagationEngine(repo, rule_registry)

    app.state.repo = repo
    app.state.context_service = context_service
    app.state.memory_service = memory_service
    app.state.skill_service = skill_service
    app.state.retrieval_service = retrieval_service
    app.state.catalog_sync_service = catalog_sync_service

    if settings.propagation_enabled:
        await propagation_engine.start()

    yield

    await propagation_engine.stop()
    await pool.close()
```

这里的关键不是“服务能拿到 repo”，而是：
- 请求 SQL 一律通过 `get_db` 提供的 `ScopedRepo`
- 后台 worker SQL 一律通过 `repo.session(account_id)` 临时获取 `ScopedRepo`
- 不存在“全局 repo 直接执行请求 SQL”的路径

### 5.1 核心接口冻结

```python
class ContextStore:
    async def read(self, db: ScopedRepo, uri: str, level: ContextLevel, ctx: RequestContext) -> str: ...
    async def write(self, db: ScopedRepo, uri: str, level: ContextLevel, content: str, ctx: RequestContext) -> None: ...
    async def ls(self, db: ScopedRepo, path: str, ctx: RequestContext) -> list[str]: ...
    async def stat(self, db: ScopedRepo, uri: str, ctx: RequestContext) -> ContextStat: ...


class RetrievalService:
    async def search(self, db: ScopedRepo, request: SearchRequest, ctx: RequestContext) -> SearchResponse: ...


class SkillService:
    async def read_resolved(
        self,
        db: ScopedRepo,
        skill_id: UUID,
        agent_id: str,
        requested_version: int | None = None,
    ) -> SkillContent: ...
```

### 5.2 Skill 版本解析冻结

```python
class SkillService:
    async def read_resolved(
        self,
        db: ScopedRepo,
        skill_id: UUID,
        agent_id: str,
        requested_version: int | None = None,
    ) -> SkillContent:
        if requested_version is not None:
            return await self._read_version(db, skill_id, requested_version)

        sub = await db.fetchrow(
            "SELECT pinned_version FROM skill_subscriptions "
            "WHERE skill_id = $1 AND agent_id = $2",
            skill_id, agent_id,
        )

        if sub and sub["pinned_version"] is not None:
            content = await self._read_version(db, skill_id, sub["pinned_version"])
            latest_ver = await db.fetchval(
                "SELECT MAX(version) FROM skill_versions "
                "WHERE skill_id = $1 AND status = 'published'",
                skill_id,
            )
            if latest_ver and latest_ver > sub["pinned_version"]:
                content.advisory = (
                    f"v{latest_ver} 已发布，当前 pin 在 v{sub['pinned_version']}"
                )
            return content

        return await self._read_latest(db, skill_id)
```

这意味着：
- URI 默认返回 latest published
- pinned 是读路径视角，不是新 URI
- `SkillService` 是唯一版本解析 owner；`ContextStore` 不做版本判断

---

## 六、FastAPI API 分层

### 6.1 MVP Core

| 方法 | 路径 | Owner | 说明 |
|------|------|-------|------|
| `POST` | `/api/v1/contexts` | `ContextService` | 创建上下文 |
| `GET` | `/api/v1/contexts/{uri:path}` | `ContextStore` / `SkillService` | 普通 context 走 `ContextStore.read()`；Skill 走 `SkillService.read_resolved()` |
| `PATCH` | `/api/v1/contexts/{uri:path}` | `ContextService` | 更新内容 |
| `DELETE` | `/api/v1/contexts/{uri:path}` | `ContextService` | 标记删除 |
| `GET` | `/api/v1/contexts/{uri:path}/stat` | `ContextStore` | 元信息 |
| `GET` | `/api/v1/contexts/{uri:path}/children` | `ContextStore` | `ls` 语义 |
| `GET` | `/api/v1/contexts/{uri:path}/deps` | `ContextService` | 查看依赖 |
| `POST` | `/api/v1/search` | `RetrievalService` | 唯一通用 search 入口 |
| `POST` | `/api/v1/memories` | `MemoryService` | 添加记忆 |
| `GET` | `/api/v1/memories` | `MemoryService` | 列记忆 |
| `POST` | `/api/v1/memories/promote` | `MemoryService` | promote 到团队共享 |
| `POST` | `/api/v1/skills/versions` | `SkillService` | 发布 Skill 新版本 |
| `GET` | `/api/v1/skills/{uri:path}/versions` | `SkillService` | 版本历史 |
| `POST` | `/api/v1/skills/subscribe` | `SkillService` | 订阅 Skill |
| `POST` | `/api/v1/tools/ls` | `ContextStore` | tool use 包装 |
| `POST` | `/api/v1/tools/read` | `ContextStore` | tool use 包装 |
| `POST` | `/api/v1/tools/grep` | `RetrievalService` | tool `grep` -> search |
| `POST` | `/api/v1/tools/stat` | `ContextStore` | tool use 包装 |

### 6.2 Carrier-Specific

| 方法 | 路径 | Owner | 说明 |
|------|------|-------|------|
| `POST` | `/api/v1/search/sql-context` | `RetrievalService` | 仅为首个垂直载体服务的上下文组装 |
| `POST` | `/api/v1/datalake/sync` | `CatalogSyncService` | 触发 catalog 同步 |
| `GET` | `/api/v1/datalake/{catalog}/{db}` | `CatalogSyncService` | 列表 |
| `GET` | `/api/v1/datalake/{catalog}/{db}/{table}` | `RetrievalService` | 表完整上下文 |
| `GET` | `/api/v1/datalake/{catalog}/{db}/{table}/lineage` | `RetrievalService` | 血缘查询 |

### 6.3 Post-MVP Reserved

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/v1/feedback` | 显式/隐式反馈上报 |
| `GET/POST` | `/api/v1/admin/*` | 质量报告、生命周期、管理运维接口 |

认证方式：
- `X-API-Key`
- `X-Account-Id`
- `X-Agent-Id`
- `If-Match`

响应通过 `ETag` 返回当前版本号。版本不匹配返回 `409 Conflict`。

---

## 七、检索、生成与传播

### 7.1 检索唯一走 `RetrievalService`

向量检索直接在 PG 上执行，不引入独立向量存储抽象层。`RetrievalService` 的 canonical 流程：

```text
query
  -> EmbeddingClient.embed(query)
  -> pgvector 检索 L0 + 标量过滤
  -> 批量读取候选 L1
  -> rerank
  -> ACLService.filter_visible()
  -> 按需加载 L2 / 结构化补充数据
```

冻结规则：
- `ContextStore` 不提供 `search()`
- tool `grep`、API `/search`、carrier-specific `/search/sql-context` 都由 `RetrievalService` 统一承接
- 默认可见性过滤属于检索结果裁剪的一部分，因此放在 `RetrievalService` 路径上，而不是塞回 `ContextStore`

### 7.2 L0/L1 生成

```python
class ContentGenerator:
    async def generate(
        self,
        context_type: str,
        raw_content: str,
        metadata: dict,
    ) -> GeneratedContent: ...


@dataclass
class GeneratedContent:
    l0: str
    l1: str
    llm_tokens_used: int
```

| context_type | L0 生成 | L1 生成 | LLM 调用 |
|---|---|---|---|
| `table_schema` | DDL/注释 -> 一句话业务描述 | schema 表格 + 查询模式建议 | 是 |
| `skill` | 标题 + 首句 | 全文截断 | 否 |
| `memory` | 前 ~100 tokens | 短内容直接返回；超长压缩 | 通常否 |
| `resource` | 一句话摘要 | 结构化概览 | 是 |

### 7.3 Embedding 一致性

写入与对账流程：

```text
write/promote/publish
  -> contexts/change_events 落库
  -> IndexerService 生成 L0/L1
  -> EmbeddingClient 写回 l0_embedding
  -> ReconcilerService 定时补写缺失 embedding
```

`ReconcilerService` 虽是后台任务，但仍使用同一套 `repo.session(account_id)` 模型，不是全局 executor。

### 7.4 PropagationEngine

- `change_events` 是唯一 source of truth
- `NOTIFY` 只是 wake-up hint
- `start()` 后执行 `requeue_stuck_events()` + `sweep_ready_events(reason="startup")`
- 周期补扫兜住漏通知和 crash 窗口
- 单个依赖目标失败不阻塞其他目标，但 event 只要有失败就回到 `retry`
- 所有副作用按 `(event_id, target_id, action)` 幂等

MVP 阶段动作语义：
- `mark_stale`：直接更新 `contexts.status='stale'`
- `auto_update`：直接重算内容
- `notify`：floating 订阅者下次读取自然获得 latest，不要求通知表
- `advisory`：由 `SkillService.read_resolved()` 在 pinned 读取时返回

---

## 八、实施顺序

### Phase 0（基础设施）

1. `docker-compose.yml` + `Dockerfile` + `.env.example`
2. `pyproject.toml` + `alembic.ini`

### Phase 1（MVP Core Foundation）

1. `config.py` + `models/context.py` + `models/request.py` + `models/team.py` + `models/skill.py` + `models/memory.py`
2. `db/pool.py` + `db/repository.py`
   - 实现 `PgRepository.session(account_id) -> ScopedRepo`
   - 冻结 `SET LOCAL` 只在这里发生
3. `alembic/versions/001_initial_schema.py`
   - 仅包含 core + carrier-specific 需要的表
   - 不包含 `access_policies`、`audit_log`、`context_feedback`、`lifecycle_policies`
4. `services/acl_service.py`
   - 只实现默认可见性 / 默认写权限
5. `store/context_store.py`
   - 只实现 `read/write/ls/stat`
6. `api/middleware.py` + `api/deps.py` + `api/routers/contexts.py`
   - middleware 只做认证 / `RequestContext`
   - `get_db` 提供 request-scoped `ScopedRepo`

### Phase 2（MVP 协作闭环）

1. `services/indexer_service.py`
2. `services/memory_service.py`
   - promote
   - `derived_from` 注册
3. `services/skill_service.py`
   - publish / subscribe / `read_resolved`
4. `services/retrieval_service.py` + `retrieval/`
   - 唯一 search owner
5. `propagation/` + `services/propagation_engine.py`
   - startup sweep / periodic sweep / retry / recovery
6. `api/routers/memories.py` + `api/routers/skills.py` + `api/routers/search.py` + `api/routers/tools.py`
7. `sdk/` + `plugins/openclaw/`
   - 只接 MVP 需要的 tools：`ls/read/grep/stat/store/promote/skill_publish`

### Phase 3（Carrier-Specific）

1. `connectors/base.py` + `connectors/mock_connector.py`
2. `services/catalog_sync_service.py`
3. `api/routers/datalake.py`
4. `/api/v1/search/sql-context`
5. `services/reconciler_service.py`
6. 企业数据分析 / Text-to-SQL demo

### Phase 4（Post-MVP Reserved）

1. 显式 ACL allow/deny/mask
2. 审计日志
3. 反馈闭环
4. 生命周期管理
5. `feedback` / `admin` 路由

---

## 九、验证方式

1. **执行模型验证**
   - 任一请求只创建一个 `ScopedRepo`
   - middleware 中不存在 `SET LOCAL`
   - 任一 service/store/helper 不自行开连接
2. **MVP 闭环验证**
   - 私有写入
   - promote 到团队共享
   - 另一个 Agent 检索并复用
   - 发布 Skill 新版本
   - 依赖方 stale / advisory
   - 断开 LISTEN 或制造 lease 超时后最终恢复
3. **载体验证**
   - 数据湖元数据检索
   - `sql-context` 组装
   - 企业数据分析 demo

本文档中的默认 MVP 场景不是“自然语言 → 检索 → SQL 生成”，而是横向协作闭环；SQL 生成只是首个垂直载体里的 demo。

---

## 十、基础设施

### 10.1 初始 migration 要点

`001_initial_schema.py` 必须包含：
- `contexts`
- `dependencies`
- `change_events`
- `teams`
- `team_memberships`
- `skill_versions`
- `skill_subscriptions`
- `table_metadata` / `lineage` / `table_relationships` / `query_templates`（若启用首个垂直载体）
- RLS 策略 + 索引
- `l0_embedding vector(1536)`

初始 migration 明确不包含：
- `access_policies`
- `audit_log`
- `context_feedback`
- `lifecycle_policies`

### 10.2 认证中间件

```python
@app.middleware("http")
async def bind_request_context(request: Request, call_next):
    request.state.api_key = request.headers.get("X-API-Key")
    request.state.account_id = request.headers.get("X-Account-Id")
    request.state.agent_id = request.headers.get("X-Agent-Id")
    return await call_next(request)
```

中间件职责只到这里为止。它不执行：
- `SET LOCAL`
- `BEGIN/COMMIT`
- 任何 tenant-scoped SQL

这些都只允许在 `PgRepository.session(account_id)` 内发生。
