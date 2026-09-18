"""M9.1:账号搜索(成员授权下拉、admin 密钥绑定下拉共用)。登录即可调。"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.session import get_db
from app.models import User
from app.schemas.users import UserBriefOut

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=list[UserBriefOut])
async def search_users(
    q: str = Query("", max_length=64),
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    where = [User.is_active == True]  # noqa: E712
    kw = q.strip()
    if kw:
        where.append(User.username.ilike(f"%{kw}%"))
    rows = (
        await db.execute(
            select(User)
            .where(*where)
            .order_by(User.username)
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    return rows
