import json

from loguru import logger

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
    queries = state.get("sub_queries") or [
        state.get("search_query") or state["question"]
    ]
    per_query = []
    async with SessionLocal() as db:
        for q in queries:
            hits = await hybrid_search(db, state["kb_ids"], q)
            per_query.append(hits)
    # 跨查询轮转交错(chunk_id 去重),合并上限 2*top_k,留给 rerank 全局重排
    merged, seen = [], set()
    cap = settings.RETRIEVAL_TOP_K * 2
    depth = 0
    while len(merged) < cap and any(depth < len(hs) for hs in per_query):
        for hs in per_query:
            if len(merged) >= cap:
                break
            if depth < len(hs):
                h = hs[depth]
                if h.chunk_id not in seen:
                    seen.add(h.chunk_id)
                    merged.append(h)
        depth += 1
    return {"hits": [h.__dict__ for h in merged]}


async def rerank_node(state: dict) -> dict:
    reranker = get_reranker()
    if reranker is None or not state.get("hits") or not state.get("rerank"):
        return {}
    import asyncio

    hits = state["hits"]
    scored = await asyncio.to_thread(
        reranker.rerank, state["question"],
        [h["content"] for h in hits], settings.RETRIEVAL_TOP_K,
    )
    # M7:排序取 top_k 后按相关度阈值过滤;relevance 回写 score
    # (rerank 开启时 score 语义=相关度分;关闭时保持 RRF 融合分)
    ranked = sorted(scored, key=lambda p: p[1], reverse=True)[: settings.RETRIEVAL_TOP_K]
    out = []
    for idx, rel in ranked:
        if not 0 <= idx < len(hits):
            continue
        if settings.RETRIEVAL_MIN_SCORE > 0 and rel < settings.RETRIEVAL_MIN_SCORE:
            continue
        h = dict(hits[idx])
        h["score"] = round(float(rel), 6)
        out.append(h)
    return {"hits": out}


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
    resp = await llm.ainvoke(messages, config={"tags": ["answer"]})
    shits = [
        SearchHit(
            chunk_id=h["chunk_id"], document_id=h["document_id"], kb_id=h["kb_id"],
            filename=h["filename"], page_no=h["page_no"], content=h["content"],
            score=h["score"], source=h["source"],
        )
        for h in hits
    ]
    return {"answer": resp.content, "citations": build_citations(shits)}


REWRITE_SYSTEM = (
    "你是检索查询改写器。根据对话历史把用户最新问题改写成独立、无指代的检索查询,"
    "直接输出改写后的查询本身,不要任何解释或前后缀。无法改写时原样输出问题。"
)

GRADE_SYSTEM = (
    "你是检索质量评审。根据问题判断参考资料是否足以回答。"
    '只输出 JSON:{"verdict":"sufficient 或 insufficient",'
    '"query":"当 insufficient 时,给出一个更利于检索的改写查询"}'
)


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


async def rewrite_node(state: dict, llm) -> dict:
    question = state["question"]
    # 每轮起点整体复位(checkpointer 状态按 thread 持久,不清则子查询跨轮泄漏)
    reset = {
        "search_query": question,
        "retries": 0,
        "grade": "",
        "hopped": False,
        "sub_queries": [],
        "proposed_query": "",
    }
    if not settings.AGENTIC_REWRITE_ENABLED:
        return reset
    history = state.get("history") or []
    if not history:
        return reset
    try:
        msgs = [("system", REWRITE_SYSTEM)]
        for m in history:
            msgs.append((m["role"], m["content"]))
        msgs.append(("user", f"最新问题:{question}"))
        resp = await llm.ainvoke(msgs)
        rewritten = (resp.content or "").strip()
        if rewritten:
            reset["search_query"] = rewritten
    except Exception:
        logger.exception("query rewrite failed; fallback to raw question")
    return reset


async def grade_node(state: dict, llm) -> dict:
    if not settings.AGENTIC_CRAG_ENABLED:
        return {}
    hits = state.get("hits") or []
    if not hits:
        return {}
    context = "\n".join(
        f"[{i+1}] {h['filename']} 第{h['page_no'] or '?'}页:{h['content'][:120]}"
        for i, h in enumerate(hits[: settings.RETRIEVAL_TOP_K])
    )
    try:
        resp = await llm.ainvoke(
            [
                ("system", GRADE_SYSTEM),
                ("user", f"问题:{state['question']}\n参考资料:\n{context}"),
            ]
        )
        parsed = json.loads(_extract_json(resp.content))
        verdict = parsed.get("verdict")
        if verdict not in ("sufficient", "insufficient"):
            return {"grade": "sufficient"}
        out = {"grade": verdict}
        if verdict == "insufficient" and parsed.get("query"):
            out["proposed_query"] = str(parsed["query"])
        return out
    except Exception:
        logger.exception("grade failed; degrade to sufficient")
        return {"grade": "sufficient"}


async def transform_node(state: dict) -> dict:
    return {
        "search_query": state.get("proposed_query") or state["question"],
        "retries": state.get("retries", 0) + 1,
    }


DECOMPOSE_SYSTEM = (
    "你是问题分解器。把复合问题拆成2~3个各自独立、无指代、可直接用于检索的子问题;"
    '只输出 JSON 字符串数组,如 ["子问题1","子问题2"]。'
    "问题本身简单时,输出只含该问题的单元素数组。"
)


async def decompose_node(state: dict, llm) -> dict:
    base = state.get("search_query") or state["question"]
    user = f"问题:{base}"
    hint = state.get("proposed_query")
    if hint:
        user += f"\n(检索改写提示:{hint})"
    try:
        resp = await llm.ainvoke([("system", DECOMPOSE_SYSTEM), ("user", user)])
        parsed = json.loads(_extract_json(resp.content))
        subs = (
            [str(q).strip() for q in parsed if isinstance(q, str) and str(q).strip()]
            if isinstance(parsed, list) else []
        )
        subs = list(dict.fromkeys(subs))[: settings.MULTI_HOP_MAX_SUBQ]
        if subs:
            return {"sub_queries": subs, "hopped": True}
    except Exception:
        logger.exception("decompose failed; fallback to single query")
    return {"sub_queries": [hint or base], "hopped": True}
