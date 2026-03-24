# 06 — 事件驱动的变更传播

## 问题本质

上下文之间存在依赖关系。当一个上下文变动时，依赖它的其他上下文可能过时。核心问题：
1. 如何把"变动"及时 broadcast 到相关组件？
2. 如何根据变动做出自身修改（尽量不依赖模型推理，节省 token）？

## PG 原生能力如何支撑变更传播

| 传播环节 | PG 机制 | 替代的原方案 |
|----------|---------|-------------|
| 变更事件持久化 | `change_events` 表（ACID 写入） | append-only JSON 文件 |
| 事件通知 | `LISTEN/NOTIFY` | 自建事件队列 + 轮询 |
| 依赖查询 | `dependencies` 表 + SQL JOIN | 遍历 `.deps.json` 文件 + 向量 DB 标量过滤 |
| STALE 标记 | `UPDATE contexts SET status = 'stale'` | 在文件头部追加注释 |
| 事务保证 | 内容更新 + 变更事件在同一事务中 | 无（可能不一致） |

## 哪些变动需要传播？

| 变动源 | 影响目标 | 耦合度 | 传播方式 |
|--------|----------|--------|----------|
| Skill 版本更新 | 基于旧版本积累的 cases/patterns | 强 | 标记过时 + 规则匹配 |
| 湖表 schema 变更 | 该表的 L0/L1 + 引用该表的查询模板 | 强 | 自动重新生成 |
| 共享 memory 被纠正 | 从该 memory 派生的 Agent 私有 memory | 中 | 通知 + 人工/Agent 确认 |
| 团队 Skill 更新 | 引用该 Skill 的其他团队 Skill | 中 | 通知订阅者 |
| 统计信息更新 | 无 | 无 | ❌ 不传播（见 03 精确传播设计） |
| 用户偏好变更 | Skill 定义 / 团队共享 memory | 无 | ❌ 不传播 |

## (1) 变更事件模型

变更事件存储在 PG `change_events` 表中（定义见 01-storage-paradigm.md），与业务操作在同一个事务中写入：

```python
# 任何产生变更的操作都在事务中同时写入 change_event
async with self.pg.transaction():
    await self.update_context(context_id, new_content)
    await self.pg.execute("""
        INSERT INTO change_events (context_id, account_id, change_type, actor, diff_summary, new_version, metadata)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
    """, context_id, account_id, change_type, actor, diff_summary, new_version, metadata)
# 无需应用层额外发送 NOTIFY。
# change_events 的 AFTER INSERT trigger 会在 commit 后发出唤醒通知。
```

可靠性语义必须明确：

- `change_events` 是传播系统的唯一 source of truth。事件是否存在、是否待处理、何时重试，都只以表内状态为准。
- `NOTIFY` 只是 wake-up hint，不是可靠投递。漏通知、重复通知、乱序通知都不应导致最终语义错误。
- 传播引擎必须至少有三个入口使用同一套 drain 逻辑：
  - 启动补扫：进程启动后立即扫描所有 ready 事件
  - 通知唤醒：收到 `context_changed` 后优先处理对应 `context_id`
  - 周期补扫：定时扫描 `pending/retry` 事件，兜住漏通知和 crash 窗口

最小 outbox 状态机：

- `pending`：事务已提交，等待传播
- `processing`：某个 worker 已领取；若超时未完成，可回收为 `retry`
- `retry`：上一次传播失败，等待 `next_retry_at`
- `processed`：所有必需副作用都已成功提交

## (2) 依赖与订阅：两类关系

变更传播需要查询两类关系（见 00a §6.4）：

### 使用依赖（`dependencies` 表）

记录 context 与 context 之间的使用依赖。所有边都是"某个 artifact 在创建时引用了另一个 context"。

```sql
-- 示例：Agent 的一个 SQL case 依赖某个 Skill v2 和某张表（两列均为上下文的 UUID）
INSERT INTO dependencies (dependent_id, dependency_id, dep_type, pinned_version) VALUES
  ('a1b2c3d4-e5f6-4789-a012-3456789abcde',  -- UUID of ctx://agent/query-bot/memories/cases/sql-pattern-001
   'b2c3d4e5-f6a7-4890-b123-456789abcdef',  -- UUID of ctx://team/skills/sql-generator
   'skill_version', 2),
  ('a1b2c3d4-e5f6-4789-a012-3456789abcde',  -- 同上 dependent（case）
   'c3d4e5f6-a7b8-4901-c234-567890abcdef',  -- UUID of ctx://datalake/prod/orders
   'table_schema', NULL);
```

