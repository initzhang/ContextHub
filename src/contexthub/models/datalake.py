from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class TableMetadata(BaseModel):
    context_id: UUID
    catalog: str
    database_name: str
    table_name: str
    ddl: str | None = None
    partition_info: dict | None = None
    stats: dict | None = None
    sample_data: dict | None = None
    stats_updated_at: datetime | None = None


class Lineage(BaseModel):
    upstream_id: UUID
    downstream_id: UUID
    transform_type: str | None = None
    description: str | None = None


class TableRelationship(BaseModel):
    table_id_a: UUID
    table_id_b: UUID
    join_type: str | None = None
    join_columns: dict
    confidence: float = 1.0


class QueryTemplate(BaseModel):
    id: int | None = None
    context_id: UUID
    sql_template: str
    description: str | None = None
    hit_count: int = 0
    last_used_at: datetime | None = None
    created_by: str | None = None
