# 00a — Canonical Invariants

本文档是 ContextHub 所有设计文档的权威约束。后续文档如与本文冲突，以本文为准。

---

## 1. 租户隔离与唯一性

### 1.1 URI 唯一性

- `contexts.uri` 的唯一性作用域是 **`(account_id, uri)`**，不是全局唯一。
- 多租户环境下，不同租户可拥有同名 URI（如 `ctx://datalake/prod/orders`）。
- RLS 策略以 `account_id = current_setting('app.account_id')` 为前提，URI 唯一性必须与之对齐。

```sql
-- 正确
UNIQUE (account_id, uri)
-- 错误（旧定义）
UNIQUE (uri)
```

### 1.2 团队路径唯一性

- `teams.path` 的唯一性作用域是 **`(account_id, path)`**，不是全局唯一。

```sql
UNIQUE (account_id, path)
```

### 1.3 所有表的租户隔离

- 所有持有 `account_id` 列且面向 Agent/用户请求直接访问的表都必须启用 RLS。适用表：`contexts`、`teams`、`skill_subscriptions`。
- 例外：`change_events` 虽持有 `account_id`（冗余字段，供传播引擎创建 tenant-scoped session），但不启用 RLS，因为传播引擎需跨租户扫描 outbox。
- 每个 PG 连接在事务开始时必须执行 `SET LOCAL app.account_id = $1`。

---

## 2. 内部主键与外键规则

### 2.1 表间引用一律使用 UUID 内部主键

- 所有表间外键引用 **必须使用** `contexts.id`（UUID），**禁止使用** `contexts.uri`（逻辑地址）做外键。
- URI 仅用于：对外接口（Agent 看到的地址）、人类可读场景、日志/审计中的辅助信息。

适用范围：

| 表 | 外键列 | 引用 |
|---|---|---|
| `dependencies.dependent_id` | UUID | `contexts.id` |
| `dependencies.dependency_id` | UUID | `contexts.id` |
| `skill_subscriptions.skill_id` | UUID | `contexts.id` |
| `change_events.context_id` | UUID | `contexts.id` |
| `context_feedback.context_id` | UUID | `contexts.id` |
| `document_sections.context_id` | UUID | `contexts.id` |
| `skill_versions.skill_id` | UUID | `contexts.id` |

### 2.2 `skill_subscriptions.agent_id` 使用 TEXT

- `skill_subscriptions.agent_id` 保留为 TEXT，因为系统中 agent 以文本标识存在（`team_memberships.agent_id`），没有 agents 表和 UUID 主键。订阅的主体是 agent 而非 context，这是订阅从 `dependencies` 表拆出的根本原因。

### 2.3 `access_policies` 和 `audit_log` 的特殊处理

- `access_policies.resource_uri_pattern` 保留为 TEXT 模式匹配（如 `ctx://datalake/prod/*`），因为 ACL 规则本质上是基于路径模式而非具体行的。
- `audit_log.resource_uri` 保留为 TEXT，作为审计记录的人类可读字段。审计日志是 append-only 的历史记录，不需要 FK 约束。

---

## 3. 类型系统

### 3.1 `context_type` 枚举

| 值 | 含义 | L2 存储位置 |
|---|---|---|
| `table_schema` | 数据湖表元数据 | 结构化子表（`table_metadata` 等） |
| `skill` | 自然语言指令 | PG `l2_content` 列 + `skill_versions` 表 |
| `memory` | 记忆（用户/Agent/团队级） | PG `l2_content` 列 |
| `resource` | 文档资源（通用） | PG `l2_content` 列；长文档子类型使用文件系统 |

- 共 4 个类型，不允许在其他文档中引入新类型。
- `long_document` **不是** 独立的 `context_type`，而是 `resource` 的子类型。通过 `contexts.file_path IS NOT NULL` 区分：`file_path` 为 NULL 表示短文档/通用资源（L2 存 PG），`file_path` 非 NULL 表示长文档（L2 存文件系统）。

### 3.2 `scope` 枚举

| 值 | 含义 | URI 命名空间 |
|---|---|---|
| `datalake` | 数据湖表 | `ctx://datalake/` |
| `team` | 团队所有（含根团队 = 全组织） | `ctx://team/` |
| `agent` | Agent 私有 | `ctx://agent/{agent_id}/` |
| `user` | 用户级 | `ctx://user/{user_id}/` |

- 共 4 个 scope，不允许在其他文档中引入新 scope。
- `ctx://resources/` 命名空间下的内容 scope 为 `team`（组织级共享资源）。如果某个资源仅属于某个 Agent，scope 为 `agent`。资源的 scope 取决于其归属，而非一个独立的 `resources` scope。

