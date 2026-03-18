# 04 — 多 Agent 协作：记忆/Skill 的隔离、共享与版本管理

## OpenViking 现状核实（基于源码验证）

| 能力 | OpenViking 状态 | 证据 |
|------|-----------------|------|
| Agent 隔离 | ✅ 已实现 | `agent_space_name()=md5(user_id:agent_id)[:12]`，每个 Agent 有独立的 memories/ 和 skills/ |
| 记忆共享 | ❌ 未实现 | `owner_space` 过滤写死只能看自己；多租户设计文档明确标注"未来扩展" |
| Skill 共享 | ❌ 未实现 | Skills 存在 per-agent 路径下，无跨 Agent 引用机制 |
| Skill 版本管理 | ❌ 未实现 | Skill 元数据只有 name/description/content/tags，无 version 字段；Roadmap 列为 Future |
| 跨 Agent 通知 | ❌ 未实现 | 无 pub/sub 机制；subagent 只向主 Agent 汇报 |

**结论：OpenViking 只做了隔离，共享和版本管理是我们必须新设计的。**

**可借鉴的基础：** OpenViking 的 `resources/` scope 已实现 account 级全员共享（`/{account_id}/resources/` 对 account 内所有用户可见，VectorDB 查询时 `owner_space=""` 不做 space 过滤）。这与 ContextHub 的 `ctx://team/`（根团队 = 全组织）在功能上重叠。ContextHub 可借鉴 `resources/` 的实现模式作为根级别共享的起点，在此基础上扩展多层嵌套和可见性继承。区别在于：OpenViking 的共享是扁平的（全员可见 or 不可见），ContextHub 需要层级化的共享粒度（子团队级 → 部门级 → 全组织级）。OpenViking 预留的 ACL 方案（设计文档 5.7 节）是点对点授权（alice 共享给 bob），不支持层级继承，不能直接复用。

## (a) 多层级团队所有权模型

| 范围 | URI 示例 | 可见性 | 写权限 |
|------|----------|--------|--------|
| Private | `ctx://agent/{agent_id}/` | 仅该 Agent | 该 Agent |
| 子团队 | `ctx://team/engineering/backend/` | 该子团队成员 + 上级继承 | 子团队成员 |
| 上级团队 | `ctx://team/engineering/` | 工程部所有成员 | 工程部管理员 |
| 根团队(=全组织) | `ctx://team/` | 所有 Agent | 组织管理员 |

## (b) Skill 版本管理（简化方案）

Skill 本质是自然语言指令（Markdown），不是代码 API。SemVer 的"兼容性"概念在语义层面无法客观判定。因此不做自动兼容性判断，改为"版本号 + changelog + 手动标记 breaking"。

```python
class SkillVersion:
    skill_id: str           # 如 "sql-generator"
    version: str            # 递增版本号, 如 "3"
    content: str            # Skill 定义（Markdown）
    changelog: str          # 变更说明（~50 tokens）
    is_breaking: bool       # 发布者手动标记
    status: str             # draft | published | deprecated
    published_by: str       # 发布者 agent_id
    published_at: datetime

class SkillSubscription:
    subscriber_agent_id: str
    skill_id: str
    pinned_version: str | None  # None = 跟随 latest
```

发布流程：
1. Agent A 创建 Skill 新版本（status=draft）
2. Agent A 发布（status=published），标注 `is_breaking` 和 `changelog`
3. 产生 ChangeEvent → 变更传播机制接管（见 06-change-propagation.md）
4. `is_breaking=True` → 依赖方被标记 STALE
5. `is_breaking=False` → 仅通知，不标记 STALE

## (c) 记忆共享与提升

```
Agent 私有记忆 → [提升请求] → 目标团队审核队列 → [审核通过] → 写入目标团队路径
                                                    ↓
                                        [该团队及子团队 Agent 收到通知]

示例：后端组 Agent 的一个 SQL pattern 提升到工程部共享
  ctx://agent/backend-bot/memories/cases/sql-pattern-001
    → 提升到 ctx://team/engineering/memories/shared_knowledge/sql-pattern-001
    → 工程部下所有子团队（backend、data 等）的 Agent 可见
```
