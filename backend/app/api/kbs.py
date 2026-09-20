from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.core.perms import get_kb_perm, has_perm
from app.db.session import get_db
from app.models import Document, KnowledgeBase, KbPermission, User
from app.schemas.kb import GrantIn, KBIn, KBOut, MemberOut, RenameIn
from app.services import kb_ops
from app.services.audit import audit

router = APIRouter(prefix="/kbs", tags=["kbs"])


@router.post("", response_model=KBOut, status_code=201)
async def create_kb(
    payload: KBIn,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if current.role == "viewer":
        raise HTTPException(status_code=403, detail="viewers cannot create knowledge bases")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="knowledge base name cannot be blank")
    dup = (
        await db.execute(select(KnowledgeBase).where(KnowledgeBase.name == name))
    ).scalars().first()
    if dup is not None:
        raise HTTPException(status_code=409, detail="knowledge base name already exists")
    kb = KnowledgeBase(
        name=name, description=payload.description, owner_id=current.id
    )
    db.add(kb)
    try:
        await db.flush()
    except IntegrityError:  # M12:并发窗口兜底,DB 唯一约束兜住
        await db.rollback()
        raise HTTPException(status_code=409,
                            detail="knowledge base name already exists")
    await audit(db, current.username, "kb_create", f"kb:{kb.id}", {"name": name})
    await db.commit()
    await db.refresh(kb)
    out = KBOut.model_validate(kb)
    out.my_perm = "owner"
    return out


async def _doc_counts(db: AsyncSession) -> dict[int, int]:
    rows = (
        await db.execute(
            select(Document.kb_id, func.count(Document.id)).group_by(Document.kb_id)
        )
    ).all()
    return dict(rows)


async def visible_kbs_for(db: AsyncSession, user: User) -> list[KnowledgeBase]:
    """user 可见库(id 倒序;不含 doc_count/my_perm 装配):
    admin 全库;否则 自有 ∪ 被授权。M12 抽出供 admin 目标用户查询复用。"""
    if user.role == "admin":
        stmt = select(KnowledgeBase)
    else:
        stmt = select(KnowledgeBase).where(
            or_(
                KnowledgeBase.owner_id == user.id,
                KnowledgeBase.id.in_(
                    select(KbPermission.kb_id).where(
                        KbPermission.user_id == user.id
                    )
                ),
            )
        )
    return (await db.execute(stmt.order_by(KnowledgeBase.id.desc()))
            ).scalars().all()


@router.get("", response_model=list[KBOut])
async def list_kbs(
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc_counts = await _doc_counts(db)
    rows = await visible_kbs_for(db, current)
    grants = () if current.role == "admin" else (
        await db.execute(
            select(KbPermission).where(KbPermission.user_id == current.id)
        )
    ).scalars().all()
    perm_by_kb = {g.kb_id: g.perm for g in grants}
    out = []
    for kb in rows:
        item = KBOut.model_validate(kb)
        item.my_perm = ("owner" if current.role == "admin"
                        or kb.owner_id == current.id
                        else perm_by_kb.get(kb.id))
        item.doc_count = doc_counts.get(kb.id, 0)
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
    out.doc_count = (
        await db.execute(
            select(func.count(Document.id)).where(Document.kb_id == kb_id)
        )
    ).scalar_one()
    return out


@router.delete("/{kb_id}", status_code=204)
async def delete_kb(
    kb_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """M12:删除知识库(admin/owner;级联见 kb_ops;不可逆)。"""
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    perm = await get_kb_perm(db, current, kb)
    if perm is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if not has_perm(perm, "owner"):  # admin/库主 get_kb_perm 即 owner
        raise HTTPException(status_code=403,
                            detail="owner or admin required")
    await kb_ops.delete_knowledge_base(db, kb, username=current.username)
    return Response(status_code=204)


@router.put("/{kb_id}", response_model=KBOut)
async def rename_kb(
    kb_id: int,
    payload: RenameIn,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """M13:重命名/改描述(admin/owner;重名 409)。"""
    kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    perm = await get_kb_perm(db, current, kb)
    if perm is None:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    if not has_perm(perm, "owner"):
        raise HTTPException(status_code=403,
                            detail="owner or admin required")
    name = payload.name.strip() if payload.name is not None else None
    if name == "":
        raise HTTPException(status_code=422,
                            detail="knowledge base name cannot be blank")
    if name is None and payload.description is None:
        raise HTTPException(status_code=422, detail="nothing to update")
    try:
        kb = await kb_ops.rename_knowledge_base(
            db, kb, name=name, description=payload.description,
            username=current.username)
    except (IntegrityError, kb_ops.DocOpError):
        # 并发兜底 + 应用层重名(duplicate 是 rename 唯一 DocOpError 码)
        await db.rollback()
        raise HTTPException(status_code=409,
                            detail="knowledge base name already exists")
    out = KBOut.model_validate(kb)
    out.my_perm = perm
    out.doc_count = (await db.execute(
        select(func.count(Document.id)).where(Document.kb_id == kb_id)
    )).scalar_one()
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
    await audit(
        db, current.username, "kb_grant", f"kb:{kb_id}",
        {"to": target.username, "perm": payload.perm},
    )
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
    await audit(db, current.username, "kb_revoke", f"kb:{kb_id}", {"from": username})
    await db.commit()
    return None
