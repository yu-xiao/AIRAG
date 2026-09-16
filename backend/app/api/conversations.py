from fastapi import APIRouter, Depends, HTTPException
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