### 3.3 scope 与 URI 命名空间的映射规则

- scope 决定可见性计算逻辑，URI 命名空间决定寻址路径。
- `datalake` scope 的特殊性在于其 L2 存储模式（结构化子表），而非可见性语义。
- `ctx://resources/` 不对应独立 scope，它是一个组织级路径约定，其下内容的 scope 为 `team`，owner_space 为空或根团队路径。

---

## 4. 可见性与权限

### 4.1 可见性继承方向：子读父

- Agent 可见其**所属团队及所有祖先团队**的内容（从子团队一路上溯到根团队）。
- 父团队成员**不能**默认看到子团队的内容。
- MVP 阶段如需跨团队共享，统一通过把内容 promote 到目标团队路径或双方共同可见的祖先路径来实现。
- `dependencies` 表只记录引用/来源/传播关系，本身**不授予读权限**。
- post-MVP 阶段，可在默认可见性之上叠加显式 ACL allow，对少量默认不可见资源做例外授权。

这遵循最小权限原则：一个人在组织根团队，不意味着能看所有子团队的私有 context。

### 4.2 两层访问模型

```
请求 → 第一层：默认可见性 / 所有权判定 → 第二层：显式 ACL 覆盖（post-MVP） → 返回
```

- **第一层（MVP 必做）**：基于团队层级、`scope` 和 `owner_space` 计算默认 read 可见性，并基于所有权 / `team_memberships` 计算默认 write 权限。
- **第二层（post-MVP）**：基于 `access_policies` 做显式 allow / deny / field mask。它是对默认访问基线的覆盖，不是取代基线的全局白名单系统。
- 因此：若无 ACL 命中，则沿用第一层的默认判定；若有显式 deny，则 deny 优先。

### 4.3 `datalake` 和 `resources` 的默认可见性

- `datalake` scope 的内容**默认对同租户所有 Agent 可见**（因为数据湖元数据通常是组织级共享的）；post-MVP 可通过 ACL deny 规则限制特定表的访问。
- `ctx://resources/` 下的内容（scope=team, owner_space=根团队）**默认对同租户所有 Agent 可见**；post-MVP 可通过 ACL deny 规则限制访问。
- 如需实现"默认不可见、需显式授权"的资源，将其 scope 设为特定团队或 Agent，利用可见性层天然限制访问范围。

### 4.4 MVP 与 post-MVP 分界

- **MVP**：默认可见性、默认写权限、`promote` 共享闭环。系统不依赖 `access_policies`、`field_masks`、`audit_log` 才能跑通核心协作能力。
- **post-MVP**：显式 ACL allow/deny、字段脱敏、审计日志，以及“reference + ACL”的窄范围跨团队共享。它们都属于明确后置 backlog，触发条件与 owner 见 `14-adr-backlog-register.md`。

---

## 5. 状态机

### 5.1 `contexts.status` — 上下文生命周期状态

```
创建 ──────────────────────→ active  ←── 被访问/更新时重置
提升请求 ──→ pending_review ──→ active     （MVP 跳过审核，直接 active）
                                  │
                      标记过时(变更传播)   或  超过 N 天未访问
                                  ▼
                                stale       stale_at 记录转换时间
                                  │
                      超过 M 天仍为 stale 且未被访问
                                  ▼
                               archived     archived_at 记录转换时间
                                  │
                      超过 K 天（可选）
                                  ▼
                               deleted      deleted_at 记录转换时间
```

Canonical 状态枚举（共 5 个）：

| 状态 | 含义 |
|---|---|
| `active` | 正常可用，参与向量检索 |
| `stale` | 过时或长期未访问，仍可通过 URI 直接读取，参与向量检索但降权 |
| `archived` | 归档，从向量索引中移除（清除 `l0_embedding`），PG 行保留，可通过 URI 直接读取 |
| `deleted` | 逻辑删除，不可访问（或移至冷存储） |
| `pending_review` | 等待审核（仅用于记忆提升流程，MVP 跳过） |

### 5.2 `skill_versions.status` — Skill 版本状态

Canonical 状态枚举（共 3 个）：

| 状态 | 含义 |
|---|---|
| `draft` | 草稿，未发布，仅作者可见 |
| `published` | 已发布，订阅者可读取 |
| `deprecated` | 已弃用，不再推荐使用，但仍可读取（历史稳定性） |

这两套状态机分属不同对象（context vs skill version），互不干扰。

### 5.3 状态转换时间戳

`contexts` 表必须包含以下时间戳列：

