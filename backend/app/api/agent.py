# backend/app/api/agent.py
"""M9:REST 面(薄壳;能力全部来自 services/agent_facade)。"""
from dataclasses import asdict

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_agent_principal
from app.db.session import get_db
from app.schemas.agent import AgentKbListOut, AgentKbOut
from app.services import agent_facade
from app.services.audit import audit

router = APIRouter(prefix="/agent", tags=["agent"])


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("/kbs", response_model=AgentKbListOut)
async def agent_kbs(
    request: Request,
    principal: Principal = Depends(get_agent_principal),
    db: AsyncSession = Depends(get_db),
):
    items = await agent_facade.list_kbs_for(db, principal.user)
    await audit(
        db, principal.user.username, "agent.list_kbs", "agent",
        {"client": "rest", "key_name": principal.key_name, "kb_count": len(items)},
        ip=_ip(request),
    )
    await db.commit()
    return AgentKbListOut(items=[AgentKbOut(**asdict(i)) for i in items])
