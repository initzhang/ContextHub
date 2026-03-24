from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel


class ContextLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"


class ContextType(StrEnum):
    TABLE_SCHEMA = "table_schema"
    SKILL = "skill"
    MEMORY = "memory"
    RESOURCE = "resource"


class Scope(StrEnum):
    DATALAKE = "datalake"
    TEAM = "team"
    AGENT = "agent"
    USER = "user"


class ContextStatus(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    ARCHIVED = "archived"
    DELETED = "deleted"
    PENDING_REVIEW = "pending_review"


class Context(BaseModel):
    id: UUID
    uri: str
    context_type: ContextType
    scope: Scope
    owner_space: str | None = None
    account_id: str
    l0_content: str | None = None
    l1_content: str | None = None
    l2_content: str | None = None
    file_path: str | None = None
    status: ContextStatus = ContextStatus.ACTIVE
    version: int = 1
    tags: list[str] = []
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_accessed_at: datetime | None = None
    stale_at: datetime | None = None
    archived_at: datetime | None = None
    deleted_at: datetime | None = None
    active_count: int = 0
    adopted_count: int = 0
    ignored_count: int = 0


class CreateContextRequest(BaseModel):
    uri: str
    context_type: ContextType
    scope: Scope
    owner_space: str | None = None
    l0_content: str | None = None
    l1_content: str | None = None
    l2_content: str | None = None
    file_path: str | None = None
    tags: list[str] = []


class UpdateContextRequest(BaseModel):
    l0_content: str | None = None
    l1_content: str | None = None
    l2_content: str | None = None
    file_path: str | None = None
    status: ContextStatus | None = None
    tags: list[str] | None = None
