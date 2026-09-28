# backend/tests/test_eval_cancel.py
"""M19 T3:协作式取消——端点状态机/循环检查点/sweep 扩容/防重兼容。

状态机:running→cancelling(端点)→cancelled(任务逐题 commit 后检查点
收口,保留已完成子集的 summary/item_count);409 防重仍仅查 running。
"""
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text, update

from app.core.timeutil import utcnow_naive
from app.models import (AuditLog, EvalItem, EvalQuestion, EvalRun,
                        WebhookDelivery, WebhookEndpoint)
import app.services.eval_runner as runner
from app.services.eval_runner import _finalize_run, run_eval_task
from app.workers.eval_tasks import _recover_orphan_runs


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


def _flip_guard(monkeypatch, db_session, run_id, to_status):
    """守卫 seam:owner 校验内经测试会话把 run 翻成 to_status 并 commit,
    确定性复现「端点读 running 之后、条件 UPDATE 之前任务收口」窗口。
    注意先跑原校验再翻转:client 覆写 get_db 与测试同会话,expire_all
    之后原校验对 user/kb 的属性同步访问会 MissingGreenlet,先校验后
    expire 才既过权限又造出端点的过期读。"""
    import app.api.eval as ev

    orig = ev._require_kb_owner

    async def guard(db, current, kb_id):
        kb = await orig(db, current, kb_id)
        await db_session.execute(
            update(EvalRun).where(EvalRun.id == run_id)
            .values(status=to_status))
        await db_session.commit()
        db_session.expire_all()
        return kb

    monkeypatch.setattr(ev, "_require_kb_owner", guard)


async def test_cancel_race_run_completed_before_update(
        client, auth_headers, db_session, monkeypatch):
    """A2 竞态:条件 UPDATE 零行→重读 409;终态不被改写、无 audit。"""
    run_id = await _mk_run(client, auth_headers, db_session)
    _flip_guard(monkeypatch, db_session, run_id, "completed")
    r = await client.post(f"/api/eval/runs/{run_id}/cancel",
                          headers=auth_headers)
    assert r.status_code == 409
    db_session.expire_all()
    run = await db_session.get(EvalRun, run_id)
    assert run.status == "completed"  # 终态未被覆盖回 cancelling
    rows = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "eval_cancel")
    )).scalars().all()
    assert rows == []


async def test_cancel_race_run_cancelling_idempotent(
        client, auth_headers, db_session, monkeypatch):
    """A2 竞态另一形态:翻成 cancelling → 条件 UPDATE 零行→重读幂等 200,
    不重复 audit。"""
    run_id = await _mk_run(client, auth_headers, db_session)
    _flip_guard(monkeypatch, db_session, run_id, "cancelling")
    r = await client.post(f"/api/eval/runs/{run_id}/cancel",
                          headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"id": run_id, "status": "cancelling"}
    rows = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "eval_cancel")
    )).scalars().all()
    assert rows == []


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


async def test_cancelled_run_emits_cancelled_event(
        client, auth_headers, db_session, monkeypatch):
    """M20:取消收口改发独立事件 eval.cancelled(仍不发 eval.completed)。
    全订阅端点(events=[] 照 test_webhook_events._subscribed_ep 模式)恰收
    一行,nudge 被踢(n>0,与 completed 同待遇)。"""
    db_session.add(WebhookEndpoint(
        name=f"cxl{time.time_ns()}", url="http://x/h", secret="wh_s",
        events=[], created_by=1))
    await db_session.commit()
    run_id = await _mk_run(client, auth_headers, db_session, n=3)
    fake, calls = _fake_retrieval_item(run_id, flip_at=1)
    monkeypatch.setattr(runner, "retrieval_item", fake)
    nudged = []
    monkeypatch.setattr(runner, "nudge", lambda: nudged.append(1))
    await run_eval_task(run_id, "retrieval", False, 8)
    db_session.expire_all()
    run = (await db_session.execute(
        select(EvalRun).where(EvalRun.id == run_id))).scalar_one()
    assert run.status == "cancelled"  # 前置:确是取消路径(非 completed)
    deliveries = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    # M20:取消改发独立事件——恰一行 eval.cancelled,绝无 eval.completed
    assert [d.event_type for d in deliveries] == ["eval.cancelled"]
    assert deliveries[0].payload["data"]["run"]["item_count"] == 1
    assert nudged == [1]  # n>0,nudge 被踢(与 completed 同待遇)


