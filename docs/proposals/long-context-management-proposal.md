# ContextHub 长上下文管理与长文档检索增强提案

## Hierarchical Adaptive Context Distillation (HACD)：面向多 Agent 系统的分层自适应上下文蒸馏

---

## 1. 摘要

本提案为 ContextHub 引入**分层自适应上下文蒸馏（Hierarchical Adaptive Context Distillation, HACD）**机制，从 **Token 效率**和**检索准确性**两个维度同时提升多 Agent 系统的长上下文处理能力。

核心创新：
1. **Query-Aware Adaptive Distillation（查询感知自适应蒸馏）**：根据下游任务的语义意图，动态决定 L0/L1/L2 各层内容的裁剪粒度，而非当前的静态截断策略。
2. **Hierarchical Recursive Retrieval（分层递归检索）**：借鉴 OpenViking 的"目录递归检索"思想，在 ContextHub 的 `ctx://` URI 命名空间上实现树形结构感知的多轮精炼检索。
3. **Cross-Agent Context Deduplication（跨 Agent 上下文去重）**：在多 Agent 协作场景中，通过语义指纹识别和合并冗余上下文片段，减少总 Token 消耗。

预期效果：在保持或提升检索准确率的前提下，将多 Agent 场景的上下文 Token 消耗降低 40–60%。

---

## 2. 问题分析

### 2.1 现状诊断

当前 ContextHub 的上下文管理存在以下可量化的瓶颈：

| 问题 | 现状 | 影响 |
|------|------|------|
| **静态 L0/L1 生成** | `ContentGenerator` 使用固定字符截断（L0=80, L1=300 字符），不涉及语义理解 | 摘要质量低，关键信息可能在截断边界处丢失 |
| **平坦的向量检索** | `vector_search` 对整个 `contexts` 表做全局 cosine similarity 搜索 | 对大规模上下文库（>10K 条），检索精度随规模下降；无法利用 `ctx://` 的层级结构 |
| **L2 全量加载** | 当 `request.level == "L2"` 时，直接加载全部 `l2_content` | 对长文档（>50K tokens），单次加载可能超出下游模型的窗口上限 |
| **多 Agent 上下文冗余** | 多个 Agent 在 `assemble()` 阶段独立检索，注入重叠上下文 | 浪费 Token 预算；重复信息降低 LLM 注意力分配效率 |
| **无任务感知的检索** | 检索管道对所有查询使用相同的 `over_retrieve_factor` 和 rerank 策略 | 简单事实查询和复杂推理任务获得相同的上下文量，前者浪费、后者不足 |

### 2.2 与 OpenViking 的差异分析

OpenViking 已实现的相关能力（ContextHub 尚未具备）：

| OpenViking 能力 | ContextHub 对应 | 差距 |
|-----------------|-----------------|------|
| L0/L1/L2 三层语义生成（VLM驱动） | 固定字符截断 | 需引入 LLM/VLM 驱动的智能摘要 |
| 目录递归检索（Intent → 定位目录 → 递归精炼） | 单层向量搜索 + BM25 rerank | 需利用 `ctx://` 层级结构进行多级检索 |
| 检索轨迹可视化 | 无 | 需记录检索路径用于调试和优化 |
| 自动会话管理与记忆提取 | `afterTurn()` 存储记忆（OpenClaw 插件） | 基本具备，但缺乏压缩和去重 |

---

## 3. 技术方案

### 3.1 Query-Aware Adaptive Distillation（查询感知自适应蒸馏）

#### 3.1.1 设计原理

当前 `ContentGenerator` 使用固定截断逻辑：

```python
# 现有实现 (generation/base.py)
_TRUNCATE_L0 = 80
_TRUNCATE_L1 = 300

def _generate_fallback(self, raw: str) -> GeneratedContent:
    l0 = raw[:_TRUNCATE_L0]
    l1 = raw[:_TRUNCATE_L1]
    return GeneratedContent(l0=l0, l1=l1)
```

**改进方案**：引入两阶段蒸馏管道。

**阶段一：写入时智能摘要（Write-Time Intelligent Summarization）**

在 `IndexerService.generate()` 中调用 LLM 生成语义摘要：

