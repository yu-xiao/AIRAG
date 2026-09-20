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


# ---- M12:文档五操作的 scope 折入(404 不泄露) ----
async def _scoped_key_db(client, auth_headers, db_session, kb_ids, role):
    from app.models import ApiKey
    from app.services.api_keys import generate_api_key

    me = await client.get("/api/auth/me", headers=auth_headers)
    raw, prefix, digest = generate_api_key()
    db_session.add(ApiKey(user_id=me.json()["id"], name=f"sc-{role}",
                          key_prefix=prefix, key_hash=digest, role=role,
                          kb_scope=list(kb_ids)))
    await db_session.commit()
    return raw


async def test_scoped_doc_ops_out_of_scope_404(client, auth_headers, db_session):
    from tests.test_agent_api import _create_kb

    kb_in = await _create_kb(client, auth_headers, "文档界内库")
    kb_out = await _create_kb(client, auth_headers, "文档界外库")
    # 界外库先放一篇文档(用 Web 面 JWT 上传,editor 用户)
    up = await client.post(
        f"/api/kbs/{kb_out}/documents",
        files={"file": ("o.docx", b"out-of-scope", "application/octet-stream")},
        headers=auth_headers,
    )
    doc_out = up.json()["id"]
    ro = await _scoped_key_db(client, auth_headers, db_session, [kb_in],
                              "read_only")
    ed = await _scoped_key_db(client, auth_headers, db_session, [kb_in],
                              "editor")
    rh = {"Authorization": f"Bearer {ro}"}
    eh = {"Authorization": f"Bearer {ed}"}
    # 读:list/get 界外 → 404
    assert (await client.get(f"/api/agent/kbs/{kb_out}/documents",
                             headers=rh)).status_code == 404
    assert (await client.get(f"/api/agent/documents/{doc_out}",
                             headers=rh)).status_code == 404
    # 写:upload/delete/reprocess 界外 → 404(可见性先于 perm/key 检查)
    assert (await client.post(
        f"/api/agent/kbs/{kb_out}/documents",
        files={"file": ("a.docx", b"x", "application/octet-stream")},
        headers=eh)).status_code == 404
    assert (await client.delete(f"/api/agent/documents/{doc_out}",
                                headers=eh)).status_code == 404
    assert (await client.post(f"/api/agent/documents/{doc_out}/reprocess",
                              headers=eh)).status_code == 404


async def test_scoped_doc_ops_in_scope_works(client, auth_headers, db_session):
    from tests.test_agent_api import _create_kb

    kb_in = await _create_kb(client, auth_headers, "文档界内库2")
    ed = await _scoped_key_db(client, auth_headers, db_session, [kb_in],
                              "editor")
    eh = {"Authorization": f"Bearer {ed}"}
    up = await client.post(
        f"/api/agent/kbs/{kb_in}/documents",
        files={"file": ("i.docx", b"in-scope", "application/octet-stream")},
        headers=eh,
    )
    assert up.status_code == 201
    doc_id = up.json()["id"]
    assert (await client.get(f"/api/agent/documents/{doc_id}",
                             headers=eh)).status_code == 200
    assert (await client.delete(f"/api/agent/documents/{doc_id}",
                                headers=eh)).status_code == 204


# ---- M13 Task6:快修② 预检后移(可见性/权限 → 预检 → 权威校验) ----
async def test_precheck_after_visibility_invisible_kb(client, auth_headers,
                                                      monkeypatch):
    """M13 快修②:不可见库+超限 body → 404(预检不得先于可见性)。"""
    from app.core.config import settings

    key = await _create_key_role(client, auth_headers, "预检序编辑", "editor")
    monkeypatch.setattr(settings, "MAX_UPLOAD_MB", 0)
    resp = await client.post(
        "/api/agent/kbs/999999/documents",
        files={"file": ("big.docx", b"x" * (2 * 1024 * 1024),
                        "application/octet-stream")},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404  # 现状是 413(预检在前)→ 本用例先红


# ---- M12 Task8:小项② 上传 Content-Length 预检(agent REST 面) ----
async def test_agent_upload_content_length_precheck_413(
        client, auth_headers, db_session, monkeypatch):
    from app.core.config import settings
    from tests.test_agent_api import _create_kb, _create_key_role

    kb_id = await _create_kb(client, auth_headers, "预检库2")
    key = await _create_key_role(client, auth_headers, "预检编辑", "editor")

    def _must_not_reach(*a, **k):
        raise AssertionError("precheck must reject before save_upload")

    monkeypatch.setattr("app.services.doc_ops.save_upload", _must_not_reach)
    monkeypatch.setattr(settings, "MAX_UPLOAD_MB", 0)
    resp = await client.post(
        f"/api/agent/kbs/{kb_id}/documents",
        files={"file": ("big.docx", b"x" * (2 * 1024 * 1024),
                        "application/octet-stream")},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 413
