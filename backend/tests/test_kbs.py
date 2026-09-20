import pytest


async def test_create_kb(client, auth_headers):
    resp = await client.post(
        "/api/kbs",
        json={"name": "产品手册库", "description": "内部产品文档"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] > 0
    assert data["name"] == "产品手册库"
    assert data["embed_provider"] == "zhipu"
    assert data["embed_model"] == "embedding-3"


async def test_create_kb_requires_auth(client):
    resp = await client.post("/api/kbs", json={"name": "匿名库"})
    assert resp.status_code == 401


async def test_list_and_get_kb(client, auth_headers):
    await client.post(
        "/api/kbs", json={"name": "法务库"}, headers=auth_headers
    )
    listed = await client.get("/api/kbs", headers=auth_headers)
    assert listed.status_code == 200
    names = [item["name"] for item in listed.json()]
    assert "法务库" in names

    kb_id = listed.json()[0]["id"]
    got = await client.get(f"/api/kbs/{kb_id}", headers=auth_headers)
    assert got.status_code == 200
    assert got.json()["id"] == kb_id

    missing = await client.get("/api/kbs/999999", headers=auth_headers)
    assert missing.status_code == 404


async def _register_and_login(client, username):
    await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_viewer_cannot_create_kb(client, auth_headers):
    plain = await _register_and_login(client, "plain_viewer1")
    resp = await client.post("/api/kbs", json={"name": "游客库"}, headers=plain)
    assert resp.status_code == 403
    # editor 仍可建(auth_headers 已升 editor)
    ok = await client.post("/api/kbs", json={"name": "编辑库"}, headers=auth_headers)
    assert ok.status_code == 201


async def test_kb_invisible_to_stranger(client, auth_headers):
    mine = await client.post("/api/kbs", json={"name": "私库"}, headers=auth_headers)
    kb_id = mine.json()["id"]
    other = await _register_and_login(client, "stranger_ed1")  # 默认 viewer
    listed = await client.get("/api/kbs", headers=other)
    assert all(k["id"] != kb_id for k in listed.json())
    got = await client.get(f"/api/kbs/{kb_id}", headers=other)
    assert got.status_code == 404


async def test_list_returns_my_perm(client, auth_headers, db_session):
    from app.models import KbPermission

    mine = await client.post("/api/kbs", json={"name": "权限标注库"}, headers=auth_headers)
    kb_id = mine.json()["id"]
    listed = await client.get("/api/kbs", headers=auth_headers)
    row = next(k for k in listed.json() if k["id"] == kb_id)
    assert row["my_perm"] == "owner"

    viewer_headers = await _register_and_login(client, "perm_viewer1")
    reg = await client.get("/api/auth/me", headers=viewer_headers)
    db_session.add(KbPermission(kb_id=kb_id, user_id=reg.json()["id"], perm="viewer"))
    await db_session.commit()
    granted = await client.get("/api/kbs", headers=viewer_headers)
    row2 = next(k for k in granted.json() if k["id"] == kb_id)
    assert row2["my_perm"] == "viewer"


async def test_admin_sees_all_kbs(client, auth_headers, db_session):
    from sqlalchemy import text as _text

    mine = await client.post("/api/kbs", json={"name": "他人库"}, headers=auth_headers)
    kb_id = mine.json()["id"]
    me = await client.get("/api/auth/me", headers=auth_headers)
    await db_session.execute(
        _text("UPDATE users SET role = 'admin' WHERE id = :i"),
        {"i": me.json()["id"]},
    )
    await db_session.commit()
    listed = await client.get("/api/kbs", headers=auth_headers)
    row = next(k for k in listed.json() if k["id"] == kb_id)
    assert row["my_perm"] == "owner"


async def test_create_kb_duplicate_name_409(client, auth_headers):
    first = await client.post("/api/kbs", json={"name": "重名库"}, headers=auth_headers)
    dup = await client.post("/api/kbs", json={"name": "重名库"}, headers=auth_headers)
    assert first.status_code == 201
    assert dup.status_code == 409
    assert dup.json()["detail"] == "knowledge base name already exists"


async def test_create_kb_duplicate_after_strip_409(client, auth_headers):
    await client.post("/api/kbs", json={"name": "归一库"}, headers=auth_headers)
    dup = await client.post("/api/kbs", json={"name": "  归一库  "}, headers=auth_headers)
    assert dup.status_code == 409
    listed = await client.get("/api/kbs", headers=auth_headers)
    names = [k["name"] for k in listed.json() if k["name"].strip() == "归一库"]
    assert names == ["归一库"]  # 入库即 strip 后形态,仅一条


async def test_create_kb_blank_after_strip_422(client, auth_headers):
    resp = await client.post("/api/kbs", json={"name": "   "}, headers=auth_headers)
    assert resp.status_code == 422
    assert resp.json()["detail"] == "knowledge base name cannot be blank"


# ---- M12:KB name DB 唯一约束 + create_kb IntegrityError 兜底 ----
async def test_db_rejects_duplicate_kb_names(client, auth_headers, db_session):
    """M12:唯一约束落地,直插同名行在 commit 时抛 IntegrityError。"""
    from sqlalchemy.exc import IntegrityError

    from app.models import KnowledgeBase

    me = await client.get("/api/auth/me", headers=auth_headers)
    owner_id = me.json()["id"]
    db_session.add(KnowledgeBase(name="双胞胎库", owner_id=owner_id))
    await db_session.commit()
    db_session.add(KnowledgeBase(name="双胞胎库", owner_id=owner_id))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_create_kb_unique_fallback_409(client, auth_headers, db_session,
                                             monkeypatch):
    """M12:并发兜底——重名 SELECT 恒空(模拟竞态)时 flush 撞唯一约束仍 409。"""
    import app.api.kbs as kbs_mod
    from app.models import KnowledgeBase

    await client.post("/api/kbs", json={"name": "竞态库"}, headers=auth_headers)
    real_select = kbs_mod.select

    def blind_select(*a, **k):
        return real_select(KnowledgeBase).where(KnowledgeBase.id < 0)

    monkeypatch.setattr(kbs_mod, "select", blind_select)
    resp = await client.post("/api/kbs", json={"name": "竞态库"},
                             headers=auth_headers)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "knowledge base name already exists"


# ---- M12:KB 删除(级联 + 权限矩阵) ----
async def _mk_full_kb(client, db_session, owner_id, name="删除库"):
    """库 + 文档(done)+ chunk + 成员 + 会话引用 + 磁盘文件(每次调用传不同 name
    防唯一约束冲突;成员用真实注册用户防 FK 违约)。"""
    from pathlib import Path

    from app.core.config import settings
    from app.models import (Chunk, Conversation, Document, KbPermission,
                            KnowledgeBase)

    # RegisterIn.username 限 ^[A-Za-z0-9_]+$(name 是中文),用 uuid 保 ASCII 唯一
    import uuid as _uuid

    member = await client.post(
        "/api/auth/register",
        json={"username": f"del_m{_uuid.uuid4().hex[:8]}",
              "password": "secret123"})
    kb = KnowledgeBase(name=name, owner_id=owner_id)
    db_session.add(kb)
    await db_session.flush()
    doc = Document(kb_id=kb.id, filename="a.docx", file_path="x", mime="m",
                   size=1, sha256="del", status="done")
    db_session.add(doc)
    await db_session.flush()
    db_session.add(Chunk(document_id=doc.id, kb_id=kb.id, chunk_index=0,
                         content="c", char_len=1, content_hash="h"))
    db_session.add(KbPermission(kb_id=kb.id,
                                user_id=member.json()["id"], perm="viewer"))
    other_kb = KnowledgeBase(name=name + "-邻", owner_id=owner_id)
    db_session.add(other_kb)
    await db_session.flush()
    db_session.add(Conversation(user_id=owner_id,
                                kb_ids=[kb.id, other_kb.id]))
    await db_session.commit()
    doc_dir = Path(settings.UPLOAD_DIR) / str(kb.id)
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / "f.docx").write_bytes(b"x")
    return kb, doc, other_kb