async def _mk_bare_run(db_session, status="running") -> int:
    run = EvalRun(kb_id=1, mode="retrieval", summary=None,
                  item_count=0, status=status, triggered_by=1)
    db_session.add(run)
    await db_session.commit()
    return run.id


async def test_finalize_completes_only_from_running(db_session):
    run_id = await _mk_bare_run(db_session, "running")
    final, n = await _finalize_run(db_session, run_id, 1, "retrieval",
                                   [{"question": "q"}], False)
    assert final == "completed"
    db_session.expire_all()
    run = await db_session.get(EvalRun, run_id)
    assert run.status == "completed"
    assert run.item_count == 1 and run.summary["item_count"] == 1


async def test_finalize_raced_cancel_wins(db_session):
    """A1 竞态:任务未察觉(cancelled=False)但行已被端点置 cancelling →
    fallback 收口 cancelled,绝不覆盖成 completed。"""
    run_id = await _mk_bare_run(db_session, "cancelling")
    final, n = await _finalize_run(db_session, run_id, 1, "retrieval",
                                   [{"question": "q"}], False)
    assert final == "cancelled"
    db_session.expire_all()
    run = await db_session.get(EvalRun, run_id)
    assert run.status == "cancelled"


async def test_finalize_double_miss_no_write_no_event(db_session):
    """两跳全零行(行已被 sweep 等收口)→ 不改写、零事件。"""
    run_id = await _mk_bare_run(db_session, "failed")
    db_session.add(WebhookEndpoint(
        name=f"dm{time.time_ns()}", url="http://x/h", secret="wh_s",
        events=[], created_by=1))
    await db_session.commit()
    final, n = await _finalize_run(db_session, run_id, 1, "retrieval",
                                   [], False)
    assert final is None and n == 0
    db_session.expire_all()
    run = await db_session.get(EvalRun, run_id)
    assert run.status == "failed"  # 他人终态未被覆盖
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert rows == []


async def test_finalize_cancelled_emits_cancelled_event(db_session):
    run_id = await _mk_bare_run(db_session, "cancelling")
    db_session.add(WebhookEndpoint(
        name=f"fc{time.time_ns()}", url="http://x/h", secret="wh_s",
        events=[], created_by=1))
    await db_session.commit()
    final, n = await _finalize_run(db_session, run_id, 1, "retrieval",
                                   [{"question": "q"}], True)
    assert final == "cancelled" and n == 1
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert [r.event_type for r in rows] == ["eval.cancelled"]


async def test_finalize_failed_only_from_running(db_session):
    """M21:failed 条件写——只落在仍是 running 的行;run 形状与 completed
    统一(item_count/summary),error 字段保留。"""
    run_id = await _mk_bare_run(db_session, "running")
    db_session.add(WebhookEndpoint(
        name=f"fl{time.time_ns()}", url="http://x/h", secret="wh_s",
        events=[], created_by=1))
    await db_session.commit()
    final, n = await _finalize_run(db_session, run_id, 1, "retrieval",
                                   [{"question": "q"}], False, error="炸了")
    assert final == "failed" and n == 1
    db_session.expire_all()
    run = await db_session.get(EvalRun, run_id)
    assert run.status == "failed" and run.error == "炸了"
    assert run.item_count == 1 and run.summary["item_count"] == 1
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert [r.event_type for r in rows] == ["eval.failed"]
    d = rows[0].payload["data"]
    assert d["error"] == "炸了"
    assert d["run"]["item_count"] == 1 and "summary" in d["run"]


async def test_finalize_failed_raced_cancel_wins(db_session):
    """异常撞上并发取消:行已被端点置 cancelling → 用户赢,收口 cancelled、
    发 eval.cancelled 而非 eval.failed,run.error 不写(取消非失败)。"""
    run_id = await _mk_bare_run(db_session, "cancelling")
    db_session.add(WebhookEndpoint(
        name=f"fr{time.time_ns()}", url="http://x/h", secret="wh_s",
        events=[], created_by=1))
    await db_session.commit()
    final, n = await _finalize_run(db_session, run_id, 1, "retrieval",
                                   [{"question": "q"}], False, error="炸了")
    assert final == "cancelled" and n == 1
    db_session.expire_all()
    run = await db_session.get(EvalRun, run_id)
    assert run.status == "cancelled" and run.error is None
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert [r.event_type for r in rows] == ["eval.cancelled"]


