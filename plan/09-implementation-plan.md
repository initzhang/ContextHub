# 09 — 实施计划、验证方案与技术选型

## MVP Claim（冻结）

- **产品定位**：ContextHub 是面向 toB 多 Agent 协作的上下文管理中间件，为共享记忆、可见性边界、版本解析和变更传播提供统一状态层。
- **本次 MVP 证明**：在单 OpenClaw 实例 + 多 `agent_id` 的条件下，ContextHub 能稳定跑通“私有写入 → 晋升共享 → 跨 Agent 复用 → Skill 更新 → 下游 stale / advisory 感知 → 补偿恢复”这条横向闭环。
- **本次 MVP 不证明**：通用 Text-to-SQL SOTA、显式 ACL allow/deny/mask、审计日志、长期反馈/生命周期收益、多实例伸缩。
- **首个垂直场景角色**：企业数据分析 / Text-to-SQL 只是验证载体，用来制造可复现的共享记忆、schema 变更和 Skill 演进场景，不是产品本体。

## 首个垂直载体验证场景

单 OpenClaw 实例 + agent_id 切换，以企业数据分析工作流作为验证载体，验证多 Agent 协作的核心横向能力：

1. 数据查询 Agent（agent_id="query-agent"）：自然语言 → ContextHub 检索湖表元数据 + 查询模板 → 生成 SQL → 执行
2. 数据分析 Agent（agent_id="analysis-agent"）：查询结果 → ContextHub 检索业务知识 + 分析 patterns → 生成报告
3. 协作验证：query-agent 积累的成功 SQL pattern 提升为共享记忆 → 切换到 analysis-agent 身份验证可见性
4. 传播验证：以 query-agent 身份更新 Skill → Server 端自动传播 → 切换到 analysis-agent 身份验证依赖被标记 stale

### 为什么单实例够用

ContextHub 的协作机制（传播、可见性/所有权、记忆晋升、版本解析）全部是 Server 端状态机，不依赖多个活跃 Agent 运行时：
- 传播引擎：Agent A 更新 Skill → PG NOTIFY → Server 查依赖图 → 标记 Agent B 的依赖为 stale。Agent B 不需要在线
- 记忆晋升：一次 API 调用，Server 端写入 + 建依赖关系
- 权限验证：MVP 在 Server 端做 RLS + 默认可见性/所有权检查；显式 ACL policy 属于明确后置 backlog，见 `14-adr-backlog-register.md`
- 版本解析：未订阅 / floating 读取 latest，pinned 读取固定版本；新版本发布后按 dependency / subscription 语义触发 stale 或 advisory

因此，单 OpenClaw 实例通过 SDK 调用时切换 `agent_id` 参数即可验证所有协作特性。多实例场景（多人各自用 OpenClaw 协作分析同一数据集）属于产品成熟期目标。

### DataAgent 对接方式

采用 OpenClaw context-engine 插件模式（参考 OpenViking 新版 openclaw-plugin 的 context-engine 架构，详见 13-related-works.md）。Plugin 声明 `kind: "context-engine"`，注册为 `plugins.slots.contextEngine`。

**注册的 Agent 工具：**

| Tool | 功能 | 对应 SDK 方法 |
|------|------|--------------|
| `ls` | 列出 ctx:// 路径下的子项 | `ctx.ls()` |
| `read` | 读取上下文内容（L0/L1/L2） | `ctx.read()` |
| `grep` | 语义搜索上下文 | `ctx.search()` |
| `stat` | 查看上下文元信息 | `ctx.stat()` |
| `contexthub_store` | 写入记忆/案例 | `ctx.memory.add_case()` |
| `contexthub_promote` | 提升记忆到团队共享 | `ctx.memory.promote()` |
| `contexthub_skill_publish` | 发布 Skill 新版本 | `ctx.skill.publish()` |
| `contexthub_feedback`（后置） | 报告上下文采纳/忽略 | `ctx.feedback.report()` |

**ContextEngine 生命周期方法：**

