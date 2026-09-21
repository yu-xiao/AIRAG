"""检索评估 CLI。

用法(backend 目录下):
    py -m scripts.eval_retrieval --kb 3 [--top-k 8] [--rerank] [--json]

题源 eval_questions 表,经评估页题集管理维护。
指标:hit@k / MRR / 关键词 recall(纯检索,无 LLM)。
"""
import argparse
import asyncio
import json
import sys


async def run(kb_id: int, top_k: int, use_rerank: bool) -> list[dict]:
    from app.db.session import SessionLocal
    from app.models import KnowledgeBase
    from app.services.eval_runner import load_questions, retrieval_item
    from app.services.rerank.base import get_reranker

    async with SessionLocal() as db:
        kb = await db.get(KnowledgeBase, kb_id)
        if kb is None:
            sys.exit(f"knowledge base {kb_id} not found")
        questions = await load_questions(db, kb_id)
        if not questions:
            sys.exit(f"no questions for kb {kb_id}(在评估页「题集管理」添加)")
        reranker = get_reranker() if use_rerank else None
        return [await retrieval_item(db, kb_id, q, top_k, reranker)
                for q in questions]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", type=int, required=True)
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--rerank", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()

    # M13 T10 快修:run 与 save_run 必须同一次 asyncio.run——两次 run 会
    # 复用 SessionLocal 引擎的池化连接(绑定已关闭的首个事件循环),
    # 第二次 checkout pre-ping 即崩(Event loop is closed / proactor None)
    async def _amain() -> list[dict]:
        results = await run(args.kb, args.top_k, args.rerank)
        if args.save:
            from scripts.eval_store import save_run

            run_id = await save_run(args.kb, "retrieval", results)
            print(f"saved: run_id={run_id}", file=sys.stderr)
        return results

    results = asyncio.run(_amain())
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    print(f"{'问题':<28} hit@{args.top_k}  MRR    关键词recall")
    for r in results:
        print(
            f"{r['question'][:26]:<28} {str(r['hit_at_k']):<7} "
            f"{r['mrr']:<6.3f} {r['keyword_recall']}"
        )
    n = len(results)
    print(
        f"\n汇总:n={n}  hit={sum(r['hit_at_k'] for r in results) / n:.2f}  "
        f"MRR={sum(r['mrr'] for r in results) / n:.3f}  "
        f"recall={sum(r['keyword_recall'] for r in results) / n:.3f}"
    )


if __name__ == "__main__":
    main()
