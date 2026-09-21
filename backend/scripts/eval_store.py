"""评估结果落库(CLI --save 路径);汇总与字段映射在 app.services.eval_runner。"""
from app.db.session import SessionLocal
from app.models import EvalItem, EvalRun
from app.services.eval_runner import item_kwargs, summarize  # noqa: F401 — re-export 保旧 import


async def save_run(kb_id: int, mode: str, results: list[dict]) -> int:
    """落 EvalRun + 逐题 EvalItem;返回 run_id(自开 session 自 commit)。"""
    async with SessionLocal() as db:
        run = EvalRun(kb_id=kb_id, mode=mode, status="completed",
                      summary=summarize(results), item_count=len(results))
        db.add(run)
        await db.flush()
        for r in results:
            db.add(EvalItem(run_id=run.id, **item_kwargs(r)))
        await db.commit()
        return run.id
