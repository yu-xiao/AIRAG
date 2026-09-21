# backend/app/api/eval.py
"""M14:评估记录只读 API(admin 全量;非 admin 仅 owner 库)。"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.core.perms import get_kb_perm, has_perm
from app.db.session import get_db
from app.models import (
    EvalItem,
    EvalRun,
    KnowledgeBase,
    KbPermission,
    User,
)
from app.schemas.eval import EvalItemOut, EvalRunDetailOut, EvalRunOut

router = APIRouter(prefix="/eval", tags=["eval"])

ITEMS_HARD_CAP = 500  # 防御性上限:评估题集通常 ≤ 几十


async def _owner_kb_ids(db: AsyncSession, user: User) -> list[int] | None:
    """owner 库集;admin → None(不过滤)。与 get_kb_perm 同语义:
    自己建的库 ∪ kb_permissions 授 owner 的行(GrantIn 只发 viewer/editor,
    后者为对齐语义的防御性条件)。"""
    if user.role == "admin":
        return None
    rows = await db.execute(
        select(KnowledgeBase.id).where(
            or_(
                KnowledgeBase.owner_id == user.id,
                KnowledgeBase.id.in_(
                    select(KbPermission.kb_id).where(
                        KbPermission.user_id == user.id,
                        KbPermission.perm == "owner",
                    )
                ),
            )
        )
    )
    return [r for (r,) in rows.all()]


@router.get("/runs")
async def list_runs(
    kb_id: int | None = None,
    mode: str | None = Query(None, pattern="^(retrieval|generation)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if kb_id is not None:
        kb = await db.get(KnowledgeBase, kb_id)
        perm = None
        if kb is not None:
            perm = await get_kb_perm(db, current, kb)
        if perm is None:
            raise HTTPException(status_code=404,
                                detail="knowledge base not found")
        if not has_perm(perm, "owner"):
            raise HTTPException(status_code=403,
                                detail="owner or admin required")
        where = [EvalRun.kb_id == kb_id]
    else:
        ids = await _owner_kb_ids(db, current)
        if ids == []:
            return {"total": 0, "items": []}
        where = [] if ids is None else [EvalRun.kb_id.in_(ids)]
    if mode:
        where.append(EvalRun.mode == mode)
    total = (await db.execute(
        select(func.count(EvalRun.id)).where(*where))).scalar_one()
    rows = (await db.execute(
        select(EvalRun).where(*where).order_by(EvalRun.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    # kb_name 联查:页内 kb_id 批量一次,避免逐行;KB 已删不在结果集 → None
    page_kb_ids = {r.kb_id for r in rows}
    kb_names: dict[int, str] = {}
    if page_kb_ids:
        kbs = (await db.execute(
            select(KnowledgeBase).where(KnowledgeBase.id.in_(page_kb_ids))
        )).scalars().all()
        kb_names = {kb.id: kb.name for kb in kbs}
    items = []
    for r in rows:
        out = EvalRunOut.model_validate(r)
        out.kb_name = kb_names.get(r.kb_id)
        items.append(out)
    return {"total": total, "items": items}


@router.get("/runs/{run_id}", response_model=EvalRunDetailOut)
async def get_run(
    run_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await db.get(EvalRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="eval run not found")
    kb = await db.get(KnowledgeBase, run.kb_id)
    if current.role != "admin":
        # 越权统一 404(不区分 403,防探测 run 存在性);KB 已删时
        # owner 身份无从核验 → 非 admin 一律 404
        perm = await get_kb_perm(db, current, kb) if kb is not None else None
        if perm is None or not has_perm(perm, "owner"):
            raise HTTPException(status_code=404, detail="eval run not found")
    # 显式有序+limit 查询(relationship 为 selectin 预载,顺序无保证)
    item_rows = (await db.execute(
        select(EvalItem).where(EvalItem.run_id == run.id)
        .order_by(EvalItem.id).limit(ITEMS_HARD_CAP + 1)
    )).scalars().all()
    # 手动构造:避免 from_attributes 触发 relationship 的无序预载
    return EvalRunDetailOut(
        id=run.id, kb_id=run.kb_id,
        kb_name=kb.name if kb is not None else None,
        mode=run.mode, summary=run.summary, item_count=run.item_count,
        created_at=run.created_at,
        items=[EvalItemOut.model_validate(i) for i in item_rows[:ITEMS_HARD_CAP]],
        items_truncated=len(item_rows) > ITEMS_HARD_CAP,
    )
