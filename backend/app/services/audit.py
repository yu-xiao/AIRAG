import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import AuditLog


async def audit(
    db: AsyncSession,
    username: str,
    action: str,
    target: str = "",
    detail=None,
    ip: str | None = None,
) -> None:
    """追加一条审计记录;与业务同事务(不自行 commit)。"""
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False)
    db.add(
        AuditLog(
            username=username[:64],
            action=action[:32],
            target=target[:128],
            detail=detail,
            ip=ip,
        )
    )


async def purge_expired(db: AsyncSession) -> int:
    """按 AUDIT_RETENTION_DAYS 清理过期审计;禁用返回 0。

    删除动作自身在本事务落一条 audit_purge 摘要(调用方负责 commit)。
    """
    days = settings.AUDIT_RETENTION_DAYS
    if days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await db.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
    deleted = result.rowcount or 0
    await audit(
        db, "system", "audit_purge", "audit_logs",
        {"deleted": deleted, "retention_days": days},
    )
    return deleted
