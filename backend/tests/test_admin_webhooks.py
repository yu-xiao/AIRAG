# backend/tests/test_admin_webhooks.py
"""M17 T5:admin webhook 端点 CRUD / rotate / test-send / 投递记录。"""
import re
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


async def _make_kb(client, headers, name) -> int:
    """KB 建行走 API(owner_id NOT NULL,裸 ORM 构造会缺列;test_kbs 模式)。"""
    resp = await client.post("/api/kbs", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ---- M18:create 扩展 ----
async def test_create_with_provider_and_kb_ids(client, db_session):
    headers = await _make_admin(client, db_session, "m18_admin1")
    kb_id = await _make_kb(client, headers, "m18kbA")
    r = await client.post("/api/admin/webhooks", json={
        "name": "wecom-ep", "url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send",
        "provider": "wecom", "kb_ids": [kb_id]}, headers=headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["provider"] == "wecom" and body["kb_ids"] == [kb_id]
    r2 = await client.get("/api/admin/webhooks", headers=headers)
    item = [e for e in r2.json() if e["name"] == "wecom-ep"][0]
    assert item["provider"] == "wecom" and item["kb_ids"] == [kb_id]


async def test_create_invalid_provider_422(client, db_session):
    headers = await _make_admin(client, db_session, "m18_admin2")
    r = await client.post("/api/admin/webhooks", json={
        "name": "bad", "url": "https://h.example.com/cb",
        "provider": "slack"}, headers=headers)
    assert r.status_code == 422


async def test_create_unknown_kb_422(client, db_session):
    headers = await _make_admin(client, db_session, "m18_admin3")
    r = await client.post("/api/admin/webhooks", json={
        "name": "kbep", "url": "https://h.example.com/cb",
        "kb_ids": [424242]}, headers=headers)
    assert r.status_code == 422
    assert "unknown kb" in r.json()["detail"]


async def test_create_url_length_422(client, db_session):
    headers = await _make_admin(client, db_session, "m18_admin4")
    r = await client.post("/api/admin/webhooks", json={
        "name": "longurl", "url": "https://h.example.com/" + "a" * 500,
        }, headers=headers)
    assert r.status_code == 422
    assert "500" in r.json()["detail"]


async def test_create_ssrf_private_422(client, db_session, monkeypatch):
    from app.services import webhook_providers as wp

    async def _priv(host):
        return ["10.0.0.1"]
    monkeypatch.setattr(wp, "_resolve_host", _priv)
    headers = await _make_admin(client, db_session, "m18_admin5")
    r = await client.post("/api/admin/webhooks", json={
        "name": "ssrf", "url": "https://internal.example.com/cb"},
        headers=headers)
    assert r.status_code == 422
    assert "SSRF" in r.json()["detail"]


async def test_create_secret_rules_per_provider(client, db_session):
    headers = await _make_admin(client, db_session, "m18_admin6")
    # dingtalk 空签名密钥:合法(不加签),secret 存空串
    r = await client.post("/api/admin/webhooks", json={
        "name": "dt", "url": "https://oapi.dingtalk.com/robot/send?access_token=t",
        "provider": "dingtalk"}, headers=headers)
    assert r.status_code == 201
    # generic 无自定义:自动生成(M17 语义)
    r = await client.post("/api/admin/webhooks", json={
        "name": "gen", "url": "https://h.example.com/g"}, headers=headers)
    assert len(r.json()["secret"]) == 32


# ---- M18:update 扩展 ----
async def test_update_description_empty_clears_to_null(client, db_session):
    headers = await _make_admin(client, db_session, "m18_admin7")
    ep = await _create_ep(client, headers, "m18clr", description="旧描述")
    r = await client.put(f"/api/admin/webhooks/{ep['id']}",
                         json={"description": ""}, headers=headers)
    assert r.status_code == 200
    assert r.json()["description"] is None


async def test_update_url_ssrf_and_length_rechecked(client, db_session,
                                                    monkeypatch):
    from app.services import webhook_providers as wp
    headers = await _make_admin(client, db_session, "m18_admin8")
    ep = await _create_ep(client, headers, "m18url")

    async def _priv(host):
        return ["192.168.0.1"]
    monkeypatch.setattr(wp, "_resolve_host", _priv)
    r = await client.put(f"/api/admin/webhooks/{ep['id']}",
                         json={"url": "https://in.example.com/x"}, headers=headers)
    assert r.status_code == 422
    r = await client.put(f"/api/admin/webhooks/{ep['id']}",
                         json={"url": "https://h.example.com/" + "b" * 500},
                         headers=headers)
    assert r.status_code == 422


async def test_update_wecom_rotate_422(client, db_session):
    headers = await _make_admin(client, db_session, "m18_admin9")
    r = await client.post("/api/admin/webhooks", json={
        "name": "wx", "url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send",
        "provider": "wecom"}, headers=headers)
    wid = r.json()["id"]
    r = await client.put(f"/api/admin/webhooks/{wid}",
                         json={"rotate_secret": True}, headers=headers)
    assert r.status_code == 422
    assert "wecom" in r.json()["detail"]


async def test_update_provider_and_kb_ids(client, db_session):
    headers = await _make_admin(client, db_session, "m18_admin10")
    kb_id = await _make_kb(client, headers, "m18kbB")
    ep = await _create_ep(client, headers, "m18upd")
    r = await client.put(f"/api/admin/webhooks/{ep['id']}", json={
        "provider": "feishu", "kb_ids": [kb_id]}, headers=headers)
    assert r.status_code == 200
    assert r.json()["provider"] == "feishu" and r.json()["kb_ids"] == [kb_id]


# ---- M18:统计聚合 ----
async def test_list_webhooks_stats_aggregation(client, db_session):
    headers = await _make_admin(client, db_session, "m18_stat1")
    ep = await _create_ep(client, headers, "statep")
    _insert_delivery(db_session, ep["id"], status="succeeded")
    _insert_delivery(db_session, ep["id"], status="succeeded")
    _insert_delivery(db_session, ep["id"], status="dead")
    await db_session.commit()
    r = await client.get("/api/admin/webhooks", headers=headers)
    item = [e for e in r.json() if e["id"] == ep["id"]][0]
    assert item["stats"]["total"] == 3
    assert item["stats"]["succeeded"] == 2
    assert item["stats"]["dead"] == 1
    assert item["stats"]["retrying"] == 0
    assert item["stats"]["last_activity_at"] is not None


async def test_list_webhooks_stats_zero_for_fresh(client, db_session):
    headers = await _make_admin(client, db_session, "m18_stat2")
    ep = await _create_ep(client, headers, "freshep")
    r = await client.get("/api/admin/webhooks", headers=headers)
    item = [e for e in r.json() if e["id"] == ep["id"]][0]
    assert item["stats"]["total"] == 0
    assert item["stats"]["last_activity_at"] is None


# ---- M18:重投 ----
async def test_redeliver_dead_resets_and_nudges(client, db_session, monkeypatch):
    from app.services import outbound
    ep = await _create_ep(client, headers := await _make_admin(
        client, db_session, "m18_redel1"), "redep")
    _insert_delivery(db_session, ep["id"], status="dead")
    await db_session.commit()
    from app.models import WebhookDelivery
    d = (await db_session.execute(select(WebhookDelivery))).scalars().one()
    d.attempts = 5
    d.last_error = "permanent 404"
    await db_session.commit()
    nudged = []
    monkeypatch.setattr(outbound, "nudge", lambda: nudged.append(1))
    r = await client.post(
        f"/api/admin/webhooks/{ep['id']}/deliveries/{d.id}/redeliver",
        headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "pending"
    await db_session.refresh(d)
    assert d.status == "pending" and d.attempts == 0
    assert d.last_error is None and d.next_attempt_at is None
    assert nudged == [1]


async def test_redeliver_succeeded_422(client, db_session):
    headers = await _make_admin(client, db_session, "m18_redel2")
    ep = await _create_ep(client, headers, "okp")
    _insert_delivery(db_session, ep["id"], status="succeeded")
    await db_session.commit()
    from app.models import WebhookDelivery
    d = (await db_session.execute(select(WebhookDelivery))).scalars().one()
    r = await client.post(
        f"/api/admin/webhooks/{ep['id']}/deliveries/{d.id}/redeliver",
        headers=headers)
    assert r.status_code == 422


async def test_redeliver_cross_endpoint_404(client, db_session):
    headers = await _make_admin(client, db_session, "m18_redel3")
    ep1 = await _create_ep(client, headers, "ep1x")
    ep2 = await _create_ep(client, headers, "ep2x")
    _insert_delivery(db_session, ep1["id"], status="dead")
    await db_session.commit()
    from app.models import WebhookDelivery
    d = (await db_session.execute(select(WebhookDelivery))).scalars().one()
    r = await client.post(
        f"/api/admin/webhooks/{ep2['id']}/deliveries/{d.id}/redeliver",
        headers=headers)
    assert r.status_code == 404


async def test_redeliver_non_admin_403(client, db_session):
    other = await _register_and_login(client, "m18_redel4")
    r = await client.post("/api/admin/webhooks/1/deliveries/1/redeliver",
                          headers=other)
    assert r.status_code == 403


# ---- M19 T2:secret 卫生(wecom 占位不回显 / 切换重置 / 轮换限 generic)----
async def _get_ep(client, headers, ep_id) -> dict:
    r = await client.get("/api/admin/webhooks", headers=headers)
    return [e for e in r.json() if e["id"] == ep_id][0]


async def test_wecom_create_no_secret_echo(client, db_session):
    """wecom 创建:secret=None(键存在值为 None)、masked 空、全文无占位 hex。"""
    headers = await _make_admin(client, db_session, "m19_wh_wecom1")
    r = await client.post("/api/admin/webhooks", json={
        "name": "wx1", "url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send",
        "events": ["document.done"], "provider": "wecom"}, headers=headers)
    assert r.status_code == 201, r.text
    assert "secret" in r.json()
    assert r.json()["secret"] is None  # 占位密钥永不下发
    assert re.search(r"[0-9a-f]{32}", r.text) is None  # 响应无 32 位 hex

    r = await client.get("/api/admin/webhooks", headers=headers)
    item = [e for e in r.json() if e["name"] == "wx1"][0]
    assert item["secret_masked"] == ""  # 空=「无需密钥」,不伪装有密钥


async def test_wecom_masked_empty_platform_not(client, db_session):
    """对照组:wecom masked 空串;dingtalk masked 非空。"""
    headers = await _make_admin(client, db_session, "m19_wh_mask2")
    await _create_ep(client, headers, "wx2", provider="wecom")
    await _create_ep(client, headers, "dt2", provider="dingtalk",
                     secret="dingtalk-sign-secret-0123456789")
    r = await client.get("/api/admin/webhooks", headers=headers)
    by_name = {e["name"]: e for e in r.json()}
    assert by_name["wx2"]["secret_masked"] == ""
    assert by_name["dt2"]["secret_masked"] != ""


async def test_provider_switch_resets_secret(client, db_session):
    """generic→wecom:masked 空、无明文字段;wecom→generic:新明文一次性。"""
    headers = await _make_admin(client, db_session, "m19_wh_sw3")
    ep = await _create_ep(client, headers, "sw3")  # generic,S1 明文
    s1 = ep["secret"]

    r = await client.put(f"/api/admin/webhooks/{ep['id']}",
                         json={"provider": "wecom"}, headers=headers)
    assert r.status_code == 200
    assert "secret" not in r.json()  # WebhookOut 形态,无明文字段
    assert r.json()["secret_masked"] == ""

    r = await client.put(f"/api/admin/webhooks/{ep['id']}",
                         json={"provider": "generic"}, headers=headers)
    assert r.status_code == 200
    s2 = r.json()["secret"]  # 新明文仅此一次
    assert s2 and s2 != s1 and len(s2) == 32

    item = await _get_ep(client, headers, ep["id"])
    assert item["secret_masked"] == f"wh_****{s2[-4:]}" != ""


async def test_switch_to_platform_takes_im_secret(client, db_session):
    """切换到平台通道:im_secret 即新加签密钥;不带=清空(不加签)。"""
    headers = await _make_admin(client, db_session, "m19_wh_im4")
    ep = await _create_ep(client, headers, "im4")  # generic
    r = await client.put(f"/api/admin/webhooks/{ep['id']}", json={
        "provider": "dingtalk", "im_secret": "NEWHOOK"}, headers=headers)
    assert r.status_code == 200
    assert (await _get_ep(client, headers, ep["id"]))["im_secret_set"] is True

    r = await client.put(f"/api/admin/webhooks/{ep['id']}",
                         json={"provider": "feishu"}, headers=headers)
    assert r.status_code == 200
    assert (await _get_ep(client, headers, ep["id"]))["im_secret_set"] is False


async def test_update_im_secret_length_capped_to_column(client, db_session):
    """im_secret 上限对齐 secret 列 String(64):65 字符 422(而非 asyncpg
    StringDataRightTruncation → 500);恰 64 字符可入列。"""
    headers = await _make_admin(client, db_session, "m19_wh_imlen9")
    ep = await _create_ep(client, headers, "imlen9")
    r = await client.put(f"/api/admin/webhooks/{ep['id']}", json={
        "provider": "dingtalk", "im_secret": "x" * 65}, headers=headers)
    assert r.status_code == 422
    # 编辑态(不切 provider)同样拦在 pydantic 层
    ep2 = await _create_ep(client, headers, "imlen9b", provider="feishu",
                           secret="feishu-sign-secret-0123456789abcdef")
    r = await client.put(f"/api/admin/webhooks/{ep2['id']}", json={
        "im_secret": "x" * 65}, headers=headers)
    assert r.status_code == 422
    # 边界:恰 64 字符过校验、入列
    r = await client.put(f"/api/admin/webhooks/{ep['id']}", json={
        "provider": "dingtalk", "im_secret": "x" * 64}, headers=headers)
    assert r.status_code == 200, r.text
    assert (await _get_ep(client, headers, ep["id"]))["im_secret_set"] is True


async def test_wecom_switch_carries_no_placeholder(client, db_session):
    """wecom→dingtalk 不带 im_secret:占位 hex 不得结转进平台密钥位。"""
    headers = await _make_admin(client, db_session, "m19_wh_noph5")
    ep = await _create_ep(client, headers, "wx5", provider="wecom")
    r = await client.put(f"/api/admin/webhooks/{ep['id']}",
                         json={"provider": "dingtalk"}, headers=headers)
    assert r.status_code == 200
    assert (await _get_ep(client, headers, ep["id"]))["im_secret_set"] is False


async def test_rotate_rejected_for_platforms(client, db_session):
    """rotate 只属 generic(平台密钥来自 IM 后台,不可随机生成)。"""
    headers = await _make_admin(client, db_session, "m19_wh_rot6")
    wx = await _create_ep(client, headers, "wx6", provider="wecom")
    dt = await _create_ep(client, headers, "dt6", provider="dingtalk")
    for ep in (wx, dt):
        r = await client.put(f"/api/admin/webhooks/{ep['id']}",
                             json={"rotate_secret": True}, headers=headers)
        assert r.status_code == 422
        assert "rotate_secret not supported" in r.json()["detail"]
    gen = await _create_ep(client, headers, "gen6")
    r = await client.put(f"/api/admin/webhooks/{gen['id']}",
                         json={"rotate_secret": True}, headers=headers)
    assert r.status_code == 200  # 回归锁:generic rotate 明文一次
    assert r.json()["secret"] and len(r.json()["secret"]) == 32


async def test_provider_reset_audited(client, db_session):
    """provider 切换的审计 detail 记 provider_reset=old->new(JSON 子串)。"""
    headers = await _make_admin(client, db_session, "m19_wh_aud7")
    ep = await _create_ep(client, headers, "aud7")
    r = await client.put(f"/api/admin/webhooks/{ep['id']}",
                         json={"provider": "wecom"}, headers=headers)
    assert r.status_code == 200
    r = await client.get("/api/admin/audit-logs?action=webhook_update",
                         headers=headers)
    assert r.status_code == 200
    hits = [i for i in r.json()["items"]
            if "provider_reset" in (i["detail"] or "")]
    assert hits, r.json()
    assert "generic->wecom" in hits[0]["detail"]


async def test_create_generic_secret_once_unchanged(client, db_session):
    """语义锁(M17 不回退):generic create 明文一次,GET 只 masked。"""
    headers = await _make_admin(client, db_session, "m19_wh_lock8")
    ep = await _create_ep(client, headers, "lock8")
    assert ep["secret"] and len(ep["secret"]) == 32
    r = await client.get("/api/admin/webhooks", headers=headers)
    item = [e for e in r.json() if e["id"] == ep["id"]][0]
    assert item["secret_masked"] == f"wh_****{ep['secret'][-4:]}"
    assert ep["secret"] not in r.text
