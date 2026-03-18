# 07 — 上下文质量反馈与生命周期管理

## 质量反馈闭环

### 问题

当前系统是开环的：提供上下文给 Agent，但不知道这些上下文是否真的有用。没有反馈信号，检索质量无法迭代优化。

### (1) 隐式反馈采集

从 Agent 的后续行为中推断上下文质量，不要求显式评分：

```python
@dataclass
class ContextFeedback:
    context_uri: str        # 被检索到的上下文 URI
    session_id: str
    retrieved_at: datetime
    outcome: str            # adopted | ignored | corrected | irrelevant
    metadata: dict

# 推断规则：
# - adopted:    Agent 在后续生成中引用了该上下文的内容（文本相似度 > 阈值）
# - ignored:    上下文被检索返回但 Agent 未在生成中使用
# - corrected:  Agent 使用后被用户纠正
# - irrelevant: Agent 显式跳过
```

### (2) 反馈信号回写

融入热度评分机制：

```python
# 原始: score = sigmoid(log1p(active_count)) * exponential_decay(updated_at)
# 新增: quality_score = adopted_count / (adopted_count + ignored_count + 1)
# 综合: final_score = score * (0.5 + 0.5 * quality_score)

# 效果：
# - 高检索量 + 高采纳率 → 分数高（优质上下文）
# - 高检索量 + 低采纳率 → 分数被压低（噪音上下文）
# - 低检索量 → quality_score 趋近 0.5（数据不足，不惩罚）
```

### (3) 低质量上下文报告

定期生成（如每周），供管理员审查：高检索+低采纳的上下文、频繁被纠正的上下文、整体质量趋势。

---

## 生命周期管理

### 上下文状态机

```
          创建 ──→  active  ←── 被访问/更新时重置
                      │ 标记过时(变更传播) 或 超过 N 天未访问
                      ▼
                    stale   ←── STALE 标记的上下文
                      │ 超过 M 天仍为 stale 且未被访问
                      ▼
                   archived ←── 移出向量索引，保留文件
                      │ 超过 K 天（可选）
                      ▼
                   deleted  ←── 移至冷存储或删除
```

### 生命周期策略配置

```python
@dataclass
class LifecyclePolicy:
    context_type: str       # resource | memory | skill
    scope: str              # agent | team | datalake
    stale_after_days: int   # 未访问 N 天后标记为 stale（0 = 不自动标记）
    archive_after_days: int # stale 状态持续 M 天后归档
    delete_after_days: int  # 归档后 K 天删除（0 = 永不删除）

DEFAULT_POLICIES = [
    LifecyclePolicy("memory", "agent",    stale_after_days=90,  archive_after_days=30, delete_after_days=180),
    LifecyclePolicy("memory", "team",     stale_after_days=0,   archive_after_days=60, delete_after_days=0),
    LifecyclePolicy("resource","datalake", stale_after_days=0,   archive_after_days=0,  delete_after_days=0),
    LifecyclePolicy("skill",  "team",     stale_after_days=0,   archive_after_days=90, delete_after_days=0),
]
```

### 归档操作

归档 = 从向量索引中移除 + 在文件头部追加 ARCHIVED 标记 + 保留原始文件。如果被直接访问 → 自动恢复为 active。

### 湖表同步删除

CatalogConnector.detect_changes() 检测到表被删除 → ChangeEvent → 通知依赖方 → 该表的 L0/L1/L2 自动归档 → 引用该表的 cases/patterns 标记 STALE。
