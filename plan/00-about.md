# ContextHub — 企业版上下文管理系统设计

## 动机与核心问题

### 问题定义

当多个 AI Agent 在企业环境中协作操作同一组业务实体时，各 Agent 需要读写的**上下文（context）**——包括对话记忆、工具与 prompt 定义、策略文档、结构化 schema 等——分散存储于各自的私有空间中，缺乏统一的可见性边界、版本语义和变更传播机制。本文将这一问题称为**企业级上下文治理问题（Enterprise Context Governance Problem）**。

### 多 Agent 协作的结构性失败

当企业部署多个 AI Agent 协作处理同一批实体（客户、数据表、业务流程）时，这些 Agent 没有共享的上下文状态层，也没有统一的治理协议。研究表明 **41–87% 的多 Agent 系统在生产中失败，其中 79% 的失败根源是协调问题而非技术 bug**[1]。对 200+ 条执行轨迹的分析发现，**36.9% 的多 Agent 失败来自 inter-agent misalignment**——Agent 忽略、重复或矛盾彼此的工作[2]。这类失败无法通过提升单个 Agent 的模型能力来解决，因为其根因在于 Agent 间缺乏共享状态层和治理协议，属于**系统架构层面的缺陷**而非单点能力不足。

### 企业级的 5 个结构性挑战

Governed Memory[3] 将此定义为"记忆治理缺口"（Memory Governance Gap），识别出 5 个个人版 Agent 记忆系统不需要面对的企业级挑战：

1. **跨工作流的 Agent 上下文孤岛**：B2B 采购场景中，各 Agent 维护独立的执行上下文，无法访问其他 Agent 在同一实体上产生的中间结果。例如，enrichment agent 协助 CTO 评估三个供应商并取得了评估结果，但 outbound agent 看不到评估结论和 CTO 的采购意愿，仍给三个供应商发送通用邮件。
2. **跨团队策略变更难以传播**：各 Agent 的行为策略分散存储于各自的私有存储形式（markdown 或外部文档）中，没有版本控制，没有 single source of truth。当某个上游策略（如数据处理合规规则）发生变更时，没有传播机制将更新同步到所有依赖该策略的 Agent 配置，导致整体行为不一致。例如，法务部门更新数据处理合规政策后，没有任何机制能自动将变更传播给销售团队和客服团队，各团队各自维护独立的"真相源"。
3. **非结构化记忆无法被下游消费**：非结构化的自由文本记忆虽然可经向量检索后注入 prompt，但无法支持按阶段过滤、按业务价值排序、同步至 CRM（Customer Relationship Management，客户关系管理系统）或跨实体聚合等结构化操作。
4. **缺乏上下文作用域导致的冗余注入**：Agent 在多步自主执行中，由于缺乏上下文作用域（context scoping）机制，不得不在每一步重复注入相同的合规策略或背景知识，造成 context window 的冗余占用。这不仅浪费 token，更在长任务链中挤压了可用于推理的有效上下文空间。
5. **无反馈环的静默质量退化**：Agent 写入业务系统时所依赖的字段 schema 会因模型迭代或系统升级而悄然失效；由于缺乏主动监控与反馈机制，组织往往在数月后才发现 CRM 等系统的字段数据长期处于错误状态。

### 现有方案的不足

为应对上述挑战，学界和工业界近期独立收敛到了一种**层级式记忆架构**（分为全局/组织、团队/角色、私有三层），CrewAI、MemOS 和 Collaborative Memory[4] 均采用了这一模式[1]。但现有框架的企业级能力仍远不够：

