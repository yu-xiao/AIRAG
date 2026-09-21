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


async def test_run_eval_task_keeps_committed_items_on_failure(
        client, auth_headers, db_session, monkeypatch):
    """I1/M2:第 2 题炸时,第 1 题已逐题 commit 的 EvalItem 保留;
    except 先 rollback 再置 failed,DB 级异常不会二次炸掉状态机。"""
    from sqlalchemy import select

    import app.services.eval_runner as runner
    from app.services.eval_runner import run_eval_task

    calls = {"n": 0}

    async def flaky(db, kb_id, q, top_k, reranker):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("第二题炸了")
        return {"question": q.question, "hit_at_k": False,
                "mrr": 0.0, "keyword_recall": 0.0}

    run_id = await _mk_running_run(client, auth_headers, db_session, n=2)
    monkeypatch.setattr(runner, "retrieval_item", flaky)
    await run_eval_task(run_id, "retrieval", False, 8)
    db_session.expire_all()
    run = (await db_session.execute(
        select(EvalRun).where(EvalRun.id == run_id))).scalar_one()
    assert run.status == "failed"
    assert "第二题炸了" in run.error
    assert len(run.items) == 1  # 第 1 题已插并 commit 的保留


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


async def test_generation_uses_fresh_uncached_llm(client, auth_headers,
                                                  db_session, monkeypatch):
    """M15 修复:worker 每任务一个新事件循环(asyncio.run),make_chat_llm 的
    lru_cache 单例跨循环复用会被毒化——run_eval_task 的 generation 分支必须
    经 _fresh_chat_llm() 绕缓存新建,graph.ainvoke 拿到的正是该 fresh 实例。"""
    from sqlalchemy import select, text

    import app.services.chat_graph.graph as graph_mod
    import app.services.eval_runner as runner
    from app.core.config import settings
    from app.services.eval_runner import run_eval_task

    class FakeLLM:
        async def ainvoke(self, msgs):  # eval_judge._judge 只读 .content
            class R:
                content = '{"score": 0.5, "reasons": "stub"}'
            return R()

    fresh_llm = FakeLLM()
    seen: dict = {}

    def fake_build_graph(llm=None, checkpointer=None):
        class FakeGraph:
            async def ainvoke(self, state):
                seen["llm"] = llm  # 闭包捕获 llm 标识
                return {"answer": "ans", "hits": [], "citations": [],
                        "refused": False}

        return FakeGraph()

    monkeypatch.setattr(settings, "ZHIPU_API_KEY", "test-key")
    monkeypatch.setattr(runner, "_fresh_chat_llm", lambda: fresh_llm)
    monkeypatch.setattr(graph_mod, "build_graph", fake_build_graph)

    run_id = await _mk_running_run(client, auth_headers, db_session, n=1)
    await db_session.execute(
        text("UPDATE eval_runs SET mode='generation' WHERE id=:i"),
        {"i": run_id})
    await db_session.commit()
    await run_eval_task(run_id, "generation", False, 8)
    db_session.expire_all()
    run = (await db_session.execute(
        select(EvalRun).where(EvalRun.id == run_id))).scalar_one()
    assert run.status == "completed"
    assert seen["llm"] is fresh_llm  # graph 消费的就是工厂 fresh 实例,非缓存单例
    assert run.items[0].faithfulness == 0.5  # judge 走的也是同一 fresh llm


async def test_run_evaluation_shell_disposes_shared_engine(
        client, auth_headers, db_session, monkeypatch):
    """M15 修复:worker 每任务一个新事件循环,图节点(retrieve 等)经全局
    SessionLocal 池化引擎取的连接绑定本任务循环;任务壳必须在 run_eval_task
    之后弃置全局池——否则下一任务 checkout 到死循环连接,pre-ping 打到已关
    proactor(NoneType.send)。spy 拦截 dispose 断言恰被 await 一次;任务壳是
    同步函数、内部 _run_async 可能切线程,list.append 即线程安全足够。"""
    from sqlalchemy import select

    import app.workers.eval_tasks as eval_tasks
    from app.workers.eval_tasks import run_evaluation

    calls: list = []

    async def spy():
        calls.append(1)

    monkeypatch.setattr(eval_tasks, "_dispose_shared_engine", spy)
    run_id = await _mk_running_run(client, auth_headers, db_session)
    run_evaluation.run(run_id, "retrieval", False, 8)
    assert calls == [1]  # 恰一次
    db_session.expire_all()
    run = (await db_session.execute(
        select(EvalRun).where(EvalRun.id == run_id))).scalar_one()
    assert run.status == "completed"  # 壳内先真跑完评估再 dispose
