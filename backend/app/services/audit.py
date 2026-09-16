import json

from sqlalchemy.ext.asyncio import AsyncSession

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
            action=action,
            target=target[:128],
            detail=detail,
            ip=ip,
        )
    )
