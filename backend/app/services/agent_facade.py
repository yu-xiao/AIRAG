# backend/app/services/agent_facade.py
"""M9:Agent 能力核心(唯一业务实现;REST 与 MCP 共用)。"""
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.perms import get_kb_perm
from app.models import KnowledgeBase, User


@dataclass
class KbBrief:
    id: int
    name: str
    description: str | None
    my_perm: str


class AgentKbDenied(Exception):
    """任一 kb_id 无权限或不存在;denied_kb_ids 不区分两者(不泄露存在性)。"""

    def __init__(self, denied_kb_ids: list[int]):
        super().__init__(denied_kb_ids)
        self.denied_kb_ids = denied_kb_ids


async def list_kbs_for(db: AsyncSession, user: User) -> list[KbBrief]:
    """与 GET /api/kbs 同语义:admin 全库 owner;否则 自有 ∪ 被授权。"""
    rows = (await db.execute(select(KnowledgeBase))).scalars().all()
    out = []
    for kb in rows:
        perm = await get_kb_perm(db, user, kb)
        if perm is not None:
            out.append(KbBrief(id=kb.id, name=kb.name,
                               description=kb.description, my_perm=perm))
    out.sort(key=lambda x: -x.id)
    return out