dep_type：
- `skill_version`：依赖某个 Skill 的特定版本（`pinned_version` 记录创建时使用的版本号）
- `table_schema`：依赖某张表的 schema
- `derived_from`：从某个共享 memory 派生

依赖何时注册：
- Agent 创建 case/pattern 时，自动记录当时使用了哪些 Skill 和表 → 写入 `dependencies` 表（`dependent_id` = 该 case 的 `contexts.id`，`dependency_id` = 被引用 Skill/表 的 `contexts.id`）
- 记忆提升（promote）时，在目标记忆中记录 `derived_from` 源（`dependency_id` 指向源 memory）
- 写入时的附加操作，不需要额外的模型调用

### Skill 订阅（`skill_subscriptions` 表）

记录 agent 与 skill 之间的持续关注关系。订阅影响读路径（agent 看到哪个版本）和通知行为，但不决定 stale 标记。

```sql
-- 示例：query-bot floating 订阅 sql-generator，analysis-bot pin 到 v2
INSERT INTO skill_subscriptions (agent_id, skill_id, pinned_version, account_id) VALUES
  ('query-bot',    'b2c3d4e5-f6a7-4890-b123-456789abcdef', NULL, 'acme'),
  ('analysis-bot', 'b2c3d4e5-f6a7-4890-b123-456789abcdef', 2,    'acme');
```

## (3) 变更传播流程

```
变动发生（如 Skill v3 发布）
    │
    ▼
业务事务提交：
    ├─ UPDATE/INSERT contexts 等业务表
    └─ INSERT change_events（source of truth）
          │
          └─ AFTER INSERT trigger → PG NOTIFY 'context_changed'
                                   （仅作唤醒，可丢、可重）
    │
    ▼
Propagation Engine 的三个入口共用同一套 drain 逻辑：
    ├─ start()   → 启动补扫所有 ready 事件
    ├─ notify()  → 按 context_id 优先 drain
    └─ ticker()  → 周期补扫 pending/retry/超时 processing
    │
    ▼
传播引擎领取 ready 事件：
    ├─ `pending/retry` → `processing`
    └─ 读取该 event 对应的 dependency / subscription
    │
    ▼
分别执行不同的传播规则：
    ├─ 对每个 dependency：根据 dep_type 执行 PropagationRule（可能 mark_stale / auto_update）
    └─ 对每个 subscription：根据 pinned_version 决定通知类型（notify / advisory）
    │
    ▼
收尾：
    ├─ 全部成功 → `processed`
    └─ 任一副作用失败 → `retry` + `next_retry_at` + `last_error`
```

## (4) PropagationRule：分路径的响应策略

核心思想：依赖和订阅走不同的规则路径；分级响应，大多数情况不需要模型推理。

```python
class PropagationRule(ABC):
    async def evaluate(self, event: ChangeEvent, target_id: str) -> PropagationAction

class PropagationAction:
    action: str          # mark_stale | auto_update | notify | advisory | no_action
    reason: str
    auto_update_fn: Callable | None
```

### 路径 A：使用依赖（dependencies 表）→ 可能 mark_stale

对 `dependencies` 中的 artifact 执行三级响应策略：

#### Level 1: 纯规则，零 token（~70% 场景）

```python
class SkillVersionDepRule(PropagationRule):
    """处理 dep_type='skill_version' 的使用依赖。
    artifact 在创建时引用了某个 skill version，现在 skill 发布了新版本。
    """
    async def evaluate(self, event, dependent_id):
        if event.metadata.get("is_breaking"):
            return PropagationAction(action="mark_stale",
                reason=f"依赖的 Skill 发布了破坏性变更 v{event.new_version}")
        else:
            return PropagationAction(action="notify",
                reason=f"Skill 更新到 v{event.new_version}，非破坏性变更")

class TableSchemaRule(PropagationRule):
    async def evaluate(self, event, dependent_id):
        return PropagationAction(action="auto_update",
            reason="依赖表的 schema 已变更",
            auto_update_fn=lambda: regenerate_l0_l1(dependent_id))
```

`mark_stale` 实现（PG 原子操作）：
```sql
UPDATE contexts SET status = 'stale', stale_at = NOW(), updated_at = NOW() WHERE id = $1;
INSERT INTO change_events (context_id, account_id, change_type, actor, diff_summary)
VALUES ($1, current_setting('app.account_id'), 'marked_stale', 'propagation_engine',
        '依赖的 Skill sql-generator 已从 v2 升级到 v3 (breaking)');
```

