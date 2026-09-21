"""M15:评估执行 Celery 任务(Web 触发)。"""
from app.workers.celery_app import celery_app
from app.workers.pipeline import _run_async


@celery_app.task(name="app.workers.eval_tasks.run_evaluation")
def run_evaluation(run_id: int, mode: str, rerank: bool, top_k: int) -> None:
    """入口:eager(测试)落在 API 事件循环线程时切一次性线程另起循环。"""
    from app.services.eval_runner import run_eval_task

    _run_async(run_eval_task(run_id, mode, rerank, top_k))
