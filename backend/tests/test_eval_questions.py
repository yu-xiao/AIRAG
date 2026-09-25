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


# ---- M19 T5:题集导出 / 批量导入 ----


async def test_export_shape_and_order(client, auth_headers):
    kb_id = await _make_kb(client, auth_headers, "导出库")
    for i in (2, 0, 1):  # 乱序建,导出须按 id asc
        await client.post("/api/eval/questions",
                          json=PAYLOAD | {"kb_id": kb_id, "question": f"q{i}"},
                          headers=auth_headers)
    r = await client.get(f"/api/eval/questions/export?kb_id={kb_id}",
                         headers=auth_headers)
    assert r.status_code == 200
    assert "eval-questions-kb" in r.headers["content-disposition"]
    body = r.json()
    assert body["kb_id"] == kb_id and body["count"] == 3
    assert body["kb_name"] == "导出库" and body["exported_at"]
    # 接口契约 id asc:创建序 q2,q0,q1 → id 序即创建序(非字母序/倒序)
    assert [q["question"] for q in body["questions"]] == ["q2", "q0", "q1"]
    # 可移植:不含 id/kb_id/created_at
    assert set(body["questions"][0]) == {"question", "expect_doc_ids",
                                         "expect_keywords", "reference_answer"}


async def test_export_empty_kb(client, auth_headers):
    kb_id = await _make_kb(client, auth_headers, "空导出库")
    r = await client.get(f"/api/eval/questions/export?kb_id={kb_id}",
                         headers=auth_headers)
    assert r.status_code == 200 and r.json()["count"] == 0


async def test_export_permissions(client, auth_headers, db_session):
    """守卫同 questions GET(_require_kb_owner):不可见库 404;
    可见非 owner(editor)403。"""
    from sqlalchemy import text

    kb_id = await _make_kb(client, auth_headers, "导出权限库")
    other = await _register_and_login(client, "m19_exp_other")
    r = await client.get(f"/api/eval/questions/export?kb_id={kb_id}",
                         headers=other)
    assert r.status_code == 404  # 无 perm → 不可见

    sid = (await client.get("/api/auth/me", headers=other)).json()["id"]
    await db_session.execute(text(
        "INSERT INTO kb_permissions (kb_id, user_id, perm) "
        "VALUES (:k, :u, 'editor')"), {"k": kb_id, "u": sid})
    await db_session.commit()
    r = await client.get(f"/api/eval/questions/export?kb_id={kb_id}",
                         headers=other)
    assert r.status_code == 403  # editor 非 owner


async def test_bulk_creates_all(client, auth_headers):
    kb_id = await _make_kb(client, auth_headers, "导入库")
    qs = [{"question": f"bq{i}", "expect_doc_ids": [i],
           "expect_keywords": [], "reference_answer": None} for i in range(3)]
    r = await client.post("/api/eval/questions/bulk",
                          json={"kb_id": kb_id, "questions": qs},
                          headers=auth_headers)
    assert r.status_code == 201
    assert r.json() == {"created": 3, "errors": []}
    r2 = await client.get(f"/api/eval/questions?kb_id={kb_id}",
                          headers=auth_headers)
    assert r2.json()["total"] == 3


async def test_bulk_partial_success(client, auth_headers):
    kb_id = await _make_kb(client, auth_headers, "半成功库")
    qs = [{"question": "ok1"}, {"question": "   "},  # 空白题 → error
          {"question": "ok2"}]
    r = await client.post("/api/eval/questions/bulk",
                          json={"kb_id": kb_id, "questions": qs},
                          headers=auth_headers)
    assert r.status_code == 201
    body = r.json()
    assert body["created"] == 2
    assert len(body["errors"]) == 1 and body["errors"][0]["index"] == 1
    assert "题干" in body["errors"][0]["detail"]
    r2 = await client.get(f"/api/eval/questions?kb_id={kb_id}",
                          headers=auth_headers)
    assert r2.json()["total"] == 2  # 合法两条全部落库


async def test_bulk_cap_and_permissions(client, auth_headers):
    kb_id = await _make_kb(client, auth_headers, "上限库")
    r = await client.post("/api/eval/questions/bulk",
                          json={"kb_id": kb_id,
                                "questions": [{"question": "x"}] * 501},
                          headers=auth_headers)
    assert r.status_code == 422  # >500 条整体拒绝
    other = await _register_and_login(client, "m19_bulk_other")
    r2 = await client.post("/api/eval/questions/bulk",
                           json={"kb_id": kb_id, "questions": []},
                           headers=other)
    assert r2.status_code == 404  # 不可见;可见非 owner 403 分支见 export 用例
