# 08 — 系统架构

## 架构图

```text
┌──────────────────────────────────────────────────────────────┐
│                 OpenClaw（单实例，多 Agent 身份）              │
│                                                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐           │
│  │ 查询 Agent│  │ 分析 Agent│  │ 其他业务 Agent   │           │
│  │ agent_id │  │ agent_id │  │ agent_id         │           │
│  └────┬─────┘  └────┬─────┘  └────────┬─────────┘           │
│       └──────────────┼────────────────┘                      │
│                      ▼                                        │
│            ContextHub OpenClaw Plugin                         │
│      （注册 tools + lifecycle hooks，调用 SDK）               │
│                      ▼                                        │
│               ContextHub Python SDK                           │
│          （ctx:// URI + typed HTTP client）                   │
└──────────────────────┬───────────────────────────────────────┘
                       ▼
┌──────────────────────────────────────────────────────────────┐
│               ContextHub Server (FastAPI)                     │
│                                                              │
│  routers + RequestContext + request-scoped ScopedRepo        │
│                                                              │
│  ┌────────────┐  ┌────────────┐  ┌────────────────┐         │
│  │ Context    │  │ Memory     │  │ Skill          │         │
│  │ Service    │  │ Service    │  │ Service         │         │
│  └─────┬──────┘  └─────┬──────┘  └──────┬─────────┘         │
│        │               │                │                    │
│  ┌─────┴───────────────┴────────────────┴──────────┐        │
│  │              Core Engine                         │        │
│  │  ┌────────────┐ ┌───────────┐ ┌──────────────┐  │        │
│  │  │ Retrieval  │ │ Indexer   │ │ Propagation  │  │        │
│  │  │ Service    │ │ Service   │ │ Engine       │  │        │
│  │  └────────────┘ └───────────┘ └──────────────┘  │        │
│  │  ┌────────────┐                                  │        │
│  │  │ ACL        │  Post-MVP Reserved:              │        │
│  │  │ Service    │  Audit / Feedback / Lifecycle    │        │
│  │  └────────────┘  (见 14-adr-backlog-register.md) │        │
│  └─────────────────────┬───────────────────────────┘        │
│                        ▼                                     │
│  ┌──────────────────────────────────────────────────┐       │
│  │         ContextStore（URI 路由层）                 │       │
│  │       ctx:// URI → read / write / ls / stat      │       │
│  └──────────────────────┬───────────────────────────┘       │
└──────────────────────────┼───────────────────────────────────┘
                           ▼
┌────────────────────────────────────┐   ┌─────────────────────┐
│ PostgreSQL + pgvector              │   │ CatalogConnector    │
│                                    │   │ (carrier-specific)  │
│ ┌──────────────┐ ┌───────────────┐ │   │                     │
│ │ contexts     │ │ pgvector      │ │   │ Hive/Iceberg/Mock   │
│ │ dependencies │ │               │ │   │                     │
│ │ change_events│ │ L0 embedding  │ │   │                     │
│ │ skill_version│ │ HNSW 索引     │ │   │                     │
│ │ skill_subscr.│ │               │ │   │                     │
│ │ teams        │ └───────────────┘ │   │                     │
│ │ team_members │                   │   │                     │
│ └──────────────┘                   │   │                     │
│                                    │   │                     │
│ PG 原生能力：                       │   │                     │
│ RLS / ACID / CTE / LISTEN/NOTIFY  │   │                     │
└────────────────────────────────────┘   └─────────────────────┘
```

## 关键架构约束

- `ContextStore` 只负责 `read/write/ls/stat`，不负责 search。
- `RetrievalService` 是唯一检索入口，统一承载 API `/search`、tool `grep` 和 carrier-specific `sql-context`。
- `SkillService` 同时持有 Skill 版本管理和版本解析；不存在独立的 `Version Manager`。
- `PropagationEngine` 只以 `change_events` 为 source of truth；`NOTIFY` 只是 wake-up hint。
- `ACLService` 在 MVP 只做默认可见性 / 默认写权限；显式 ACL allow/deny/mask 属于明确后置 backlog，见 `14-adr-backlog-register.md`。
- 每个请求或后台 work item 都必须显式创建一个 tenant-scoped `ScopedRepo`；middleware 不执行 `SET LOCAL`。

