from pydantic import BaseModel


class UserBriefOut(BaseModel):
    """M9.1:账号搜索条目(刻意只含 id+username,不泄角色/状态给普通成员)。"""

    id: int
    username: str

    model_config = {"from_attributes": True}
