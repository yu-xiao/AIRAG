from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "airag",
    broker=settings.REDIS_URL,
    include=["app.workers.pipeline", "app.workers.maintenance",
             "app.workers.eval_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    beat_schedule={
        "purge-expired-audit-logs": {
            "task": "app.workers.maintenance.purge_expired_audit_logs",
            "schedule": crontab(hour=3, minute=0),
        }
    },
)
