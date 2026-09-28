"""M15:评估执行 Celery 任务(Web 触发)。"""
from datetime import datetime, timedelta, timezone

from celery.signals import worker_ready
from loguru import logger

from app.services.outbound import emit_event, nudge
from app.workers.celery_app import celery_app
from app.workers.pipeline import _engine, _run_async

ORPHAN_ERROR = "orphaned: heartbeat expired (worker died or restarted)"


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
    """在途(status='running' 或 'cancelling',M19 T3)且两段租约任一
    过期的 EvalRun 收口为 failed,返回受影响行数。

    M22 两段判据:已开跑(heartbeat 非 NULL)按心跳宽限
    (EVAL_HEARTBEAT_GRACE_MINUTES);从未开跑(heartbeat NULL,含排队
    中)按创建龄(EVAL_QUEUE_GRACE_MINUTES)兜底——排队宽限取代 M21 的
    「创建即心跳」。时钟域铁律:heartbeat_at 是 naive TIMESTAMP,只与
    naive UTC(utcnow_naive)比较;created_at 是 timestamptz,只与 aware
    UTC(datetime.now(timezone.utc))比较——两列各域,绝不混用。任务
    逐题续签 heartbeat_at。除 worker_ready 外,beat 60s 周期兜底:
    worker 崩溃后孤儿不再「只能等下次重启」,≤ 宽限+间隔内必被收口
    (强于 M15 现状)。DB 访问同 pipeline._mark_failed 模式:自持
    NullPool 引擎,用完 dispose(不与 API/worker 常驻引擎共享连接池)。"""
    from sqlalchemy import and_, or_, update
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.core.config import settings
    from app.core.timeutil import utcnow_naive
    from app.models import EvalRun

    engine = _engine(settings.DATABASE_URL)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            hb_cutoff = utcnow_naive() - timedelta(
                minutes=settings.EVAL_HEARTBEAT_GRACE_MINUTES)
            # created_at 是 timestamptz:比较参数必须 aware UTC(时钟域铁律)
            q_cutoff = datetime.now(timezone.utc) - timedelta(
                minutes=settings.EVAL_QUEUE_GRACE_MINUTES)
            result = await session.execute(
                update(EvalRun)
                .where(EvalRun.status.in_(("running", "cancelling")),
                       or_(
                           and_(EvalRun.heartbeat_at.is_not(None),
                                EvalRun.heartbeat_at <= hb_cutoff),
                           and_(EvalRun.heartbeat_at.is_(None),
                                EvalRun.created_at <= q_cutoff),
                       ))
                .values(status="failed", error=ORPHAN_ERROR)
                .returning(EvalRun.id, EvalRun.kb_id, EvalRun.mode,
                           EvalRun.item_count)
            )
            swept = result.all()
            n = 0
            for rid, kb_id, mode, item_count in swept:
                # 孤儿从未收口:summary 诚实 null,item_count 为创建时题数
                n += await emit_event(session, "eval.failed", {
                    "run": {"id": rid, "kb_id": kb_id, "mode": mode,
                            "item_count": item_count, "summary": None},
                    "error": ORPHAN_ERROR})
            await session.commit()
            if n:
                nudge()
            return len(swept)
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
