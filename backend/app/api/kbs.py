from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.core.perms import get_kb_perm, has_perm
from app.db.session import get_db
from app.models import KnowledgeBase, KbPermission, User
from app.schemas.kb import GrantIn, KBIn, KBOut, MemberOut

router = APIRouter(prefix="/kbs", tags=["kbs"])


@router.post("", response_model=KBOut, status_code=201)
async def create_kb(
    payload: KBIn,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if current.role == "viewer":
        raise HTTPException(status_code=403, detail="viewers cannot create knowledge bases")
    kb = KnowledgeBase(
        name=payload.name, description=payload.description, owner_id=current.id
    )
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    out = KBOut.model_validate(kb)
    out.my_perm = "owner"
    return out


@router.get("", response_model=list[KBOut])
async def list_kbs(
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if current.role == "admin":
        rows = (
            await db.execute(select(KnowledgeBase).order_by(KnowledgeBase.id.desc()))
        ).scalars().all()
        out = []
        for kb in rows:
            item = KBOut.model_validate(kb)
            item.my_perm = "owner"
            out.append(item)
        return out
    rows = (
        await db.execute(
            select(KnowledgeBase)
            .where(
                or_(
                    KnowledgeBase.owner_id == current.id,
                    KnowledgeBase.id.in_(
                        select(KbPermission.kb_id).where(
                            KbPermission.user_id == current.id
                        )
                    ),
                )
            )
            .order_by(KnowledgeBase.id.desc())
        )
    ).scalars().all()
    grants = (
        await db.execute(
            select(KbPermission).where(KbPermission.user_id == current.id)
        )
    ).scalars().all()
    perm_by_kb = {g.kb_id: g.perm for g in grants}
    out = []
    for kb in rows:
        item = KBOut.model_validate(kb)
        item.my_perm = "owner" if kb.owner_id == current.id else perm_by_kb.get(kb.id)
        out.append(item)
    return out


@router.get("/{kb_id}", response_model=KBOut)
async def get_kb(
    kb_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    perm = await get_kb_perm(db, current, kb)
    if perm is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    out = KBOut.model_validate(kb)
    out.my_perm = perm
    return out


@router.get("/{kb_id}/permissions", response_model=list[MemberOut])
async def list_members(
    kb_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if not has_perm(await get_kb_perm(db, current, kb), "owner"):
        raise HTTPException(status_code=403, detail="owner permission required")
    rows = (
        await db.execute(
            select(KbPermission, User)
            .join(User, User.id == KbPermission.user_id)
            .where(KbPermission.kb_id == kb_id)
            .order_by(KbPermission.id)
        )
    ).all()
    return [
        MemberOut(user_id=p.user_id, username=u.username, perm=p.perm)
        for p, u in rows
    ]


@router.put("/{kb_id}/permissions", response_model=MemberOut)
async def grant_permission(
    kb_id: int,
    payload: GrantIn,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if not has_perm(await get_kb_perm(db, current, kb), "owner"):
        raise HTTPException(status_code=403, detail="owner permission required")
    target = (
        await db.execute(select(User).where(User.username == payload.username))
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    if target.id == kb.owner_id:
        raise HTTPException(status_code=400, detail="owner already has full access")
    row = (
        await db.execute(
            select(KbPermission).where(
                KbPermission.kb_id == kb_id, KbPermission.user_id == target.id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = KbPermission(kb_id=kb_id, user_id=target.id, perm=payload.perm)
        db.add(row)
    else:
        row.perm = payload.perm
    await db.commit()
    return MemberOut(user_id=target.id, username=target.username, perm=payload.perm)


@router.delete("/{kb_id}/permissions", status_code=204)
async def revoke_permission(
    kb_id: int,
    username: str,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if not has_perm(await get_kb_perm(db, current, kb), "owner"):
        raise HTTPException(status_code=403, detail="owner permission required")
    row = (
        await db.execute(
            select(KbPermission)
            .join(User, User.id == KbPermission.user_id)
            .where(KbPermission.kb_id == kb_id, User.username == username)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="permission not found")
    await db.delete(row)
    await db.commit()
    return None
