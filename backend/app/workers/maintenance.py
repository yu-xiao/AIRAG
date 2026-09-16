"""审计留存定时任务(beat 每日 03:00,见 celery_app.beat_schedule)。"""

from loguru import logger
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.services.audit import purge_expired
from app.workers.celery_app import celery_app
from app.workers.pipeline import _engine, _run_async


@celery_app.task(name="app.workers.maintenance.purge_expired_audit_logs")
def purge_expired_audit_logs():
    async def _inner():
        engine = _engine(settings.DATABASE_URL)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                deleted = await purge_expired(session)
                await session.commit()
                return deleted
        finally:
            await engine.dispose()

    deleted = _run_async(_inner())
    logger.info("audit retention purge deleted {} rows", deleted)
    return deleted
