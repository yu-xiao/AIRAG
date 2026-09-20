"""M13:评估结果落库(两 CLI 共用)与汇总口径。"""
from app.db.session import SessionLocal
from app.models import EvalItem, EvalRun


def _avg(vals: list[float]):
    return round(sum(vals) / len(vals), 4) if vals else None


def summarize(results: list[dict]) -> dict:
    """retrieval/generation 通用汇总:各自字段缺席则跳过。"""
    s: dict = {"item_count": len(results)}
    hits = [r["hit_at_k"] for r in results if "hit_at_k" in r]
    if hits:
        s["hit"] = _avg([float(h) for h in hits])
        s["mrr"] = _avg([r["mrr"] for r in results if "mrr" in r])
        s["keyword_recall"] = _avg(
            [r["keyword_recall"] for r in results if "keyword_recall" in r])
    faith = [r["faithfulness"]["score"] for r in results
             if r.get("faithfulness", {}).get("score") is not None]
    if faith or any("faithfulness" in r for r in results):
        s["faithfulness_avg"] = _avg(faith)
        s["relevancy_avg"] = _avg(
            [r["relevancy"]["score"] for r in results
             if r.get("relevancy", {}).get("score") is not None])
        s["refused_count"] = sum(1 for r in results if r.get("refused"))
    refs = [r["reference"]["score"] for r in results
            if r.get("reference", {}).get("score") is not None]
    if refs or any("reference" in r for r in results):
        s["reference_avg"] = _avg(refs)
    return s


async def save_run(kb_id: int, mode: str, results: list[dict]) -> int:
    """落 EvalRun + 逐题 EvalItem;返回 run_id(自开 session 自 commit)。"""
    async with SessionLocal() as db:
        run = EvalRun(kb_id=kb_id, mode=mode, summary=summarize(results),
                      item_count=len(results))
        db.add(run)
        await db.flush()
        for r in results:
            hk = r.get("hit_at_k")
            if isinstance(hk, bool):
                hk = 1.0 if hk else 0.0
            db.add(EvalItem(
                run_id=run.id, question=r["question"],
                expect_doc_ids=r.get("expect_doc_ids"),
                expect_keywords=r.get("expect_keywords"),
                answer=r.get("answer"), refused=r.get("refused"),
                hit_at_k=hk,
                mrr=r.get("mrr"), keyword_recall=r.get("keyword_recall"),
                faithfulness=(r.get("faithfulness") or {}).get("score"),
                relevancy=(r.get("relevancy") or {}).get("score"),
                reference_score=(r.get("reference") or {}).get("score"),
            ))
        await db.commit()
        return run.id
