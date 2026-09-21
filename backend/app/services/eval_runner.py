"""M15:评估执行核心(CLI 与 Celery 任务共用);题源 eval_questions 表。

指标函数自 scripts/eval_metrics 迁入(文件删除);汇总/字段映射自
scripts/eval_store 迁入(那边 re-export 保 CLI 兼容)。searcher/judge
在模块顶导入(测试 monkeypatch 面);rerank/chat_graph 较重且仅按
mode 需要,函数内延迟导入。
"""
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EvalQuestion
from app.services.eval_judge import (
    faithfulness_score,
    reference_score,
    relevancy_score,
)
from app.services.retrieval.searcher import hybrid_search


def hit_at_k(retrieved_doc_ids: list[int], expect_doc_ids: list[int]) -> bool:
    top = set(retrieved_doc_ids)
    return any(d in top for d in expect_doc_ids)


def mrr(retrieved_doc_ids: list[int], expect_doc_ids: list[int]) -> float:
    exp = set(expect_doc_ids)
    for rank, doc_id in enumerate(retrieved_doc_ids, start=1):
        if doc_id in exp:
            return 1.0 / rank
    return 0.0


def keyword_recall(hit_contents: list[str], expect_keywords: list[str]) -> float:
    if not expect_keywords:
        return 1.0
    blob = "\n".join(hit_contents)
    found = [k for k in expect_keywords if k in blob]
    return round(len(found) / len(expect_keywords), 4)


async def load_questions(db: AsyncSession, kb_id: int) -> list[EvalQuestion]:
    return (await db.execute(
        select(EvalQuestion).where(EvalQuestion.kb_id == kb_id)
        .order_by(EvalQuestion.id)
    )).scalars().all()


async def retrieval_item(db: AsyncSession, kb_id: int, q: EvalQuestion,
                         top_k: int, reranker) -> dict:
    hits = await hybrid_search(db, [kb_id], q.question, top_k)
    if reranker and hits:
        order = reranker.rerank(q.question, [h.content for h in hits], top_k)
        hits = [hits[i] for i in order if 0 <= i < len(hits)]
    doc_ids = [h.document_id for h in hits]
    return {
        "question": q.question,
        "expect_doc_ids": q.expect_doc_ids,
        "expect_keywords": q.expect_keywords,
        "hit_at_k": hit_at_k(doc_ids, q.expect_doc_ids or []),
        "mrr": mrr(doc_ids, q.expect_doc_ids or []),
        "keyword_recall": keyword_recall(
            [h.content for h in hits], q.expect_keywords or []),
    }


async def generation_item(kb_id: int, q: EvalQuestion, llm, graph,
                          use_rerank: bool) -> dict:
    from app.core.config import settings

    final = await graph.ainvoke(
        {"question": q.question, "kb_ids": [kb_id], "rerank": use_rerank,
         "history": []})
    answer = final.get("answer") or ""
    contexts = [h["content"]
                for h in (final.get("hits") or [])[: settings.RETRIEVAL_TOP_K]]
    return {
        "question": q.question,
        "answer": answer,
        "reference": (await reference_score(llm, q.question, answer,
                                            q.reference_answer)
                      if q.reference_answer else None),
        "faithfulness": await faithfulness_score(
            llm, q.question, answer, contexts),
        "relevancy": await relevancy_score(llm, q.question, answer),
        "citations": len(final.get("citations") or []),
        "refused": bool(final.get("refused")),
    }


def _avg(vals: list[float]):
    return round(sum(vals) / len(vals), 4) if vals else None


def summarize(results: list[dict]) -> dict:
    """retrieval/generation 通用汇总:各自字段缺席则跳过(自 eval_store 迁入)。"""
    s: dict = {"item_count": len(results)}
    hits = [r["hit_at_k"] for r in results if "hit_at_k" in r]
    if hits:
        s["hit"] = _avg([float(h) for h in hits])
        s["mrr"] = _avg([r["mrr"] for r in results if "mrr" in r])
        s["keyword_recall"] = _avg(
            [r["keyword_recall"] for r in results if "keyword_recall" in r])
    faith = [v for v in ((r.get("faithfulness") or {}).get("score")
                         for r in results) if v is not None]
    if faith or any("faithfulness" in r for r in results):
        s["faithfulness_avg"] = _avg(faith)
        rel = [v for v in ((r.get("relevancy") or {}).get("score")
                           for r in results) if v is not None]
        s["relevancy_avg"] = _avg(rel)
        s["refused_count"] = sum(1 for r in results if r.get("refused"))
    refs = [v for v in ((r.get("reference") or {}).get("score")
                        for r in results) if v is not None]
    if refs or any("reference" in r for r in results):
        s["reference_avg"] = _avg(refs)
    return s


