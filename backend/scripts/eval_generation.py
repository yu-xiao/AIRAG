"""生成质量评估 CLI(LLM-judge)。

用法(backend 目录下):
    .venv\\Scripts\\python -m scripts.eval_generation --kb 3 [--rerank] [--json]

题源 eval_questions 表,经评估页题集管理维护(expect_* 字段透传不使用);
对每题跑完整问答图,LLM 评 faithfulness(忠实度)与 relevancy(切题度)。
"""
import argparse
import asyncio
import json
import sys


def _fmt(score):
    return f"{score:.2f}" if score is not None else "N/A"


async def run(kb_id: int, use_rerank: bool) -> list[dict]:
    from app.core.config import settings
    from app.db.session import SessionLocal
    from app.models import KnowledgeBase
    from app.services.chat_graph.graph import build_graph, make_chat_llm
    from app.services.eval_runner import generation_item, load_questions

    if not settings.ZHIPU_API_KEY:
        sys.exit("ZHIPU_API_KEY 未配置:桩答案的 LLM-judge 评估无意义,拒绝运行")
    async with SessionLocal() as db:
        kb = await db.get(KnowledgeBase, kb_id)
        if kb is None:
            sys.exit(f"knowledge base {kb_id} not found")
        questions = await load_questions(db, kb_id)
        if not questions:
            sys.exit(f"no questions for kb {kb_id}(在评估页「题集管理」添加)")
    llm = make_chat_llm()  # 生成与评审共用同一实例(单例)
    graph = build_graph(llm=llm)
    return [await generation_item(kb_id, q, llm, graph, use_rerank)
            for q in questions]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", type=int, required=True)
    ap.add_argument("--rerank", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()

    # M13 T10 快修:与 eval_retrieval 同款——run 与 save_run 同一次
    # asyncio.run,避免池化连接绑死首个(已关闭的)事件循环
    async def _amain() -> list[dict]:
        results = await run(args.kb, args.rerank)
        if args.save:
            from scripts.eval_store import save_run

            run_id = await save_run(args.kb, "generation", results)
            print(f"saved: run_id={run_id}", file=sys.stderr)
        return results

    results = asyncio.run(_amain())
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    print(f"{'问题':<28} 忠实度  切题度  引用数")
    for r in results:
        print(f"{r['question'][:26]:<28} "
              f"{_fmt(r['faithfulness']['score']):<7} "
              f"{_fmt(r['relevancy']['score']):<7} {r['citations']}")
    n = len(results)
    parse_errors = sum(
        1 for r in results
        if r["faithfulness"]["score"] is None or r["relevancy"]["score"] is None
    )
    refused_n = sum(1 for r in results if r["refused"])
    for key, label in (("faithfulness", "忠实度"), ("relevancy", "切题度")):
        vals = [r[key]["score"] for r in results if r[key]["score"] is not None]
        avg = f"{sum(vals) / len(vals):.2f}" if vals else "N/A"
        print(f"\n汇总:n={n}  {label}={avg}", end="")
    print(f"  (parse_errors={parse_errors}  refused={refused_n}/{n})")


if __name__ == "__main__":
    main()