| 方法 | 行为 |
|------|------|
| `assemble` | 透传 messages，通过 `systemPromptAddition` 注入 PG auto-recall 结果 |
| `afterTurn` | 自动提取记忆写入 PG（auto-capture） |
| `compact` | 委托给 OpenClaw LegacyContextEngine |
| `ingest` / `ingestBatch` | 空操作 |

这种"增强型适配器"模式确保 ContextHub 的上下文注入不会被 compaction 引擎当作对话历史来压缩（详见 08-architecture.md "OpenClaw 插件架构决策"）。

注：MVP 初版只要求 `ls/read/grep/stat/store/promote/skill_publish` 这 7 个工具。`contexthub_feedback` 属于明确后置 backlog，不进入当前实施主线；触发条件见 `14-adr-backlog-register.md`。

## MVP 验证矩阵（横向能力 × 垂直载体）

| 横向能力 | 在数据分析载体中的对应动作 | 主要验证方式 | 本次 MVP 退出要求 |
|----------|----------------------------|--------------|-------------------|
| 默认可见性 / 隔离 | query-agent 与 analysis-agent 在私有空间、团队空间、promote 后空间互读 | Tier 3 A-1~A-4 | 权限泄漏为 0 |
| 共享晋升 / 跨 Agent 复用 | query-agent 将成功 case promote 给 analysis-agent | Tier 3 C-1~C-2 + demo | 晋升后可见、可检索、来源可追踪 |
| 版本解析正确性 | analysis-agent 分别订阅 pinned / latest 的 Skill | Tier 3 C-4~C-5 | pinned/latest 解析正确，pinned 不被新版本污染 |
| 变更传播正确性 | schema 变更、breaking Skill 发布、级联依赖 | Tier 3 P-1~P-6 | 该 stale 的 stale，不该 stale 的不误伤 |
| 传播可靠性 | 模拟 NOTIFY 丢失、worker lease 超时 | Tier 3 P-7~P-8 | 依靠补扫 / retry 最终收敛 |
| 跨 Agent 知识迁移信号 | analysis-agent 复用 query-agent 已晋升案例 | demo + 后续 A/B | MVP 先证明“能复用”，量化收益后置到 ECMB |

### MVP 必采系统指标

这些指标用于证明系统性闭环，而不只是单个 happy path：

| 指标 | 定义 | 用途 |
|------|------|------|
| 传播命中率 | 真正受影响的依赖方中，被正确 mark stale / notify / advisory 的比例 | 防止只验证单个样例 |
| 变更收敛时延 | 从上游变更 commit 到下游可观察到 stale / advisory / auto_update 结果的时间，区分 notify 路径与 sweep fallback 路径 | 证明传播不是“最终会好”，而是可观测、可界定 |
| pinned/latest 解析正确性 | latest / floating / pinned 读取命中预期版本的比例 | 版本治理是核心 claim，不能只靠 demo |
| 跨 Agent 复用信号 | analysis-agent 实际命中的 promoted context 数、被采用次数 | 证明共享记忆不是只“可见”而是被用到 |
| 权限泄漏率 | 本不应通过的读取 / 搜索中实际返回成功的比例 | 目标必须为 0 |
| 事件丢失恢复能力 | 模拟丢失 NOTIFY / worker crash 后最终 recovered 的比例与时延 | 验证 outbox + 补扫设计闭环 |

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

## Post-MVP 深化量化 Benchmark: ECMB（非 MVP 退出标准）

下面的 ECMB 主要量化“首个垂直载体”上的收益，尤其是数据湖 / Text-to-SQL 这一条线。它保留为 MVP 之后的深化验证预案，不作为本次 MVP 的退出标准。

### 设计原则

评估分为三个层级，按量化可信度从高到低排列：
- Tier 1（量化 Benchmark）：有客观 ground truth、可画曲线的硬指标
- Tier 2（A/B 消融实验）：隔离单个特性的贡献，对比有/无该特性的效果差异
- Tier 3（功能正确性测试）：pass/fail 的集成测试，验证架构能力而非优化指标

