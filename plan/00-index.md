# ContextHub — 企业版上下文管理系统设计

面向 toB 场景的企业版上下文管理系统，借鉴 OpenViking 核心 idea 但从零开发。对上通过自研 DataAgent 连接用户（数据分析和数据查询），对下连接企业存储后端（数据湖表、湖表元数据、文档、用户记忆和 skills）。

关键约束：
- 全新项目，不 fork OpenViking，只借鉴设计理念
- 存储后端尚未确定 → 通用存储抽象层（Connector 接口）
- DataAgent 已有自研实现 → ContextHub 提供 API/SDK 供对接
- 数据湖表管理 和 多 Agent 协作 两条线并行推进

---

## 设计文档索引

| 文件 | 主题 | 关键内容 |
|------|------|----------|
| [01-storage-paradigm.md](01-storage-paradigm.md) | 统一存储范式 | URI 目录结构、向量索引层、关系文件、可见性与权限规则 |
| [02-information-model.md](02-information-model.md) | 信息模型 | L0/L1/L2 三层模型、记忆分类、层级检索、热度评分 |
| [03-datalake-management.md](03-datalake-management.md) | 数据湖表管理 | 湖表元数据、CatalogConnector、Text-to-SQL 上下文组装 |
| [04-multi-agent-collaboration.md](04-multi-agent-collaboration.md) | 多 Agent 协作 | 团队所有权模型、Skill 版本管理、记忆共享与提升 |
| [05-access-control-audit.md](05-access-control-audit.md) | 权限与审计 | ACL 策略、deny-override、字段脱敏、审计日志 |
| [06-change-propagation.md](06-change-propagation.md) | 变更传播 | ChangeEvent、依赖注册、PropagationRule 三级响应、完整性校验 |
| [07-feedback-lifecycle.md](07-feedback-lifecycle.md) | 反馈与生命周期 | 隐式反馈采集、质量评分、状态机、归档策略 |
| [08-architecture.md](08-architecture.md) | 系统架构 | 架构图、核心模块职责 |
| [09-implementation-plan.md](09-implementation-plan.md) | 实施计划 | MVP 场景、SDK、Benchmark、Phase 1-3、技术选型 |

## 依赖关系

```
01-storage-paradigm ──→ 02-information-model ──→ 03-datalake-management
        │                                              │
        └──→ 04-multi-agent-collaboration              │
                    │                                   │
                    ├──→ 05-access-control-audit        │
                    │                                   │
                    └──→ 06-change-propagation ←────────┘
                                │
                                └──→ 07-feedback-lifecycle
                                            │
                    08-architecture ←────────┘
                            │
                            └──→ 09-implementation-plan
```

## 建议阅读顺序

实现时按编号顺序阅读即可。如果只关注某条线：
- 线 A（数据湖）：01 → 02 → 03 → 08 → 09
- 线 B（多 Agent）：01 → 02 → 04 → 05 → 06 → 07 → 08 → 09