async def test_delete_kb_cascade(client, auth_headers, db_session, monkeypatch,
                                 tmp_path):
    import json as _json
    from pathlib import Path

    from sqlalchemy import select

    from app.models import (AuditLog, Chunk, Conversation, Document,
                            KbPermission)
    from app.services import kb_ops

    me = await client.get("/api/auth/me", headers=auth_headers)
    uid = me.json()["id"]
    kb, doc, other_kb = await _mk_full_kb(client, db_session, uid,
                                          name="级联删除库")
    monkeypatch.setattr(kb_ops, "EVAL_DIR", tmp_path)
    eval_file = tmp_path / f"{kb.id}.json"
    eval_file.write_text("{}")
    from app.core.config import settings

    # expire_all 后访问持久对象的过期属性会触发同步刷新(MissingGreenlet),先取
    other_id = other_kb.id
    resp = await client.delete(f"/api/kbs/{kb.id}", headers=auth_headers)
    assert resp.status_code == 204
    db_session.expire_all()
    assert await db_session.get(type(kb), kb.id) is None
    assert await db_session.get(Document, doc.id) is None
    assert (await db_session.execute(
        select(Chunk).where(Chunk.kb_id == kb.id))).scalars().first() is None
    assert (await db_session.execute(
        select(KbPermission).where(KbPermission.kb_id == kb.id))
    ).scalars().first() is None
    conv = (await db_session.execute(
        select(Conversation).where(Conversation.user_id == uid))).scalars().one()
    assert conv.kb_ids == [other_id]  # array_remove 只清本库
    assert not (Path(settings.UPLOAD_DIR) / str(kb.id)).exists()
    assert not eval_file.exists()
    audit_row = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "kb_delete",
                               AuditLog.target == f"kb:{kb.id}")
    )).scalars().one()
    detail = _json.loads(audit_row.detail)
    assert detail["doc_count"] == 1 and detail["member_count"] == 1


