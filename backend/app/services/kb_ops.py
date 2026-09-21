# backend/app/services/kb_ops.py
"""M12:KB 删除级联(Web 面唯一实现;spec C)。

顺序:busy 409 → chunks → documents → kb_permissions → conversations
array_remove 清悬空 id → KB 行 → 审计 → commit → 磁盘/评估集尽力清理。
"""
import shutil
from pathlib import Path

from loguru import logger
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import (Chunk, Conversation, Document, KbPermission,
                        KnowledgeBase)
from app.services.audit import audit
from app.services.doc_ops import BUSY_STATUSES, DocOpError

# backend/eval_sets(文件题集遗留,M15 起题源入 eval_questions 表)
EVAL_DIR = Path(__file__).resolve().parents[2] / "eval_sets"


async def delete_knowledge_base(
    db: AsyncSession, kb: KnowledgeBase, *, username: str,
) -> None:
    """级联删除知识库(不可逆;调用面前置 owner/admin 校验)。"""
    kb_id = kb.id
    busy = (await db.execute(
        select(Document.id).where(
            Document.kb_id == kb_id,
            Document.status.in_(BUSY_STATUSES),
        ).limit(1)
    )).scalar_one_or_none()
    if busy is not None:
        # M13 快修①:统一走 DocOpError(main.py 全局 handler,报文零变化)
        raise DocOpError("busy", 409,
                         "knowledge base has documents being processed")
    doc_count = (await db.execute(
        select(func.count(Document.id)).where(Document.kb_id == kb_id)
    )).scalar_one()
    member_count = (await db.execute(
        select(func.count(KbPermission.id)).where(KbPermission.kb_id == kb_id)
    )).scalar_one()
    await db.execute(delete(Chunk).where(Chunk.kb_id == kb_id))
    await db.execute(delete(Document).where(Document.kb_id == kb_id))
    await db.execute(delete(KbPermission).where(KbPermission.kb_id == kb_id))
    # 清悬空 id:不清会让旧会话提问撞 kb_forbidden(spec C2 步5)
    await db.execute(
        update(Conversation)
        .where(func.array_position(Conversation.kb_ids, kb_id).isnot(None))
        .values(kb_ids=func.array_remove(Conversation.kb_ids, kb_id))
    )
    await audit(db, username, "kb_delete", f"kb:{kb_id}",
                {"name": kb.name, "doc_count": doc_count,
                 "member_count": member_count})
    await db.delete(kb)
    await db.commit()
    # 尽力清理(行已删,失败仅日志;孤儿由 purge 哲学兜底)
    shutil.rmtree(Path(settings.UPLOAD_DIR) / str(kb_id), ignore_errors=True)
    try:
        (EVAL_DIR / f"{kb_id}.json").unlink(missing_ok=True)
    except OSError:
        logger.warning(f"eval set removal failed: kb {kb_id}")


async def rename_knowledge_base(
    db: AsyncSession, kb: KnowledgeBase, *, name: str | None,
    description: str | None, username: str,
) -> KnowledgeBase:
    """重命名/改描述(owner/admin 由端点校验);重名 409,审计 kb_update。"""
    old_name, old_desc = kb.name, kb.description
    if name is not None and name != kb.name:
        dup = (await db.execute(
            select(KnowledgeBase).where(KnowledgeBase.name == name)
        )).scalars().first()
        if dup is not None:
            raise DocOpError("duplicate", 409,
                             "knowledge base name already exists")
        kb.name = name
    if description is not None:
        # M14:空串=显式清空,入库 NULL(空=无描述的单一表示)
        kb.description = description or None
    # M14:detail 如实反映实际变更;比较基于规范化后的值
    changes: dict = {}
    if kb.name != old_name:
        changes["name"] = {"old": old_name, "new": kb.name}
    if kb.description != old_desc:
        changes["description"] = "updated"
    await audit(db, username, "kb_update", f"kb:{kb.id}",
                changes or {"no_change": True})
    await db.commit()
    await db.refresh(kb)
    return kb