### 测试数据集

**数据湖模拟（核心数据集）**

基于 BIRD 数据集改造，增加企业级元素：

| 数据项 | 数量 | 来源 |
|--------|------|------|
| 数据库 | 5-8 个 | BIRD 子集（选业务复杂度高的库：financial, retail, healthcare 等） |
| 表 | 30-50 张 | BIRD 原始表 + 补充 JOIN 关系和血缘 |
| 自然语言问题 | 200-300 条 | BIRD 原始问题 + 自构造多表 JOIN / 业务术语问题 |
| Gold SQL | 与问题一一对应 | BIRD 原始 + 人工标注 |
| 表间 JOIN 关系 | 40-60 条 | 从 BIRD 的 FK 关系提取 + 补充 common_join |
| 血缘关系 | 15-20 条 | 自构造（ODS → DWD → DWS 三层） |
| 查询模板 | 每表 3-5 条，共 100-150 条 | 从 Gold SQL 中提取通用模式 |
| 业务术语 | 30-50 条 | 自构造（如"GMV"="SUM(amount) WHERE status='completed'"） |

**变更传播场景数据**

| 场景 | 数量 | 说明 |
|------|------|------|
| Schema 变更事件 | 10-15 次 | ALTER TABLE（加字段、改类型、删字段） |
| 依赖关系 | 每张表 2-4 条依赖，共 60-100 条 | skill_version + table_schema + derived_from |
| Skill 版本更新 | 5-8 次 | 其中 3-4 次 is_breaking=True |
| 记忆晋升事件 | 5-8 次 | agent private → team shared |

**多 Agent 工作流数据**

| 场景 | 数量 | 说明 |
|------|------|------|
| query-agent 学习 episode | 10-15 轮 | 每轮：问题 → 检索 → 生成 SQL → 执行 → 记录 case |
| analysis-agent 分析任务 | 8-10 轮 | 依赖 query-agent 的共享记忆和查询结果 |
| 跨 Agent 协作场景 | 3-5 个 | 端到端：查询 → 分析 → 报告 |

### Tier 1: 量化 Benchmark

这些指标有客观 ground truth，可以在不同配置间做统计显著性比较。

| 指标 | 量化方式 | Ground Truth | 预期信号 |
|------|----------|-------------|----------|
| SQL Execution Accuracy (EX) | 生成 SQL 在数据库上执行结果与 Gold SQL 结果一致的比例 | BIRD Gold SQL | ContextHub 结构化上下文 > Baseline 平坦 RAG |
| Table Retrieval Precision@5 | 检索到的 top-5 表中，Gold SQL 实际用到的表的比例 | Gold SQL 中的 FROM/JOIN 表 | L0/L1 两阶段 > 单阶段向量检索 |
| Table Retrieval Recall | Gold SQL 用到的表被检索到的比例 | 同上 | 结构化关系辅助 > 纯语义匹配 |
| Token per Query (检索阶段) | 每次查询注入 LLM 的上下文 token 数 | 直接计数 | L0/L1/L2 分层 << 全量 schema dump |
| Token per Query (端到端) | 包含 LLM 生成的总 token 消耗 | 直接计数 | 更精准的上下文 → 更短的推理链 |
| Propagation Token Cost | 一次变更事件触发的传播总 token 消耗 | 直接计数 | 三级规则 << 全 LLM 重评估 |
| E2E Latency P50/P99 | 从用户提问到返回 SQL 的端到端延迟 | 直接计时 | PG JOIN 组装 vs 多次向量库查询 |

### Tier 2: A/B 消融实验

每个实验隔离一个特性，其他条件保持一致。使用 Tier 1 的指标做对比。

**实验 1：L0/L1/L2 分层检索 vs 平坦 RAG**

