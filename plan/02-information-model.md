# 02 — L0/L1/L2 信息模型与记忆分类

## L0/L1/L2 三层信息模型

| 层级 | Token 量 | 用途 | 数据湖表示例 |
|------|----------|------|-------------|
| L0 Abstract | ~100 | 向量检索、快速过滤 | 表名 + 一句话描述 |
| L1 Overview | ~2k | Rerank、内容导航 | schema + 字段说明 + 样例数据 |
| L2 Detail | 不限 | 按需加载 | 完整 DDL + 血缘 + 查询模板 |

## 记忆分类

借鉴 OpenViking 的 6 类记忆并扩展：

| 范围 | 类别 | 说明 | 更新策略 |
|------|------|------|----------|
| 用户级 | profile | 用户基本信息 | 可追加 |
| 用户级 | preferences | 用户偏好 | 可追加 |
| 用户级 | entities | 实体记忆（人、项目） | 可追加 |
| 用户级 | events | 事件记录 | 不可变 |
| Agent级 | cases | 学到的案例 | 不可变 |
| Agent级 | patterns | 学到的模式 | 可追加 |
| 团队级（任意层级） | shared_knowledge | 该层级团队共享的业务知识 | 可追加，需审核 |
| 团队级（根 = 全组织） | business_rules | 全组织业务规则（存在 `ctx://team/memories/`） | 管理员维护 |
| 团队级（根 = 全组织） | data_dictionary | 全组织数据字典（存在 `ctx://team/memories/`） | 管理员维护 |

## 层级检索

采用目录层级递归检索（借鉴 OpenViking 的目录递归 + 优先队列 + 收敛检测），天然适配数据湖的 catalog → database → table 层级。跨目录的关联检索通过读取 `.relations.json` 文件实现。

## 热度评分

```
score = sigmoid(log1p(active_count)) * exponential_decay(updated_at)
```

用于冷热记忆管理。
