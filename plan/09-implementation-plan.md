# 09 — 实施计划、验证方案与技术选型

## MVP 验证场景

多 Agent 协作的企业数据分析：
1. 数据查询 Agent：自然语言 → ContextHub 检索湖表元数据 + 查询模板 → 生成 SQL → 执行
2. 数据分析 Agent：查询结果 → ContextHub 检索业务知识 + 分析 patterns → 生成报告
3. 协作验证：查询 Agent 积累的成功 SQL pattern 提升为共享记忆 → 分析 Agent 可用

## SDK 对接方式

```python
from contexthub import ContextHubClient

ctx = ContextHubClient(url="http://localhost:8000", api_key="...")

# 检索湖表元数据
tables = await ctx.search("月度销售额统计", scope="datalake", level="L1")

# 检索历史查询 cases
cases = await ctx.search("销售额统计 SQL", scope="agent_memory", category="cases")

# 记录成功的查询为 case
await ctx.memory.add_case(
    content="SELECT ... GROUP BY month",
    context={"question": "月度销售额", "tables_used": ["orders", "products"]}
)

# 提升为团队共享
await ctx.memory.promote(
    uri="ctx://agent/my-agent/cases/xxx",
    target_team="engineering/backend"
)
```

## 自定义 Benchmark: ECMB

| 维度 | 指标 | 方法 |
|------|------|------|
| 湖表检索 | Table Precision@K, Field Recall | 自然语言问题 → 检索表和字段 → 对比 ground truth |
| 跨 Agent 共享 | Knowledge Transfer Accuracy | Agent A 的 pattern → Agent B 能否正确使用 |
| Skill 版本 | Propagation Latency, Compat Rate | 更新 Skill → 依赖方是否被正确标记 stale |
| 变更传播 | Stale Detection Rate, Token Cost | 过时上下文被正确标记的比例 + token 消耗 |
| 反馈闭环 | Adoption Rate, Quality Score Correlation | 采纳率 + quality_score 与实际有用性的相关性 |
| 生命周期 | Archive Precision, Index Bloat Ratio | 归档准确率 + 向量索引中过时条目占比 |
| 权限隔离 | Isolation Violation Rate (=0) | 模拟越权访问 |
| 端到端质量 | SQL Accuracy, Answer Faithfulness | 全链路评估 |
| 效率 | Token Reduction, Latency P50/P99 | 有/无 ContextHub 对比 |

数据集来源：Spider/BIRD 改造 + 自行构造多 Agent 场景 + 权限测试用例。

## Evaluation Metrics 汇总

| 类别 | 指标 | 说明 |
|------|------|------|
| **检索质量** | Context Precision@K | 检索到的上下文中相关的比例 |
| | Context Recall | 所有相关上下文被检索到的比例 |
| | Table Retrieval Accuracy | 数据湖表检索准确率 |
| **生成质量** | SQL Execution Accuracy | 生成 SQL 的执行正确率 |
| | Answer Faithfulness | 回答对上下文的忠实度 |
| | Answer Relevance | 回答与问题的相关度 |
| **多 Agent** | Knowledge Transfer Rate | 共享知识被正确使用的比例 |
| | Skill Version Sync Latency | Skill 版本同步延迟 |
| | Memory Promotion Accuracy | 记忆提升后的质量保持率 |
| **反馈闭环** | Context Adoption Rate | 检索到的上下文被 Agent 实际采纳的比例 |
| | Quality Score Accuracy | quality_score 与人工标注质量的相关性 |
| | Low-Quality Detection Rate | 低质量上下文报告的准确率 |
| **生命周期** | Archive Precision | 归档决策的准确率（不误归档活跃上下文） |
| | Index Bloat Ratio | 向量索引中 stale/无用条目的占比（目标 < 10%） |
| **安全** | Isolation Violation Rate | 权限越界率（目标 = 0） |
| | Audit Completeness | 审计日志覆盖率 |
| **效率** | Token Reduction Ratio | 相比 baseline 的 token 节省 |
| | E2E Latency P50/P99 | 端到端延迟 |

## 实施计划（并行推进）

### Phase 1: 项目骨架 & 核心抽象（1-2 周）
1. 初始化项目（Python, FastAPI, pyproject.toml）
2. 定义核心数据模型：ContextNode, OwnerScope, ContextLevel, ContextType
3. 定义存储抽象接口：ContentStore, VectorStore, CatalogConnector
4. 实现 LocalFS ContentStore + Chroma VectorStore（开发用）
5. 实现 MockCatalogConnector（硬编码几张表的元数据）
6. 设计并实现 Python SDK 接口

### Phase 2: 两条线并行（3-4 周）

线 A — 数据湖表上下文管理：
7. 湖表元数据的 L0/L1/L2 自动生成（CatalogConnector → LLM 生成摘要）
8. Retrieval Engine（意图分析 → 目录层级递归 + 关系跳转 → Rerank）
9. Text-to-SQL 上下文组装逻辑
10. 对接 DataAgent，跑通"自然语言 → 上下文检索 → SQL 生成"链路

线 B — 多 Agent 协作 + 变更传播 + 质量闭环：
11. 多层级团队模型 + ACL（deny-override + 字段脱敏）
12. Memory Service（提取、去重、热度、共享提升）
13. Skill Service（版本管理、发布/订阅、is_breaking 标记）
14. Propagation Engine（ChangeEvent + .deps.json + PropagationRule 三级响应 + 完整性校验）
15. Feedback Collector（隐式反馈 + quality_score + 低质量报告）
16. Lifecycle Manager（状态机 + 归档策略 + 湖表同步删除）

### Phase 3: 集成与评估（2 周）
17. 两条线集成：多 Agent 通过 ContextHub 协作完成数据分析任务
18. 准备 ECMB 测试数据集
19. 运行 benchmark，对比有/无 ContextHub 的端到端效果
20. 输出评估报告

## 技术选型

| 组件 | 推荐 | 理由 |
|------|------|------|
| Web 框架 | FastAPI | 异步、类型安全、OpenAPI 自动生成 |
| 向量数据库（开发） | ChromaDB | 零配置、嵌入式 |
| 向量数据库（生产） | Milvus / Qdrant | 分布式、高可用 |
| 内容存储（开发） | LocalFS | 简单直接 |
| 内容存储（生产） | S3/OSS | 企业标准 |
| Embedding | text-embedding-3-small 或 BGE-M3 | 成本/效果平衡 |
| LLM（摘要生成） | Claude / GPT-4o-mini | L0/L1 生成不需要最强模型 |
| Event Log（开发） | append-only JSON 文件 | 简单、可审计 |
| Event Log（生产） | Redis Streams / Kafka | 持久化、可靠 |
| 测试 | pytest + pytest-asyncio | Python 标准 |

## 参考资料

- [OpenViking](https://github.com/volcengine/OpenViking) — 核心设计理念来源
- [Mem0](https://mem0.ai/blog/multi-agent-memory-systems) — 多 Agent 记忆系统
- [Letta Memory Blocks](https://www.letta.com/blog/memory-blocks) — Memory Block 设计
- [ContextBench](https://www.sundeepteki.org/blog/context-bench-a-benchmark-for-evaluating-agentic-context-engineering) — Agentic Context Engineering 评估
- [Spider](https://yale-lily.github.io/spider) / [BIRD](https://bird-bench.github.io/) — Text-to-SQL 评估数据集