## SDK、API、Plugin 的职责

| 层 | 职责 | 不负责 |
|----|------|--------|
| SDK | 把 `ctx.read()`、`ctx.search()`、`ctx.memory.promote()` 等映射到 HTTP API | 不实现服务端语义 |
| API | 身份解析、协议转换、注入 request-scoped `ScopedRepo` | 不做产品规则判断 |
| Plugin | 注册 tools、`assemble` 注入 recall、`afterTurn` auto-capture、委托 compaction | 不重复实现 ACL / 版本解析 / 传播 |

## DataAgent 层说明

MVP 阶段采用单 OpenClaw 实例作为 DataAgent 运行时。ContextHub OpenClaw Plugin 注册为 **context-engine 插件**（占据 `plugins.slots.contextEngine` 槽位），采用增强型适配器模式（参考 OpenViking 新版 openclaw-plugin 的 context-engine 架构，详见 13-related-works.md）：

- Plugin 声明 `kind: "context-engine"`，通过 `api.registerContextEngine()` 注册
- Plugin 注册 ContextTools（`ls`、`read`、`grep`、`stat`）和业务工具（`contexthub_store`、`contexthub_promote` 等）
- 在 ContextEngine 的 `assemble` 方法中，通过 `systemPromptAddition` 注入来自 PG 的相关上下文（auto-recall），不修改 messages 数组
- 在 ContextEngine 的 `afterTurn` 方法中，提取记忆写入 PG（auto-capture）
- 在 ContextEngine 的 `compact` 方法中，委托给 OpenClaw 内置 LegacyContextEngine（不声明 `ownsCompaction`）
- 多 Agent 协作通过 SDK 调用时切换 `agent_id` 参数实现，ContextHub Server 端按 `agent_id` 做隔离和协作
- 协作逻辑（传播、ACL、记忆晋升）全部在 Server 端闭环，不依赖多个 Agent 运行时实例

### OpenClaw 插件架构决策

基于对 lossless-claw 和 OpenViking 两个 OpenClaw context-engine 插件的分析（详见 13-related-works.md），ContextHub 的 OpenClaw 插件采用以下架构：

**1. 增强型适配器，而非完整 ContextEngine**

ContextHub 的核心价值是企业上下文管理（数据湖元数据、团队记忆、Skills、权限治理），不是对话历史压缩。因此：
- `assemble`：透传 messages（不修改对话历史），通过 `systemPromptAddition` 注入 PG 上下文
- `compact`：委托给 OpenClaw 内置引擎或 lossless-claw（不声明 `ownsCompaction`）
- `afterTurn`：唯一的主动逻辑——提取记忆写入 PG
- `ingest` / `ingestBatch`：空操作

**2. 上下文注入通道选择**

使用 `AssembleResult.systemPromptAddition` 而非修改 messages 数组。原因：
- PG 上下文（auto-recall 结果、数据湖元数据）应每轮动态生成，不应进入对话历史
- 如果注入到 messages 中，compaction 引擎会将其当作普通对话消息压缩，导致上下文失真
- `systemPromptAddition` 进入系统提示，与对话历史隔离，compaction 不会触碰

**3. Compaction 委托**

ContextHub 不管理对话历史压缩。compact 方法内部尝试动态 import OpenClaw 的 LegacyContextEngine 进行委托（与 OpenViking 新版的 `tryLegacyCompact()` 模式一致）。如果未来需要更高质量的压缩，可选择将 lossless-claw 的 compaction 算法作为内部依赖集成。

**4. 与 lossless-claw 的共存**

