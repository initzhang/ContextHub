from dataclasses import dataclass


@dataclass
class RequestContext:
    account_id: str
    agent_id: str
    expected_version: int | None = None