| 列 | 含义 | 更新时机 |
|---|---|---|
| `created_at` | 创建时间 | 插入时 |
| `updated_at` | 最后内容更新时间 | 任何内容变更时 |
| `last_accessed_at` | 最后访问时间 | 每次读取时（初始值 = created_at） |
| `stale_at` | 进入 stale 状态的时间 | `status` 变为 `stale` 时写入；恢复 `active` 时清空 |
| `archived_at` | 进入 archived 状态的时间 | `status` 变为 `archived` 时写入；恢复 `active` 时清空 |
| `deleted_at` | 进入 deleted 状态的时间 | `status` 变为 `deleted` 时写入 |

---

## 6. 版本管理

### 6.1 版本不可变性原则

- `skill_versions` 表中的每一行一旦 `status = 'published'`，其 `content`、`changelog`、`is_breaking` 字段**不可修改**。
- `draft` 状态的版本可以修改。
- `published → deprecated` 是允许的状态转换（标记弃用），但内容不变。

### 6.2 URI、订阅与版本解析

#### 6.2.1 默认行为：URI 返回 latest published

- 通过 URI 读取 Skill（`ctx://team/skills/my-skill`）**默认返回最新 published 版本**的内容。
- `contexts` 表的 L0/L1/L2 列始终反映最新 published 版本（每次发布时覆盖更新）。这是 latest/head pointer——`contexts` 行是可变的指针，`skill_versions` 行是不可变的历史。
- **不引入版本化 URI**（如 `ctx://team/skills/my-skill@v2`）。URI 是逻辑地址，不是版本寻址。

#### 6.2.2 订阅上下文下的版本解析

当读取请求携带 agent 身份时，版本解析需查询 `skill_subscriptions` 表：

| 订阅状态 | 读取时返回 | 数据来源 |
|---|---|---|
| 无订阅 | latest published | `contexts` 表 L0/L1/L2 列 |
| floating（`pinned_version IS NULL`） | latest published | `contexts` 表 L0/L1/L2 列 |
| pinned（`pinned_version = N`） | 版本 N 的内容 | `skill_versions(skill_id, version=N)` |

#### 6.2.3 显式版本参数

- 读取历史版本通过 API 参数（`?version=N`）或直接查询 `skill_versions` 表。此方式不受订阅状态影响。

> **设计理念**：URI 是"这个 Skill 在哪"，subscription 是"我要用哪个版本"，两者正交。
> URI 永远指向同一个 Skill（一个 `contexts` 行），subscription 决定该 agent 看到的版本快照。
> 这使得 pin 不是"创建一个新地址"，而是"同一个地址，不同的视角"——agent 之间讨论 `ctx://team/skills/sql-generator` 时指向同一事物，只是各自可能看到不同版本。
> 这也意味着 `contexts` 表的 L0/L1/L2 列永远是 latest——它们是未订阅者和 floating 订阅者的快速路径，也是向量检索的数据源。pinned 读取走 `skill_versions` 表，是有意的慢路径（需要额外一次查询），因为 pin 本身就是一个需要显式选择的行为。

### 6.3 历史可稳定读取

- 任何已 published 的版本，只要未被 hard delete，都可通过 `skill_versions(skill_id, version)` 稳定读取。
- `deprecated` 版本仍可读取，但 API 应返回弃用警告。
- pinned subscription 的稳定性依赖此保证：agent pin 到 v2 后，v2 的内容永远不变、永远可读。

### 6.4 订阅与依赖的区别

订阅（subscription）和使用依赖（dependency）是两类不同的边，分属不同的表：

| 维度 | 订阅（`skill_subscriptions`） | 使用依赖（`dependencies`） |
|---|---|---|
| 主体 | agent（TEXT 标识） | context（UUID 行） |
| 语义 | "我要持续关注这个 skill" | "我的内容在创建时引用了某个 skill version" |
| 影响读路径 | 是（决定 agent 读到哪个版本） | 否 |
| 影响传播 | 通知订阅者 | 标记 artifact 为 stale |
| pinned 时的行为 | 读取返回 pin 版本；新版本发布时收到 advisory 通知但不被标记 stale | pinned_version 记录创建时用的版本；新 breaking version 时 artifact 被标记 stale |

---

## 7. 文档冲突清单

以下是本文档冻结时，已识别的需要回写修正的文档及其冲突项：

