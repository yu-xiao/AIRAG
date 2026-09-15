from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.session import get_db
from app.models import KnowledgeBase, User
from app.schemas.kb import KBIn, KBOut

router = APIRouter(prefix="/kbs", tags=["kbs"])


@router.post("", response_model=KBOut, status_code=201)
async def create_kb(
    payload: KBIn,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = KnowledgeBase(
        name=payload.name, description=payload.description, owner_id=current.id
    )
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return kb


@router.get("", response_model=list[KBOut])
async def list_kbs(
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(KnowledgeBase).order_by(KnowledgeBase.id.desc()))
    return list(result.scalars().all())


@router.get("/{kb_id}", response_model=KBOut)
async def get_kb(
    kb_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return kb
