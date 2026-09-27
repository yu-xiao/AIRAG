# backend/app/api/eval.py
"""M15:评估 API——只读记录+题集 CRUD+Web 触发(admin 全量;非 admin 仅 owner 库)。"""
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import ValidationError
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user
from app.core.perms import get_kb_perm, has_perm
from app.db.session import get_db
from app.models import (
    EvalItem,
    EvalQuestion,
    EvalRun,
    KnowledgeBase,
    KbPermission,
    User,
)
from app.schemas.eval import (
    EvalItemOut,
    EvalQuestionBulkError,
    EvalQuestionBulkIn,
    EvalQuestionBulkResultOut,
    EvalQuestionIn,
    EvalQuestionOut,
    EvalQuestionUpdate,
    EvalRunDetailOut,
    EvalRunOut,
    EvalTriggerIn,
)
from app.services.audit import audit

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
    user_names: dict[int, str] = {}
    page_uids = {r.triggered_by for r in rows if r.triggered_by is not None}
    if page_uids:
        users = (await db.execute(
            select(User.id, User.username).where(User.id.in_(page_uids))
        )).all()
        user_names = {u[0]: u[1] for u in users}
    items = []
    for r in rows:
        out = EvalRunOut.model_validate(r)
        out.kb_name = kb_names.get(r.kb_id)
        out.created_by = (user_names.get(r.triggered_by)
                          if r.triggered_by is not None else None)
        out.done_count = len(r.items)
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
    created_by = None
    if run.triggered_by is not None:
        u = await db.get(User, run.triggered_by)
        created_by = u.username if u is not None else None
    return EvalRunDetailOut(
        id=run.id, kb_id=run.kb_id,
        kb_name=kb.name if kb is not None else None,
        mode=run.mode, summary=run.summary, item_count=run.item_count,
        status=run.status, error=run.error, created_by=created_by,
        done_count=len(item_rows[:ITEMS_HARD_CAP]),
        created_at=run.created_at,
        items=[EvalItemOut.model_validate(i) for i in item_rows[:ITEMS_HARD_CAP]],
        items_truncated=len(item_rows) > ITEMS_HARD_CAP,
    )


async def _require_kb_owner(db: AsyncSession, current: User,
                            kb_id: int) -> KnowledgeBase:
    """题集/触发的统一权限门:M14 list_runs 的 kb 分支同款语义。
    M19 T5 起返回 KB 本体(export 需要 kb_name;既有调用方忽略返回值)。"""
    kb = await db.get(KnowledgeBase, kb_id)
    perm = await get_kb_perm(db, current, kb) if kb is not None else None
    if perm is None:
        raise HTTPException(status_code=404,
                            detail="knowledge base not found")
    if not has_perm(perm, "owner"):
        raise HTTPException(status_code=403, detail="owner or admin required")
    return kb