| 配置 | 说明 |
|------|------|
| A (Baseline) | 所有表 schema 切成 ~500 token 的 chunk，存入向量库，检索 top-K chunk 直接塞入 prompt |
| B (ContextHub) | L0 向量检索 → L1 精排 → 选择性加载 L2 结构化数据 |
| 对比指标 | EX, Token per Query, Table Precision@5 |
| 预期结果 | B 的 Token 显著降低（60-80%），EX 持平或略高（因为噪音更少） |

**实验 2：有/无结构化关系（table_relationships + query_templates）**

| 配置 | 说明 |
|------|------|
| A | ContextHub 检索但不注入 JOIN 关系和查询模板 |
| B | ContextHub 检索 + PG JOIN 组装完整上下文（关系 + 模板 + 业务术语） |
| 对比指标 | EX（尤其是多表 JOIN 子集）, Token per Query |
| 预期结果 | B 在多表 JOIN 查询上 EX 显著提升（这是结构化元数据的核心价值） |
| 子集分析 | 按查询复杂度分层：单表 / 2 表 JOIN / 3+ 表 JOIN / 含业务术语 |

**实验 3：有/无变更传播（schema 变更后的 SQL 质量）**

| 配置 | 说明 |
|------|------|
| 时序设计 | T0: 正常状态，跑一轮 SQL 生成 → T1: 模拟 schema 变更（加字段/改类型） → T2: 再跑一轮 SQL 生成 |
| A | 无传播：T2 时 Agent 仍使用 T0 的旧 context（stale schema、旧查询模板） |
| B | 有传播：T1 变更触发传播引擎 → 标记 stale → 重新生成 L0/L1 → T2 时 Agent 用更新后的 context |
| 对比指标 | T2 的 EX 差异, 传播 token 消耗 |
| 预期结果 | A 在 T2 的 EX 下降（用了过时 schema），B 维持 T0 水平 |

**实验 4：有/无共享记忆（冷启动 vs 知识继承）**

| 配置 | 说明 |
|------|------|
| A (冷启动) | analysis-agent 没有任何历史记忆，从零开始 |
| B (知识继承) | query-agent 先跑 10-15 轮积累 cases → 晋升为团队共享 → analysis-agent 可检索 |
| 对比指标 | analysis-agent 的 EX, 首次正确回答所需轮次 |
| 预期结果 | B 的 EX 更高，尤其在 query-agent 已解决过的类似问题上 |
| 诚实说明 | 这个实验本质上测的是"更多相关上下文是否提升准确率"，信号可能与实验 2 重叠 |

### Tier 3: MVP 功能正确性测试（pass/fail）

这些不是 benchmark 指标，而是集成测试。用 pytest + httpx 直接测 ContextHub Server API。

**变更传播正确性**

| 测试用例 | 操作 | 预期结果 |
|----------|------|----------|
| P-1: Schema 变更 → 依赖标记 stale | ALTER TABLE orders ADD COLUMN discount DECIMAL | 所有依赖 orders 的 cases/patterns status='stale' |
| P-2: Breaking Skill 更新 → 订阅者标记 stale | 发布 sql-generator v3 (is_breaking=True) | 所有 pinned_version < 3 的订阅者的依赖 context status='stale' |
| P-3: Non-breaking Skill 更新 → 仅通知 | 发布 sql-generator v4 (is_breaking=False) | 依赖方 status 不变，change_events 有通知记录 |
| P-4: 统计信息更新 → 不传播 | UPDATE table_metadata SET stats = ... | 无 change_event，无 NOTIFY |
| P-5: 级联传播 | 表 A schema 变 → Skill X 依赖 A 被标记 stale → Case Y 依赖 Skill X | Case Y 也被标记 stale（二级传播） |
| P-6: 表删除 → 归档 + 传播 | CatalogConnector 检测到表被删除 | 表 context status='archived'，依赖方 status='stale' |
| P-7: NOTIFY 丢失 → 补扫恢复 | 传播引擎断开监听期间写入 change_event，随后恢复引擎或等待周期补扫 | 事件最终被处理，下游状态与正常路径一致 |
| P-8: worker crash / lease 超时 → retry 恢复 | 人工制造 `processing` 且 `claimed_at` 过期的事件 | `requeue_stuck_events` 回收为 `retry`，最终 `processed` |