- **Mem0**[2]：提供平坦的 user/agent/app/run 级隔离，但没有团队层级、没有变更传播、没有版本管理、仅 SaaS
- **CrewAI/LangGraph**：记忆系统面向单一框架内的协调，无法跨框架、跨团队、跨时间管理组织级知识
- **OpenAI Agents SDK**：无内置记忆、无 ACL、无租户隔离——完全交由实现者自建[1]
- **OpenViking**[7]：具备核心的上下文管理理念（一切皆文件 + 记忆管线 + 向量检索），但定位于个人版——不支持多 Agent 隔离、团队层级、ACL、变更传播
- **Governed Memory (Personize.ai)**[3]：最接近的方案，有治理路由和实体隔离，但聚焦于 CRM 实体（contacts/companies/deals），非通用 Agent 上下文管理

安全是最大的缺口——**大多数框架没有内置的记忆访问控制**。企业部署需要租户隔离、来源追踪和最小权限访问，而当前工具基本留给实现者自行解决[1]。

### 从记忆管理到统一上下文治理

上述对比揭示了一个更深层的问题：现有工作将"Agent 上下文管理"几乎等同于"记忆管理"。Governed Memory[3]、Collaborative Memory[4]、MemOS 均以记忆为核心抽象。但企业 Agent 系统实际需要治理的上下文远不止记忆一种类型，而是涵盖以下四类：

| 上下文类型 | 典型内容 | 多版本/多用户治理的文献覆盖程度 |
|---|---|---|
| **Memory** | 对话记忆、实体状态、工作记忆 | 相对最多，但多用户协作版本管理仍稀缺[4] |
| **Skill** | 工具定义、prompt 模板、Agent 配置 | **几乎空白** |
| **Resource（RAG 文档）** | 政策文档、合规规则、产品知识库 | 仅覆盖"检索最新版本"，不覆盖变更传播 |
| **结构化元数据** | 数据库 schema、数据湖表 catalog | 无 AI Agent 语境下的研究 |

**文献空白的具体证据：**

- **Skill 版本管理**：ToolBench/ToolLLM[8] 等 tool learning 研究大规模解决了工具选择问题，但完全不涉及 breaking change 检测、pinned/latest 订阅语义或订阅者通知。MCP（Anthropic, 2024）[9] 在协议层面规范了工具发现与调用接口，但无版本语义——没有 is_breaking 标记、没有订阅者 stale 通知、没有 advisory 机制。目前 AI Agent 文献中没有处理"skill 发布者标记 breaking change → 订阅者被标记 stale → 收到 advisory"这一完整生命周期的工作。
- **RAG 文档变更传播**：Temporal RAG / Corrective RAG[10] 等工作解决了"检索当前有效文档"的问题，但不解决"上游文档更新后，哪些下游 Agent 依赖了它、需要被通知"——即版本变更沿依赖图向下传播这一治理问题。
- **多用户 Skill/Resource 协作**：Collaborative Memory[4] 的双向二分图权限模型仅覆盖 memory，未扩展到 skill 和 resource；隐私保护 RAG 领域的工作[11] 关注租户间隔离，而非协作共享与版本一致性。

ContextHub 的设计贡献因此超出了"记忆治理工程化"的范畴。以 Skill 版本管理、breaking change 传播、订阅者通知为代表的上下文治理机制，在现有文献中没有对应的端到端研究。据我们所知，对 Memory、Skill、Resource（RAG 文档）和结构化元数据的**统一版本治理**，作为一个端到端问题，在现有文献中尚无系统性研究。

### ContextHub 的定位

ContextHub 是面向 toB 多 Agent 协作场景的**企业级上下文管理中间件**，提供统一的上下文状态层，涵盖共享记忆、可见性边界、版本治理和变更传播四项核心能力。其设计继承了 OpenViking[7] 的核心抽象（URI 文件语义、L0/L1/L2 分层模型、记忆管线），并将其扩展至支持多租户、多团队协作的层级式架构。

**MVP 核心能力：**