| 文档 | 冲突项 | 修正方向 |
|---|---|---|
| `01-storage-paradigm` | `contexts.uri` 定义为 `UNIQUE`（应为 `UNIQUE(account_id, uri)`） | 修正唯一约束 |
| `01-storage-paradigm` | `teams.path` 定义为 `UNIQUE`（应为 `UNIQUE(account_id, path)`） | 修正唯一约束 |
| `01-storage-paradigm` | `contexts.status` 注释中的枚举缺少时间戳说明 | 添加 `stale_at`/`archived_at`/`deleted_at` 列 |
| `01-storage-paradigm` | `context_type` 注释包含 `'resource'` 但未说明与 `long_document` 的关系 | 添加说明：`long_document` 是 `resource` 子类型 |
| `11-long-document-retrieval` | `document_sections.context_uri` 用 URI 做外键 | 改为 `context_id UUID REFERENCES contexts(id)` |
| `11-long-document-retrieval` | `context_type = 'long_document'` 作为独立类型 | 改为 `context_type = 'resource'`，用 `file_path IS NOT NULL` 区分 |
| `07-feedback-lifecycle` | 状态机图中缺少时间戳字段 | 添加 `stale_at`/`archived_at`/`deleted_at` 说明 |
| `04-multi-agent-collaboration` | `skill_versions` 未声明不可变性原则 | 添加说明 |

### 7.1 Session 2 修正记录（Versioning / Subscription / Dependency）

以下冲突已在 Session 2 中修正：

| 文档 | 修正项 | 修正内容 |
|---|---|---|
| `01-storage-paradigm` | `dependencies` 表包含 `dep_type='skill_subscription'` | 拆出为独立 `skill_subscriptions` 表；`dependencies` 仅保留 context→context 使用依赖 |
| `01-storage-paradigm` | `dependencies.pinned_version` 类型为 TEXT | 改为 INT（版本号是递增整数） |
| `04-multi-agent-collaboration` | 注释引用"订阅合并到 dependencies 表" | 改为引用独立 `skill_subscriptions` 表，并引用 00a §2.2, §6.4 |
| `04-multi-agent-collaboration` | 传播逻辑仅区分 breaking/non-breaking | 增加 dependency vs subscription 两条路径的分别说明 |
| `06-change-propagation` | 传播引擎只查 `dependencies` 表 | 增加并行查询 `skill_subscriptions` 表的路径 B |
| `06-change-propagation` | `SkillVersionRule` 不区分 dependency 和 subscription | 拆分为 `SkillVersionDepRule`（路径 A）和 `SkillSubscriptionNotifyRule`（路径 B） |
| `06-change-propagation` | 无 advisory 通知概念 | 增加 pinned 订阅者的 advisory 通知（"v3 已发布，你仍在 v2"） |
| `00a` 本文 | §6.2 仅定义 URI 返回 latest | 增加 §6.2.2 订阅上下文下的版本解析；增加 §6.4 订阅与依赖的区别；增加设计理念说明 |

### 7.2 GPT-5.4 审查后修正记录

以下问题在 GPT-5.4 修订后经独立审查发现并修正：

| 文档 | 修正项 | 修正内容 |
|---|---|---|
| `01-storage-paradigm` | `change_events` 表缺少 `account_id` 列，但 `10-code-architecture` 的 PropagationEngine 使用 `event.account_id` 创建 ScopedRepo | 增加 `account_id TEXT NOT NULL` 列；不启用 RLS（传播引擎需跨租户扫描） |
| `01-storage-paradigm` | `teams` 和 `skill_subscriptions` 有 `account_id` 但未启用 RLS，违反 00a §1.3 | 为两表添加 RLS 策略 |
| `00a` 本文 | §1.3 "所有持有 `account_id` 的表都必须启用 RLS" 未考虑内部 outbox 表 | 精确化为"面向 Agent/用户请求直接访问的表"，并增加 `change_events` 例外说明 |
| `03-datalake-management` | `ON CONFLICT (uri)` 不匹配唯一约束 `UNIQUE(account_id, uri)` | 改为 `ON CONFLICT (account_id, uri)` |
| `07-feedback-lifecycle` | `handle_table_deleted` 手动调用 `NOTIFY`，与 `change_events` trigger 自动通知的冻结设计矛盾 | 删除手动 NOTIFY，改为注释说明 trigger 自动处理 |
| `08-architecture` | 缺少"OpenClaw 插件架构决策"小节，导致 09/13 的交叉引用断裂 | 恢复 DataAgent 层说明 + OpenClaw 插件架构决策章节 |
| `01/03/04/06/07` | 所有 `INSERT INTO change_events` 语句缺少 `account_id` 参数 | 统一补充 `account_id` 参数 |
| `09-implementation-plan` | 未说明 MVP 阶段团队结构如何预置 | 增加"团队与 Agent 预置"说明（seed data / 无 CRUD API） |
