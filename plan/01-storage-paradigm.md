# 01 — 统一存储范式：一切皆文件

## 核心理念

借鉴 OpenViking 的"一切皆文件"范式。文件存储 + 索引叠加。

```
存储层：一切皆文件（Markdown/JSON），通过 URI 路径组织
索引层：向量索引 + 关系文件（在文件之上，不替代文件）
```

## URI 目录结构

```
ctx://
├── datalake/{catalog}/{db}/{table}/      # 数据湖表
│   ├── .abstract.md                      # L0
│   ├── .overview.md                      # L1
│   ├── schema.json                       # L2
│   ├── lineage.json                      # 血缘关系
│   ├── .relations.json                   # 关联表
│   └── query_templates/                  # 查询模板
│
├── resources/{project}/                  # 文档资源
│   ├── .abstract.md
│   ├── .overview.md
│   └── {files...}
│
├── team/                                 # 共享空间（根 = 全组织）
│   ├── memories/                         # 全组织共享记忆
│   │   ├── business_rules/
│   │   └── data_dictionary/
│   ├── skills/                           # 全组织共享 skills
│   │
│   ├── engineering/                      # 工程部（子团队）
│   │   ├── memories/
│   │   ├── skills/
│   │   ├── backend/                      # 后端组（子子团队）
│   │   │   ├── memories/
│   │   │   └── skills/
│   │   └── data/                         # 数据组
│   │       ├── memories/
│   │       └── skills/
│   └── sales/                            # 销售部
│       ├── memories/
│       └── skills/
│
├── agent/{agent_id}/                     # Agent 私有空间
│   ├── memories/
│   │   ├── cases/
│   │   └── patterns/
│   └── skills/
│
└── user/{user_id}/                       # 用户空间
    └── memories/
        ├── profile/
        ├── preferences/
        ├── entities/
        └── events/
```

## 向量索引层

参考 OpenViking 的实现（`embedding_msg_converter.py`），L0、L1、L2 三个层级的文件都会被向量化，各自作为独立条目存入向量数据库，通过 `level` 字段区分。

| 文件 | 层级 | 被向量化的文本 | 向量 DB 中的 level 值 |
|------|------|---------------|---------------------|
| `.abstract.md` | L0 | 文件全文（~100 tokens） | 0 |
| `.overview.md` | L1 | 文件全文（~2k tokens） | 1 |
| 原始文件（如 `schema.json`） | L2 | `context.vectorize.text`（通常是 abstract） | 2 |

**注意：** L2 文件虽然也入向量库，但被向量化的文本不是 L2 全文（太长），而是该文件对应的 abstract 摘要文本。向量检索的核心文本来源是 L0 摘要。

### 向量 DB 记录字段（参考 OpenViking `collection_schemas.py`）

```
向量 DB 记录 = {
    id:            md5(account_id:uri)
    uri:           "ctx://datalake/prod/orders/.abstract.md"
    vector:        [0.12, -0.34, ...]      # dense embedding
    sparse_vector: {...}                   # 可选
    context_type:  "resource"              # resource | memory | skill
    level:         0                       # 0=L0, 1=L1, 2=L2
    parent_uri:    "ctx://datalake/prod/"
    account_id:    "acme"                  # 租户隔离
    owner_space:   "engineering/backend"   # 团队路径（用于权限过滤）
    name:          "orders"
    abstract:      "orders 表 - 存储所有订单交易记录..."
    tags:          "datalake,orders,交易"
    active_count:  42                      # 热度
    created_at:    "2026-03-01T..."
    updated_at:    "2026-03-18T..."
}
```

### 检索流程

```
用户问题："上个月销售额是多少？"

1. 意图分析 → TypedQuery: {query: "月度销售额统计", context_type: "resource", scope: "datalake"}
2. 向量检索（level=0，只搜 L0，速度快）+ 标量过滤（context_type, owner_space）→ top-K 候选
3. Rerank：读取候选的 .overview.md（L1），精排
4. 按需加载 L2：Agent 决定需要完整 schema 时，读取 schema.json
```

## 关系：用文件存储

关系 = 文件（`.relations.json`），不是独立的图数据库。

```json
// ctx://datalake/prod/orders/.relations.json
[
    {
        "id": "link_1",
        "uris": ["ctx://datalake/prod/users"],
        "reason": "orders.user_id JOIN users.id"
    },
    {
        "id": "link_2",
        "uris": ["ctx://datalake/prod/products"],
        "reason": "orders.product_id JOIN products.id"
    }
]
```

| 关系类型 | 存储方式 | 举例 |
|----------|----------|------|
| 父子关系 | URI 路径隐含 | `ctx://datalake/prod/` 是 `ctx://datalake/prod/orders/` 的父 |
| 表间 JOIN | `.relations.json` | orders → users（通过 user_id） |
| 数据血缘 | `lineage.json` | dwd_orders 上游是 ods_orders |
| 跨团队引用 | `.relations.json` + ACL 授权 | 后端组的 Skill 被销售部引用 |
| Skill 依赖 | `manifest.json` 中的 `dependencies` | sql-generator 依赖 schema-reader |

**统一 team 多层嵌套的优势：**
- `ctx://team/` = 根团队 = 全组织，不需要单独的 `org/` 概念
- 子目录天然表达子团队：`ctx://team/engineering/backend/` 就是工程部下的后端组
- 权限继承沿目录树向上：后端组 Agent 自动可见 `engineering/` 和 `team/`（根）的上下文
- 深度任意，企业按需嵌套，不受框架限制
- 完美契合"一切皆文件"范式——共享范围就是目录路径

## 可见性与权限规则

### 可见性（目录继承）

```
Agent 所属团队路径: team/engineering/backend

该 Agent 可见的上下文（从私有到全局）:
  1. ctx://agent/{self}/              ← 私有空间
  2. ctx://user/{user_id}/            ← 所服务用户的记忆
  3. ctx://team/engineering/backend/  ← 所属团队
  4. ctx://team/engineering/          ← 上级团队（自动继承）
  5. ctx://team/                      ← 根团队 = 全组织（自动继承）
  6. ctx://datalake/                  ← 数据湖（受 ACL 控制）
  7. ctx://resources/                 ← 文档资源（受 ACL 控制）
```

### 写权限

| 范围 | 谁可以写 |
|------|----------|
| `ctx://agent/{id}/` | 该 Agent 自己 |
| `ctx://team/.../` 某层级 | 该层级的成员（或管理员） |
| `ctx://team/` 根 | 组织管理员 |

### 跨团队共享

- 方案 1：提升到共同祖先 `ctx://team/` — 简单但范围过大
- 方案 2：通过 `.relations.json` 建立跨团队引用链接 — 精准但需要权限（更合理）

### Agent 多团队归属

```python
class AgentTeamMembership:
    agent_id: str
    memberships: list[TeamRole]

class TeamRole:
    team_path: str          # 如 "engineering/backend"
    role: str               # member | admin
    access: str             # read_write | read_only
```

- 主团队（primary）：Agent 写入共享记忆的默认目标
- 附属团队（secondary）：只读访问其他团队的共享上下文
- 类似 Unix 的主组 + 附属组

### 与 OpenViking 的区别

不在存储范式上做改变（仍然是文件），而是扩展了 URI 命名空间（新增 `datalake/`、多层级 `team/`）。索引层沿用 OpenViking 的双层设计（向量索引 + 关系文件），不引入额外的图数据库。
