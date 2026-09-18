# backend/tests/test_agent_docs_api.py
"""M11 Task4:agent REST 文档五端点(守卫矩阵/全链路/审计)。"""
import io
import json

from sqlalchemy import select, text

from app.models import AuditLog

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


async def _create_key_role(client, headers, name, role=None):
    payload = {"name": name}
    if role:
        payload["role"] = role
    r = await client.post("/api/auth/keys", json=payload, headers=headers)
    assert r.status_code == 201
    return r.json()["key"]


async def _upload(client, headers, kb_id, content=b"m11", name="a.docx"):
    return await client.post(
        f"/api/agent/kbs/{kb_id}/documents",
        files={"file": (name, io.BytesIO(content), DOCX_MIME)},
        data={"ocr": "auto"}, headers=headers,
    )


async def test_read_only_key_matrix(client, auth_headers):
    from tests.test_agent_api import _create_kb

    kb_id = await _create_kb(client, auth_headers, "矩阵库")
    ro = {"Authorization": f"Bearer {await _create_key_role(client, auth_headers, '只读')}"}
    # 读操作放行
    r_list = await client.get(f"/api/agent/kbs/{kb_id}/documents", headers=ro)
    assert r_list.status_code == 200
    # 写操作 403 editor_key_required
    r_up = await _upload(client, ro, kb_id)
    assert r_up.status_code == 403
    assert r_up.json()["detail"] == {"code": "editor_key_required"}
    # 不存在的文档:404 优先于能力校验(不泄露存在性)
    r_del = await client.delete("/api/agent/documents/999999", headers=ro)
    assert r_del.status_code == 404
    # editor key 造一个文档再验证 delete/reprocess 的 403
    ed = {"Authorization": f"Bearer {await _create_key_role(client, auth_headers, '编辑', 'editor')}"}
    doc_id = (await _upload(client, ed, kb_id)).json()["id"]
    r_del2 = await client.delete(f"/api/agent/documents/{doc_id}", headers=ro)
    assert r_del2.status_code == 403
    assert r_del2.json()["detail"] == {"code": "editor_key_required"}
    r_rep = await client.post(f"/api/agent/documents/{doc_id}/reprocess",
                              headers=ro)
    assert r_rep.status_code == 403


async def test_editor_key_full_cycle(client, auth_headers, db_session):
    from tests.test_agent_api import _create_kb

    kb_id = await _create_kb(client, auth_headers, "全链路库")
    ed = {"Authorization": f"Bearer {await _create_key_role(client, auth_headers, '编辑', 'editor')}"}
    up = await _upload(client, ed, kb_id, content=b"m11-cycle")
    assert up.status_code == 201
    doc = up.json()
    got = await client.get(f"/api/agent/documents/{doc['id']}", headers=ed)
    assert got.status_code == 200 and got.json()["filename"] == doc["filename"]
    lst = await client.get(f"/api/agent/kbs/{kb_id}/documents", headers=ed)
    assert doc["id"] in [d["id"] for d in lst.json()]
    dup = await _upload(client, ed, kb_id, content=b"m11-cycle")
    assert dup.status_code == 409
    bad = await _upload(client, ed, kb_id, content=b"x", name="a.exe")
    assert bad.status_code == 415
    rep = await client.post(f"/api/agent/documents/{doc['id']}/reprocess",
                            headers=ed)
    assert rep.status_code == 200
    dele = await client.delete(f"/api/agent/documents/{doc['id']}", headers=ed)
    assert dele.status_code == 204
    assert (await client.get(f"/api/agent/documents/{doc['id']}",
                             headers=ed)).status_code == 404
    db_session.expire_all()
    rows = (await db_session.execute(
        select(AuditLog).where(AuditLog.action.in_([
            "agent.upload_document", "agent.delete_document",
            "agent.reprocess_document", "agent.list_documents",
            "agent.get_document",
        ]))
    )).scalars().all()
    actions = {r.action for r in rows}
    assert {"agent.upload_document", "agent.delete_document"} <= actions
    up_row = next(r for r in rows if r.action == "agent.upload_document")
    assert json.loads(up_row.detail)["client"] == "rest"


async def test_editor_key_viewer_user_403(client, auth_headers, db_session):
    from tests.test_agent_api import _create_kb

    kb_id = await _create_kb(client, auth_headers, "授权库")
    await client.post("/api/auth/register",
                      json={"username": "m11viewer", "password": "secret123"})
    await db_session.execute(text(
        "UPDATE users SET role = 'editor' WHERE username = 'm11viewer'"))
    await db_session.commit()
    vl = await client.post("/api/auth/login",
                           json={"username": "m11viewer",
                                 "password": "secret123"})
    vh = {"Authorization": f"Bearer {vl.json()['access_token']}"}
    # 库主授予 viewer(字段名以 tests/test_kb_permissions.py 现有用例为准)
    r = await client.put(f"/api/kbs/{kb_id}/permissions",
                         json={"username": "m11viewer", "perm": "viewer"},
                         headers=auth_headers)
    assert r.status_code == 200
    vh_key = await _create_key_role(client, vh, "viewer的editor", "editor")
    hdr = {"Authorization": f"Bearer {vh_key}"}
    up = await _upload(client, hdr, kb_id)
    assert up.status_code == 403
    assert up.json()["detail"] == "editor permission required"
    # 读不受影响
    assert (await client.get(f"/api/agent/kbs/{kb_id}/documents",
                             headers=hdr)).status_code == 200


async def test_jwt_debug_upload_allowed(client, auth_headers):
    from tests.test_agent_api import _create_kb

    kb_id = await _create_kb(client, auth_headers, "JWT调试库")
    r = await _upload(client, auth_headers, kb_id, content=b"jwt-dbg")
    assert r.status_code == 201


async def test_delete_busy_409(client, auth_headers, db_session):
    from tests.test_agent_api import _create_kb

    kb_id = await _create_kb(client, auth_headers, "busy库")
    ed = {"Authorization": f"Bearer {await _create_key_role(client, auth_headers, '编辑', 'editor')}"}
    doc_id = (await _upload(client, ed, kb_id)).json()["id"]
    await db_session.execute(text(
        "UPDATE documents SET status = 'parsing' WHERE id = :i"),
        {"i": doc_id})
    await db_session.commit()
    # 原生 SQL 不回写身份映射;expire_all 让端点侧 db.get 读到 parsing
    db_session.expire_all()
    r = await client.delete(f"/api/agent/documents/{doc_id}", headers=ed)
    assert r.status_code == 409


async def test_agent_quota_endpoint(client, auth_headers):
    from app.core.config import settings

    key = await _create_key_role(client, auth_headers, "配额查询")
    r = await client.get("/api/agent/quota",
                         headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    body = r.json()
    assert body["limit"] == settings.AGENT_ASK_DAILY_TOKENS
    assert body["used"] is None or isinstance(body["used"], int)
    assert body["reset_at"]
    r2 = await client.get("/api/agent/quota", headers=auth_headers)
    assert r2.status_code == 403
    assert r2.json()["detail"] == "api key principal required"
