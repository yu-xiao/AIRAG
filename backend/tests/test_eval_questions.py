# backend/tests/test_eval_questions.py
"""M15 T2:题集 CRUD 权限矩阵 + my-kbs。"""
from sqlalchemy import text

from app.models import EvalQuestion


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


async def _promote_admin(db_session, client, headers) -> int:
    me = await client.get("/api/auth/me", headers=headers)
    uid = me.json()["id"]
    await db_session.execute(
        text("UPDATE users SET role='admin' WHERE id=:i"), {"i": uid})
    await db_session.commit()
    return uid


PAYLOAD = {"question": "预算多少", "expect_doc_ids": [1, 2],
           "expect_keywords": ["预算"], "reference_answer": "三千万"}


async def test_owner_crud_roundtrip(client, auth_headers):
    kb_id = await _make_kb(client, auth_headers, "题集库A")
    body = PAYLOAD | {"kb_id": kb_id}
    r = await client.post("/api/eval/questions", json=body, headers=auth_headers)
    assert r.status_code == 201
    qid = r.json()["id"]
    assert r.json()["question"] == "预算多少"

    r = await client.get(f"/api/eval/questions?kb_id={kb_id}",
                         headers=auth_headers)
    assert r.json() == {"total": 1, "items": [r.json()["items"][0]]}
    assert r.json()["total"] == 1

    r = await client.put(f"/api/eval/questions/{qid}",
                         json=PAYLOAD | {"question": "改后"},
                         headers=auth_headers)
    assert r.status_code == 200 and r.json()["question"] == "改后"

    r = await client.delete(f"/api/eval/questions/{qid}", headers=auth_headers)
    assert r.status_code == 204
    r = await client.put(f"/api/eval/questions/{qid}", json=PAYLOAD,
                         headers=auth_headers)
    assert r.status_code == 404  # 删除后更新 → 404


async def test_question_validation(client, auth_headers):
    kb_id = await _make_kb(client, auth_headers, "题集库B")
    r = await client.post("/api/eval/questions",
                          json={"kb_id": kb_id, "question": "   "},
                          headers=auth_headers)
    assert r.status_code == 422  # 去空白后为空
    r = await client.post("/api/eval/questions",
                          json={"kb_id": kb_id, "question": "x" * 2001},
                          headers=auth_headers)
    assert r.status_code == 422  # 超长


async def test_question_perm_matrix(client, auth_headers, db_session):
    """不可见库 404;可见非 owner(editor)403;admin 200。"""
    from sqlalchemy import text

    kb_id = await _make_kb(client, auth_headers, "题集库C")
    stranger = await _register_and_login(client, "m15_q_stranger")
    r = await client.post("/api/eval/questions",
                          json=PAYLOAD | {"kb_id": kb_id}, headers=stranger)
    assert r.status_code == 404  # 无 perm → 404

    sid = (await client.get("/api/auth/me", headers=stranger)).json()["id"]
    await db_session.execute(text(
        "INSERT INTO kb_permissions (kb_id, user_id, perm) "
        "VALUES (:k, :u, 'editor')"), {"k": kb_id, "u": sid})
    await db_session.commit()
    r = await client.post("/api/eval/questions",
                          json=PAYLOAD | {"kb_id": kb_id}, headers=stranger)
    assert r.status_code == 403  # editor 非 owner

    admin = await _register_and_login(client, "m15_q_admin")
    await _promote_admin(db_session, client, admin)
    r = await client.post("/api/eval/questions",
                          json=PAYLOAD | {"kb_id": kb_id}, headers=admin)
    assert r.status_code == 201  # admin 全量

    qid = r.json()["id"]
    # M16 D:PUT/DELETE 越权变体——admin 200/204;editor 403;无 perm 局外人 404
    r = await client.put(f"/api/eval/questions/{qid}",
                         json=PAYLOAD | {"question": "admin改"},
                         headers=admin)
    assert r.status_code == 200 and r.json()["question"] == "admin改"

    r = await client.put(f"/api/eval/questions/{qid}", json=PAYLOAD,
                         headers=stranger)
    assert r.status_code == 403  # editor 非 owner
    r = await client.delete(f"/api/eval/questions/{qid}", headers=stranger)
    assert r.status_code == 403

    outsider = await _register_and_login(client, "m16_q_outsider")
    r = await client.put(f"/api/eval/questions/{qid}", json=PAYLOAD,
                         headers=outsider)
    assert r.status_code == 404  # 不可见库
    r = await client.delete(f"/api/eval/questions/{qid}", headers=outsider)
    assert r.status_code == 404

    r = await client.delete("/api/eval/questions/999999", headers=admin)
    assert r.status_code == 404  # 不存在
    r = await client.delete(f"/api/eval/questions/{qid}", headers=admin)
    assert r.status_code == 204


async def test_my_kbs_counts(client, auth_headers, db_session):
    kb_id = await _make_kb(client, auth_headers, "mykbs库")
    r = await client.get("/api/eval/my-kbs", headers=auth_headers)
    mine = [x for x in r.json() if x["kb_id"] == kb_id]
    assert mine and mine[0]["question_count"] == 0
    db_session.add(EvalQuestion(kb_id=kb_id, question="q1"))
    db_session.add(EvalQuestion(kb_id=kb_id, question="q2"))
    await db_session.commit()
    r = await client.get("/api/eval/my-kbs", headers=auth_headers)
    mine = [x for x in r.json() if x["kb_id"] == kb_id]
    assert mine[0]["question_count"] == 2

    other = await _register_and_login(client, "m15_mykbs_other")
    r = await client.get("/api/eval/my-kbs", headers=other)
    assert all(x["kb_id"] != kb_id for x in r.json())  # 非 owner 不见
