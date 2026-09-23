# backend/tests/test_admin_webhooks.py
"""M17 T5:admin webhook 端点 CRUD / rotate / test-send / 投递记录。"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app.models import WebhookDelivery


def _now() -> datetime:
    """naive UTC(与 outbound._utcnow_naive 同源,asyncpg 拒 aware 入参)。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _register_and_login(client, username):
    await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"})
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _make_admin(client, db_session, username):
    """注册 → SQL 提权 → 返回该 admin 的请求头(照 test_eval_questions 模式)。"""
    headers = await _register_and_login(client, username)
    me = await client.get("/api/auth/me", headers=headers)
    uid = me.json()["id"]
    await db_session.execute(
        text("UPDATE users SET role='admin' WHERE id=:i"), {"i": uid})
    await db_session.commit()
    return headers


async def _create_ep(client, headers, name, **extra) -> dict:
    body = {"name": name, "url": "https://hooks.example.com/cb",
            "events": ["document.done"]} | extra
    resp = await client.post("/api/admin/webhooks", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _insert_delivery(db_session, endpoint_id, event_type="document.done",
                     status="succeeded") -> None:
    db_session.add(WebhookDelivery(
        endpoint_id=endpoint_id, event_type=event_type,
        event_id=uuid.uuid4().hex,
        payload={"event_id": uuid.uuid4().hex, "event_type": event_type,
                 "occurred_at": _now().isoformat(), "data": {}},
        status=status, attempts=1, response_status=200,
        next_attempt_at=(_now() + timedelta(minutes=5)
                         if status == "retrying" else None),
    ))


# ---- 1. 创建 + 列表:明文只此一次,列表 masked ----
async def test_admin_create_and_list_masks_secret(client, db_session):
    headers = await _make_admin(client, db_session, "m17_wh_admin1")
    r = await client.post("/api/admin/webhooks", json={
        "name": "ep1", "url": "https://hooks.example.com/cb",
        "events": ["document.done"]}, headers=headers)
    assert r.status_code == 201, r.text
    secret = r.json()["secret"]
    assert secret and not secret.startswith("wh_****")  # 明文,非 masked
    assert r.json()["events"] == ["document.done"]      # events 回显

    r = await client.get("/api/admin/webhooks", headers=headers)
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    assert items[0]["secret_masked"] == f"wh_****{secret[-4:]}"
    assert "secret" not in items[0]
    assert secret not in r.text  # 列表响应绝不泄露明文


# ---- 2. secret 策略:缺省 token_hex(16);显式传入则用之 ----
async def test_create_auto_secret_and_custom(client, db_session):
    headers = await _make_admin(client, db_session, "m17_wh_admin2")
    r = await client.post("/api/admin/webhooks", json={
        "name": "auto", "url": "https://hooks.example.com/a"}, headers=headers)
    assert r.status_code == 201
    assert len(r.json()["secret"]) == 32  # token_hex(16) → 32 字符

    custom = "wh_customsecret12345"
    r = await client.post("/api/admin/webhooks", json={
        "name": "custom", "url": "https://hooks.example.com/b",
        "secret": custom}, headers=headers)
    assert r.status_code == 201
    assert r.json()["secret"] == custom
    assert r.json()["secret_masked"] == "wh_****2345"


# ---- 3. 创建校验:重名 409;非法 event 422;非 http(s) url 422 ----
async def test_create_validates(client, db_session):
    headers = await _make_admin(client, db_session, "m17_wh_admin3")
    await _create_ep(client, headers, "dup")
    r = await client.post("/api/admin/webhooks", json={
        "name": "dup", "url": "https://hooks.example.com/c"}, headers=headers)
    assert r.status_code == 409

    r = await client.post("/api/admin/webhooks", json={
        "name": "ok1", "url": "https://hooks.example.com/c",
        "events": ["document.done", "bogus.event"]}, headers=headers)
    assert r.status_code == 422

    r = await client.post("/api/admin/webhooks", json={
        "name": "ok2", "url": "ftp://x"}, headers=headers)
    assert r.status_code == 422


# ---- 4. 权限矩阵:viewer 403;无 token 401 ----
async def test_non_admin_forbidden(client, db_session):
    headers = await _make_admin(client, db_session, "m17_wh_admin4")
    ep = await _create_ep(client, headers, "ep4")
    viewer = await _register_and_login(client, "m17_wh_viewer4")
    r = await client.post("/api/admin/webhooks", json={
        "name": "x", "url": "https://hooks.example.com/x"}, headers=viewer)
    assert r.status_code == 403
    r = await client.delete(f"/api/admin/webhooks/{ep['id']}", headers=viewer)
    assert r.status_code == 403
    r = await client.get("/api/admin/webhook-deliveries", headers=viewer)
    assert r.status_code == 403

    r = await client.get("/api/admin/webhooks")  # 无 token
    assert r.status_code == 401


# ---- 5. 更新 + 轮换:普通 PUT 回显 masked;rotate 回明文一次 ----
async def test_update_and_rotate(client, db_session):
    headers = await _make_admin(client, db_session, "m17_wh_admin5")
    ep = await _create_ep(client, headers, "ep5")
    old_secret = ep["secret"]

    r = await client.put(f"/api/admin/webhooks/{ep['id']}",
                         json={"enabled": False}, headers=headers)
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    assert r.json()["secret_masked"] == f"wh_****{old_secret[-4:]}"

    r = await client.put(f"/api/admin/webhooks/{ep['id']}",
                         json={"rotate_secret": True}, headers=headers)
    assert r.status_code == 200
    new_secret = r.json()["secret"]
    assert new_secret and new_secret != old_secret  # 新明文,仅此一次

    r = await client.get("/api/admin/webhooks", headers=headers)
    assert r.json()[0]["secret_masked"] == f"wh_****{new_secret[-4:]}"
    assert new_secret not in r.text

    # PUT 不接受明文写回:WebhookUpdateIn 无 secret 字段,pydantic 默认
    # 忽略多余键 → 200 且 masked 不变(选择「忽略」而非 422,docstring 注明)。
    r = await client.put(f"/api/admin/webhooks/{ep['id']}",
                         json={"secret": "wh_newplainsecret99"},
                         headers=headers)
    assert r.status_code == 200
    assert r.json()["secret_masked"] == f"wh_****{new_secret[-4:]}"
    r = await client.get("/api/admin/webhooks", headers=headers)
    assert r.json()[0]["secret_masked"] == f"wh_****{new_secret[-4:]}"


# ---- 6. 删除:204,投递行 FK 级联清空 ----
async def test_delete_cascades_deliveries(client, db_session):
    headers = await _make_admin(client, db_session, "m17_wh_admin6")
    ep = await _create_ep(client, headers, "ep6")
    _insert_delivery(db_session, ep["id"], status="succeeded")
    _insert_delivery(db_session, ep["id"], status="retrying")
    await db_session.commit()

    r = await client.delete(f"/api/admin/webhooks/{ep['id']}", headers=headers)
    assert r.status_code == 204

    r = await client.get("/api/admin/webhooks", headers=headers)
    assert r.json() == []
    r = await client.get(
        f"/api/admin/webhook-deliveries?endpoint_id={ep['id']}", headers=headers)
    assert r.status_code == 200
    assert r.json()["total"] == 0

    r = await client.delete(f"/api/admin/webhooks/{ep['id']}", headers=headers)
    assert r.status_code == 404  # 重复删 → 404


# ---- 7. test-send:内联同步单发,patch deliver_one ----
async def test_test_send_inline(client, db_session, monkeypatch):
    headers = await _make_admin(client, db_session, "m17_wh_admin7")
    ep = await _create_ep(client, headers, "ep7")

    async def fake_deliver_one(db, delivery, client=None):
        delivery.status = "succeeded"
        delivery.response_status = 200
        await db.commit()

    monkeypatch.setattr("app.api.admin.deliver_one", fake_deliver_one)
    r = await client.post(f"/api/admin/webhooks/{ep['id']}/test", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "succeeded", "response_status": 200,
                        "error": None}

    rows = (await db_session.execute(
        select(WebhookDelivery).where(
            WebhookDelivery.endpoint_id == ep["id"]))).scalars().all()
    assert len(rows) == 1 and rows[0].event_type == "test"  # test 行落库

    r = await client.put(f"/api/admin/webhooks/{ep['id']}",
                         json={"enabled": False}, headers=headers)
    assert r.status_code == 200
    r = await client.post(f"/api/admin/webhooks/{ep['id']}/test", headers=headers)
    assert r.status_code == 400  # 禁用端点拒发


# ---- 8. 投递记录:过滤/分页/endpoint_name 联查 ----
async def test_deliveries_list_filter_page(client, db_session):
    headers = await _make_admin(client, db_session, "m17_wh_admin8")
    ep_a = await _create_ep(client, headers, "ep8a")
    ep_b = await _create_ep(client, headers, "ep8b")
    _insert_delivery(db_session, ep_a["id"], event_type="document.done",
                     status="succeeded")
    _insert_delivery(db_session, ep_a["id"], event_type="eval.completed",
                     status="retrying")
    _insert_delivery(db_session, ep_b["id"], event_type="chat.refused",
                     status="dead")
    await db_session.commit()

    r = await client.get("/api/admin/webhook-deliveries", headers=headers)
    assert r.status_code == 200
    assert r.json()["total"] == 3
    assert len(r.json()["items"]) == 3
    names = {i["endpoint_name"] for i in r.json()["items"]}
    assert names == {"ep8a", "ep8b"}  # endpoint_name 联查
    ids = [i["id"] for i in r.json()["items"]]
    assert ids == sorted(ids, reverse=True)  # id desc

    r = await client.get(
        "/api/admin/webhook-deliveries?status=retrying", headers=headers)
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["event_type"] == "eval.completed"

    r = await client.get(
        f"/api/admin/webhook-deliveries?endpoint_id={ep_b['id']}",
        headers=headers)
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["endpoint_name"] == "ep8b"

    r = await client.get(
        "/api/admin/webhook-deliveries?page_size=2", headers=headers)
    assert r.json()["total"] == 3
    assert len(r.json()["items"]) == 2

    r = await client.get(
        "/api/admin/webhook-deliveries?event_type=document.done",
        headers=headers)
    assert r.json()["total"] == 1
