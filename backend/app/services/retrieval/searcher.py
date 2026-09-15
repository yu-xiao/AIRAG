import asyncio
import math
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.embedding import get_provider
from app.services.retrieval.tokenize import tokenize

RRF_K = 60


@dataclass
class SearchHit:
    chunk_id: int
    document_id: int
    kb_id: int
    filename: str
    page_no: int | None
    content: str
    score: float
    source: str  # vector|keyword|both


def rrf_fuse(vec: list, kw: list, k: int = RRF_K) -> list:
    """输入 [(id, raw_score)...],输出 [(id, rrf_score)...] 按 rrf 降序。"""
    scores: dict = {}
    for ranking in (vec, kw):
        for rank, (cid, _) in enumerate(ranking, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    both = {cid for cid, _ in vec} & {cid for cid, _ in kw}
    out = [
        (cid, s + (0.001 if cid in both else 0.0)) for cid, s in scores.items()
    ]
    return sorted(out, key=lambda x: -x[1])


def _tsquery(query: str) -> str:
    terms = [t for t in tokenize(query) if t.isalnum() or "\u4e00" <= t[0] <= "\u9fff"]
    return " | ".join(dict.fromkeys(terms)) or "''"


async def hybrid_search(
    db: AsyncSession, kb_ids: list[int], query: str, top_k: int = 20
) -> list[SearchHit]:
    provider = get_provider()
    qvec = (await asyncio.to_thread(provider.embed_documents, [query]))[0]
    qvec_str = "[" + ",".join(f"{x:.6f}" for x in qvec) + "]"

    vec_rows = (
        await db.execute(
            text(
                "SELECT c.id FROM chunks c "
                "WHERE c.kb_id = ANY(:kb_ids) AND c.embedding IS NOT NULL "
                "ORDER BY c.embedding <=> :qv ::vector LIMIT :k"
            ),
            {"kb_ids": kb_ids, "qv": qvec_str, "k": top_k},
        )
    ).scalars().all()

    kw_rows = (
        await db.execute(
            text(
                "SELECT c.id FROM chunks c "
                "WHERE c.kb_id = ANY(:kb_ids) AND c.tsv @@ to_tsquery('simple', :tsq) "
                "ORDER BY ts_rank(c.tsv, to_tsquery('simple', :tsq)) DESC LIMIT :k"
            ),
            {"kb_ids": kb_ids, "tsq": _tsquery(query), "k": top_k},
        )
    ).scalars().all()

    fused = rrf_fuse([(i, 0) for i in vec_rows], [(i, 0) for i in kw_rows])
    top_ids = [cid for cid, _ in fused[: top_k * 2]]
    if not top_ids:
        return []
    rows = (
        await db.execute(
            text(
                "SELECT c.id, c.document_id, c.kb_id, c.page_no, c.content, d.filename "
                "FROM chunks c JOIN documents d ON d.id = c.document_id "
                "WHERE c.id = ANY(:ids)"
            ),
            {"ids": top_ids},
        )
    ).all()
    by_id = {r.id: r for r in rows}
    vec_set, kw_set = set(vec_rows), set(kw_rows)
    hits = []
    for cid, score in fused:
        if cid not in by_id:
            continue
        r = by_id[cid]
        src = "both" if cid in vec_set and cid in kw_set else ("vector" if cid in vec_set else "keyword")
        hits.append(
            SearchHit(cid, r.document_id, r.kb_id, r.filename, r.page_no,
                      r.content, round(score, 6), src)
        )
    return hits