#### Level 2: 模板替换，极少 token（~20% 场景）

```python
class DerivedMemoryRule(PropagationRule):
    async def evaluate(self, event, dependent_id):
        if event.change_type == "modified":
            old_text = extract_changed_text(event)
            new_text = extract_new_text(event)
            if old_text and can_simple_replace(dependent_id, old_text):
                return PropagationAction(action="auto_update",
                    auto_update_fn=lambda: simple_text_replace(dependent_id, old_text, new_text))
            else:
                return PropagationAction(action="notify", reason="源 memory 已修改，请人工确认")
```

#### Level 3: 模型推理（~10% 场景）

```python
class ComplexPropagationRule(PropagationRule):
    async def evaluate(self, event, dependent_id):
        l0 = await self.pg.fetchval("SELECT l0_content FROM contexts WHERE id = $1", dependent_id)
        prompt = f"""
        变动：{event.diff_summary}
        受影响的上下文摘要：{l0}
        问题：这个变动是否使上述上下文过时？回答 yes/no 及原因（一句话）。
        """
        result = await llm_call(prompt)  # ~200 input + ~30 output tokens
        if result.startswith("yes"):
            return PropagationAction(action="mark_stale", reason=result)
        return PropagationAction(action="no_action", reason=result)
```

### 路径 B：订阅关系（skill_subscriptions 表）→ 通知，不 mark_stale

订阅者不是 artifact，没有可以过时的"内容"。传播对订阅者只做通知，不做 stale 标记。

```python
class SkillSubscriptionNotifyRule(PropagationRule):
    """处理 skill_subscriptions 中的订阅者。"""
    async def evaluate(self, event, subscription):
        new_ver = event.new_version
        if subscription['pinned_version'] is None:
            # floating 订阅者：自动跟随 latest，通知新版本可用
            return PropagationAction(action="notify",
                reason=f"Skill 已更新到 v{new_ver}")
        else:
            # pinned 订阅者：advisory 通知，不打扰正常使用
            pinned = subscription['pinned_version']
            return PropagationAction(action="advisory",
                reason=f"Skill v{new_ver} 已发布，你当前 pin 在 v{pinned}")
```

### 路径对比总结

| 维度 | 使用依赖（路径 A） | 订阅（路径 B） |
|---|---|---|
| 数据源 | `dependencies` | `skill_subscriptions` |
| 目标标识 | `dependent_id`（context UUID） | `agent_id`（TEXT） |
| 可能的动作 | mark_stale / auto_update / notify | notify / advisory |
| breaking change 时 | artifact 被标记 stale | floating→通知 / pinned→advisory |
| non-breaking change 时 | 通知 | floating→通知 / pinned→advisory |

## (5) 完整传播流程示例

```
场景：sql-generator Skill 发布新版本 v3（breaking，新增 window function 支持，废弃子查询替代方案）
当前状态：
  - query-bot: floating 订阅者，有 2 个 case 依赖 skill v2
  - analysis-bot: pinned 到 v2 的订阅者，有 1 个 case 依赖 skill v2

1. Skill Service 发布新版本（事务内）
   → INSERT INTO skill_versions (...)
   → UPDATE contexts SET l0_content=..., l1_content=..., l2_content=..., version=3
     WHERE id = '<uuid-of-sql-generator-skill>'
   → INSERT INTO change_events (context_id='<uuid-of-sql-generator-skill>',
       account_id='acme', change_type='version_published', new_version='3',
       metadata='{"is_breaking": true}',
       diff_summary='新增 window function 支持，废弃子查询替代方案')
   → COMMIT

2. 事务 commit 后，`change_events` trigger 发出 PG NOTIFY 'context_changed'
   （payload = 该 Skill 上下文的 UUID）→ 传播引擎被唤醒

3. 传播引擎并行查询两类关系
   → SELECT * FROM dependencies WHERE dependency_id = '<skill-uuid>'
     → 找到 3 个 artifact（query-bot 的 2 个 case + analysis-bot 的 1 个 case）
   → SELECT * FROM skill_subscriptions WHERE skill_id = '<skill-uuid>'
     → 找到 2 个订阅者（query-bot floating + analysis-bot pinned v2）

4. 路径 A：对每个 dependency 执行 SkillVersionDepRule
   → is_breaking=True → action="mark_stale"
   → 全部 3 个 case 被标记 stale（无论其 agent 的订阅状态，artifact 本身确实是用 v2 生成的）

5. 路径 B：对每个 subscription 执行 SkillSubscriptionNotifyRule
   → query-bot（floating）→ action="notify"："Skill 已更新到 v3"
   → analysis-bot（pinned v2）→ action="advisory"："Skill v3 已发布，你当前 pin 在 v2"

6. 后续行为
   → query-bot 读取 sql-generator 时拿到 v3（floating，走 contexts 表快速路径）
     → 决定用 v3 重新生成 case，更新 dependencies.pinned_version=3，恢复 case status='active'
   → analysis-bot 读取 sql-generator 时仍拿到 v2（pinned，走 skill_versions 表）
     → 其 case 虽被标记 stale（因为 case 内容是用 v2 生成的），但 agent 仍在 v2 上工作
     → 当 analysis-bot 决定升级时：更新 skill_subscriptions.pinned_version=3（或设为 NULL），
       然后用 v3 重新生成 case，恢复 status='active'
```