```python
class AdaptiveContentGenerator(ContentGenerator):
    def __init__(self, llm_client: LLMClient):
        self._llm = llm_client

    async def generate(
        self,
        context_type: str,
        raw_content: str,
        metadata: dict | None = None,
    ) -> GeneratedContent:
        l2_tokens = count_tokens(raw_content)

        if l2_tokens <= L0_TOKEN_BUDGET:
            return GeneratedContent(l0=raw_content, l1=raw_content)

        l0 = await self._llm.summarize(
            raw_content,
            instruction="Generate a single-sentence abstract (max 100 tokens) "
                        "capturing the core topic, entities, and purpose.",
            max_tokens=100,
        )
        l1 = await self._llm.summarize(
            raw_content,
            instruction="Generate a structured overview (max 2000 tokens) "
                        "including: key concepts, relationships, usage scenarios, "
                        "and important details.",
            max_tokens=2000,
        )
        return GeneratedContent(l0=l0, l1=l1, llm_tokens_used=l2_tokens)
```

**阶段二：读取时查询感知裁剪（Read-Time Query-Aware Trimming）**

在 `RetrievalService.search()` 返回结果后，根据查询意图对 L1/L2 内容进行动态裁剪：

```python
class QueryAwareDistiller:
    """根据查询意图，从 L2 内容中提取与查询最相关的片段。"""

    async def distill(
        self,
        query: str,
        l2_content: str,
        token_budget: int,
        strategy: DistillStrategy = DistillStrategy.EXTRACTIVE,
    ) -> DistilledContent:
        if strategy == DistillStrategy.EXTRACTIVE:
            return await self._extractive_distill(query, l2_content, token_budget)
        return await self._abstractive_distill(query, l2_content, token_budget)

    async def _extractive_distill(
        self, query: str, content: str, budget: int
    ) -> DistilledContent:
        chunks = self._split_into_semantic_chunks(content)
        query_embedding = await self._embed(query)
        chunk_scores = []
        for chunk in chunks:
            chunk_emb = await self._embed(chunk.text)
            score = cosine_similarity(query_embedding, chunk_emb)
            chunk_scores.append((chunk, score))

        chunk_scores.sort(key=lambda x: x[1], reverse=True)

        selected = []
        used_tokens = 0
        for chunk, score in chunk_scores:
            if used_tokens + chunk.token_count > budget:
                break
            selected.append(chunk)
            used_tokens += chunk.token_count

        # 按原始顺序排列以保持连贯性
        selected.sort(key=lambda c: c.position)
        return DistilledContent(
            text="\n".join(c.text for c in selected),
            token_count=used_tokens,
            coverage_ratio=used_tokens / count_tokens(content),
            chunks_selected=len(selected),
            chunks_total=len(chunks),
        )
```

#### 3.1.2 Token 预算自适应

引入查询复杂度评估器，根据查询的复杂度动态调整 Token 预算：

```python
class TokenBudgetAllocator:
    """根据查询复杂度和上下文类型分配 Token 预算。"""

    COMPLEXITY_PROFILES = {
        QueryComplexity.FACTUAL: {
            "per_result_budget": 500,
            "max_results": 3,
            "prefer_level": ContextLevel.L1,
        },
        QueryComplexity.ANALYTICAL: {
            "per_result_budget": 2000,
            "max_results": 5,
            "prefer_level": ContextLevel.L2,
        },
        QueryComplexity.SYNTHESIS: {
            "per_result_budget": 4000,
            "max_results": 8,
            "prefer_level": ContextLevel.L2,
        },
    }

    async def classify_complexity(self, query: str) -> QueryComplexity:
        # 基于查询特征的轻量级分类（无需 LLM 调用）
        ...

    def allocate(
        self, complexity: QueryComplexity, total_budget: int
    ) -> BudgetAllocation:
        profile = self.COMPLEXITY_PROFILES[complexity]
        per_result = min(profile["per_result_budget"], total_budget // profile["max_results"])
        return BudgetAllocation(
            per_result_tokens=per_result,
            max_results=profile["max_results"],
            prefer_level=profile["prefer_level"],
        )
```

### 3.2 Hierarchical Recursive Retrieval（分层递归检索）

#### 3.2.1 设计原理

借鉴 OpenViking 的"目录递归检索策略"，充分利用 ContextHub 的 `ctx://` URI 层级结构。当前的 `vector_search` 和 `keyword_search` 忽略了 URI 中蕴含的层级信息。

**核心思路**：将 `ctx://` URI 空间看作一棵虚拟文件树，检索过程变为"从根到叶的逐层聚焦"：

```
ctx://
├── team/engineering/
│   ├── memories/shared_knowledge/  ← 目录级 L0 摘要
│   │   ├── monthly-sales-pattern   ← 叶节点
│   │   └── api-rate-limit-pattern  ← 叶节点
│   └── skills/
│       ├── sql-generator/          ← 目录级 L0 摘要
│       │   ├── v1
│       │   └── v2
│       └── data-pipeline/
├── agent/query-agent/
│   └── memories/
└── datalake/mock/prod/
    ├── orders                      ← table_schema
    └── users                       ← table_schema
```

