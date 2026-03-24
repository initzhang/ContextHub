# ContextHub 设计讨论路线图

## 摘要

这份文档用于把 ContextHub 的后续设计 refinement 拆成多个低耦合 session，避免上下文爆炸。每个 session 只解决一个主题，必须在结束时产出明确决议，而不是继续保留模糊表述。

使用方式：

- 每个新 session 开始时，只读本文件的“摘要 + 对应 session 小节 + 已产出的前置决议文档”。
- 每个 session 只讨论一个主题，不跨题发散。
- 每个 session 的目标不是“聊清楚”，而是“定案并明确要改哪些设计文档”。
- 如果某个问题会改变数据模型、权限边界、版本语义、传播语义或 MVP 对外承诺，它必须在对应 session 中被明确决议。
- 如果某个方案只是中长期可选增强，应放进最后的 ADR/backlog session，不提前展开。

## 全局规则

- 每个 session 结束时都要产出 4 类结果：`决议`、`仍未决问题`、`受影响文档`、`下一 session 入口条件`。
- 每个 session 都应明确“本次不讨论什么”，防止范围失控。
- 除 Session 1 外，其余 session 都默认以上一 session 的决议为前提，不重新打开已冻结的话题。
- 如果某个 session 发现前置决议不成立，应先显式指出冲突，再决定是回滚前置决议还是局部修订。
- 所有后续文档都应服从一份权威规范文档；建议在 Session 1 产出 `00a-canonical-invariants.md`。

---

## Session 1: Canonical Invariants

**目标**

冻结系统最基础的不变式，建立后续所有文档必须服从的权威约束。

**本次建议阅读**

- `00-index`
- `01-storage-paradigm`
- `04-multi-agent-collaboration`
- `07-feedback-lifecycle`
- 只在需要统一类型系统时参考 `11-long-document-retrieval`

**必须定案**

- `uri` 是否全局唯一，还是 `(account_id, uri)` 唯一
- `teams.path` 是否也是 `(account_id, path)` 唯一
- 哪些关系必须一律用内部主键，禁止再用逻辑 URI 做外键
- `context_type` 的 canonical 枚举
- `scope` 的 canonical 枚举
- `long_document`、`resource`、`resources` 的关系
- 团队可见性的继承方向
- visibility 与 ACL 的关系
- 状态机的 canonical 状态枚举
- 是否需要 `stale_at`、`archived_at`、`deleted_at`
- 版本对象是否必须满足“历史可稳定读取”和“不可变”原则

**本次不讨论**

- Skill 版本表怎么拆
- 传播引擎实现细节
- Benchmark 指标
- OpenClaw 插件细节

**预期产出**

- 新增一份权威规范文档：`00a-canonical-invariants.md`
- 明确需要回写修正的文档：`00`、`01`、`04`、`07`
- 一份“当前文档冲突清单已清零”的检查结果

**退出标准**

- 系统里所有对象、层级、状态、唯一性和继承方向都有唯一说法
- 后续 session 不再需要重新解释“ContextHub 最基本对象是什么”

**建议开场 prompt**

请先和我一起定义 ContextHub 的 canonical invariants：租户唯一性、context/scope 类型系统、团队可见性继承方向、visibility 与 ACL 的关系、状态机字段、版本不可变性原则。目标是产出一页规范，作为后续所有设计文档的约束。

---

## Session 2: Versioning / Subscription / Dependency Model

**目标**

把版本、订阅、依赖三件事拆清楚，形成不会互相污染的模型。

**本次建议阅读**

- Session 1 的规范文档
- `01-storage-paradigm`
- `04-multi-agent-collaboration`
- `06-change-propagation`

**必须定案**

- Skill 是否采用“不可变版本对象 + latest/head 引用”
- “订阅某个 skill”与“某个 artifact 依赖某个 version”是否为两类不同边
- pinned subscription 与 floating subscription 的语义
- 读取 skill 时的版本解析规则
- 发布新版本时，哪些对象会变，哪些对象不该变
- “历史版本可稳定读取”如何落到模型上
- stale 的触发条件是基于 subscription、usage dependency，还是两者都要看

**本次不讨论**

- LISTEN/NOTIFY 的可靠性
- 具体 ACL 策略
- MVP 验证指标

**预期产出**

- 版本对象、订阅对象、依赖对象三者的关系定义
- 关键读写流程的决议版
- 必须修改的 schema 和文档清单

**退出标准**

- 能明确回答：`analysis-agent` pin 到 v2 后，v3 发布时它读到什么、被标记为什么、何时恢复
- 不再出现“latest 覆盖原对象”和“历史版本稳定可读”同时成立的矛盾

**建议开场 prompt**

我们基于 Session 1 的 invariants，只讨论 Skill versioning / subscription / dependency model。请帮我把“版本对象、订阅关系、使用依赖”三者拆开，产出一个可实现且无歧义的模型。

---

## Session 3: Propagation Reliability / Outbox Semantics

**目标**

把传播系统从“能跑”提升到“语义正确且可恢复”。

