# 06 — 事件驱动的变更传播

## 问题本质

上下文之间存在依赖关系。当一个上下文变动时，依赖它的其他上下文可能过时。核心问题：
1. 如何把"变动"及时 broadcast 到相关组件？
2. 如何根据变动做出自身修改（尽量不依赖模型推理，节省 token）？

## OpenViking 现状核实（基于源码验证）

| 能力 | OpenViking 状态 | 证据 |
|------|-----------------|------|
| 异步消息队列基础设施 | ✅ 已实现 | `NamedQueue` + `SemanticQueue` + `EmbeddingQueue`：支持 enqueue/dequeue、hook（`EnqueueHookBase`/`DequeueHandlerBase`）、状态追踪（pending/in_progress/processed/error） |
| 回调机制（事件完成通知） | ✅ 已实现 | `DequeueHandlerBase.set_callbacks(on_success, on_error)` + `EmbeddingTaskTracker` 的 `on_complete` 回调 |
| DAG 式事件驱动执行 | ✅ 已实现 | `SemanticDagExecutor`：底层文件/目录完成后通过 `_on_file_done` / `_on_child_done` 回调驱动父节点执行，lazy dispatch |
| 变更消息模型（SemanticMsg.changes） | ✅ 已实现 | `SemanticMsg.changes` 字段：`{"added": [...], "modified": [...], "deleted": [...]}`，支持增量更新 |
| 增量变更检测（文件内容 diff） | ✅ 已实现 | `SemanticDagExecutor._check_file_content_changed()` + `_check_dir_children_changed()`：对比新旧内容，跳过未变更文件 |
| 关系管理（.relations.json） | ✅ 已实现 | `VikingFS.link()` / `unlink()` / `get_relation_table()`：`RelationEntry{id, uris, reason}` 存储在 `.relations.json` 中 |
| 记忆→资源/Skill 双向关系自动创建 | ✅ 已实现 | `compressor._create_relations()`：记忆提取时自动从消息中提取引用的 resource/skill URI，创建双向 link |
| Observer 模式（系统监控） | ✅ 已实现 | `observers/` 目录：`QueueObserver`、`VikingDBObserver`、`TransactionObserver`、`RetrievalObserver`，但仅用于健康监控，不用于业务事件传播 |
| ChangeEvent 事件模型（业务级） | ❌ 不存在 | 无 `ChangeEvent` 类；`SemanticMsg` 是内部处理管道消息，不是面向业务的变更事件 |
| 依赖注册（.deps.json） | ❌ 不存在 | `.relations.json` 记录的是"关联"（A 和 B 有关系），不是"依赖"（A 依赖 B 的特定版本）；无 `dep_type`、`version` 等字段 |
| 变更传播引擎（Propagation Engine） | ❌ 不存在 | 无"变更发生 → 查依赖方 → 执行规则"的流程；DAG executor 只处理内容摘要生成的内部管道 |
| PropagationRule（分级响应策略） | ❌ 不存在 | 无 `mark_stale` / `auto_update` / `notify` 等响应动作；无规则引擎 |
| STALE 标记机制 | ❌ 不存在 | 无任何过时标记概念；上下文一旦写入就是"当前"状态 |
| Event Log（持久化审计日志） | ❌ 不存在 | 队列消息处理完即消费，无 append-only 持久化日志，无重放能力 |
| 跨 Agent 通知 / 通知队列 | ❌ 不存在 | 回调机制是进程内的（同一个 SemanticMsg 的任务完成回调），不是跨 Agent 的通知 |
| Skill 版本追踪与订阅 | ❌ 不存在 | `Context` 类无 `version` 字段；无 `SkillSubscription` 概念 |
| 依赖完整性校验 | ❌ 不存在 | 无离线扫描或校验机制 |

**结论：17 项能力中，OpenViking 提供了 8 项"管道基础设施"（异步队列、回调、DAG 执行、增量检测、关系管理），缺失 9 项"业务级变更传播"能力。** 已有的 8 项解决的是"内容写入后如何异步生成摘要和向量"（单向内部管道：`写入 → SemanticQueue → DAG → L0/L1 → EmbeddingQueue → 向量化 → 回调`）。缺失的 9 项是"跨上下文的多向传播链"（`Skill v2 发布 → 查依赖方 → 规则匹配 → mark_stale / notify / auto_update`）。管道基础设施可作为传播引擎的底层载体（如用 NamedQueue 投递 ChangeEvent），但传播引擎的核心逻辑（依赖注册、依赖查询、规则匹配、分级响应）需全新实现。另外，`.relations.json` 可作为 `.deps.json` 的扩展起点，但语义不同——"关联"≠"依赖"（缺少版本绑定和依赖类型）。**总体评估：OpenViking 提供了约 40% 的基础设施（队列、回调、关系管理），剩余 60% 的业务逻辑需要全新实现。**