async def test_delete_kb_busy_409(client, auth_headers, db_session):
    from app.models import Document, KnowledgeBase

    me = await client.get("/api/auth/me", headers=auth_headers)
    kb = KnowledgeBase(name="忙库", owner_id=me.json()["id"])
    db_session.add(kb)
    await db_session.flush()
    db_session.add(Document(kb_id=kb.id, filename="a.docx", file_path="x",
                            mime="m", size=1, sha256="busy",
                            status="parsing"))
    await db_session.commit()
    kb_id = kb.id  # expire_all 后属性访问会触发同步刷新,先取
    resp = await client.delete(f"/api/kbs/{kb_id}", headers=auth_headers)
    assert resp.status_code == 409
    db_session.expire_all()
    assert await db_session.get(KnowledgeBase, kb_id) is not None  # 行未动


# ---- M13 Task6:快修① busy 统一 DocOpError(HTTP 报文零变化) ----
async def test_delete_kb_busy_docoperror_response(client, auth_headers,
                                                  db_session):
    """M13 快修①:kb_ops busy 409 走 DocOpError,HTTP 报文不变。"""
    from app.models import Document, KnowledgeBase

    me = await client.get("/api/auth/me", headers=auth_headers)
    kb = KnowledgeBase(name="快修忙库", owner_id=me.json()["id"])
    db_session.add(kb)
    await db_session.flush()
    db_session.add(Document(kb_id=kb.id, filename="a.docx", file_path="x",
                            mime="m", size=1, sha256="fixbusy",
                            status="parsing"))
    await db_session.commit()
    r = await client.delete(f"/api/kbs/{kb.id}", headers=auth_headers)
    assert r.status_code == 409
    assert r.json()["detail"] == "knowledge base has documents being processed"


async def test_delete_kb_permissions(client, auth_headers, db_session):
    from sqlalchemy import text

    # 授权成员(editor)可见但非 owner → 403
    kb, _, _ = await _mk_full_kb(
        client, db_session,
        (await client.get("/api/auth/me", headers=auth_headers)).json()["id"],
        name="权限矩阵库")
    await client.post("/api/auth/register",
                      json={"username": "del_member1", "password": "secret123"})
    member_login = await client.post(
        "/api/auth/login",
        json={"username": "del_member1", "password": "secret123"})
    member_hdr = {"Authorization":
                  f"Bearer {member_login.json()['access_token']}"}
    from app.models import KbPermission

    mid = (await client.get("/api/auth/me", headers=member_hdr)).json()["id"]
    db_session.add(KbPermission(kb_id=kb.id, user_id=mid, perm="editor"))
    await db_session.commit()
    assert (await client.delete(f"/api/kbs/{kb.id}",
                                headers=member_hdr)).status_code == 403
    # 陌生人 → 404
    await client.post(
        "/api/auth/register",
        json={"username": "del_str1", "password": "secret123"})
    stranger_login = await client.post(
        "/api/auth/login",
        json={"username": "del_str1", "password": "secret123"})
    str_hdr = {"Authorization":
               f"Bearer {stranger_login.json()['access_token']}"}
    assert (await client.delete(f"/api/kbs/{kb.id}",
                                headers=str_hdr)).status_code == 404
    # admin → 204
    me = await client.get("/api/auth/me", headers=auth_headers)
    await db_session.execute(
        text("UPDATE users SET role = 'admin' WHERE id = :i"),
        {"i": me.json()["id"]})
    await db_session.commit()
    assert (await client.delete(f"/api/kbs/{kb.id}",
                                headers=auth_headers)).status_code == 204
