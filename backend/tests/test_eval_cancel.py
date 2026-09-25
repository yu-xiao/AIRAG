# backend/tests/test_eval_cancel.py
"""M19 T3:协作式取消——端点状态机/循环检查点/sweep 扩容/防重兼容。

状态机:running→cancelling(端点)→cancelled(任务逐题 commit 后检查点
收口,保留已完成子集的 summary/item_count);409 防重仍仅查 running。
"""
import time

from sqlalchemy import select, text, update

from app.models import AuditLog, EvalItem, EvalQuestion, EvalRun


async def _register_and_login(client, username):
    await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"})
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _mk_run(client, auth_headers, db_session, status="running",
                  n=1) -> int:
    """照 test_eval_task._mk_running_run 模式:API 建 KB + 直插题集与 run。"""
    kb_id = (await client.post(
        "/api/kbs", json={"name": f"cancel库{time.time_ns()}"},
        headers=auth_headers)).json()["id"]
    db_session.add_all([EvalQuestion(kb_id=kb_id, question=f"q{i}")
                        for i in range(n)])
    run = EvalRun(kb_id=kb_id, mode="retrieval", summary=None,
                  item_count=n, status=status, triggered_by=1)
    db_session.add(run)
    await db_session.commit()
    return run.id


async def test_cancel_running_then_idempotent(client, auth_headers,
                                              db_session):
    run_id = await _mk_run(client, auth_headers, db_session)
    r = await client.post(f"/api/eval/runs/{run_id}/cancel",
                          headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"id": run_id, "status": "cancelling"}
    r2 = await client.post(f"/api/eval/runs/{run_id}/cancel",
                           headers=auth_headers)
    assert r2.status_code == 200  # 幂等:cancelling 再调仍 200
    assert r2.json() == {"id": run_id, "status": "cancelling"}


async def test_cancel_terminal_conflicts(client, auth_headers, db_session):
    for st in ("completed", "failed", "cancelled"):
        run_id = await _mk_run(client, auth_headers, db_session, status=st)
        r = await client.post(f"/api/eval/runs/{run_id}/cancel",
                              headers=auth_headers)
        assert r.status_code == 409, st


async def test_cancel_permissions(client, auth_headers, db_session):
    """局外人(不可见库)404;viewer 403;不存在 run 404。"""
    run_id = await _mk_run(client, auth_headers, db_session)
    stranger = await _register_and_login(client, "m19_c_stranger")
    r = await client.post(f"/api/eval/runs/{run_id}/cancel",
                          headers=stranger)
    assert r.status_code == 404  # 无 perm → 404(不区分 403,防探测)

    sid = (await client.get("/api/auth/me", headers=stranger)).json()["id"]
    run = await db_session.get(EvalRun, run_id)
    await db_session.execute(text(
        "INSERT INTO kb_permissions (kb_id, user_id, perm) "
        "VALUES (:k, :u, 'viewer')"), {"k": run.kb_id, "u": sid})
    await db_session.commit()
    r = await client.post(f"/api/eval/runs/{run_id}/cancel",
                          headers=stranger)
    assert r.status_code == 403  # 可见非 owner

    r = await client.post("/api/eval/runs/999999/cancel",
                          headers=auth_headers)
    assert r.status_code == 404


async def test_cancel_audited_once(client, auth_headers, db_session):
    """首次取消落 eval_cancel 审计;幂等重调不重复落。"""
    run_id = await _mk_run(client, auth_headers, db_session)
    await client.post(f"/api/eval/runs/{run_id}/cancel",
                      headers=auth_headers)
    await client.post(f"/api/eval/runs/{run_id}/cancel",
                      headers=auth_headers)
    rows = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "eval_cancel")
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].target == f"eval_run:{run_id}"


def _fake_retrieval_item(run_id, flip_at):
    """返回 fake retrieval_item:第 flip_at 次调用时经任务会话把 run 置
    cancelling 并 commit(模拟并发取消;同会话 update 后 loop 的 refresh 能见)。"""
    calls = {"n": 0}

    async def fake(db, kb_id, q, top_k, reranker):
        calls["n"] += 1
        if calls["n"] == flip_at:
            await db.execute(update(EvalRun).where(EvalRun.id == run_id)
                             .values(status="cancelling"))
            await db.commit()
        return {"question": q.question, "hit_at_k": None, "mrr": None,
                "keyword_recall": None}

    return fake, calls


