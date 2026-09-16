from sqlalchemy import text


async def test_beat_schedule_targets_purge():
    from app.workers.celery_app import celery_app

    sched = celery_app.conf.beat_schedule
    assert "purge-expired-audit-logs" in sched
    entry = sched["purge-expired-audit-logs"]
    assert entry["task"] == "app.workers.maintenance.purge_expired_audit_logs"


async def test_purge_task_deletes_expired_rows(db_session):
    from app.services.audit import audit
    from app.workers.maintenance import purge_expired_audit_logs

    await audit(db_session, "u1", "login_success", "t")
    await db_session.commit()
    await db_session.execute(
        text("UPDATE audit_logs SET created_at = now() - interval '400 days'")
    )
    await db_session.commit()

    deleted = purge_expired_audit_logs()  # sync 壳;测试进程 env 指向 airag_test
    assert deleted >= 1
    # 简报原样代码对 Result 迭代两次:CursorResult 是一次性迭代器,第二次
    # 列表推导恒为空("audit_purge" 断言必挂)。改为 fetchall() 物化一次,
    # 两行断言保持逐字不变。
    rows = (await db_session.execute(text("SELECT action FROM audit_logs"))).fetchall()
    assert "login_success" not in [r[0] for r in rows]
    assert "audit_purge" in [r[0] for r in rows]