1. **多层级团队所有权模型**：默认可见性 + 默认写权限 + 团队层级继承（显式 ACL allow/deny/mask 属于明确后置 backlog，见 `14-adr-backlog-register.md`）
2. **依赖图驱动的变更传播**：三级传播规则（纯规则 / 模板替换 / LLM 推理），确保上游策略变更能沿依赖图自动传播至所有下游 Agent（预期可大幅减少因策略不同步导致的冗余 token 消耗，具体量化待 MVP 验证）
3. **Skill 版本管理 + breaking change 传播**：发布者手动标记 is_breaking，订阅者按 pinned/latest 语义读取，并在需要时被标记 stale 或收到 advisory
4. **记忆晋升机制**：私有→团队→组织，derived_from 追踪来源，源记忆变更时传播通知
5. **可私有化部署**：PG 中心架构，适合 toB 企业的 on-premise 需求（区别于 Mem0 / Governed Memory 的 SaaS-only 模式）

MVP 不将显式 ACL、审计日志、长期反馈/生命周期收益作为已验证能力；这些能力均已在 `14-adr-backlog-register.md` 中完成分流。

**垂直验证载体**：数据湖表管理（L2 结构化子表 + CatalogConnector + Text-to-SQL 上下文组装）作为 MVP 之上的第一个垂直场景，用于构造可复现的检索、共享和 schema 变更场景，验证上述核心能力，不属于产品本体。

### 参考文献

- [1] [AI Agent Memory Architectures for Multi-Agent Systems](https://zylos.ai/research/2026-03-09-multi-agent-memory-architectures-shared-isolated-hierarchical) — Zylos Research, 2026-03. 综合分析共享/隔离/层级式记忆模式、安全缺口、存储收敛趋势
- [2] [How to Design Multi-Agent Memory Systems for Production](https://mem0.ai/blog/multi-agent-memory-systems) — Mem0, 2026-03. 36.9% 失败来自 inter-agent misalignment (引用 [Cemri et al., 2025](https://arxiv.org/abs/2503.13657))
- [3] [Governed Memory: A Production Architecture for Multi-Agent Workflows](https://arxiv.org/abs/2603.17787) — Taheri, 2026-03. 5 个结构性挑战定义、双记忆模型、治理路由、实体隔离
- [4] [Collaborative Memory: Multi-User Memory Sharing with Dynamic Access Control](https://arxiv.org/abs/2505.18279) — 动态访问控制的协作记忆，双向二分图权限模型
- [5] [Context Engineering for Commercial Agent Systems](https://www.jeremydaly.com/context-engineering-for-commercial-agent-systems) — Jeremy Daly, 2026-02. 商业多租户 Agent 系统的上下文工程实践
- [6] [Why Multi-Agent Systems Need Memory Engineering](https://www.mongodb.com/company/blog/technical/why-multi-agent-systems-need-memory-engineering) — MongoDB/O'Reilly, 2026. Context poisoning/distraction/confusion/clash 四类记忆问题
- [7] [OpenViking](https://github.com/volcengine/OpenViking) — 核心设计理念来源（个人版上下文管理）
- [8] [ToolLLM: Facilitating Large Language Models to Master 16000+ Real-world APIs](https://arxiv.org/abs/2307.16789) — Qin et al., 2023. 大规模 tool learning 研究，解决工具选择与调用，但不涉及版本管理或 breaking change 语义
- [9] [Model Context Protocol (MCP)](https://www.anthropic.com/news/model-context-protocol) — Anthropic, 2024-11. 协议层面规范工具/资源发现与调用，无版本语义（无 is_breaking、无订阅者通知）
- [10] [Corrective Retrieval Augmented Generation (CRAG)](https://arxiv.org/abs/2401.15884) — Yan et al., 2024. 代表性 Temporal/Corrective RAG 工作，关注检索文档的时效性与可信度，不处理版本变更沿依赖图传播的治理问题
- [11] [Privacy-Preserving Retrieval-Augmented Generation with Differential Privacy](https://arxiv.org/abs/2412.04697) — 代表性隐私保护 RAG 工作，关注租户间知识隔离，不覆盖协作共享与版本一致性
