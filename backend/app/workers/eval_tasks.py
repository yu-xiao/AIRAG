"""M15:评估执行 Celery 任务(Web 触发)。"""
from datetime import timedelta

from celery.signals import worker_ready
from loguru import logger

from app.workers.celery_app import celery_app
from app.workers.pipeline import _engine, _run_async


@celery_app.task(name="app.workers.eval_tasks.run_evaluation")
def run_evaluation(run_id: int, mode: str, rerank: bool, top_k: int) -> None:
    """入口:eager(测试)落在 API 事件循环线程时切一次性线程另起循环。"""
    from app.services.eval_runner import run_eval_task

    _run_async(run_eval_task(run_id, mode, rerank, top_k))
    # 图内节点(retrieve 等)经全局 SessionLocal 池化引擎取连接,绑定本任务
    # 的事件循环;worker 每任务一个新循环,任务结束必须弃置全局池——否则
    # 下一任务 checkout 到绑定已关闭循环的连接,pre-ping 打到死 proactor
    # (NoneType.send)。dispose 只关池内连接,后续 checkout 自动重建。
    _run_async(_dispose_shared_engine())


async def _dispose_shared_engine() -> None:
    from app.db.session import engine

    await engine.dispose()


async def _sweep_orphan_runs() -> int:
    """在途(status='running' 或 'cancelling',M19 T3)且心跳过期(M21
    租约)的 EvalRun 收口为 failed,返回受影响行数。

    M21 前:solo 池 worker_ready 瞬间无在途任务,无条件收口成立;多
    worker 前必须能区分「活任务」与「孤儿」——任务逐题续签 heartbeat_at,
    本函数只收 stale(NULL 或早于宽限;NULL 兼容存量行与「已建未开跑」
    孤儿)。除 worker_ready 外,beat 60s 周期兜底:worker 崩溃后孤儿
    不再「只能等下次重启」,≤ 宽限+间隔内必被收口(强于 M15 现状)。
    DB 访问同 pipeline._mark_failed 模式:自持 NullPool 引擎,用完
    dispose(不与 API/worker 常驻引擎共享连接池)。"""
    from sqlalchemy import or_, update
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.core.config import settings
    from app.core.timeutil import utcnow_naive
    from app.models import EvalRun

    engine = _engine(settings.DATABASE_URL)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            cutoff = utcnow_naive() - timedelta(
                minutes=settings.EVAL_HEARTBEAT_GRACE_MINUTES)
            result = await session.execute(
                update(EvalRun)
                .where(EvalRun.status.in_(("running", "cancelling")),
                       or_(EvalRun.heartbeat_at.is_(None),
                           EvalRun.heartbeat_at <= cutoff))
                .values(
                    status="failed",
                    error="orphaned: heartbeat expired (worker died or restarted)",
                )
            )
            await session.commit()
            return result.rowcount
    finally:
        await engine.dispose()


def _recover_orphan_runs() -> int:
    """信号处理器本体;独立成可直调函数供测试调用(信号在 pytest 不触发)。"""
    n = _run_async(_sweep_orphan_runs())
    if n:
        logger.info(f"recovered {n} orphaned eval run(s) (stale heartbeat)")
    return n


@celery_app.task(name="app.workers.eval_tasks.sweep_orphan_runs",
                 ignore_result=True)
def sweep_orphan_runs() -> int:
    """beat 60s 周期兜底(worker_ready 之外的第二个触发面)。"""
    return _recover_orphan_runs()


@worker_ready.connect
def _on_worker_ready(sender=None, **kwargs):
    _recover_orphan_runs()