#### 3.2.2 检索管道

```python
class HierarchicalRetriever:
    """分层递归检索器。"""

    async def search(
        self,
        db: ScopedRepo,
        query: str,
        query_embedding: list[float],
        top_k: int,
        max_depth: int = 3,
    ) -> HierarchicalSearchResult:

        trajectory = RetrievalTrajectory()

        # Step 1: 意图分析 — 生成多路检索条件
        intents = await self._analyze_intent(query)
        trajectory.record_step("intent_analysis", intents)

        # Step 2: 目录级初始定位 — 找到高分目录
        directory_scores = await self._score_directories(
            db, query_embedding, intents
        )
        top_dirs = sorted(
            directory_scores, key=lambda d: d.score, reverse=True
        )[:5]
        trajectory.record_step("directory_positioning", top_dirs)

        # Step 3: 目录内精炼检索
        candidates = []
        for dir_entry in top_dirs:
            dir_results = await self._search_within_directory(
                db, dir_entry.uri_prefix, query_embedding, top_k
            )
            candidates.extend(dir_results)
            trajectory.record_step(
                f"refine:{dir_entry.uri_prefix}", dir_results
            )

            # Step 4: 子目录递归
            if dir_entry.has_subdirectories and max_depth > 1:
                sub_results = await self._recursive_search(
                    db, dir_entry.uri_prefix,
                    query_embedding, top_k,
                    depth=max_depth - 1,
                )
                candidates.extend(sub_results)

        # Step 5: 全局聚合与去重
        final = self._aggregate_and_deduplicate(candidates, top_k)
        trajectory.record_step("aggregation", final)

        return HierarchicalSearchResult(
            results=final,
            trajectory=trajectory,
        )
```

#### 3.2.3 目录摘要索引

为每个 URI 前缀（目录）维护一个聚合摘要（类似 OpenViking 的 `.abstract` 和 `.overview` 文件），存储在新的 `directory_summaries` 表中：

```sql
CREATE TABLE directory_summaries (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id  TEXT NOT NULL,
    uri_prefix  TEXT NOT NULL,
    depth       INT NOT NULL,
    summary     TEXT NOT NULL,
    embedding   vector(1536),
    child_count INT NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (account_id, uri_prefix)
);

CREATE INDEX idx_dir_summary_embedding
    ON directory_summaries USING ivfflat (embedding vector_cosine_ops);
```

目录摘要通过 `PropagationEngine` 增量维护：当叶节点的 `change_events` 触发时，自动重新生成所属目录的摘要。

### 3.3 Cross-Agent Context Deduplication（跨 Agent 上下文去重）

#### 3.3.1 问题

在多 Agent 场景中，当 `assemble()` 被多个 Agent 并发调用时，不同 Agent 可能检索到语义重叠的上下文（例如同一条被 promote 到团队的 memory 会被所有 Agent 注入）。当这些上下文通过 Agent 间通信汇聚时，冗余加剧。

#### 3.3.2 方案：语义指纹去重

```python
class ContextDeduplicator:
    """使用 SimHash 语义指纹对上下文进行近似去重。"""

    def __init__(self, similarity_threshold: float = 0.85):
        self._threshold = similarity_threshold

    async def deduplicate(
        self,
        contexts: list[SearchResult],
        query: str,
    ) -> DeduplicationResult:
        if len(contexts) <= 1:
            return DeduplicationResult(
                deduplicated=contexts, removed=[], savings_ratio=0.0
            )

        fingerprints = {}
        unique = []
        removed = []

        for ctx in contexts:
            fp = self._compute_fingerprint(ctx.l0_content or ctx.l1_content or "")

            is_duplicate = False
            for existing_fp, existing_ctx in fingerprints.items():
                if self._hamming_similarity(fp, existing_fp) > self._threshold:
                    # 保留得分更高的版本
                    if ctx.score > existing_ctx.score:
                        unique.remove(existing_ctx)
                        removed.append(existing_ctx)
                        unique.append(ctx)
                        del fingerprints[existing_fp]
                        fingerprints[fp] = ctx
                    else:
                        removed.append(ctx)
                    is_duplicate = True
                    break

            if not is_duplicate:
                fingerprints[fp] = ctx
                unique.append(ctx)

        original_tokens = sum(estimate_tokens(c) for c in contexts)
        deduped_tokens = sum(estimate_tokens(c) for c in unique)

        return DeduplicationResult(
            deduplicated=unique,
            removed=removed,
            savings_ratio=1 - (deduped_tokens / original_tokens) if original_tokens > 0 else 0,
        )
```

### 3.4 增强的检索管道（完整流程）

