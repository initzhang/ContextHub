# ContextHub — 企业版上下文管理系统设计

## 动机与核心问题

### 多 Agent 协作的结构性失败

当企业部署多个 AI Agent 协作处理同一批实体（客户、数据表、业务流程）时，这些 Agent 没有共享记忆、没有共同治理层。研究表明 **41-87% 的多 Agent 系统在生产中失败，其中 79% 的失败根源是协调问题而非技术 bug**[1]。对 200+ 条执行轨迹的分析发现，**36.9% 的多 Agent 失败来自 inter-agent misalignment**——Agent 忽略、重复或矛盾彼此的工作[2]。更好的模型不会修复这些问题，因为失败是结构性的。

### 企业级的 5 个结构性挑战

Governed Memory[3] 将此定义为"记忆治理缺口"（Memory Governance Gap），识别出 5 个个人版 Agent 记忆系统不需要面对的企业级挑战：

1. **跨工作流的记忆孤岛**：enrichment agent 发现 CTO 在评估三个供应商，outbound agent 数小时后发送通用邮件——因为它看不到前者的发现。组织智慧无处积累。
2. **跨团队的治理碎片化**：销售用一套 prompt 内嵌品牌语调，客服从上季度的 Notion 复制合规规则。当法律更新数据处理政策后，没有机制传播到 14 个 Agent 配置。没有版本控制，没有 single source of truth。
3. **非结构化记忆无法被下游消费**：自由文本记忆可以被检索并塞进 prompt，但无法被按阶段过滤、按价值排序、同步到 CRM、或跨实体聚合。
4. **自主执行中的上下文冗余**：Agent 在多步自主循环中每步重复注入相同的合规策略，浪费 context window 和 token。
5. **无反馈环的静默质量退化**：Schema 老化、模型更新、内容类型变化——组织在三个月后才发现 CRM 字段一直是错的。

### 现有方案的不足

行业正在收敛到层级式记忆架构（Global → Group/Role → Private），CrewAI、MemOS 和学术研究[4]独立到达了同一模式[1]。但现有框架的企业级能力远不够：

- **Mem0**[2]：提供平坦的 user/agent/app/run 级隔离，但没有团队层级、没有变更传播、没有版本管理、仅 SaaS
- **CrewAI/LangGraph**：记忆系统面向单一框架内的协调，无法跨框架、跨团队、跨时间管理组织级知识
- **OpenAI Agents SDK**：无内置记忆、无 ACL、无租户隔离——完全交由实现者自建[1]
- **OpenViking**：具备核心的上下文管理理念（一切皆文件 + 记忆管线 + 向量检索），但定位于个人版——不支持多 Agent 隔离、团队层级、ACL、变更传播
- **Governed Memory (Personize.ai)**[3]：最接近的方案，有治理路由和实体隔离，但聚焦于 CRM 实体（contacts/companies/deals），非通用 Agent 上下文管理

安全是最大的缺口——**大多数框架没有内置的记忆访问控制**。企业部署需要租户隔离、来源追踪和最小权限访问，而当前工具基本留给实现者自行解决[1]。

### ContextHub 的定位

ContextHub 是面向 toB 多 Agent 协作的**企业版上下文管理中间件**。它提供共享记忆、可见性边界、版本治理和变更传播的统一状态层；将 OpenViking 的核心理念（URI 文件语义 + L0/L1/L2 分层模型 + 记忆管线）扩展为支持企业级协作的层级式架构。MVP 核心闭环能力：

1. **多层级团队所有权模型**：默认可见性 + 默认写权限 + 团队层级继承（显式 ACL allow/deny/mask 属于明确后置 backlog，见 `14-adr-backlog-register.md`）
2. **依赖图驱动的变更传播**：三级规则（纯规则/模板替换/LLM 推理）节省 99% token（不是"法律更新了但 14 个 Agent 不知道"）
3. **Skill 版本管理 + breaking change 传播**：发布者手动标记 is_breaking，订阅者按 pinned/latest 语义读取，并在需要时被标记 stale 或收到 advisory
4. **记忆晋升机制**：私有→团队→组织，derived_from 追踪来源，源记忆变更时传播通知
5. **可私有化部署**：PG 中心架构，适合 toB 企业的 on-premise 需求（不是 Mem0/Governed Memory 的 SaaS-only）