## 哪些变动需要传播？

| 变动源 | 影响目标 | 耦合度 | 传播方式 |
|--------|----------|--------|----------|
| Skill 版本更新 | 基于旧版本积累的 cases/patterns | 强 | 标记过时 + 规则匹配 |
| 湖表 schema 变更 | 该表的 L0/L1/L2 + 引用该表的查询模板 | 强 | 自动重新生成 |
| 共享 memory 被纠正 | 从该 memory 派生的 Agent 私有 memory | 中 | 通知 + 人工/Agent 确认 |
| 团队 Skill 更新 | 引用该 Skill 的其他团队 Skill | 中 | 通知订阅者 |
| 用户偏好变更 | Skill 定义 / 团队共享 memory | 无 | ❌ 不传播 |

## (1) 变更事件模型

```python
@dataclass
class ChangeEvent:
    event_id: str               # UUID
    timestamp: datetime
    source_uri: str             # 变动的上下文 URI
    change_type: str            # created | modified | deleted | version_published
    actor: str                  # agent_id / system / catalog_sync
    diff_summary: str           # 变动摘要（~50 tokens）
    previous_version: str|None
    new_version: str|None
    metadata: dict
```

## (2) 依赖关系注册

在上下文创建时显式注册依赖，不用运行时扫描：

```python
# 存储在每个上下文目录下的 .deps.json
# ctx://agent/query-bot/memories/cases/sql-pattern-001/.deps.json
{
    "depends_on": [
        {
            "uri": "ctx://team/skills/sql-generator",
            "version": "1.2.0",
            "dep_type": "skill_version"
        },
        {
            "uri": "ctx://datalake/prod/orders",
            "dep_type": "table_schema"
        }
    ]
}
```

依赖类型：
- `skill_version`：依赖某个 Skill 的特定版本
- `table_schema`：依赖某张表的 schema
- `derived_from`：从某个共享 memory 派生

依赖何时注册：
- Agent 创建 case/pattern 时，自动记录当时使用了哪些 Skill 和表 → 写入 `.deps.json`
- 记忆提升（promote）时，在目标记忆中记录 `derived_from` 源
- 写入时的附加操作，不需要额外的模型调用

## (3) 变更传播流程

```
变动发生（如 Skill v2.0.0 发布）
    │
    ▼
ChangeEvent 写入 Event Log（持久化，用于审计和重放）
    │
    ▼
Propagation Engine 查询：谁依赖了这个 URI？
    │  方法：在向量 DB 中按 depends_on URI 做标量过滤（精确匹配，极快）
    │
    ▼
对每个依赖方，根据 dep_type 执行对应的 PropagationRule
```

## (4) PropagationRule：三级响应策略

核心思想：分级响应，大多数情况不需要模型推理。

```python
class PropagationRule(ABC):
    async def evaluate(self, event: ChangeEvent, dependent_uri: str) -> PropagationAction

class PropagationAction:
    action: str          # mark_stale | auto_update | notify | no_action
    reason: str
    auto_update_fn: Callable | None
```

### Level 1: 纯规则，零 token（~70% 场景）

```python
class SkillVersionRule(PropagationRule):
    async def evaluate(self, event, dependent_uri):
        if event.metadata.get("is_breaking"):
            return PropagationAction(action="mark_stale",
                reason=f"依赖的 Skill {event.source_uri} 发布了破坏性变更 v{event.new_version}")
        else:
            return PropagationAction(action="notify",
                reason=f"Skill 更新到 v{event.new_version}，非破坏性变更")

class TableSchemaRule(PropagationRule):
    async def evaluate(self, event, dependent_uri):
        return PropagationAction(action="auto_update",
            reason=f"表 {event.source_uri} schema 已变更",
            auto_update_fn=lambda: regenerate_l0_l1(dependent_uri))
```

`mark_stale` 实现：在文件头部追加标记，不需要模型：
```markdown
<!-- STALE: 依赖的 Skill sql-generator 已从 v1.2.0 升级到 v2.0.0 (2026-03-18) -->
```

### Level 2: 模板替换，极少 token（~20% 场景）

