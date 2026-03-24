from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class Team(BaseModel):
    id: UUID
    path: str
    parent_id: UUID | None = None
    display_name: str | None = None
    account_id: str
    created_at: datetime | None = None


class TeamMembership(BaseModel):
    agent_id: str
    team_id: UUID
    role: str = "member"
    access: str = "read_write"
    is_primary: bool = False