async def test_run_task_exception_failed_via_finalize(
        client, auth_headers, db_session, monkeypatch):
    """M21:run_eval_task 异常路径经 _finalize_run 条件收口——不再 ORM
    无条件写;部分结果照写 summary(数据诚实,与取消同语义)。"""
    run_id = await _mk_run(client, auth_headers, db_session, n=2)
    calls = {"n": 0}

    async def flaky(db, kb_id, q, top_k, reranker):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("第二题炸")
        return {"question": q.question, "hit_at_k": None, "mrr": None,
                "keyword_recall": None}

    monkeypatch.setattr(runner, "retrieval_item", flaky)
    monkeypatch.setattr(runner, "nudge", lambda: None)
    await run_eval_task(run_id, "retrieval", False, 8)
    db_session.expire_all()
    run = (await db_session.execute(
        select(EvalRun).where(EvalRun.id == run_id))).scalar_one()
    assert run.status == "failed"
    assert "第二题炸" in run.error
    assert run.item_count == 1 and run.summary["item_count"] == 1
    items = (await db_session.execute(
        select(EvalItem).where(EvalItem.run_id == run_id))).scalars().all()
    assert len(items) == 1  # 第 1 题已逐题落库,不受异常影响


async def test_run_task_aborts_when_closed_before_start(
        client, auth_headers, db_session, monkeypatch):
    """M22 领租:行已被收口终态 → 条件 UPDATE 零行命中,任务直接退出
    ——零题被跑、终态不被覆盖(杜绝「被误杀后白跑全程」)。真题集(n=2)
    使测试具区分力:无领租块则两题照跑(calls==2、EvalItem 落库)。"""
    run_id = await _mk_run(client, auth_headers, db_session,
                           status="failed", n=2)
    fake, calls = _fake_retrieval_item(run_id, flip_at=99)
    monkeypatch.setattr(runner, "retrieval_item", fake)
    await run_eval_task(run_id, "retrieval", False, 8)
    assert calls["n"] == 0
    items = (await db_session.execute(
        select(EvalItem).where(EvalItem.run_id == run_id))).scalars().all()
    assert items == []
    db_session.expire_all()
    run = await db_session.get(EvalRun, run_id)
    assert run.status == "failed"  # 终态未被覆盖


async def test_run_task_cancelled_while_queued_closes_zero_subset(
        client, auth_headers, db_session, monkeypatch):
    """M22:排队期间被取消(行 cancelling)→ 领租成功但提前收口:零子集
    cancelled + eval.cancelled 事件,一题不跑(M19 检查点语义提前到零题)。
    真题集(n=2)使测试具区分力:无领租块则第 1 题照跑后检查点收口
    (item_count==1),而非零子集。"""
    db_session.add(WebhookEndpoint(
        name=f"cq{time.time_ns()}", url="http://x/h", secret="wh_s",
        events=[], created_by=1))
    await db_session.commit()
    run_id = await _mk_run(client, auth_headers, db_session,
                           status="cancelling", n=2)
    fake, calls = _fake_retrieval_item(run_id, flip_at=99)
    monkeypatch.setattr(runner, "retrieval_item", fake)
    await run_eval_task(run_id, "retrieval", False, 8)
    assert calls["n"] == 0
    db_session.expire_all()
    run = await db_session.get(EvalRun, run_id)
    assert run.status == "cancelled" and run.item_count == 0
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert [r.event_type for r in rows] == ["eval.cancelled"]


async def test_sweep_collects_cancelling(client, auth_headers, db_session):
    """worker 重启清扫:running 与 cancelling 两类在途都收口 failed。"""
    db_session.add_all([
        EvalRun(kb_id=1, mode="retrieval", summary=None,
                item_count=1, status="cancelling",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=90)),
        EvalRun(kb_id=2, mode="retrieval", summary=None,
                item_count=1, status="running",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=90)),
        EvalRun(kb_id=3, mode="retrieval", summary={"hit": 1.0},
                item_count=1, status="completed"),
    ])
    await db_session.commit()

    _recover_orphan_runs()  # 直调处理器本体(信号在 pytest 不触发)

    db_session.expire_all()
    runs = (await db_session.execute(
        select(EvalRun).order_by(EvalRun.id))).scalars().all()
    assert [r.status for r in runs] == ["failed", "failed", "completed"]
    assert runs[0].error == "orphaned: heartbeat expired (worker died or restarted)"
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


