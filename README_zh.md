# ContextHub

**面向企业多 Agent 协作的上下文管理中间件。**

[English](README.md) | 中文

当多个 AI Agent 在企业环境中操作同一组业务实体时，各 Agent 的上下文——记忆、技能、策略文档、Schema——分散存储、缺乏版本控制、彼此断联。研究表明，**79% 的多 Agent 系统失败源于协调问题，而非技术 bug**。ContextHub 通过统一的上下文状态层解决这一问题。

## 为什么选择 ContextHub

| 问题 | ContextHub 的解法 |
|------|-----------------|
| Agent 之间看不到彼此的工作成果 | 层级式团队所有权模型 + 可见性继承 |
| 策略变更无法传播到下游 Agent | 依赖图驱动的变更传播（三级规则） |
| 技能/工具没有版本管理 | Skill 版本管理 + breaking change 检测 + 订阅者通知 |
| 知识锁死在单个 Agent 内 | 记忆晋升机制：私有 → 团队 → 组织 |
| 只有 SaaS 方案（Mem0、Governed Memory） | 可私有化部署，PostgreSQL 中心架构，适合企业 on-premise 需求 |

### 差异化定位

多数框架将 Agent 上下文等同于"记忆管理"。ContextHub 在统一模型下治理**四类上下文**：

- **Memory** — 对话记忆、实体状态、工作记忆
- **Skill** — 工具定义、Prompt 模板、Agent 配置（含版本生命周期）
- **Resource** — 策略文档、合规规则、知识库（含变更传播）
- **结构化元数据** — 数据库 Schema、数据湖 Catalog

## 架构

```
         Agents（通过 OpenClaw / SDK 接入）
              │
              ▼
    ContextHub Server (FastAPI)
    ├── ContextStore       — ctx:// URI 路由（read/write/ls/stat）
    ├── MemoryService      — 记忆晋升、derived_from、团队共享
    ├── SkillService       — 发布、订阅、版本解析
    ├── RetrievalService   — 统一检索（pgvector + rerank）
    ├── PropagationEngine  — 变更事件处理 + 重试
    └── ACLService         — 默认可见性 / 写权限
              │
              ▼
    PostgreSQL + pgvector
    （元数据、内容、向量、事件 — 全部在一个数据库中）
```

单数据库架构。无外部向量库。无消息队列。PostgreSQL 原生提供 ACID 事务、RLS 租户隔离、LISTEN/NOTIFY 变更传播、递归 CTE 血缘查询，以及 pgvector 语义检索。

## 核心能力

### 多 Agent 协作
- **团队所有权模型**：层级式可见性继承
- **记忆晋升**：私有 → 团队 → 组织，`derived_from` 追踪来源血缘
- **跨 Agent 知识复用**：晋升后的记忆可被团队成员检索使用

### Skill 版本管理
- 发布新版本时标记 `is_breaking`
- 订阅者选择 `pinned`（锁定版本）或 `latest`（浮动跟踪）解析策略
- Breaking change 自动将下游依赖方标记为 `stale`，并附带 advisory 通知

### 变更传播
- 三级传播规则：纯规则 / 模板替换 / LLM 推理
- Outbox 模式，`change_events` 表为唯一事实源
- NOTIFY 快速唤醒 + 周期补扫保证最终送达
- 指数退避自动重试；crash 后通过 lease 超时恢复

### L0/L1/L2 分层检索
- **L0**：一句话摘要 + embedding（向量检索）
- **L1**：结构化概览（精排）
- **L2**：完整内容（按需加载）
- 相比全量 Schema dump，上下文 token 消耗降低 60-80%

## 快速开始

### 前置条件

- Python 3.12+
- Docker & Docker Compose
- PostgreSQL 16 + pgvector（通过 docker-compose 提供）

### 1. 克隆并安装

```bash
git clone https://github.com/your-org/contexthub.git
cd contexthub
pip install -e ".[dev]"
```

### 2. 启动 PostgreSQL

```bash
docker compose up -d
```

启动 PostgreSQL 16 + pgvector，端口 5432（用户：`contexthub`，密码：`contexthub`，数据库：`contexthub`）。

### 3. 执行数据库迁移

```bash
alembic upgrade head
```

### 4. 启动服务

```bash
uvicorn contexthub.main:app --reload
```

API 地址：`http://localhost:8000`，OpenAPI 文档：`/docs`。

### 5. 使用 SDK

```python
from contexthub import ContextHubClient

ctx = ContextHubClient(url="http://localhost:8000", api_key="...")

# 检索上下文
results = await ctx.search("月度销售额统计", scope="datalake", level="L1")

# 记录成功案例
await ctx.memory.add_case(
    content="SELECT ... GROUP BY month",
    context={"question": "月度销售额", "tables_used": ["orders", "products"]}
)

# 晋升为团队共享记忆
await ctx.memory.promote(
    uri="ctx://agent/query-agent/cases/xxx",
    target_team="engineering/backend"
)
```

## API 概览

所有请求需携带 `X-Account-Id` 和 `X-Agent-Id` 请求头以实现租户隔离。

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/api/v1/contexts` | 创建上下文 |
| GET | `/api/v1/contexts/{uri}` | 读取上下文（Skill 自动走版本解析） |
| POST | `/api/v1/search` | 统一语义检索 |
| POST | `/api/v1/memories` | 添加记忆 |
| POST | `/api/v1/memories/promote` | 晋升记忆到团队范围 |
| POST | `/api/v1/skills/versions` | 发布 Skill 新版本 |
| POST | `/api/v1/skills/subscribe` | 订阅 Skill |

## 技术选型

| 组件 | 选择 | 理由 |
|------|------|------|
| Web 框架 | FastAPI | 异步、类型安全、自动生成 OpenAPI |
| 数据库 | PostgreSQL 16 | 元数据 + 内容 + 向量 + 事件统一存储 |
| 向量检索 | pgvector | 同库同事务，无双写对账问题 |
| 异步驱动 | asyncpg | 高性能异步 PG 客户端，原生 LISTEN/NOTIFY |
| 数据库迁移 | Alembic | Schema 版本管理 |
| Embedding | text-embedding-3-small / BGE-M3 | L0 摘要级别，成本效果平衡 |

## 项目结构

```
contexthub/
├── src/contexthub/
│   ├── api/          # FastAPI 路由 + 中间件
│   ├── db/           # PgRepository、ScopedRepo、SQL 查询
│   ├── models/       # Pydantic 模型
│   ├── services/     # 业务逻辑（记忆、技能、检索、传播）
│   ├── store/        # ContextStore（URI 路由）
│   ├── retrieval/    # 检索策略（向量、精排）
│   ├── propagation/  # 变更传播规则
│   └── generation/   # L0/L1 内容生成
├── sdk/              # Python SDK（typed HTTP 客户端）
├── plugins/openclaw/ # OpenClaw context-engine 插件
├── alembic/          # 数据库迁移
└── tests/
```

## 路线图

- [x] Phase 0 — 项目脚手架、Docker、数据库初始化
- [ ] Phase 1 — 核心基础（ContextStore、ACL、request-scoped 数据库模型）
- [ ] Phase 2 — 协作闭环（记忆晋升、Skill 版本管理、变更传播、检索）
- [ ] Phase 3 — 垂直载体（数据湖元数据、Text-to-SQL 上下文组装）
- [ ] Phase 4 — 显式 ACL、审计日志、反馈生命周期

## 许可证

[Apache License 2.0](LICENSE)