默认场景不需要共存——ContextHub 占据 contextEngine slot，compact 委托给 Legacy 引擎。如果用户同时需要 lossless-claw 的 DAG 无损压缩能力，采用方案 A：ContextHub 内部集成 lossless-claw 的压缩组件作为依赖，在 compact 方法中调用其 compaction 算法替代 Legacy 引擎。

## 核心模块职责

| 模块 | 职责 | MVP 状态 |
|------|------|----------|
| `ContextStore` | URI 路由：`ctx://` -> `read/write/ls/stat` | MVP Core |
| `RetrievalService` | embedding、pgvector 检索、L1 rerank、结果过滤 | MVP Core |
| `ContextService` | 通用 CRUD 编排 | MVP Core |
| `MemoryService` | promote、团队共享、`derived_from` 依赖注册 | MVP Core |
| `SkillService` | publish / subscribe / `read_resolved` | MVP Core |
| `PropagationEngine` | outbox drain、retry、requeue stuck events | MVP Core |
| `ACLService` | 默认可见性 / 默认写权限 | MVP Core |
| `IndexerService` | L0/L1 生成、embedding 更新 | MVP Core |
| `CatalogSyncService` | Catalog 同步 | Carrier-Specific |
| `ReconcilerService` | embedding 补写 / 一致性修复 | Carrier-Specific |
| 审计、反馈、生命周期 | 独立后置能力 | Post-MVP Reserved |

## 数据流

### 写入 / Promote / Publish

```text
Agent 写入或 promote / publish
    │
    ▼
API router 获取 RequestContext + ScopedRepo
    │
    ▼
ContextService / MemoryService / SkillService
    │
    ├─ 1. ContextStore.write()
    │
    ├─ 2. 同一 ScopedRepo 内事务 {
    │      INSERT/UPDATE contexts 表
    │      INSERT dependencies 表（自动注册依赖）
    │      INSERT change_events 表
    │  }
    │
    ├─ 3. commit 后 trigger 发出 NOTIFY
    │
    └─ 4. 异步补全：
           IndexerService 生成 L0/L1 embedding → 写入 pgvector
           PropagationEngine 处理变更传播
```

### 检索

```text
Agent 搜索 "月度销售额统计"
    │
    ▼
/api/v1/search 或 tool grep
    │
    ▼
RetrievalService.search()
    │
    ├─ 1. query embedding 生成
    │
    ├─ 2. pgvector 检索 L0 + 标量过滤 → top-K
    │
    ├─ 3. 读取 L1 内容 → Rerank
    │
    ├─ 4. 默认可见性过滤
    │
    └─ 5. 返回结果（按需加载 L2 / 结构化补充数据）
```

### 变更传播

```text
上游变更提交
    │
    ▼
INSERT change_events + NOTIFY context_changed
    │
    ▼
PropagationEngine 被唤醒 / 启动补扫 / 周期补扫
    │
    ├─ 用 event.account_id 创建 ScopedRepo
    │
    ├─ 查 dependencies / skill_subscriptions
    │     WHERE target_uri = changed_uri
    │
    ├─ 对每个依赖方执行传播规则
    │     ├─ mark_stale（标记过期）
    │     ├─ auto_update（重新生成 L0/L1）
    │     └─ advisory（仅通知，人工决策）
    │
    └─ 成功则 processed，失败则 retry（指数退避）
```

## 与旧方案的关键区别

| 维度 | 旧表述 | 冻结后的实现 |
|------|--------|--------------|
| 检索入口 | `ContextStore.search()` | `RetrievalService.search()` |
| 版本解析 | 独立 Version Manager 或 ContextStore 特判 | `SkillService.read_resolved()` |
| RLS 绑定 | middleware / 全局 repo 模糊负责 | `PgRepository.session(account_id)` 唯一负责 |
| MVP 边界 | audit / feedback / lifecycle 混在主图中 | 明确后置，不进初始骨架 |
| MVP 主线 | 垂直 Text-to-SQL 链路 | 横向协作闭环；Text-to-SQL 仅载体 |