async def test_heartbeat_renewed_per_item(client, auth_headers,
                                          db_session, monkeypatch):
    """M21 租约:逐题续签 heartbeat_at(naive UTC,随逐题 commit 落库);
    全部题跑完后心跳不应早于宽限。"""
    run_id = await _mk_run(client, auth_headers, db_session, n=2)
    before = utcnow_naive()
    fake, _ = _fake_retrieval_item(run_id, flip_at=99)  # 不触发取消
    monkeypatch.setattr(runner, "retrieval_item", fake)
    await run_eval_task(run_id, "retrieval", False, 8)
    db_session.expire_all()
    run = await db_session.get(EvalRun, run_id)
    assert run.status == "completed"
    assert run.heartbeat_at is not None
    assert run.heartbeat_at >= before  # 续租发生在观测点之后
    assert (utcnow_naive() - run.heartbeat_at).total_seconds() < 60


async def test_sweep_two_stage_predicate(db_session):
    """M22 两段判据:已开跑看心跳宽限;从未开跑(NULL)看创建龄
    (EVAL_QUEUE_GRACE_MINUTES,默认 60)——排队中的新鲜行绝不收口,
    超龄 NULL 行兜底收口。created_at 为 timestamptz,回填用 aware UTC。"""
    from datetime import datetime, timezone

    aware_now = datetime.now(timezone.utc)
    db_session.add_all([
        EvalRun(kb_id=1, mode="retrieval", summary=None, item_count=0,
                status="running", heartbeat_at=utcnow_naive()),
        EvalRun(kb_id=2, mode="retrieval", summary=None, item_count=0,
                status="cancelling",
                heartbeat_at=utcnow_naive() - timedelta(minutes=30)),
        EvalRun(kb_id=3, mode="retrieval", summary=None, item_count=0,
                status="running", heartbeat_at=None,
                created_at=aware_now - timedelta(minutes=30)),
        EvalRun(kb_id=4, mode="retrieval", summary=None, item_count=0,
                status="running", heartbeat_at=None,
                created_at=aware_now - timedelta(minutes=90)),
    ])
    await db_session.commit()
    _recover_orphan_runs()
    db_session.expire_all()
    runs = (await db_session.execute(
        select(EvalRun).order_by(EvalRun.id))).scalars().all()
    assert [r.status for r in runs] == \
        ["running", "failed", "running", "failed"]


async def test_sweep_emits_eval_failed_per_orphan(db_session):
    """M22:孤儿收口不再静默——每个被收口行恰一条 eval.failed,负载
    summary 诚实为 null、item_count 为创建时题数;completed 行零事件。"""
    from datetime import datetime, timezone

    db_session.add(WebhookEndpoint(
        name=f"sw{time.time_ns()}", url="http://x/h", secret="wh_s",
        events=[], created_by=1))
    db_session.add_all([
        EvalRun(kb_id=1, mode="retrieval", summary=None, item_count=7,
                status="running", heartbeat_at=None,
                created_at=datetime.now(timezone.utc)
                - timedelta(minutes=90)),
        EvalRun(kb_id=1, mode="generation", summary=None, item_count=3,
                status="cancelling",
                heartbeat_at=utcnow_naive() - timedelta(minutes=30)),
        EvalRun(kb_id=1, mode="retrieval", summary={"hit": 1.0},
                item_count=2, status="completed"),
    ])
    await db_session.commit()
    _recover_orphan_runs()
    db_session.expire_all()
    rows = (await db_session.execute(
        select(WebhookDelivery).order_by(WebhookDelivery.id))
    ).scalars().all()
    assert [r.event_type for r in rows] == ["eval.failed", "eval.failed"]
    assert all(r.payload["data"]["run"]["summary"] is None
               for r in rows)
    assert rows[0].payload["data"]["run"]["item_count"] == 7
    assert "orphaned" in rows[0].payload["data"]["error"]


def test_beat_schedule_registers_orphan_sweep():
    from app.workers.celery_app import celery_app

    entry = celery_app.conf.beat_schedule["eval-orphan-sweep"]
    assert entry["task"] == "app.workers.eval_tasks.sweep_orphan_runs"
    assert entry["schedule"] == 60.0