**本次建议阅读**

- Session 1 与 Session 2 的决议
- `06-change-propagation`
- `10-code-architecture`
- `12-evolution-notes`

**必须定案**

- `change_events` 是否是唯一 source of truth
- `NOTIFY` 的角色到底是唤醒还是可靠传递
- 启动补扫、周期补扫、失败重试的最小机制
- 事件处理的幂等边界
- “processed”的语义是否足够，还是需要更细状态
- 单实例 MVP 与多实例未来演进的兼容边界
- 传播失败时如何避免永久静默丢失

**本次不讨论**

- Skill 的权限模型
- 长文档检索
- Benchmark 设计

**预期产出**

- 一份可靠传播的状态机
- 一份事件消费契约
- 对 `06`、`10`、`12` 的重写方向

**退出标准**

- 即使漏掉 `NOTIFY`，事件也不会永久丢失
- 即使部分依赖处理失败，也不会破坏整体重试语义
- 文档中不再混用不同事件字段名或不同 source-of-truth 说法

**建议开场 prompt**

我们这场只解决 propagation reliability。请基于前两场决议，把 ContextHub 的 change_events / outbox / notify 语义定义完整，目标是保证漏通知或局部失败不会造成永久静默错误。

---

## Session 4: Visibility / ACL / Cross-Team Sharing

**目标**

冻结“谁默认能看见什么、谁还能进一步访问什么、跨团队怎么共享”这三层语义。

**本次建议阅读**

- Session 1 的规范文档
- `01-storage-paradigm`
- `04-multi-agent-collaboration`
- `05-access-control-audit`

**必须定案**

- visibility 是否作为第一层过滤，ACL 是否作为第二层授权
- 团队层级到底是“子读父”还是“父读子”
- `team/` 根空间的真实语义
- `datalake` 与 `resources` 是默认可见还是默认需要显式授权
- cross-team sharing 是 promote 到共同空间、reference、还是两者并存
- mask、deny、allow 的优先关系
- MVP 是否只实现 visibility，不实现 deny-override ACL

**本次不讨论**

- Skill 版本细节
- 传播引擎实现
- 长期 ReBAC/Zanzibar 升级方案

**预期产出**

- visibility 与 ACL 的两层模型
- cross-team sharing 的最小闭环
- 一份 MVP 与 post-MVP 的权限能力分界

**退出标准**

- 对任意一条资源，能明确判断：是否可见、是否允许读写、是否需要 mask
- 不再出现“父团队默认能看子团队”与“最小权限”并存的矛盾

**建议开场 prompt**

我们这场只讨论 visibility / ACL / cross-team sharing。请帮我把“默认可见性、显式授权、字段脱敏、跨团队共享”四件事拆清楚，并明确 MVP 到底做到哪一层。

---

## Session 5: MVP Claim / Validation Redesign

**目标**

让产品定位、MVP 承诺和验证方案互相一致。

**本次建议阅读**

- `00-index`
- `08-architecture`
- `09-implementation-plan`
- 前四场的决议

**必须定案**

- MVP 的真实 claim 是什么
- 哪些能力只是愿景，不应在 MVP 文案里当成已验证能力
- 首个垂直场景是否只是 Text-to-SQL 验证载体，而非产品本体
- 除 SQL EX 和 token 外，还应验证哪些横向能力
- 应保留哪些功能正确性测试
- 应新增哪些系统性验证指标

**建议补强的验证项**

- 传播命中率
- stale 恢复时延
- pinned/latest 版本解析正确性
- 跨 Agent 知识迁移收益
- 权限泄漏为 0
- 事件丢失恢复能力

**本次不讨论**

- 代码目录结构
- OpenClaw plugin 的具体实现

**预期产出**

- 一版真实、可 defend 的 MVP 定义
- 一版新的 success criteria
- 一版新的验证矩阵：横向能力 + 垂直场景

**退出标准**

- 能用一句话准确描述“ContextHub 的产品定位”
- 能再用一句话准确描述“MVP 这次到底证明了什么，没证明什么”
- 验证计划不再只是在证明“数据湖 SQL 上下文系统”

**建议开场 prompt**

我们这场只做 MVP claim 和 validation redesign。请帮我把产品定位、MVP 承诺和验证矩阵重新对齐，避免继续把愿景能力和已验证能力混在一起。

---

## Session 6: Code Architecture Rewrite

**目标**

在语义冻结后，重写代码架构文档，使实现者可以不再自行做产品级决策。

**本次建议阅读**

- `10-code-architecture`
- 前五场全部决议

**必须定案**

- 服务边界与依赖方向
- 哪些模块持有 canonical truth
- 版本解析在哪里做
- 传播消费接口如何抽象
- visibility / ACL 在哪一层执行
- SDK、API、plugin 各自负责什么
- migration 顺序与最小实现路径
- 哪些模块是 MVP 必需，哪些明确后置

**本次不讨论**

- 中长期替代方案
- 详细 Benchmark 执行脚本

**预期产出**

