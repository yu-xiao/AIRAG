"""M13:评估运行历史查询。

用法(backend 目录):py -m scripts.eval_runs --kb 3 [--mode retrieval]
                                     [--last 10] [--json]
"""
import argparse
import asyncio
import json


async def query(kb_id: int, mode: str | None, last: int) -> list[dict]:
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models import EvalRun

    async with SessionLocal() as db:
        stmt = select(EvalRun).where(EvalRun.kb_id == kb_id)
        if mode:
            stmt = stmt.where(EvalRun.mode == mode)
        stmt = stmt.order_by(EvalRun.id.desc()).limit(last)
        rows = (await db.execute(stmt)).scalars().all()
        return [
            {"id": r.id, "kb_id": r.kb_id, "mode": r.mode,
             "item_count": r.item_count, "summary": r.summary,
             "created_at": r.created_at.isoformat()}
            for r in rows
        ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", type=int, required=True)
    ap.add_argument("--mode", choices=["retrieval", "generation"])
    ap.add_argument("--last", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = asyncio.run(query(args.kb, args.mode, args.last))
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("无评估记录")
        return
    print(f"{'id':<6} {'模式':<12} {'题数':<5} 汇总(截选)                 时间")
    for r in rows:
        s = json.dumps(r["summary"], ensure_ascii=False)[:44]
        print(f"{r['id']:<6} {r['mode']:<12} {r['item_count']:<5} "
              f"{s:<44} {r['created_at'][:19]}")


if __name__ == "__main__":
    main()
