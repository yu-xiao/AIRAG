import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.session import get_db, SessionLocal
from app.models import Conversation, Message, User
from app.schemas.chat import AskIn
from app.services.chat_graph.graph import build_graph

router = APIRouter(prefix="/chat", tags=["chat"])


def _sse(evt_type: str, data) -> str:
    return f"data: {json.dumps({'type': evt_type, 'data': data}, ensure_ascii=False)}\n\n"


@router.post("/ask")
async def ask(
    payload: AskIn,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
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
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="conversation not found")

    db.add(Message(conversation_id=conv.id, role="user", content=payload.question))
    await db.commit()

    graph = build_graph()
    init = {"question": payload.question, "kb_ids": payload.kb_ids}
    cfg = {"configurable": {"thread_id": str(conv.id)}}

    async def gen():
        final_state = {}
        try:
            async for ev in graph.astream_events(init, config=cfg, version="v2"):
                if ev["event"] == "on_chat_model_stream":
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