- 一版真正与前置决议一致的代码架构文档
- 一版清晰的实现顺序
- 一版实现前提与默认假设

**退出标准**

- 实现者拿到文档后，不需要再自行决定关键语义
- 代码架构不再复活前面已经被推翻的旧假设

**建议开场 prompt**

我们这场只根据前五场的决议重写 code architecture。目标不是补充模块名，而是让实现者不再需要自己做任何产品级决策。

---

## Session 7: ADR / Backlog Consolidation

**目标**

在前六场已经冻结主线语义之后，建立一套严格的分流规则：哪些议题必须回流主线，哪些可以明确后置，哪些只需要保留 ADR，哪些应该直接放弃。Session 7 的本质不是继续设计系统，而是关闭不必要的设计空间。

**本次建议阅读**

- 前六场的决议文档与已回写的权威规范
- `09-implementation-plan`
- `11-long-document-retrieval`
- `12-evolution-notes`
- `13-related-works`
- 所有仍然带有 `future`、`post-MVP`、`可选`、`可探索` 表述的段落

**判定顺序**

1. 如果没有这个主题，当前 MVP 就不正确、已冻结语义就会自相矛盾、或对外承诺就无法成立，它不属于 Session 7，而应 `回流主线`。
2. 如果它不影响当前正确性，但解决的是一个明确、可命名的未来问题，并且触发条件可观测，它应进入 `明确后置` backlog。
3. 如果它本质上是替代架构或升级路径，当前没有排期，只需要记录“为什么现在不做、什么条件下重开”，它应进入 `保留 ADR`。
4. 如果它既不影响当前正确性，也没有明确触发条件，或只是为了保留抽象上的可能性，则应 `直接放弃`。

**候选主题池（默认归类，供本次校验）**

- `直接放弃`
  - 自动依赖捕获替代写入路径显式登记。依赖传播的正确性必须来自写入路径的确定性建边；离线扫描只能是诊断/补洞工具，不能升级为 canonical 依赖语义。
- `明确后置`
  - run snapshot / context bundle
  - MCP Server
  - 长文档高级检索扩展
- `保留 ADR`
  - path ACL 升级到 ReBAC / Zanzibar
  - LISTEN/NOTIFY 迁移到消息队列
  - PG L2 迁移到对象存储
- `回流主线`
  - 默认应为空。只有当某个条目被证明会改变前六场已冻结的语义、或会阻塞当前实现正确性时，才允许回流。

**本次必须定案**

- 每个候选主题必须被归到 `回流主线`、`明确后置`、`保留 ADR`、`直接放弃` 四类之一，不允许继续停留在“以后再说”。
- `回流主线` 条目必须明确回流到哪一个 session、哪一份文档，而不是在本场继续展开设计。
- `明确后置` 条目必须写清 `解决的问题`、`触发条件`、`最小闭环`、`当前不做的代价`。
- `保留 ADR` 条目必须写清 `当前拒绝原因`、`重开前提`、`需要重新验证的假设`。
- `直接放弃` 条目必须从主线文档里移除悬空表述，避免未来继续污染讨论。

**本次不讨论**

- 重新打开前六场已冻结的问题
- 以“可能以后有用”为理由保留没有触发条件的方案
- 用长期扩展性假设稀释 MVP 的实现边界

**预期产出**

- 一份 ADR/backlog register（建议新增 `14-adr-backlog-register.md`）
- 每个条目的分类、触发条件、owner 文档、重开入口
- 一份 rejected ideas 清单，用于清理主线文档中的悬空 TODO / future notes

**退出标准**

- 主线文档只保留当前要实现的语义与机制，不再夹带“也许以后这样做”的替代方案
- 任一未来条目都能明确回答：为什么现在不做、什么条件下重开、重开时回到哪份文档
- 不再有悬空的“以后可能要做这个”但没人知道什么时候再讨论

**建议开场 prompt**

我们这场不再设计主线，而是关闭设计空间。请基于前六场已冻结的决议，把剩余候选项分成 `回流主线`、`明确后置`、`保留 ADR`、`直接放弃` 四类；只有会影响当前正确性或 MVP 承诺的议题才能回流主线，其余条目必须写清触发条件、owner 和重开入口。

---

## 最终完成标准

当 7 个 session 都完成后，应该得到以下状态：

- ContextHub 的对象模型、权限边界、版本语义、传播语义已经冻结
- MVP 的真实承诺和验证方式已经与产品定位对齐
- 代码架构文档已建立在冻结语义上，而非旧假设上
- 所有中长期方案都已被归类为 backlog / ADR / rejected，并具有明确触发条件或拒绝理由
- 后续任何实现 session 都可以直接基于这些决议推进，而不需要再回头补产品定义

## 默认假设

- 后续 session 仍以“先讨论并定案，再修改文档”为工作方式
- 每个 session 都允许对对应设计文档做修订，但不应跨 session 同时重构整套文档
- 如果中途发现新的根级矛盾，优先判断它属于哪个 session，而不是立即扩展当前 session 范围