**多 Agent 协作正确性**

| 测试用例 | 操作 | 预期结果 |
|----------|------|----------|
| C-1: 记忆晋升 | query-agent 的 case → promote 到 team/engineering | team 路径下可查到，dependencies 有 derived_from 记录 |
| C-2: 晋升后可见性 | analysis-agent search scope=team | 能检索到 query-agent 晋升的记忆 |
| C-3: 源记忆变更传播 | 修改 query-agent 的原始 case | 晋升后的团队记忆收到通知（derived_from 依赖） |
| C-4: Skill 版本订阅 | analysis-agent 订阅 sql-generator, pinned_version=2 | 检索时返回 v2 内容，不受 v3 发布影响 |
| C-5: Skill 订阅 latest | analysis-agent 订阅 sql-generator, pinned_version=NULL | 检索时返回最新版本 |

**可见性与隔离正确性（MVP）**

| 测试用例 | 操作 | 预期结果 |
|----------|------|----------|
| A-1: Agent 私有隔离 | query-agent 写入 ctx://agent/query-agent/... → analysis-agent 尝试读取 | 403 或空结果 |
| A-2: 团队层级继承 | 写入 ctx://team/engineering/ → backend 子团队 agent 读取 | 可见 |
| A-3: 子团队默认不向父团队暴露 | 写入 ctx://team/engineering/backend/... → engineering 父团队 agent 读取 | 403 或空结果 |
| A-4: promote 之后共享可见 | query-agent promote 到 team/engineering → analysis-agent 读取 | 可见 |

### Post-MVP 功能正确性（暂不纳入本次 MVP 退出）

**显式 ACL 正确性（post-MVP）**

| 测试用例 | 操作 | 预期结果 |
|----------|------|----------|
| A-5: Deny override | 父团队 deny 某资源 → 子团队 allow 同一资源 | deny 生效（deny-override） |
| A-6: 字段脱敏 | 标记某字段为 sensitive → Agent 检索 | 字段值替换为 [MASKED] |

**反馈与生命周期正确性**

| 测试用例 | 操作 | 预期结果 |
|----------|------|----------|
| F-1: 隐式反馈记录 | 检索 context → Agent 使用（adopted） | context_feedback 表有记录，adopted_count +1 |
| F-2: 质量评分计算 | 10 次 adopted + 5 次 ignored | quality_score = 10/(10+5+1) ≈ 0.625 |
| F-3: 低质量报告 | 构造高检索低采纳的 context | 出现在低质量报告 SQL 结果中 |
| F-4: 生命周期状态机 | active → 超过 stale_after_days 未访问 | status 变为 stale |
| F-5: Stale 自动恢复 | stale context 被直接访问 | status 恢复为 active |

### 量化可行性总结

| ContextHub 设计优势 | 可量化程度 | 最佳指标 | 评估层级 |
|---------------------|-----------|----------|----------|
| L0/L1/L2 分层检索 | 强 | Token per Query（预期降低 60-80%） | Tier 1 + A/B 实验 1 |
| 结构化上下文组装（关系+模板+术语） | 强 | 多表 JOIN 的 EX（预期提升显著） | Tier 1 + A/B 实验 2 |
| 精确变更传播（stats≠schema） | 强（token）| Propagation Token Cost（预期节省 99%） | Tier 1 + A/B 实验 3 |
| 传播后下游质量保持 | 中 | Schema 变更后 EX 不下降 | A/B 实验 3 |
| 多 Agent 知识共享 | 弱（与检索重叠）| 冷启动 vs 知识继承的 EX 差异 | A/B 实验 4 |
| ACL/隔离/版本管理 | 不可量化 | pass/fail | Tier 3 功能测试 |
| 反馈生命周期 | 不可量化（需长期数据）| pass/fail | post-MVP 功能测试 |

## 实施计划（先闭环，再载体）