> **退出标准验证**：analysis-bot pin 到 v2 后，v3 (breaking) 发布时——
> 1. **读到什么？** v2（pinned_version=2，从 `skill_versions` 表读取）
> 2. **被标记为什么？** 其 case/artifact 被标记 stale（因为 case 内容是用 v2 指令生成的），但 agent 的订阅关系本身不受影响
> 3. **何时恢复？** 当 analysis-bot 主动升级 pin 并用新版本重新生成 case 时

## (6) 传播引擎实现

事件级 outbox 的最小契约：

- `processed` 只能表示“该事件要求的全部副作用已经完成”，不能再用单个布尔值表达处理中、失败待重试、崩溃回收等中间态。
- 事件是否可安全重试，取决于消费侧幂等边界。MVP 不引入 `change_event_deliveries` 明细表，但要求每个副作用都对 `(event_id, target_id, action)` 幂等。
- 幂等要求的最小例子：
  - `mark_stale`：使用 guard update，例如 `WHERE status <> 'stale'`
  - `auto_update`：重复执行必须得到同一最终内容；若做不到，目标侧必须记录已应用的 `event_id`
  - `notify/advisory`：通知下游必须支持 idempotency key（如 `event_id:agent_id:action`）

```python
LEASE_TIMEOUT = timedelta(minutes=5)
SWEEP_INTERVAL = 30  # seconds

class PropagationEngine:
    """单实例 MVP：change_events 是 source of truth，NOTIFY 只负责唤醒。

    MVP 限制：单实例部署。多实例需要 SELECT FOR UPDATE SKIP LOCKED（见下方注释）。
    """

    async def start(self):
        self._listen_conn = await asyncpg.connect(self._dsn)
        await self._listen_conn.add_listener("context_changed", self._on_notify)
        await self.requeue_stuck_events()
        await self.sweep_ready_events(reason="startup")
        self._sweep_task = asyncio.create_task(self._periodic_sweep())

    def _on_notify(self, conn, pid, channel, payload):
        self._schedule_context_drain(payload)

    async def _periodic_sweep(self):
        while True:
            await asyncio.sleep(SWEEP_INTERVAL)
            await self.requeue_stuck_events()
            await self.sweep_ready_events(reason="periodic")

    async def requeue_stuck_events(self):
        await self.pg.execute("""
            UPDATE change_events
            SET delivery_status = 'retry',
                next_retry_at = NOW(),
                claimed_at = NULL,
                last_error = COALESCE(last_error, 'processing lease expired')
            WHERE delivery_status = 'processing'
              AND claimed_at < NOW() - INTERVAL '5 minutes'
        """)

    async def sweep_ready_events(self, reason: str, context_id: str | None = None):
        while True:
            events = await self.claim_ready_events(context_id=context_id, limit=100)
            if not events:
                return
            for event in events:
                await self._process_claimed_event(event)

    async def claim_ready_events(self, context_id: str | None, limit: int):
        return await self.pg.fetch("""
            UPDATE change_events
            SET delivery_status = 'processing',
                claimed_at = NOW(),
                attempt_count = attempt_count + 1,
                last_error = NULL
            WHERE event_id IN (
                SELECT event_id
                FROM change_events
                WHERE ($1::uuid IS NULL OR context_id = $1)
                  AND delivery_status IN ('pending', 'retry')
                  AND next_retry_at <= NOW()
                ORDER BY timestamp ASC
                LIMIT $2
            )
            RETURNING *
        """, context_id, limit)

    async def _process_claimed_event(self, event):
        dependents = await self.pg.fetch("""
            SELECT dependent_id, dep_type, pinned_version
            FROM dependencies WHERE dependency_id = $1
        """, event['context_id'])

        subscribers = await self.pg.fetch("""
            SELECT agent_id, pinned_version
            FROM skill_subscriptions WHERE skill_id = $1
        """, event['context_id'])

        all_succeeded = True

        for dep in dependents:
            try:
                rule = self.get_dep_rule(dep['dep_type'])
                action = await rule.evaluate(event, dep['dependent_id'])
                await self.execute_action(action, dep['dependent_id'], event['event_id'])
            except Exception as e:
                logger.error(f"Propagation failed for dependency {dep['dependent_id']}: {e}")
                all_succeeded = False

        for sub in subscribers:
            try:
                rule = SkillSubscriptionNotifyRule()
                action = await rule.evaluate(event, sub)
                await self.send_notification(action, sub['agent_id'], event['event_id'])
            except Exception as e:
                logger.error(f"Notification failed for subscriber {sub['agent_id']}: {e}")
                all_succeeded = False

        if all_succeeded:
            await self.pg.execute("""
                UPDATE change_events
                SET delivery_status = 'processed',
                    processed_at = NOW(),
                    claimed_at = NULL,
                    last_error = NULL
                WHERE event_id = $1
            """, event['event_id'])
        else:
            await self.pg.execute("""
                UPDATE change_events
                SET delivery_status = 'retry',
                    claimed_at = NULL,
                    next_retry_at = NOW() + make_interval(secs => LEAST(300, 5 * attempt_count)),
                    last_error = 'partial propagation failure'
                WHERE event_id = $1
            """, event['event_id'])
```