```python
class DerivedMemoryRule(PropagationRule):
    async def evaluate(self, event, dependent_uri):
        if event.change_type == "modified":
            old_text = extract_changed_text(event)
            new_text = extract_new_text(event)
            if old_text and can_simple_replace(dependent_uri, old_text):
                return PropagationAction(action="auto_update",
                    auto_update_fn=lambda: simple_text_replace(dependent_uri, old_text, new_text))
            else:
                return PropagationAction(action="notify", reason="源 memory 已修改，请人工确认")
```

### Level 3: 模型推理（~10% 场景）

```python
class ComplexPropagationRule(PropagationRule):
    async def evaluate(self, event, dependent_uri):
        prompt = f"""
        变动：{event.diff_summary}
        受影响的上下文摘要：{read_l0(dependent_uri)}
        问题：这个变动是否使上述上下文过时？回答 yes/no 及原因（一句话）。
        """
        result = await llm_call(prompt)  # ~200 input + ~30 output tokens
        if result.startswith("yes"):
            return PropagationAction(action="mark_stale", reason=result)
        return PropagationAction(action="no_action", reason=result)
```

## (5) 完整传播流程示例

```
场景：sql-generator Skill 发布新版本 v3（breaking，新增 window function 支持，废弃子查询替代方案）

1. Skill Service 发布新版本
   → 写入 ctx://team/skills/sql-generator/versions/v3/SKILL.md
   → 产生 ChangeEvent{source_uri="ctx://team/skills/sql-generator",
                       change_type="version_published", new_version="3",
                       metadata={"is_breaking": true},
                       diff_summary="新增 window function 支持，废弃子查询替代方案"}

2. Propagation Engine 查询依赖方
   → 向量 DB 标量过滤：depends_on.uri = "ctx://team/skills/sql-generator"
   → 找到 3 个 cases

3. 对每个依赖方执行 SkillVersionRule
   → is_breaking=True → action="mark_stale"
   → 在每个 case 头部追加 STALE 标记（零 token）

4. 通知相关 Agent（写入通知队列，Agent 下次活跃时读取）

5. Agent 下次使用时
   → 检索到 case，看到 STALE 标记
   → Agent 决定：用新版 Skill 重新生成，或忽略旧 case
   → 如果重新生成，更新 .deps.json 中的 version，移除 STALE 标记
```

## (6) 实现架构

```
                    ChangeEvent
                        │
                        ▼
              ┌─────────────────┐
              │   Event Log     │  ← 持久化（审计 + 重放）
              │  (append-only)  │
              └────────┬────────┘
                       │
                       ▼
              ┌─────────────────┐
              │  Propagation    │  ← 查依赖方 + 执行规则
              │  Engine         │
              └────────┬────────┘
                       │
            ┌──────────┼──────────┐
            ▼          ▼          ▼
      mark_stale  auto_update  notify
      (零 token)  (极少 token) (写通知队列)
```

借鉴 OpenViking 的设计：
- Event Log 类似 OpenViking 的 SemanticQueue（异步、持久化、可重放）
- Propagation Engine 类似 SemanticDagExecutor 的回调机制（事件驱动、非轮询）
- 依赖查询利用向量 DB 的标量过滤（OpenViking 已有 `owner_space` 等标量索引，扩展一个 `depends_on` 索引即可）

## (7) Token 消耗估算

| 响应级别 | 占比 | 每次 token 消耗 | 说明 |
|----------|------|----------------|------|
| Level 1: 纯规则 | ~70% | 0 | is_breaking 判断、STALE 标记 |
| Level 2: 模板替换 | ~20% | 0 | 字符串替换 |
| Level 3: 模型推理 | ~10% | ~230 | 仅读 diff_summary + L0 |

一次 Skill 升级影响 10 个依赖方：总共 ~230 token（vs 全部模型推理 ~20000 token，节省 99%）。

## (8) 依赖完整性校验（兜底机制）

`.deps.json` 依赖写入方自觉注册。如果漏了，传播系统会静默失败。

离线扫描方案（定期运行，如每天一次）：

```python
class DependencyIntegrityChecker:
    """扫描上下文内容中的实际引用，与 .deps.json 声明对比"""

    async def check(self) -> list[IntegrityIssue]:
        issues = []
        for ctx in all_contexts():
            actual_refs = extract_uri_references(ctx.content)
            declared_deps = read_deps(ctx.uri)
            undeclared = actual_refs - declared_deps
            if undeclared:
                issues.append(IntegrityIssue(
                    uri=ctx.uri, type="undeclared_dependency",
                    missing_deps=undeclared,
                    suggestion="自动补充 .deps.json 或通知 Agent 确认"
                ))
        return issues
```

处理策略：
- 高置信度遗漏 → 自动补充
- 低置信度遗漏 → 生成报告，管理员确认
- 扫描结果写入审计日志