将上述三个模块集成到 `RetrievalService` 中：

```
┌─────────────────────────────────────────────────────────────┐
│                  Enhanced Retrieval Pipeline                 │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  1. Query Analysis                                          │
│     ├── Complexity Classification (factual/analytical/...)  │
│     ├── Token Budget Allocation                             │
│     └── Intent Decomposition (multi-intent queries)        │
│                                                             │
│  2. Hierarchical Recursive Retrieval                        │
│     ├── Directory-Level Positioning (directory_summaries)   │
│     ├── In-Directory Vector Search                          │
│     ├── Recursive Sub-Directory Exploration                 │
│     └── Retrieval Trajectory Recording                      │
│                                                             │
│  3. Post-Retrieval Processing                               │
│     ├── BM25 Reranking (existing)                          │
│     ├── Stale Penalty (existing)                           │
│     ├── ACL Filtering (existing)                           │
│     ├── Cross-Agent Deduplication (NEW)                    │
│     └── Query-Aware Distillation (NEW)                     │
│                                                             │
│  4. Budget-Constrained Assembly                             │
│     ├── Adaptive Level Selection (L0/L1/L2 per result)     │
│     ├── Extractive Trimming (for L2 over-budget)           │
│     └── Token Count Verification                            │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 4. 数据库 Schema 变更

### 4.1 新增表

```sql
-- 目录摘要索引（分层检索的核心数据结构）
CREATE TABLE directory_summaries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      TEXT NOT NULL,
    uri_prefix      TEXT NOT NULL,
    depth           INT NOT NULL,
    summary_l0      TEXT,
    summary_l1      TEXT,
    embedding       vector(1536),
    child_count     INT NOT NULL DEFAULT 0,
    total_tokens    BIGINT NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (account_id, uri_prefix)
);

-- 语义指纹表（去重加速）
CREATE TABLE context_fingerprints (
    context_id      UUID NOT NULL REFERENCES contexts(id) ON DELETE CASCADE,
    fingerprint     BIGINT NOT NULL,
    account_id      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (context_id)
);

CREATE INDEX idx_fingerprint_lookup
    ON context_fingerprints (account_id, fingerprint);

