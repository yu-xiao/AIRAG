from app.core.config import settings
from app.db.session import SessionLocal
from app.services.retrieval.searcher import SearchHit, hybrid_search
from app.services.rerank.base import get_reranker

SYSTEM_PROMPT = (
    "你是企业知识库助手。只依据下面提供的参考资料回答;"
    "引用资料时标注编号如 [1][2];若资料不足以回答,明确说"
    "\"知识库中未找到相关内容\"。用中文,简洁分点。"
)


def build_citations(hits: list[SearchHit]) -> list[dict]:
    return [
        {
            "number": i + 1,
            "chunk_id": h.chunk_id,
            "document_id": h.document_id,
            "filename": h.filename,
            "page_no": h.page_no,
            "excerpt": h.content[:160],
        }
        for i, h in enumerate(hits[: settings.RETRIEVAL_TOP_K])
    ]


async def retrieve_node(state: dict) -> dict:
    async with SessionLocal() as db:
        hits = await hybrid_search(db, state["kb_ids"], state["question"])
    return {"hits": [h.__dict__ for h in hits]}


async def rerank_node(state: dict) -> dict:
    reranker = get_reranker()
    if reranker is None or not state.get("hits"):
        return {}
    import asyncio

    hits = state["hits"]
    order = await asyncio.to_thread(
        reranker.rerank, state["question"],
        [h["content"] for h in hits], settings.RETRIEVAL_TOP_K,
    )
    return {"hits": [hits[i] for i in order if i < len(hits)]}


async def generate_node(state: dict, llm) -> dict:
    hits = state.get("hits", [])[: settings.RETRIEVAL_TOP_K]
    context = "\n\n".join(
        f"[{i+1}] {h['filename']} 第{h['page_no'] or '?'}页:{h['content']}"
        for i, h in enumerate(hits)
    )
    messages = [
        ("system", SYSTEM_PROMPT),
        ("user", f"参考资料:\n{context}\n\n问题:{state['question']}"),
    ]
    resp = await llm.ainvoke(messages)
    shits = [
        SearchHit(
            chunk_id=h["chunk_id"], document_id=h["document_id"], kb_id=h["kb_id"],
            filename=h["filename"], page_no=h["page_no"], content=h["content"],
            score=h["score"], source=h["source"],
        )
        for h in hits
    ]
    return {"answer": resp.content, "citations": build_citations(shits)}
