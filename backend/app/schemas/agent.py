# backend/app/schemas/agent.py
"""M9:agent 面请求/响应 schema(Task 4 追加 search 部分)。"""
from pydantic import BaseModel


class AgentKbOut(BaseModel):
    id: int
    name: str
    description: str | None
    my_perm: str


class AgentKbListOut(BaseModel):
    items: list[AgentKbOut]
