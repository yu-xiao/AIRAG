# backend/tests/test_eval_api.py
"""M14 Task1:评估只读 API——权限矩阵/分页过滤/kb_name 联查/明细。"""
from sqlalchemy import select, text

from app.models import EvalItem, EvalRun, KbPermission, KnowledgeBase


async def _register_and_login(client, username):
    await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _seed_run(db_session, kb_id: int, mode: str = "retrieval",
                    n_items: int = 1) -> int:
    """直插 EvalRun/EvalItem(API 只读,种数据走 ORM);返回 run_id。"""
    run = EvalRun(kb_id=kb_id, mode=mode,
                  summary={"hit": 0.5, "item_count": n_items},
                  item_count=n_items)
    db_session.add(run)
    await db_session.flush()
    for i in range(n_items):
        db_session.add(EvalItem(
            run_id=run.id, question=f"问题{i}",
            expect_doc_ids=[1], expect_keywords=["关键词"],
            hit_at_k=1.0, mrr=1.0, keyword_recall=1.0,
        ))
    await db_session.commit()
    return run.id


async def _make_kb(client, auth_headers, name) -> int:
    resp = await client.post("/api/kbs", json={"name": name},
                             headers=auth_headers)
    assert resp.status_code == 201
    return resp.json()["id"]


async def _promote_admin(db_session, client, auth_headers) -> None:
    me = await client.get("/api/auth/me", headers=auth_headers)
    await db_session.execute(
        text("UPDATE users SET role='admin' WHERE id=:i"),
        {"i": me.json()["id"]},
    )
    await db_session.commit()


async def test_owner_sees_own_runs(client, auth_headers, db_session):
    kb_id = await _make_kb(client, auth_headers, "评估库A")
    await _seed_run(db_session, kb_id)
    await _seed_run(db_session, kb_id, mode="generation")
    resp = await client.get("/api/eval/runs", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["items"][0]["kb_name"] == "评估库A"
    assert body["items"][0]["mode"] in ("retrieval", "generation")


async def test_no_owner_libs_returns_empty_set(client, auth_headers):
    """无任何 owner 库的用户:空集而非 403。"""
    resp = await client.get("/api/eval/runs", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"total": 0, "items": []}


async def test_admin_sees_all_runs(client, auth_headers, db_session):
    """admin 不做 owner 过滤:看得到别人库的 run(第二个用户提为 admin,
    他不拥有任何库——若列表按 owner 过滤会是空)。"""
    kb_id = await _make_kb(client, auth_headers, "他人评估库")
    await _seed_run(db_session, kb_id)
    admin = await _register_and_login(client, "eval_admin_b")
    me = await client.get("/api/auth/me", headers=admin)
    await db_session.execute(
        text("UPDATE users SET role='admin' WHERE id=:i"),
        {"i": me.json()["id"]},
    )
    await db_session.commit()
    resp = await client.get("/api/eval/runs", headers=admin)
    assert resp.status_code == 200
    body = resp.json()
    assert any(r["kb_id"] == kb_id for r in body["items"])


async def test_kb_id_invisible_404(client, auth_headers):
    kb_id = await _make_kb(client, auth_headers, "私评估库")
    stranger = await _register_and_login(client, "eval_stranger1")
    resp = await client.get(f"/api/eval/runs?kb_id={kb_id}",
                            headers=stranger)
    assert resp.status_code == 404


async def test_kb_id_visible_non_owner_403(client, auth_headers, db_session):
    kb_id = await _make_kb(client, auth_headers, "共享评估库")
    await _seed_run(db_session, kb_id)
    viewer = await _register_and_login(client, "eval_viewer1")
    me = await client.get("/api/auth/me", headers=viewer)
    db_session.add(KbPermission(kb_id=kb_id, user_id=me.json()["id"],
                                perm="viewer"))
    await db_session.commit()
    resp = await client.get(f"/api/eval/runs?kb_id={kb_id}", headers=viewer)
    assert resp.status_code == 403
    # 不带 kb_id 的列表对该用户只看 owner 库 → 空集
    resp2 = await client.get("/api/eval/runs", headers=viewer)
    assert resp2.json() == {"total": 0, "items": []}


async def test_mode_filter_and_pagination(client, auth_headers, db_session):
    kb_id = await _make_kb(client, auth_headers, "过滤评估库")
    for _ in range(3):
        await _seed_run(db_session, kb_id, mode="retrieval")
    await _seed_run(db_session, kb_id, mode="generation")
    r1 = await client.get("/api/eval/runs?mode=retrieval&page_size=2",
                          headers=auth_headers)
    body = r1.json()
    assert body["total"] == 3 and len(body["items"]) == 2
    r2 = await client.get("/api/eval/runs?mode=retrieval&page_size=2&page=2",
                          headers=auth_headers)
    assert len(r2.json()["items"]) == 1
    bad = await client.get("/api/eval/runs?mode=bogus", headers=auth_headers)
    assert bad.status_code == 422


async def test_kb_deleted_name_none_for_admin(client, auth_headers, db_session):
    """KB 删除后 run 保留(EvalRun 无 FK,设计意图);kb_name 落空仅 admin 可见。"""
    kb_id = await _make_kb(client, auth_headers, "将删评估库")
    run_id = await _seed_run(db_session, kb_id)
    await _promote_admin(db_session, client, auth_headers)
    await db_session.execute(
        KnowledgeBase.__table__.delete().where(KnowledgeBase.id == kb_id))
    await db_session.commit()
    resp = await client.get("/api/eval/runs", headers=auth_headers)
    row = next(r for r in resp.json()["items"] if r["id"] == run_id)
    assert row["kb_name"] is None


async def test_run_detail_roundtrip(client, auth_headers, db_session):
    kb_id = await _make_kb(client, auth_headers, "明细评估库")
    run_id = await _seed_run(db_session, kb_id, n_items=3)
    resp = await client.get(f"/api/eval/runs/{run_id}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == run_id and body["items_truncated"] is False
    assert len(body["items"]) == 3
    assert body["items"][0]["question"] == "问题0"
    assert body["items"][0]["hit_at_k"] == 1.0
    assert body["kb_name"] == "明细评估库"


async def test_run_detail_forbidden_and_missing(client, auth_headers, db_session):
    kb_id = await _make_kb(client, auth_headers, "私明细库")
    run_id = await _seed_run(db_session, kb_id)
    stranger = await _register_and_login(client, "eval_stranger2")
    r1 = await client.get(f"/api/eval/runs/{run_id}", headers=stranger)
    assert r1.status_code == 404  # 越权不区分 403,防探测
    r2 = await client.get("/api/eval/runs/999999", headers=auth_headers)
    assert r2.status_code == 404