MVP 不把显式 ACL、审计日志、长期反馈/生命周期收益当成已验证能力；这些能力均已在 `14-adr-backlog-register.md` 中完成分流。

数据湖表管理（L2 结构化子表 + CatalogConnector + Text-to-SQL 上下文组装）是其上的第一个垂直验证载体，用来构造可复现的检索、共享和 schema 变更场景，不是产品本体。

### 参考文献

- [1] [AI Agent Memory Architectures for Multi-Agent Systems](https://zylos.ai/research/2026-03-09-multi-agent-memory-architectures-shared-isolated-hierarchical) — Zylos Research, 2026-03. 综合分析共享/隔离/层级式记忆模式、安全缺口、存储收敛趋势
- [2] [How to Design Multi-Agent Memory Systems for Production](https://mem0.ai/blog/multi-agent-memory-systems) — Mem0, 2026-03. 36.9% 失败来自 inter-agent misalignment (引用 [Cemri et al., 2025](https://arxiv.org/abs/2503.13657))
- [3] [Governed Memory: A Production Architecture for Multi-Agent Workflows](https://arxiv.org/abs/2603.17787) — Taheri, 2026-03. 5 个结构性挑战定义、双记忆模型、治理路由、实体隔离
- [4] [Collaborative Memory: Multi-User Memory Sharing with Dynamic Access Control](https://arxiv.org/abs/2505.18279) — 动态访问控制的协作记忆，双向二分图权限模型
- [5] [Context Engineering for Commercial Agent Systems](https://www.jeremydaly.com/context-engineering-for-commercial-agent-systems) — Jeremy Daly, 2026-02. 商业多租户 Agent 系统的上下文工程实践
- [6] [Why Multi-Agent Systems Need Memory Engineering](https://www.mongodb.com/company/blog/technical/why-multi-agent-systems-need-memory-engineering) — MongoDB/O'Reilly, 2026. Context poisoning/distraction/confusion/clash 四类记忆问题
- [7] [OpenViking](https://github.com/volcengine/OpenViking) — 核心设计理念来源（个人版上下文管理）

---

## 系统概览

面向 toB 场景的企业版上下文管理系统，借鉴 OpenViking 核心 idea 但从零开发。对上通过 OpenClaw（作为 DataAgent）连接用户（数据分析和数据查询），对下连接企业存储后端（数据湖表、湖表元数据、文档、用户记忆和 skills）。
```
        用户
         │
    ┌────┴────┐
    │ OpenClaw │  ← Agent 运行时
    └────┬────┘
         │
    ┌────┴─────────┐
    │ ContextHub   │  ← 上下文管理中间件（应用层）
    └────┬─────────┘
         │
    ┌────┴───┐
    │  PG    │  ← 存储层
    └────────┘
```

关键约束：
- 全新项目，不 fork OpenViking，只借鉴设计理念
- 对外保留 `ctx://` URI 文件语义（Agent 看到的不变），对内以 PG 为核心存储
- PG 统一管理元数据 + 内容（TOAST 处理大文本），pgvector 扩展提供向量检索（同库同事务）
- 利用 PG 原生能力：ACID 事务、LISTEN/NOTIFY（变更传播）、RLS（租户隔离）、递归 CTE（血缘查询）
- DataAgent 采用 OpenClaw → 以 OpenClaw context-engine 插件形式对接 ContextHub SDK（参考 OpenViking 新版 context-engine 架构，详见 13-related-works.md）
- MVP 阶段使用单 OpenClaw 实例 + agent_id 切换验证多 Agent 协作（详见 09-implementation-plan.md）
- 多Agent协作（核心）和 数据湖表管理（首个垂直场景）两条线并行推进

---

## 设计文档索引

| 文件 | 主题 | 关键内容 |
|------|------|----------|
| [00a-canonical-invariants.md](00a-canonical-invariants.md) | **权威不变式** | 租户唯一性、类型系统、可见性继承、两层访问模型、状态机、版本不可变性。**后续所有文档的约束基准。** |
| [01-storage-paradigm.md](01-storage-paradigm.md) | 统一存储范式 | URI 路由层、PG 核心表结构、向量索引层、可见性与权限 |
| [02-information-model.md](02-information-model.md) | 信息模型 | L0/L1/L2 三层模型（PG 列存储）、记忆分类、热度评分 |
| [03-datalake-management.md](03-datalake-management.md) | 数据湖表管理 | L2 拆解为结构化表、CatalogConnector、Text-to-SQL 上下文组装（PG JOIN） |
| [04-multi-agent-collaboration.md](04-multi-agent-collaboration.md) | 多 Agent 协作 | 团队所有权模型、Skill 版本管理（PG 表）、记忆共享与提升（PG 事务） |
| [05-access-control-audit.md](05-access-control-audit.md) | 权限与审计 | **明确后置 backlog owner**：显式 ACL、字段脱敏、审计日志与窄范围共享 |
| [06-change-propagation.md](06-change-propagation.md) | 变更传播 | PG LISTEN/NOTIFY、dependencies 表、PropagationRule 三级响应 |
| [07-feedback-lifecycle.md](07-feedback-lifecycle.md) | 反馈与生命周期 | **明确后置 backlog owner**：反馈闭环、质量信号与生命周期治理 |
| [08-architecture.md](08-architecture.md) | 系统架构 | PG 中心架构图、ContextStore URI 路由层、数据流 |
| [09-implementation-plan.md](09-implementation-plan.md) | 实施计划 | MVP 场景、SDK、Benchmark、Phase 1-3、PG 中心技术选型 |
| [10-code-architecture.md](10-code-architecture.md) | 代码架构 | 项目目录结构、依赖注入、API 端点、VectorStore 抽象、L0/L1 生成 |
| [11-long-document-retrieval.md](11-long-document-retrieval.md) | 长文档检索策略 | **明确后置 backlog owner**：长文档高级检索扩展的触发条件与设计种子 |
| [12-evolution-notes.md](12-evolution-notes.md) | 保留 ADR | 对象存储、消息队列、ReBAC/Zanzibar 等替代架构的拒绝原因与重开条件 |
| [13-related-works.md](13-related-works.md) | 相关工作分析 | OpenClaw 插件体系、lossless-claw DAG 无损压缩、OpenViking 记忆适配器、ContextEngine 接口、架构决策参考 |
| [14-adr-backlog-register.md](14-adr-backlog-register.md) | ADR / Backlog Register | Session 7 的统一分流结果：后置项、ADR、rejected ideas、重开入口 |

## 依赖关系

```
00a-canonical-invariants ──→ 所有文档（权威约束基准）

01-storage-paradigm ──→ 02-information-model ──→ 03-datalake-management
        │                       │                        │
        │                       └──→ 11-long-document-retrieval
        │                                                │
        └──→ 04-multi-agent-collaboration                │
                    │                                    │
                    ├──→ 05-access-control-audit          │
                    │                                    │
                    └──→ 06-change-propagation ←─────────┘
                                │        ↑
                                │        └── 11-long-document-retrieval
                                └──→ 07-feedback-lifecycle
                                            │
                    08-architecture ←────────┘
                            │
                            └──→ 09-implementation-plan
                                        │
                                        ├──→ 10-code-architecture
                                        │
                                        ├──→ 12-evolution-notes（保留 ADR，依赖 01 + 05 + 06）
                                        │
                                        ├──→ 13-related-works（独立参考文档，依赖 08）
                                        │
                                        └──→ 14-adr-backlog-register（汇总 05 / 07 / 09 / 11 / 12 / 13 的后置项与 rejected ideas）
```

## 建议阅读顺序

所有线路都应先读 `00a-canonical-invariants`（权威约束），再按编号顺序阅读。如果只关注某条线：
- 线 A（数据湖）：01 → 02 → 03 → 08 → 09
- 线 B（多 Agent）：01 → 02 → 04 → 05 → 06 → 07 → 08 → 09
- 线 C（长文档检索）：01 → 02 → 11 → 06 → 09
- 只看当前主线时，读到 `10-code-architecture.md` 即可；所有后置项、ADR 和 rejected ideas 统一看 `14-adr-backlog-register.md`
