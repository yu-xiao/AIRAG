"""M15:评估执行 Celery 任务(Web 触发)。"""
from celery.signals import worker_ready
from loguru import logger

from app.workers.celery_app import celery_app
from app.workers.pipeline import _engine, _run_async


@celery_app.task(name="app.workers.eval_tasks.run_evaluation")
def run_evaluation(run_id: int, mode: str, rerank: bool, top_k: int) -> None:
    """入口:eager(测试)落在 API 事件循环线程时切一次性线程另起循环。"""
    from app.services.eval_runner import run_eval_task

    _run_async(run_eval_task(run_id, mode, rerank, top_k))


async def _sweep_orphan_runs() -> int:
    """所有 status='running' 的 EvalRun 收口为 failed,返回受影响行数。

    触发时机是 worker_ready:solo 池单 worker,启动瞬间不可能有执行中的
    评估任务,残留 running 必是孤儿(worker 被杀/重启、Redis 断线丢
    .delay() 消息),不收口则同 kb+mode 永久 409、前端 hasRunning()
    3s 轮询永不停。DB 访问同 pipeline._mark_failed 模式:自持 NullPool
    引擎,用完 dispose(不与 API/worker 常驻引擎共享连接池)。"""
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.core.config import settings
    from app.models import EvalRun

    engine = _engine(settings.DATABASE_URL)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            result = await session.execute(
                update(EvalRun)
                .where(EvalRun.status == "running")
                .values(
                    status="failed",
                    error="worker restarted while evaluation was running",
                )
            )
            await session.commit()
            return result.rowcount
    finally:
        await engine.dispose()


def _recover_orphan_runs() -> None:
    """信号处理器本体;独立成可直调函数供测试调用(信号在 pytest 不触发)。"""
    n = _run_async(_sweep_orphan_runs())
    if n:
        logger.info(f"recovered {n} orphaned running eval run(s) on worker start")


@worker_ready.connect
def _on_worker_ready(sender=None, **kwargs):
    _recover_orphan_runs()
