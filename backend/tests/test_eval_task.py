# backend/tests/test_eval_task.py
"""M15 T4:run_eval_task 状态机——成功/失败/空 run 三路。

任务自持 NullPool 引擎连 settings.DATABASE_URL(conftest 已指向测试库),
不走 db_session fixture;这里用 db_session 造数/断言(同一物理库)。
"""
from app.models import EvalQuestion, EvalRun


async def _mk_running_run(client, auth_headers, db_session, n=2) -> int:
    kb_id = (await client.post(
        "/api/kbs", json={"name": "task库"}, headers=auth_headers)).json()["id"]
    db_session.add_all([EvalQuestion(kb_id=kb_id, question=f"q{i}")
                         for i in range(n)])
    run = EvalRun(kb_id=kb_id, mode="retrieval", summary=None,
                  item_count=n, status="running", triggered_by=1)
    db_session.add(run)
    await db_session.commit()
    return run.id


async def test_run_eval_task_completes(client, auth_headers, db_session):
    from sqlalchemy import select

    from app.services.eval_runner import run_eval_task
    run_id = await _mk_running_run(client, auth_headers, db_session)
    await run_eval_task(run_id, "retrieval", False, 8)
    db_session.expire_all()
    run = (await db_session.execute(
        select(EvalRun).where(EvalRun.id == run_id))).scalar_one()
    assert run.status == "completed"
    assert run.summary["item_count"] == 2
    assert "hit" in run.summary  # 空 KB 检索:hit=0.0 但键在
    assert len(run.items) == 2


async def test_run_eval_task_failure_marks_failed(client, auth_headers,
                                                  db_session, monkeypatch):
    from sqlalchemy import select

    import app.services.eval_runner as runner
    from app.services.eval_runner import run_eval_task

    async def boom(db, kb_id, q, top_k, reranker):
        raise RuntimeError("检索炸了")

    run_id = await _mk_running_run(client, auth_headers, db_session)
    monkeypatch.setattr(runner, "retrieval_item", boom)
    await run_eval_task(run_id, "retrieval", False, 8)
    db_session.expire_all()
    run = (await db_session.execute(
        select(EvalRun).where(EvalRun.id == run_id))).scalar_one()
    assert run.status == "failed"
    assert "检索炸了" in run.error


async def test_generation_without_key_fails(client, auth_headers, db_session,
                                            monkeypatch):
    from sqlalchemy import select, text

    from app.core.config import settings
    from app.services.eval_runner import run_eval_task

    monkeypatch.setattr(settings, "ZHIPU_API_KEY", "")
    run_id = await _mk_running_run(client, auth_headers, db_session, n=1)
    await db_session.execute(
        text("UPDATE eval_runs SET mode='generation' WHERE id=:i"),
        {"i": run_id})
    await db_session.commit()
    await run_eval_task(run_id, "generation", False, 8)
    db_session.expire_all()
    run = (await db_session.execute(
        select(EvalRun).where(EvalRun.id == run_id))).scalar_one()
    assert run.status == "failed" and "ZHIPU" in run.error
