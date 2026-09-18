from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import require_admin
from app.db.session import get_db
from app.models import AuditLog, User
from app.schemas.admin import AdminUserIn, AdminUserOut, AuditLogOut
from app.services.audit import audit, purge_expired

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/users", response_model=list[AdminUserOut])
async def list_users(
    q: str | None = None,
    limit: int | None = Query(None, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(User).order_by(User.id)
    if q and q.strip():
        stmt = stmt.where(User.username.ilike(f"%{q.strip()}%"))
    stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = await db.execute(stmt)
    return list(rows.scalars().all())


@router.patch("/users/{user_id}", response_model=AdminUserOut)
async def update_user(
    user_id: int,
    payload: AdminUserIn,
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if user_id == current.id:
        raise HTTPException(status_code=400, detail="cannot modify self")
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    changes = {
        k: v
        for k, v in {"role": payload.role, "is_active": payload.is_active}.items()
        if v is not None
    }
    if payload.role is not None:
        user.role = payload.role
    if payload.is_active is not None:
        user.is_active = payload.is_active
    await audit(
        db, current.username, "user_admin_update", f"user:{user_id}",
        {"target": user.username, **changes},
    )
    await db.commit()
    await db.refresh(user)
    return user


@router.get("/audit-logs")
async def list_audit_logs(
    username: str | None = None,
    action: str | None = None,
    page: int = 1,
    page_size: int = 20,
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    where = []
    if username:
        where.append(AuditLog.username == username)
    if action:
        where.append(AuditLog.action == action)
    total = (
        await db.execute(select(func.count(AuditLog.id)).where(*where))
    ).scalar_one()
    rows = (
        await db.execute(
            select(AuditLog)
            .where(*where)
            .order_by(AuditLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return {"total": total, "items": [AuditLogOut.model_validate(r) for r in rows]}


@router.post("/audit-logs/purge")
async def purge_audit_logs(
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    deleted = await purge_expired(db)
    await db.commit()
    return {"deleted": deleted, "retention_days": settings.AUDIT_RETENTION_DAYS}
