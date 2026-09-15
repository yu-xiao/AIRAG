from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.retrieval.tokenize import tokenize


async def rebuild_tsv(db: AsyncSession) -> int:
    """用 jieba 分词版重建全部 chunks 的 tsv(存量数据迁移,幂等)。"""
    rows = (await db.execute(text("SELECT id, content FROM chunks"))).all()
    for r in rows:
        joined = " ".join(tokenize(r.content))
        await db.execute(
            text("UPDATE chunks SET tsv = to_tsvector('simple', :t) WHERE id = :id"),
            {"t": joined, "id": r.id},
        )
    await db.commit()
    return len(rows)
