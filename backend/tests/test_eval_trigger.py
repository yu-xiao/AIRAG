# backend/tests/test_eval_trigger.py
"""M15 T5:触发端点守卫(409/422/403/404/422 top_k)+ eager 内联执行 +
列表/明细新字段(status/created_by/done_count/error)。"""
from sqlalchemy import select, text

from app.models import EvalQuestion, EvalRun


async def _register_and_login(client, username):
    await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"})
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _make_kb(client, headers, name) -> int:
    resp = await client.post("/api/kbs", json={"name": name}, headers=headers)
    assert resp.status_code == 201
    return resp.json()["id"]


async def _add_question(db_session, kb_id, n=1):
    db_session.add_all([EvalQuestion(kb_id=kb_id, question=f"q{i}")
                         for i in range(n)])
    await db_session.commit()


async def test_trigger_runs_eager_and_completes(client, auth_headers,
                                                db_session):
    """celery_eager:.delay() 在请求线程内联跑完 → 返回后即 completed。"""
    kb_id = await _make_kb(client, auth_headers, "触发库A")
    await _add_question(db_session, kb_id, n=2)
    r = await client.post("/api/eval/runs", headers=auth_headers,
                          json={"kb_id": kb_id, "mode": "retrieval"})
    assert r.status_code == 201
    run_id = r.json()["run_id"]
    r = await client.get(f"/api/eval/runs/{run_id}", headers=auth_headers)
    d = r.json()
    assert d["status"] == "completed"
    assert d["created_by"] is not None  # owner 触发,联查到用户名
    assert len(d["items"]) == 2


async def test_trigger_guards(client, auth_headers, db_session, monkeypatch):
    from app.core.config import settings

    # 钉死空 key:generation 触发的 eager 内联执行确定性走 failed 分支,
    # 不打真 LLM(dev .env 的真 key 会泄进测试进程,必须显式压掉)
    monkeypatch.setattr(settings, "ZHIPU_API_KEY", "")

    kb_id = await _make_kb(client, auth_headers, "触发库B")
    # 空题集 → 422
    r = await client.post("/api/eval/runs", headers=auth_headers,
                          json={"kb_id": kb_id, "mode": "retrieval"})
    assert r.status_code == 422
    # 非法 top_k → 422
    await _add_question(db_session, kb_id)
    r = await client.post("/api/eval/runs", headers=auth_headers,
                          json={"kb_id": kb_id, "mode": "retrieval",
                                "top_k": 0})
    assert r.status_code == 422
    # 同 kb+mode 已有 running → 409(直插 running 行,绕开 eager 即完)
    db_session.add(EvalRun(kb_id=kb_id, mode="retrieval", summary=None,
                           item_count=1, status="running"))
    await db_session.commit()
    r = await client.post("/api/eval/runs", headers=auth_headers,
                          json={"kb_id": kb_id, "mode": "retrieval"})
    assert r.status_code == 409
    # 同 kb 另一 mode 不拦:eager 内联执行,无 key → 201 后终态 failed
    r = await client.post("/api/eval/runs", headers=auth_headers,
                          json={"kb_id": kb_id, "mode": "generation"})
    assert r.status_code == 201
    r = await client.get(f"/api/eval/runs/{r.json()['run_id']}",
                         headers=auth_headers)
    assert r.json()["status"] == "failed"
    assert "ZHIPU" in r.json()["error"]

    # 权限:不可见 404;editor 403
    stranger = await _register_and_login(client, "m15_tr_stranger")
    r = await client.post("/api/eval/runs", headers=stranger,
                          json={"kb_id": kb_id, "mode": "retrieval"})
    assert r.status_code == 404
    sid = (await client.get("/api/auth/me", headers=stranger)).json()["id"]
    await db_session.execute(text(
        "INSERT INTO kb_permissions (kb_id, user_id, perm) "
        "VALUES (:k, :u, 'editor')"), {"k": kb_id, "u": sid})
    await db_session.commit()
    r = await client.post("/api/eval/runs", headers=stranger,
                          json={"kb_id": kb_id, "mode": "retrieval"})
    assert r.status_code == 403


async def test_list_fields_and_running_visibility(client, auth_headers,
                                                  db_session):
    kb_id = await _make_kb(client, auth_headers, "触发库C")
    db_session.add(EvalRun(kb_id=kb_id, mode="retrieval", summary=None,
                           item_count=3, status="running", triggered_by=None))
    await db_session.flush()
    r = await client.get("/api/eval/runs?kb_id=" + str(kb_id),
                         headers=auth_headers)
    item = r.json()["items"][0]
    assert item["status"] == "running"
    assert item["done_count"] == 0  # 进度 = len(items)
    assert item["created_by"] is None  # CLI/未触发行
