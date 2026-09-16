import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user
from app.core.perms import get_kb_perm
from app.db.session import get_db, SessionLocal
from app.models import Conversation, KnowledgeBase, Message, User
from app.schemas.chat import AskIn
from app.services.audit import audit
from app.services.chat_graph.checkpointer import get_checkpointer
from app.services.chat_graph.graph import build_graph

router = APIRouter(prefix="/chat", tags=["chat"])


def _sse(evt_type: str, data) -> str:
    return f"data: {json.dumps({'type': evt_type, 'data': data}, ensure_ascii=False)}\n\n"


async def _recent_history(db: AsyncSession, conv_id: int, limit: int = 6) -> list[dict]:
    rows = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id == conv_id)
            .order_by(Message.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [{"role": m.role, "content": m.content} for m in reversed(rows)]


@router.post("/ask")
async def ask(
    payload: AskIn,
    request: Request,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    for kb_id in payload.kb_ids:
        kb = await db.get(KnowledgeBase, kb_id)
        if kb is None or await get_kb_perm(db, current, kb) is None:
            raise HTTPException(
                status_code=403, detail=f"no permission for knowledge base {kb_id}"
            )

    if payload.conversation_id is None:
        conv = Conversation(
            user_id=current.id, kb_ids=payload.kb_ids,
            title=payload.question[:20],
        )
        db.add(conv)
        await db.commit()
        await db.refresh(conv)
    else:
        conv = await db.get(Conversation, payload.conversation_id)
        if conv is None or conv.user_id != current.id:
            raise HTTPException(status_code=404, detail="conversation not found")

    # 历史要在落当前问题之前读(当前问题以 state.question 直达图,不进 history)
    history = (
        await _recent_history(db, conv.id) if payload.conversation_id is not None else []
    )
    db.add(Message(conversation_id=conv.id, role="user", content=payload.question))
    await db.commit()

    checkpointer = await get_checkpointer() if settings.CHECKPOINTER_ENABLED else None
    graph = build_graph(checkpointer=checkpointer)
    init = {
        "question": payload.question,
        "kb_ids": payload.kb_ids,
        "rerank": payload.rerank,
        "history": history,
    }
    cfg = {"configurable": {"thread_id": str(conv.id)}}

    async def gen():
        final_state = {}
        try:
            async for ev in graph.astream_events(init, config=cfg, version="v2"):
                # 只放行 generate 的 LLM 流(带 answer tag);rewrite/grade 的
                # 内部流式事件不得混入答案
                if (
                    ev["event"] == "on_chat_model_stream"
                    and "answer" in (ev.get("tags") or [])
                ):
                    chunk = ev["data"]["chunk"]
                    delta = getattr(chunk, "content", "") or ""
                    if isinstance(delta, str) and delta:
                        yield _sse("token", delta)
                elif ev["event"] == "on_chain_end" and ev["name"] == "generate":
                    final_state.update(ev["data"].get("output") or {})
            citations = final_state.get("citations") or []
            yield _sse("citations", citations)
            answer = final_state.get("answer") or ""
            async with SessionLocal() as s2:
                s2.add(Message(conversation_id=conv.id, role="assistant",
                               content=answer, citations=citations))
                await audit(
                    s2, current.username, "ask", f"conv:{conv.id}",
                    {"q": payload.question[:50], "kb_ids": payload.kb_ids},
                    request.client.host if request.client else None,
                )
                await s2.commit()
            # langchain-core 1.6: chat models stream internally on ainvoke, and
            # FakeListChatModel yields per-char chunks — done 携带完整 answer 作为
            # 权威终稿(客户端可对账),详见 task-5 报告"偏差"一节。
            yield _sse("done", {"conversation_id": conv.id, "answer": answer})
        except Exception:  # 断连/取消也会走这里
            # 真实异常只进日志;SSE 帧对客户端输出通用文案,避免泄露内部细节
            logger.exception("SSE 回答生成失败 conversation_id={}", conv.id)
            yield _sse("error", "回答生成失败,请稍后重试")

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
