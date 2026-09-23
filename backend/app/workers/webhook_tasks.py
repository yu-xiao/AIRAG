"""M17:webhook 投递任务。自持 NullPool 引擎(worker 每任务新事件循环,
池化连接不得跨循环复用——eval_tasks 同款);异常吞掉——beat 下一轮
再扫,单批失败不得炸掉 worker/beat。"""
from loguru import logger
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.services.outbound import deliver_due
from app.workers.celery_app import celery_app
from app.workers.pipeline import _engine, _run_async


@celery_app.task(name="app.workers.webhook_tasks.deliver_pending",
                 ignore_result=True)
def deliver_pending():
    async def _amain() -> int:
        engine = _engine(settings.DATABASE_URL)
        try:
            async with async_sessionmaker(engine,
                                          expire_on_commit=False)() as db:
                return await deliver_due(db)
        finally:
            await engine.dispose()

    try:
        return _run_async(_amain())
    except Exception:
        logger.exception("webhook deliver_pending failed; beat will retry")
        return 0