## (7) Token 消耗估算

| 响应级别 | 占比 | 每次 token 消耗 | 说明 |
|----------|------|----------------|------|
| Level 1: 纯规则 | ~70% | 0 | is_breaking 判断、PG UPDATE status |
| Level 2: 模板替换 | ~20% | 0 | 字符串替换 |
| Level 3: 模型推理 | ~10% | ~230 | 仅读 diff_summary + L0 |

一次 Skill 升级影响 10 个依赖方：总共 ~230 token（vs 全部模型推理 ~20000 token，节省 99%）。

## (8) 依赖完整性校验（诊断兜底，不进入 canonical 写入路径）

`dependencies` 表依赖写入路径显式注册。如果漏了，传播系统会静默失败。

离线扫描只能用于诊断与补洞建议，不能替代写入路径的显式登记，更不能升级为 canonical 依赖语义。定期扫描方案（如每天一次）：

```python
class DependencyIntegrityChecker:
    """扫描上下文内容中的实际引用，与 dependencies 表声明对比"""

    async def check(self) -> list[IntegrityIssue]:
        issues = []
        # 从 PG 批量读取所有上下文内容
        contexts = await self.pg.fetch("SELECT id, uri, l0_content, l1_content, l2_content FROM contexts WHERE status = 'active'")
        for ctx in contexts:
            # 实现上需将正文中的 URI 引用解析为 contexts.id，再与 dependency_id 比较
            actual_refs = extract_uri_references(ctx['l1_content'], ctx['l2_content'])
            declared_deps = await self.pg.fetch(
                "SELECT dependency_id FROM dependencies WHERE dependent_id = $1", ctx['id'])
            declared_set = {d['dependency_id'] for d in declared_deps}
            undeclared = actual_refs - declared_set
            if undeclared:
                issues.append(IntegrityIssue(
                    uri=ctx['uri'], type="undeclared_dependency",
                    missing_deps=undeclared,
                    suggestion="生成补边建议并通知维护者确认"
                ))
        return issues
```

处理策略：
- 高置信度遗漏 → 生成修复建议或待确认任务，由写入方/维护者显式补边
- 低置信度遗漏 → 生成报告，管理员确认
- 扫描结果输出为结构化报告或运行日志；如未来落地审计 backlog，再挂接 `audit_log` hook

结论：
- canonical 依赖边只来自写入事务中的确定性建边。
- 离线扫描是诊断工具，不是自动依赖捕获系统。
- Session 7 已将“自动依赖捕获替代显式登记”归入 `直接放弃`，统一见 `14-adr-backlog-register.md`。