MVP 的主实施路径是横向协作闭环，而不是某个垂直 demo。数据湖 / Text-to-SQL 继续保留为首个验证载体，但它排在协作主线之后落地。

### 团队与 Agent 预置

MVP 阶段团队结构（`teams`、`team_memberships`）通过初始 migration seed data 预置，不提供 CRUD API。验证场景所需的团队层级（如 `engineering`、`engineering/backend`、`data/analytics`）和 Agent 归属关系在 `001_initial_schema.py` 或独立的 seed 脚本中一次性写入。团队管理 API 属于 post-MVP 产品化需求。

### Phase 1: MVP Core Foundation（1-2 周）
1. 初始化项目（Python, FastAPI, pyproject.toml）
2. PG 数据库初始化：
   - 创建核心表：`contexts`、`dependencies`、`change_events`、`teams`、`team_memberships`、`skill_versions`、`skill_subscriptions`
   - 若保留首个垂直载体，则同时创建 `table_metadata`、`lineage`、`table_relationships`、`query_templates`
   - 创建索引 + 在核心表启用租户 RLS
   - 明确不进初始 migration：`access_policies`、`audit_log`、`lifecycle_policies`、`context_feedback`
3. 实现 request-scoped DB 执行模型：
   - `PgRepository.session(account_id) -> ScopedRepo`
   - middleware 只做认证与 `RequestContext` 注入，不做 `SET LOCAL`
4. 实现 `ACLService`（仅默认可见性 / 默认写权限）
5. 实现 `ContextStore`（只做 `read/write/ls/stat`，不做 search）

### Phase 2: MVP 协作闭环（3-4 周）
6. 多层级团队模型（`teams` + `team_memberships` + 递归 CTE 可见性展开）
7. `MemoryService`
   - 私有记忆写入
   - `promote`
   - `derived_from` 注册
8. `SkillService`
   - 版本发布
   - 订阅
   - `read_resolved()` pinned/latest 解析
9. `RetrievalService`
   - 唯一通用 search 入口
   - tool `grep`
   - 默认可见性过滤
10. `PropagationEngine`
   - `change_events` drain
   - startup / periodic sweep
   - retry / stuck lease recovery
11. OpenClaw Plugin + Python SDK
   - 只接 MVP 需要的工具
   - 跑通“私有写入 → promote → 跨 Agent 检索复用 → Skill 更新 → stale/advisory → recovery”

### Phase 3: Carrier-Specific 垂直载体（2-3 周）
12. `MockCatalogConnector` + `CatalogSyncService`
13. 数据湖表 L0/L1 自动生成
14. `sql-context` 组装逻辑（schema + 关系 + 模板 + 业务术语）
15. 企业数据分析 / Text-to-SQL demo

### Phase 4: 集成与评估（2-3 周）

**Step 16: 测试数据集准备**
- 构造多 Agent 工作流数据：私有写入、5-8 次记忆晋升、Skill 发布/订阅场景
- 构造传播场景：breaking / non-breaking Skill 版本更新、NOTIFY 丢失、lease 超时
- 如保留垂直载体，再准备 schema 变更与数据湖 demo 数据

**Step 17: Tier 3 功能正确性测试**
- pytest + httpx 实现传播 P-1~P-8、协作 C-1~C-5、可见性 A-1~A-4
- 额外验证 request-scoped `ScopedRepo` 执行模型和 pinned/latest 解析
- CI 集成

**Step 18: 端到端 demo**
- 默认 demo 路径：私有写入 → promote → analysis-agent 检索复用 → query-agent 发布 Skill → 下游 stale/advisory → retry/recovery
- 载体 demo 路径：企业数据分析 / Text-to-SQL，只作为附加垂直演示
- 记录共享记忆命中、版本解析和变更收敛时延等关键系统指标

