# 03 — 数据湖表管理

## OpenViking 现状核实（基于源码验证）

| 能力 | OpenViking 状态 | 证据 |
|------|-----------------|------|
| L0/L1/L2 三层存储范式 | ✅ 已实现 | `Context` 类有 `level` 字段（0/1/2）；`write_context()` 自动创建 `.abstract.md`、`.overview.md` 和原始内容文件 |
| 通用 URI 文件系统 | ✅ 已实现 | `VikingFS` 提供 URI→路径转换，理论上可扩展新命名空间 |
| 向量索引 + 标量过滤 | ✅ 已实现 | `collection_schemas.py` 定义了 `context_type`、`level`、`parent_uri` 等标量索引字段 |
| `datalake/` URI 命名空间 | ❌ 不存在 | `directories.py` 的 preset scopes 只有 session/user/agent/resources，无 datalake |
| CatalogConnector（外部数据目录连接） | ❌ 不存在 | adapter 模式仅用于向量 DB 后端（local/http/volcengine/vikingdb）；无外部数据源连接器 |
| 湖表 schema 解析 | ❌ 不存在 | `parse/registry.py` 支持 text/md/pdf/html/word/excel/code/image 等，无 table schema parser |
| 表级元数据字段（column definitions、partition、stats） | ❌ 不存在 | 向量 DB schema 只有通用字段（name/description/tags/abstract/meta），无表结构专用字段 |
| 数据血缘（lineage）管理 | ❌ 不存在 | 无 lineage 相关代码或数据结构 |
| 查询模板（query templates）管理 | ❌ 不存在 | 无 query template 概念；Skill 是最接近的，但面向 Agent 指令而非 SQL 模板 |
| 表间关系（JOIN 关系） | ❌ 不存在 | 无 `.relations.json` 机制（这是 plan 中我们自己设计的） |
| catalog 变更检测（detect_changes） | ❌ 不存在 | 无任何外部数据源变更监听机制 |
| Text-to-SQL 上下文组装 | ❌ 不存在 | 检索引擎（`hierarchical_retriever.py`）是通用的目录递归检索，无 SQL 生成专用逻辑 |

**结论：12 项能力中，OpenViking 只提供了 3 项通用基础设施（L0/L1/L2 范式、URI 文件系统、向量索引）。剩余 9 项数据湖专用能力全部需要从零实现。** 这不是"加个 connector 就行"的事——需要新的命名空间、新的 parser、新的元数据模型、新的关系管理、新的变更检测机制，以及 Text-to-SQL 专用的上下文组装逻辑。

## (a) 湖表元数据作为 Context

```
ctx://datalake/{catalog}/{database}/{table}
  L0: "orders 表 - 存储所有订单交易记录，包含订单金额、状态、时间等"
  L1: |
    ## Schema
    | 字段 | 类型 | 说明 |
    | order_id | BIGINT | 订单ID，主键 |
    | user_id | BIGINT | 用户ID，关联 users 表 |
    | amount | DECIMAL | 订单金额 |
    | status | STRING | 订单状态: pending/paid/shipped/completed |
    | created_at | TIMESTAMP | 创建时间 |

    ## 常用查询模式
    - 按时间范围统计订单金额
    - 按状态分组统计
    - 与 users 表 JOIN 查询用户订单

    ## 样例数据
    | order_id | user_id | amount | status |
    | 1001 | 42 | 299.00 | completed |
    ...
  L2: 完整 DDL + 分区信息 + 统计信息 + 数据血缘 + 查询模板集合
```

## (b) 通用 CatalogConnector 接口

```python
class CatalogConnector(ABC):
    """通用数据目录连接器，后续可实现 Hive/Iceberg/Delta Lake 等"""
    async def list_databases(self) -> list[str]
    async def list_tables(self, database: str) -> list[str]
    async def get_table_schema(self, database: str, table: str) -> TableSchema
    async def get_table_stats(self, database: str, table: str) -> TableStats
    async def get_sample_data(self, database: str, table: str, limit: int) -> list[dict]
    async def detect_changes(self, since: datetime) -> list[ChangeEvent]
```

## (c) Text-to-SQL 上下文组装

根据用户问题：
1. 检索相关表的 L1（schema + 字段说明）
2. 附加表间关系（JOIN 关系图）
3. 附加历史成功查询的 cases（从 Agent 记忆中检索）
4. 附加业务术语表（从根团队记忆 `ctx://team/memories/data_dictionary/` 中检索）
