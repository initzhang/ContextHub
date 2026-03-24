from pydantic import BaseModel


class PromoteRequest(BaseModel):
    uri: str
    target_team: str
