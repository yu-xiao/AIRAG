"""M17 T4:投递任务壳(自持 NullPool 引擎,eval_tasks 同款)。"""


def test_task_registered():
    from app.workers.webhook_tasks import deliver_pending
    assert "app.workers.webhook_tasks.deliver_pending" in \
        deliver_pending.app.tasks


def test_task_runs_deliver_due(monkeypatch):
    import app.workers.webhook_tasks as wt
    calls = []

    async def fake_due(db, client=None):
        calls.append(1)
        return 3

    monkeypatch.setattr(wt, "deliver_due", fake_due)
    assert wt.deliver_pending.run() == 3 and calls == [1]


def test_task_swallows_exception(monkeypatch):
    """投递批失败不炸 worker/beat——beat 下一轮再扫。"""
    import app.workers.webhook_tasks as wt

    async def boom(db, client=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(wt, "deliver_due", boom)
    assert wt.deliver_pending.run() == 0  # 吞掉,返回 0 不抛