-- 检索轨迹记录（可观测性）
CREATE TABLE retrieval_trajectories (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    query           TEXT NOT NULL,
    query_complexity TEXT,
    steps           JSONB NOT NULL DEFAULT '[]',
    results_count   INT NOT NULL DEFAULT 0,
    token_budget    INT,
    tokens_used     INT,
    duration_ms     FLOAT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### 4.2 contexts 表扩展

```sql
ALTER TABLE contexts ADD COLUMN token_count INT;
ALTER TABLE contexts ADD COLUMN semantic_fingerprint BIGINT;
ALTER TABLE contexts ADD COLUMN distillation_version INT DEFAULT 0;
```

---

## 5. API 变更

### 5.1 增强现有 API

#### `/api/v1/search` — 增加参数

```json
{
    "query": "string",
    "top_k": 10,
    "level": "L1",
    "token_budget": 8000,
    "retrieval_strategy": "hierarchical",
    "enable_deduplication": true,
    "enable_distillation": true,
    "return_trajectory": false
}
```

#### `/api/v1/search` — 响应增强

```json
{
    "results": [...],
    "total": 5,
    "metadata": {
        "strategy_used": "hierarchical",
        "query_complexity": "analytical",
        "token_budget": 8000,
        "tokens_used": 6240,
        "deduplication": {
            "original_count": 8,
            "deduplicated_count": 5,
            "savings_ratio": 0.35
        },
        "trajectory": {
            "steps": [...],
            "directories_explored": 3,
            "depth_reached": 2
        }
    }
}
```

### 5.2 新增 API

#### `POST /api/v1/search/distill`

针对已知上下文的 query-aware 蒸馏：

```json
// Request
{
    "query": "How to handle rate limiting in our API?",
    "context_uris": ["ctx://team/engineering/memories/shared_knowledge/api-rate-limit"],
    "token_budget": 2000,
    "strategy": "extractive"
}

// Response
{
    "distilled": [
        {
            "uri": "ctx://team/engineering/memories/shared_knowledge/api-rate-limit",
            "original_tokens": 15000,
            "distilled_tokens": 1800,
            "content": "...",
            "coverage_ratio": 0.78,
            "chunks_selected": 4,
            "chunks_total": 12
        }
    ],
    "total_tokens_saved": 13200
}
```

#### `GET /api/v1/directories/{uri_prefix}/summary`

获取目录摘要：

```json
{
    "uri_prefix": "ctx://team/engineering/memories/",
    "summary_l0": "Engineering team's shared knowledge covering API patterns, SQL optimizations, and operational procedures.",
    "summary_l1": "...",
    "child_count": 47,
    "total_tokens": 285000,
    "subdirectories": [
        {"prefix": "ctx://team/engineering/memories/shared_knowledge/", "count": 23},
        {"prefix": "ctx://team/engineering/memories/operational/", "count": 24}
    ]
}
```

#### `GET /api/v1/retrieval/trajectory/{trajectory_id}`

获取检索轨迹详情（可观测性）：

```json
{
    "id": "uuid",
    "query": "...",
    "query_complexity": "analytical",
    "steps": [
        {
            "type": "intent_analysis",
            "intents": ["api_pattern", "rate_limiting"],
            "duration_ms": 45
        },
        {
            "type": "directory_positioning",
            "directories_scored": 12,
            "top_directories": ["ctx://team/engineering/memories/shared_knowledge/"],
            "duration_ms": 23
        },
        {
            "type": "in_directory_search",
            "directory": "ctx://team/engineering/memories/shared_knowledge/",
            "candidates_found": 8,
            "duration_ms": 15
        }
    ],
    "total_duration_ms": 120
}
```

---

## 6. 实验设计与 Benchmark

### 6.1 Benchmark 总览

设计三套 Benchmark，分别评估 Token 效率、检索准确性和端到端 Agent 性能：

| Benchmark | 目标维度 | 关键指标 | 数据集规模 |
|-----------|----------|----------|------------|
| HACD-TokenEff | Token 效率 | Token Savings Ratio, Information Retention Rate | 1K–50K contexts |
| HACD-Retrieval | 检索准确性 | Recall@K, NDCG@K, MRR | 5K–50K contexts |
| HACD-E2E | 端到端效果 | Task Completion Rate, Token Cost, Latency | 1K–10K contexts |

### 6.2 Benchmark 1：Token 效率评测（HACD-TokenEff）

#### 数据集构建

| 类别 | 来源 | 条目数 | 平均 Token 数 |
|------|------|--------|--------------|
| 短记忆 | 合成 Agent 交互日志 | 5,000 | 50–200 |
| 中等文档 | Wikipedia 段落 / arXiv 摘要 | 3,000 | 500–2,000 |
| 长文档 | 技术文档 / RFC / API 参考手册 | 1,000 | 5,000–50,000 |
| SQL Schema | TPC-H/TPC-DS 表元数据 | 500 | 200–1,000 |
| 混合 | 上述按比例混合 | 10,000 | 混合 |

#### 实验组设置

| 实验组 | L0/L1 生成 | 检索时蒸馏 | 去重 | 说明 |
|--------|-----------|-----------|------|------|
| Baseline-Truncate | 固定截断 (80/300 char) | 无 | 无 | 当前 ContextHub |
| Baseline-OpenViking | VLM 摘要 | 无 | 无 | 模拟 OpenViking 方案 |
| HACD-Summarize | LLM 智能摘要 | 无 | 无 | 仅改进写入时摘要 |
| HACD-Distill | LLM 智能摘要 | Query-Aware 抽取 | 无 | + 读取时蒸馏 |
| HACD-Full | LLM 智能摘要 | Query-Aware 抽取 | SimHash 去重 | 完整方案 |

#### 评测指标

```python
class TokenEfficiencyMetrics:
    """Token 效率评测指标集。"""

    @staticmethod
    def token_savings_ratio(original_tokens: int, compressed_tokens: int) -> float:
        """Token 节省率 = 1 - (压缩后 / 原始)"""
        return 1 - (compressed_tokens / original_tokens)

    @staticmethod
    def information_retention_rate(
        original_content: str,
        compressed_content: str,
        eval_questions: list[str],
        llm_judge: LLMClient,
    ) -> float:
        """信息保留率：用 LLM 判断压缩后内容能否回答原始内容可回答的问题。
        
        IRR = (压缩后可回答的问题数) / (原始内容可回答的问题数)
        """
        ...

    @staticmethod
    def compression_quality_score(
        savings_ratio: float,
        retention_rate: float,
        alpha: float = 0.5,
    ) -> float:
        """综合压缩质量分 = alpha * savings + (1-alpha) * retention"""
        return alpha * savings_ratio + (1 - alpha) * retention_rate

    @staticmethod
    def deduplication_precision(
        removed_items: list,
        ground_truth_duplicates: list,
    ) -> float:
        """去重精确率：被移除项中确实是重复项的比例。"""
        ...
```

#### 评测流程

```
对每个 (query, ground_truth_contexts) 对：

1. 将 ground_truth_contexts 写入 ContextHub
2. 对每个实验组：
   a. 执行检索，记录返回的 contexts 和 token 数
   b. 计算 token_savings_ratio
   c. 使用 LLM-as-judge 评估 information_retention_rate
   d. 记录端到端延迟
3. 汇总各组的平均指标
```

### 6.3 Benchmark 2：检索准确性评测（HACD-Retrieval）

#### 数据集构建

基于 ContextHub 的四种 context type 构建评测集：

| 子集 | Query 数 | 每 Query 相关文档数 | 总库规模 | 说明 |
|------|---------|-------------------|---------|------|
| Memory-QA | 200 | 1–3 | 5,000 memories | "之前学到的 X 模式是什么？" |
| Skill-Discovery | 100 | 1–5 | 500 skills | "有没有处理 Y 的技能？" |
| Resource-Deep | 150 | 2–8 | 2,000 resources | "文档中关于 Z 的详细说明" |
| Schema-SQL | 100 | 1–10 | 300 schemas | "哪些表包含用户订单数据？" |
| Cross-Type | 150 | 3–10 (跨类型) | 混合 | "完成任务 W 需要哪些上下文？" |

每条 query 附带人工标注的相关 context URI 列表及相关性等级（0/1/2）。

#### 实验组设置

| 实验组 | 检索策略 | 说明 |
|--------|---------|------|
| Flat-Vector | 全局 pgvector cosine | 当前 ContextHub |
| Flat-Vector+BM25 | 全局 pgvector + BM25 rerank | 当前 ContextHub (完整管道) |
| Hierarchical-2 | 分层递归 (max_depth=2) | 本方案 |
| Hierarchical-3 | 分层递归 (max_depth=3) | 本方案 (更深递归) |
| Hierarchical-Adaptive | 分层递归 + 查询复杂度自适应 | 本方案 (完整) |
| OpenViking-Style | 模拟 OpenViking 目录递归 | 对照组 |

#### 评测指标

```python
class RetrievalAccuracyMetrics:

    @staticmethod
    def recall_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
        """Recall@K = |retrieved[:k] ∩ relevant| / |relevant|"""
        retrieved_set = set(retrieved[:k])
        relevant_set = set(relevant)
        return len(retrieved_set & relevant_set) / len(relevant_set)

    @staticmethod
    def ndcg_at_k(
        retrieved: list[str],
        relevance_scores: dict[str, int],
        k: int,
    ) -> float:
        """NDCG@K：考虑位置和相关性等级的排序质量。"""
        dcg = sum(
            relevance_scores.get(uri, 0) / math.log2(i + 2)
            for i, uri in enumerate(retrieved[:k])
        )
        ideal = sorted(relevance_scores.values(), reverse=True)[:k]
        idcg = sum(
            score / math.log2(i + 2) for i, score in enumerate(ideal)
        )
        return dcg / idcg if idcg > 0 else 0

    @staticmethod
    def mrr(retrieved: list[str], relevant: list[str]) -> float:
        """Mean Reciprocal Rank：第一个相关结果的排名倒数。"""
        for i, uri in enumerate(retrieved):
            if uri in relevant:
                return 1.0 / (i + 1)
        return 0.0

    @staticmethod
    def cross_type_coverage(
        retrieved: list[dict],
        relevant_by_type: dict[str, list[str]],
    ) -> dict[str, float]:
        """跨类型覆盖率：每种 context_type 的检出比例。"""
        coverage = {}
        for ctype, relevant_uris in relevant_by_type.items():
            found = sum(
                1 for r in retrieved
                if r["uri"] in relevant_uris and r["context_type"] == ctype
            )
            coverage[ctype] = found / len(relevant_uris) if relevant_uris else 0
        return coverage
```

### 6.4 Benchmark 3：端到端 Agent 性能评测（HACD-E2E）

#### 评测场景

基于 OpenViking 在 OpenClaw 上的评测方法论（LoCoMo 数据集），扩展为多 Agent 场景：

| 场景 | Agent 数 | 上下文规模 | 任务类型 | 数据源 |
|------|---------|-----------|---------|--------|
| Single-Agent Memory QA | 1 | 500–2000 memories | 长对话记忆检索 | LoCoMo 改编 |
| Multi-Agent Collaboration | 2–4 | 1000–5000 mixed | 任务分解与知识共享 | 合成 |
| Long-Document QA | 1 | 10–50 long docs (>10K tokens each) | 文档问答 | NarrativeQA / QuALITY 改编 |
| Schema-Aware SQL | 1 | 100–500 schemas | Text-to-SQL | Spider/BIRD 改编 |

#### 实验组设置

| 实验组 | 上下文引擎 | 说明 |
|--------|-----------|------|
| No-Context | 无上下文注入 | 基线（纯 LLM） |
| Naive-RAG | 标准向量检索 + 全量注入 | 传统 RAG |
| ContextHub-Current | 当前 ContextHub（截断 + 平坦检索） | 现状基线 |
| ContextHub-HACD | ContextHub + HACD 完整方案 | 本提案 |
| OpenViking-Reference | OpenViking 0.1.18 | 参考对照 |

#### 评测指标

```python
class EndToEndMetrics:

    @staticmethod
    def task_completion_rate(
        results: list[TaskResult],
        judge: LLMClient,
    ) -> float:
        """任务完成率（LLM-as-judge 评分 >= 阈值的比例）。"""
        ...

    @staticmethod
    def token_efficiency_ratio(
        tokens_used: int,
        task_completion_rate: float,
    ) -> float:
        """Token 效率比 = 任务完成率 / 每千 Token 消耗"""
        return task_completion_rate / (tokens_used / 1000)

    @staticmethod
    def context_utilization_rate(
        contexts_retrieved: int,
        contexts_actually_used: int,
    ) -> float:
        """上下文利用率：实际被 Agent 引用的上下文占检索到的上下文的比例。"""
        return contexts_actually_used / contexts_retrieved if contexts_retrieved > 0 else 0

    @staticmethod
    def multi_agent_redundancy_rate(
        total_contexts_across_agents: int,
        unique_contexts: int,
    ) -> float:
        """多 Agent 冗余率 = 1 - (去重后 / 总量)"""
        return 1 - (unique_contexts / total_contexts_across_agents)
```

### 6.5 Benchmark 运行基础设施

扩展现有的 `scripts/benchmark_workflow.py` 框架：

```python
# scripts/benchmark_hacd.py

SUITE_MAP = {
    "token_eff":   ("Token Efficiency",       suite_token_efficiency),
    "retrieval":   ("Retrieval Accuracy",      suite_retrieval_accuracy),
    "e2e_single":  ("E2E Single Agent",        suite_e2e_single_agent),
    "e2e_multi":   ("E2E Multi Agent",         suite_e2e_multi_agent),
    "scalability": ("Scalability",             suite_scalability),
}

# 使用方式:
# python scripts/benchmark_hacd.py                      # 全部
# python scripts/benchmark_hacd.py --suite token_eff    # 仅 Token 效率
# python scripts/benchmark_hacd.py --suite retrieval --scale 50000  # 50K 规模检索测试
```

### 6.6 预期结果

基于 OpenViking 的公开评测数据和相关文献，预估各 Benchmark 的目标数值：

#### Token 效率目标

| 指标 | Baseline (当前) | HACD-Full (目标) | 参考依据 |
|------|----------------|-----------------|---------|
| Token Savings Ratio (短文本) | 0% | 20–30% | 去重为主 |
| Token Savings Ratio (中等文档) | 0% | 40–50% | 蒸馏 + 去重 |
| Token Savings Ratio (长文档) | 0% | 60–75% | 分层蒸馏主导 |
| Information Retention Rate | 100% (无压缩) | ≥ 90% | OpenViking L0/L1 保留率参考 |
| Multi-Agent Dedup Savings | 0% | 25–40% | 基于 2-4 Agent 共享场景 |

#### 检索准确性目标

| 指标 | Flat-Vector (当前) | Hierarchical-Adaptive (目标) | 提升幅度 |
|------|-------------------|-------------------------------|---------|
| Recall@5 | ~0.60 | ≥ 0.78 | +30% |
| Recall@10 | ~0.72 | ≥ 0.88 | +22% |
| NDCG@10 | ~0.55 | ≥ 0.72 | +31% |
| MRR | ~0.50 | ≥ 0.68 | +36% |
| Cross-Type Coverage | ~0.45 | ≥ 0.70 | +56% |

#### 端到端目标

| 指标 | ContextHub-Current | ContextHub-HACD (目标) | OpenViking (参考) |
|------|-------------------|------------------------|-------------------|
| Task Completion Rate (Single) | ~45% | ≥ 55% | 52% |
| Task Completion Rate (Multi) | ~35% | ≥ 50% | N/A |
| Input Token Cost | 100% | ≤ 45% | ~17% (LoCoMo) |
| P95 Retrieval Latency | ~120ms | ≤ 200ms | N/A |

---

## 7. 实施计划

### Phase 1：智能摘要生成（基础设施）

**涉及组件**：`generation/`, `services/indexer_service.py`, `config.py`

- 实现 `AdaptiveContentGenerator`，支持 LLM 驱动的 L0/L1 生成
- 扩展 `Settings` 添加 `llm_provider`、`summarization_model` 等配置
- 添加 `token_count` 字段到 `contexts` 表
- 回填脚本：对存量 contexts 重新生成 L0/L1
- 单元测试：验证不同长度文档的摘要质量

### Phase 2：分层递归检索

**涉及组件**：`retrieval/`, `services/retrieval_service.py`, `store/`, `db/`, Alembic migration

- 创建 `directory_summaries` 表和 migration
- 实现 `HierarchicalRetriever`
- 实现目录摘要的增量维护（通过 `PropagationEngine`）
- 在 `RetrievalRouter` 中添加策略路由（flat vs hierarchical）
- 实现检索轨迹记录

### Phase 3：上下文去重与蒸馏

**涉及组件**：`retrieval/`, `services/`, `api/routers/search.py`

- 实现 `ContextDeduplicator`（SimHash 语义指纹）
- 实现 `QueryAwareDistiller`（extractive 模式优先）
- 实现 `TokenBudgetAllocator`
- 扩展搜索 API（`token_budget`、`enable_deduplication` 等参数）
- 新增 `/search/distill` API

### Phase 4：Benchmark 与评测

**涉及组件**：`scripts/`, `tests/`, `docs/`

- 构建评测数据集（合成 + 改编公开数据集）
- 实现 `scripts/benchmark_hacd.py`
- 运行完整 Benchmark，生成对比报告
- 性能调优（索引参数、递归深度、Token 预算阈值等）

### Phase 5：OpenClaw/Agent 集成

**涉及组件**：`plugins/openclaw/`, `bridge/`

- 修改 `assemble()` 使用增强检索管道
- 添加 `token_budget` 参数到 Agent 配置
- 实现多 Agent 共享上下文去重
- 端到端集成测试

---

## 8. 风险与缓解措施

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| LLM 摘要引入延迟 | 写入时延增加 2–5 秒 | 异步处理 + 后台任务队列；先写入原始内容，异步更新摘要 |
| LLM 摘要成本 | 每次写入消耗 ~500 tokens | 批量处理 + 缓存 + 仅对 >500 token 的文档启用 LLM 摘要 |
| 目录摘要维护开销 | 高频写入场景下摘要更新风暴 | 防抖机制（debounce）+ 异步更新 + 变更合并窗口 |
| 分层检索增加延迟 | 多轮数据库查询 | 限制 max_depth + 并行化目录内搜索 + 目录摘要预加载 |
| SimHash 去重精度 | 语义相近但不同的内容被错误去重 | 可配置阈值 + 二次校验 + 不删除原始数据，只在装配时过滤 |
| Embedding 维度不匹配 | 目录摘要和叶节点使用不同 embedding | 统一使用相同的 embedding model/dimension |

---

## 9. 成功标准

本提案在以下所有条件满足时视为成功：

| 标准 | 目标值 | 验证方式 |
|------|--------|---------|
| 长文档 Token 节省率 | ≥ 50% | HACD-TokenEff Benchmark |
| 信息保留率 | ≥ 88% | HACD-TokenEff Benchmark (LLM-as-judge) |
| Recall@10 提升 | ≥ 15% (相对) | HACD-Retrieval Benchmark |
| NDCG@10 提升 | ≥ 20% (相对) | HACD-Retrieval Benchmark |
| 端到端任务完成率提升 | ≥ 10% (绝对) | HACD-E2E Benchmark |
| 多 Agent 总 Token 消耗 | ≤ 50% of baseline | HACD-E2E Benchmark |
| P95 检索延迟 | ≤ 250ms (10K contexts) | 性能测试 |
| 所有现有测试通过 | 100% | `pytest` + `benchmark_workflow.py` |

---

## 10. 参考文献

1. OpenViking: The Context Database for AI Agents. https://github.com/volcengine/OpenViking
2. Zylos Research (2026). "AI Agent Memory Architectures: Shared, Isolated, Hierarchical."
3. Cemri et al. (2025). "Multi-Agent Collaboration Failures." arXiv:2503.13657.
4. Taheri (2026). "Governed Memory." arXiv:2603.17787.
5. LoCoMo: Long Context Memory Benchmark. https://github.com/snap-research/locomo
6. NarrativeQA: Reading Comprehension over Long Documents. https://github.com/google-deepmind/narrativeqa
7. QuALITY: Question Answering with Long Input Texts. https://github.com/nyu-mll/quality
8. Spider: Text-to-SQL Benchmark. https://yale-lily.github.io/spider
9. BIRD: Big Bench for Large-Scale Database Grounded Text-to-SQL. https://bird-bench.github.io/
10. Model Context Protocol. Anthropic, 2024.