**Step 19: 评估报告**
- Tier 3 功能测试结果
- 端到端 demo 录制/文档
- 与 Mem0/CrewAI/Governed Memory 的功能对比表
- 关键系统指标：传播命中率、变更收敛时延、权限泄漏率、事件丢失恢复能力、跨 Agent 复用信号
- Token 消耗对比只作为首个垂直载体的补充证据，而非 MVP 主 claim
- 诚实标注局限性，并把所有后置项统一指向 `14-adr-backlog-register.md`

## MVP 成功标准

| 维度 | 标准 | 说明 |
|------|------|------|
| 功能正确性 | Tier 3 MVP 测试全部 pass | 传播 P-1~P-8、协作 C-1~C-5、可见性 A-1~A-4 |
| 权限安全 | 权限泄漏率为 0 | 不允许出现越权可见或越权搜索结果 |
| 版本治理 | pinned/latest 解析正确 | C-4/C-5 通过，pinned 不被新版本污染 |
| 传播可靠性 | NOTIFY 丢失 / lease 超时后仍最终收敛 | P-7/P-8 通过，outbox 补偿设计成立 |
| 端到端 demo | 默认演示路径可稳定跑通 | 私有写入 → 晋升共享 → 跨 Agent 复用 → Skill 更新 → 下游感知 |
| 评估报告 | 输出关键系统指标 | 传播命中率、变更收敛时延、权限泄漏率、事件丢失恢复能力、跨 Agent 复用信号 |

### 后置项入口（非 MVP 范围）

MVP 之后不再在本文件里维护开放式“以后要做什么”清单。所有后置项统一收敛到 `14-adr-backlog-register.md`，当前已归类的条目包括：
1. 显式 ACL / 审计 / 窄范围共享
2. 反馈闭环与生命周期管理
3. ECMB 量化 benchmark
4. MCP Server
5. run snapshot / context bundle

## 技术选型

| 组件 | 推荐 | 理由 |
|------|------|------|
| Web 框架 | FastAPI | 异步、类型安全、OpenAPI 自动生成 |
| 主数据库 | PostgreSQL | 元数据 + 内容统一存储，ACID 事务，LISTEN/NOTIFY 驱动传播，RLS 租户隔离，递归 CTE 血缘查询 |
| PG 驱动 | asyncpg | 高性能异步 PG 客户端，原生支持 LISTEN/NOTIFY |
| 向量索引 | pgvector（PG 扩展） | 向量与元数据同库，天然事务一致，无双写对账问题。L0 摘要量级（万级）HNSW 索引完全胜任。消除独立向量库依赖，MVP 架构最简 |
| Embedding | text-embedding-3-small 或 BGE-M3 | 成本/效果平衡 |
| LLM（摘要生成） | Claude / GPT-4o-mini | L0/L1 生成不需要最强模型 |
| DB Migration | Alembic | PG schema 版本管理 |
| 测试 | pytest + pytest-asyncio | Python 标准 |

### 不再需要的组件

| 原方案组件 | 状态 | 原因 |
|-----------|------|------|
| ContentStore 接口（S3/LocalFS） | 移除 | 内容存 PG TEXT 列 |
| Event Log（JSON / Redis Streams） | 移除 | 变更事件存 PG change_events 表 |
| 独立审计日志存储 | 移除 | 审计日志存 PG audit_log 表 |
| 独立向量数据库（ChromaDB / Milvus） | 移除 | 向量索引由 pgvector 扩展承担，与 PG 同库，消除双写对账和额外基础设施 |

## 参考资料

- [OpenViking](https://github.com/volcengine/OpenViking) — 核心设计理念来源
- [Mem0](https://mem0.ai/blog/multi-agent-memory-systems) — 多 Agent 记忆系统
- [Letta Memory Blocks](https://www.letta.com/blog/memory-blocks) — Memory Block 设计
- [ContextBench](https://www.sundeepteki.org/blog/context-bench-a-benchmark-for-evaluating-agentic-context-engineering) — Agentic Context Engineering 评估
- [Spider](https://yale-lily.github.io/spider) / [BIRD](https://bird-bench.github.io/) — Text-to-SQL 评估数据集