@router.get("/questions")
async def list_questions(
    kb_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_kb_owner(db, current, kb_id)
    where = EvalQuestion.kb_id == kb_id
    total = (await db.execute(
        select(func.count(EvalQuestion.id)).where(where))).scalar_one()
    rows = (await db.execute(
        select(EvalQuestion).where(where).order_by(EvalQuestion.id)
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return {"total": total,
            "items": [EvalQuestionOut.model_validate(r) for r in rows]}


@router.post("/questions", response_model=EvalQuestionOut, status_code=201)
async def create_question(
    payload: EvalQuestionIn,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_kb_owner(db, current, payload.kb_id)
    q = EvalQuestion(
        kb_id=payload.kb_id, question=payload.question,
        expect_doc_ids=payload.expect_doc_ids,
        expect_keywords=payload.expect_keywords,
        reference_answer=payload.reference_answer or None)
    db.add(q)
    await db.commit()
    await db.refresh(q)
    return q


@router.get("/questions/export")
async def export_questions(
    kb_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """M19 T5:题集导出——可移植 JSON 附件(不含 id/kb_id/created_at,
    可直接喂回 bulk);id asc;读操作不记 audit(与 questions GET 一致)。"""
    kb = await _require_kb_owner(db, current, kb_id)
    rows = (await db.execute(
        select(EvalQuestion).where(EvalQuestion.kb_id == kb_id)
        .order_by(EvalQuestion.id))).scalars().all()
    body = {
        "kb_id": kb_id, "kb_name": kb.name,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "count": len(rows),
        "questions": [{"question": q.question,
                       "expect_doc_ids": q.expect_doc_ids,
                       "expect_keywords": q.expect_keywords,
                       "reference_answer": q.reference_answer}
                      for q in rows],
    }
    return Response(content=json.dumps(body, ensure_ascii=False),
                    media_type="application/json",
                    headers={"Content-Disposition":
                             f'attachment; filename="eval-questions-kb{kb_id}.json"'})


@router.post("/questions/bulk",
             response_model=EvalQuestionBulkResultOut, status_code=201)
async def bulk_create_questions(
    payload: EvalQuestionBulkIn,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """M19 T5:批量导入(≤500)——部分成功:逐条走 EvalQuestionIn 校验
    (与单条 POST 同款),非法条进 errors[{index, detail}],合法条一次
    commit 收尾(audit 同事务)。"""
    await _require_kb_owner(db, current, payload.kb_id)
    created = 0
    errors: list[EvalQuestionBulkError] = []
    for i, raw in enumerate(payload.questions):
        try:
            item = EvalQuestionIn.model_validate(raw | {"kb_id": payload.kb_id})
        except ValidationError as e:
            first = e.errors()[0]
            field = first["loc"][0] if first["loc"] else "questions"
            label = "题干" if field == "question" else f"字段 {field}"
            errors.append(EvalQuestionBulkError(
                index=i, detail=f"{label}: {first['msg']}"))
            continue
        db.add(EvalQuestion(
            kb_id=payload.kb_id, question=item.question,
            expect_doc_ids=item.expect_doc_ids,
            expect_keywords=item.expect_keywords,
            reference_answer=item.reference_answer or None))
        created += 1
    await audit(db, current.username, "eval_questions_bulk",
                f"kb:{payload.kb_id}",
                {"created": created, "errors": len(errors)})
    await db.commit()
    return EvalQuestionBulkResultOut(created=created, errors=errors)


@router.put("/questions/{question_id}", response_model=EvalQuestionOut)
async def update_question(
    question_id: int,
    payload: EvalQuestionUpdate,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = await db.get(EvalQuestion, question_id)
    if q is None:
        raise HTTPException(status_code=404, detail="eval question not found")
    await _require_kb_owner(db, current, q.kb_id)
    q.question = payload.question
    q.expect_doc_ids = payload.expect_doc_ids
    q.expect_keywords = payload.expect_keywords
    q.reference_answer = payload.reference_answer or None
    await db.commit()
    await db.refresh(q)
    return q


@router.delete("/questions/{question_id}", status_code=204)
async def delete_question(
    question_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = await db.get(EvalQuestion, question_id)
    if q is None:
        raise HTTPException(status_code=404, detail="eval question not found")
    await _require_kb_owner(db, current, q.kb_id)
    await db.delete(q)
    await db.commit()


@router.get("/my-kbs")
async def my_kbs(
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """触发对话框与题集管理共用:admin 全部库,非 admin owner 库集。"""
    ids = await _owner_kb_ids(db, current)
    stmt = (
        select(KnowledgeBase.id, KnowledgeBase.name,
               func.count(EvalQuestion.id).label("qc"))
        .outerjoin(EvalQuestion, EvalQuestion.kb_id == KnowledgeBase.id)
        .group_by(KnowledgeBase.id, KnowledgeBase.name)
        .order_by(KnowledgeBase.id)
    )
    if ids is not None:
        if not ids:
            return []
        stmt = stmt.where(KnowledgeBase.id.in_(ids))
    rows = (await db.execute(stmt)).all()
    return [{"kb_id": r[0], "kb_name": r[1], "question_count": r[2]}
            for r in rows]


@router.post("/runs", status_code=201)
async def trigger_run(
    payload: EvalTriggerIn,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_kb_owner(db, current, payload.kb_id)
    q_count = (await db.execute(
        select(func.count(EvalQuestion.id))
        .where(EvalQuestion.kb_id == payload.kb_id))).scalar_one()
    if q_count == 0:
        raise HTTPException(status_code=422,
                            detail="no questions for this knowledge base")
    dup = (await db.execute(
        select(EvalRun.id).where(
            EvalRun.kb_id == payload.kb_id, EvalRun.mode == payload.mode,
            EvalRun.status == "running"))).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(status_code=409, detail="evaluation already running")
    top_k = (payload.top_k if payload.top_k is not None
             else settings.RETRIEVAL_TOP_K)
    run = EvalRun(kb_id=payload.kb_id, mode=payload.mode, summary=None,
                  item_count=q_count, status="running",
                  triggered_by=current.id)
    db.add(run)
    await db.commit()  # 铁律:先 commit 再 delay(eager/竞态下任务要看得见行)
    await db.refresh(run)
    from app.workers.eval_tasks import run_evaluation

    try:
        run_evaluation.delay(run.id, payload.mode, payload.rerank, top_k)
    except Exception as e:
        # I-1:.delay() 抛异常(broker 不可达/连接断)时消息已丢,run 若留
        # running 则同 kb+mode 永久 409、前端轮询永不停 → 收口 failed
        run.status = "failed"
        run.error = f"dispatch failed: {e}"[:500]
        await db.commit()
        raise HTTPException(
            status_code=502, detail="evaluation dispatch failed") from e
    return {"run_id": run.id}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """M19 T3:协作式取消——running→cancelling;任务循环逐题 commit 后
    检查点收口 cancelled(已完成子集照写 summary/item_count)。"""
    run = await db.get(EvalRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="eval run not found")
    await _require_kb_owner(db, current, run.kb_id)
    # M20:条件 UPDATE 防丢更新——读后任务恰收口终态时零行命中,
    # 重读分流(幂等/409),绝不覆盖任务已落的终态
    res = await db.execute(
        update(EvalRun)
        .where(EvalRun.id == run_id, EvalRun.status == "running")
        .values(status="cancelling"))
    if res.rowcount == 0:
        await db.refresh(run)
        if run.status == "cancelling":
            return {"id": run_id, "status": "cancelling"}  # 幂等,不重复 audit
        raise HTTPException(status_code=409, detail="run already finished")
    await audit(db, current.username, "eval_cancel", f"eval_run:{run_id}",
                {"kb_id": run.kb_id, "mode": run.mode})
    await db.commit()
    return {"id": run_id, "status": "cancelling"}