def item_kwargs(r: dict) -> dict:
    """EvalItem 构造字段映射(自 eval_store 迁入;bool→1.0/0.0)。"""
    hk = r.get("hit_at_k")
    if isinstance(hk, bool):
        hk = 1.0 if hk else 0.0
    return dict(
        question=r["question"],
        expect_doc_ids=r.get("expect_doc_ids"),
        expect_keywords=r.get("expect_keywords"),
        answer=r.get("answer"), refused=r.get("refused"),
        hit_at_k=hk, mrr=r.get("mrr"), keyword_recall=r.get("keyword_recall"),
        faithfulness=(r.get("faithfulness") or {}).get("score"),
        relevancy=(r.get("relevancy") or {}).get("score"),
        reference_score=(r.get("reference") or {}).get("score"),
    )


def _fresh_chat_llm():
    """绕过 make_chat_llm 的 lru_cache 新建 ChatOpenAI(仅 worker 评估用)。

    worker 里 _run_async→asyncio.run 每个任务一个新事件循环;lru_cache 的
    进程级单例(内部 httpx/openai 异步客户端)绑定首个任务后即关闭的循环,
    同 worker 第二个 generation 任务复用会在 llm.ainvoke 抛
    'NoneType' object has no attribute 'send'。__wrapped__ 是 functools
    契约属性,直调被包裹函数即绕缓存;graph.py 的缓存本身不动——API 进程
    (单一常驻循环)依赖它省客户端开销。API/CLI 不经此路径,不受影响。"""
    from app.services.chat_graph.graph import make_chat_llm

    return make_chat_llm.__wrapped__()


async def run_eval_task(run_id: int, mode: str, rerank: bool,
                        top_k: int) -> None:
    """状态机:running→completed/failed;逐题插 EvalItem+commit(进度可见)。

    自持 NullPool 引擎(任务的事件循环与 API/CLI 不共享,池化连接
    不得跨循环复用——pipeline._run_async + _engine 同款防御)。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.core.config import settings
    from app.models import EvalItem, EvalRun
    from app.workers.pipeline import _engine

    engine = _engine(settings.DATABASE_URL)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            run = await db.get(EvalRun, run_id)
            if run is None:
                logger.info(f"eval run {run_id} gone, skip")
                return
            questions = await load_questions(db, run.kb_id)
            results: list[dict] = []
            try:
                if mode == "retrieval":
                    from app.services.rerank.base import get_reranker

                    reranker = get_reranker() if rerank else None
                    for q in questions:
                        r = await retrieval_item(db, run.kb_id, q, top_k,
                                                 reranker)
                        results.append(r)
                        db.add(EvalItem(run_id=run.id, **item_kwargs(r)))
                        await db.commit()
                else:
                    from app.core.config import settings as _s

                    if not _s.ZHIPU_API_KEY:
                        raise RuntimeError(
                            "ZHIPU_API_KEY 未配置,生成评估无法执行")
                    from app.services.chat_graph.graph import build_graph

                    llm = _fresh_chat_llm()
                    graph = build_graph(llm=llm)
                    for q in questions:
                        r = await generation_item(run.kb_id, q, llm, graph,
                                                  rerank)
                        results.append(r)
                        db.add(EvalItem(run_id=run.id, **item_kwargs(r)))
                        await db.commit()
                run.summary = summarize(results)
                run.item_count = len(results)
                run.status = "completed"
                await db.commit()
            except Exception as e:
                # 先 rollback 丢弃未提交脏状态:异常可能源自 DB 操作本身
                # (逐题 commit/flush 失败、连接中断),session 处于
                # PendingRollback 时直接 commit 会二次抛异常、逃出函数,
                # run 永远停在 running——状态机必须兜住。已逐题 commit 的
                # items 不受影响(它们已落库)。
                await db.rollback()
                run.status = "failed"
                run.error = str(e)[:500]
                await db.commit()
    finally:
        await engine.dispose()
