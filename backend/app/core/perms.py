from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KnowledgeBase, KbPermission, User

RANK = {"viewer": 1, "editor": 2, "owner": 3}


async def get_kb_perm(db: AsyncSession, user: User, kb: KnowledgeBase) -> str | None:
    """KB 级权限:admin 全局隐式 owner;库主 owner;否则查 kb_permissions。"""
    if user.role == "admin" or kb.owner_id == user.id:
        return "owner"
    row = await db.execute(
        select(KbPermission.perm).where(
            KbPermission.kb_id == kb.id, KbPermission.user_id == user.id
        )
    )
    return row.scalar_one_or_none()


def has_perm(perm: str | None, min_perm: str) -> bool:
    return perm is not None and RANK[perm] >= RANK[min_perm]
