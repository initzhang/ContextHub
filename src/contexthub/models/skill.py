from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel


class SkillVersionStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"


class SkillVersion(BaseModel):
    skill_id: UUID
    version: int
    content: str
    changelog: str | None = None
    is_breaking: bool = False
    status: SkillVersionStatus = SkillVersionStatus.DRAFT
    published_by: str | None = None
    published_at: datetime | None = None


class SkillSubscription(BaseModel):
    id: int | None = None
    agent_id: str
    skill_id: UUID
    pinned_version: int | None = None
    account_id: str
    created_at: datetime | None = None
