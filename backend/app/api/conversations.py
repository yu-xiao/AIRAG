from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.session import get_db
from app.models import Conversation, Message, User
from app.schemas.chat import ConversationIn, ConversationOut, MessageOut
from app.services.audit import audit

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/conversations", response_model=ConversationOut, status_code=201)
async def create_conversation(
    payload: ConversationIn,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = Conversation(
        user_id=current.id, kb_ids=payload.kb_ids,
        title=payload.name or "新对话",
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.execute(
        select(Conversation)
        .where(Conversation.user_id == current.id)
        .order_by(Conversation.id.desc())
    )
    return list(rows.scalars().all())


@router.get("/conversations/{conv_id}/messages", response_model=list[MessageOut])
async def list_messages(
    conv_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = await db.get(Conversation, conv_id)
    if conv is None or conv.user_id != current.id:
        raise HTTPException(status_code=404, detail="conversation not found")
    rows = await db.execute(
        select(Message).where(Message.conversation_id == conv_id).order_by(Message.id)
    )
    return list(rows.scalars().all())


@router.delete("/conversations/{conv_id}", status_code=204)
async def delete_conversation(
    conv_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = await db.get(Conversation, conv_id)
    if conv is None or conv.user_id != current.id:
        raise HTTPException(status_code=404, detail="conversation not found")
    await audit(
        db, current.username, "conv_delete", f"conv:{conv_id}", {"title": conv.title}
    )
    await db.execute(delete(Message).where(Message.conversation_id == conv_id))
    await db.delete(conv)
    await db.commit()
    return None


@router.get("/conversations/{conv_id}/export")
async def export_conversation(
    conv_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = await db.get(Conversation, conv_id)
    if conv is None or conv.user_id != current.id:
        raise HTTPException(status_code=404, detail="conversation not found")
    rows = (
        await db.execute(
            select(Message).where(Message.conversation_id == conv_id).order_by(Message.id)
        )
    ).scalars().all()

    lines = [f"# {conv.title}", "", f"> 创建时间:{conv.created_at:%Y-%m-%d %H:%M}", ""]
    appendix: list[tuple[str, int | None, str]] = []
    for m in rows:
        speaker = "用户" if m.role == "user" else "助手"
        lines.append(f"**{speaker}**:{m.content}")
        lines.append("")
        if m.role == "assistant" and m.refused:
            continue  # M7:拒答消息的引用是噪声,与前端隐藏口径一致
        for c in m.citations or []:
            appendix.append(
                (c.get("filename", "?"), c.get("page_no"), c.get("excerpt", ""))
            )
    if appendix:
        lines.append("---")
        lines.append("")
        lines.append("## 引用附录")
        for i, (fname, page, excerpt) in enumerate(appendix, start=1):
            page_s = f"第{page}页" if page else "页码未知"
            lines.append(f"[{i}] {fname} {page_s}:{excerpt}")
            lines.append("")
    md_text = "\n".join(lines)
    return Response(
        content=md_text,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="conv-{conv_id}.md"'},
    )