async def test_run_task_stops_on_cancelling(client, auth_headers,
                                            db_session, monkeypatch):
    """第 1 题提交后置 cancelling → 任务收口 cancelled + 子集数据。"""
    import app.services.eval_runner as runner
    from app.services.eval_runner import run_eval_task

    run_id = await _mk_run(client, auth_headers, db_session, n=3)
    fake, calls = _fake_retrieval_item(run_id, flip_at=1)
    monkeypatch.setattr(runner, "retrieval_item", fake)
    await run_eval_task(run_id, "retrieval", False, 8)
    db_session.expire_all()
    run = (await db_session.execute(
        select(EvalRun).where(EvalRun.id == run_id))).scalar_one()
    assert run.status == "cancelled"
    assert run.item_count == 1 and run.summary["item_count"] == 1
    items = (await db_session.execute(
        select(EvalItem).where(EvalItem.run_id == run_id))).scalars().all()
    assert len(items) == 1
    assert calls["n"] == 1  # 第 1 题检查点即退出,未跑 q2/q3


async def test_run_task_cancel_at_later_question(client, auth_headers,
                                                 db_session, monkeypatch):
    """第 2 题提交后才取消 → 已提交的 2 题保留(逐题检查点,非首题短路)。"""
    import app.services.eval_runner as runner
    from app.services.eval_runner import run_eval_task

    run_id = await _mk_run(client, auth_headers, db_session, n=3)
    fake, calls = _fake_retrieval_item(run_id, flip_at=2)
    monkeypatch.setattr(runner, "retrieval_item", fake)
    await run_eval_task(run_id, "retrieval", False, 8)
    db_session.expire_all()
    run = (await db_session.execute(
        select(EvalRun).where(EvalRun.id == run_id))).scalar_one()
    assert run.status == "cancelled"
    assert run.item_count == 2 and run.summary["item_count"] == 2
    items = (await db_session.execute(
        select(EvalItem).where(EvalItem.run_id == run_id))).scalars().all()
    assert len(items) == 2
    assert calls["n"] == 2


async def test_sweep_collects_cancelling(client, auth_headers, db_session):
    """worker 重启清扫:running 与 cancelling 两类在途都收口 failed。"""
    from app.workers.eval_tasks import _recover_orphan_runs

    db_session.add_all([
        EvalRun(kb_id=1, mode="retrieval", summary=None,
                item_count=1, status="cancelling"),
        EvalRun(kb_id=2, mode="retrieval", summary=None,
                item_count=1, status="running"),
        EvalRun(kb_id=3, mode="retrieval", summary={"hit": 1.0},
                item_count=1, status="completed"),
    ])
    await db_session.commit()

    _recover_orphan_runs()  # 直调处理器本体(信号在 pytest 不触发)

    db_session.expire_all()
    runs = (await db_session.execute(
        select(EvalRun).order_by(EvalRun.id))).scalars().all()
    assert [r.status for r in runs] == ["failed", "failed", "completed"]
    assert runs[0].error == "worker restarted while evaluation was running"
    assert runs[2].error is None


async def test_dup_guard_ignores_cancelling(client, auth_headers,
                                            db_session):
    """409 防重仅查 running:cancelling 残留不拦同 kb+mode 的新触发。"""
    run_id = await _mk_run(client, auth_headers, db_session,
                           status="cancelling", n=1)
    kb_id = (await db_session.get(EvalRun, run_id)).kb_id
    r = await client.post("/api/eval/runs",
                          json={"kb_id": kb_id, "mode": "retrieval"},
                          headers=auth_headers)
    assert r.status_code == 201  # 不 409;eager 下新 run 正常内联跑完
    new_run = (await db_session.execute(
        select(EvalRun).where(EvalRun.kb_id == kb_id,
                              EvalRun.status == "completed"))).scalar_one()
    assert new_run.id != run_id
