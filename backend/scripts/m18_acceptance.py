"""M18 无头验收(真栈:8001 + worker + beat)。

前置:worker/beat 必须 start_worker.bat / start_beat.bat 已起;.env 须含
WEBHOOK_SSRF_ALLOWLIST=127.0.0.1(本地 mock 接收器在回环上,白名单放行);
后端 8001;迁移已在 b0c1d2e3f4a5。

覆盖:平台适配器全链路(wecom markdown 体 / 钉钉 URL 加签 / 飞书 body 加签,
平台面无 X-AIRag-Signature 头)/ 平台 body 错误码分类(93000 立即 dead、
45009 retrying 退避)/ per-KB 订阅过滤 / dead→改 URL→重投 / SSRF 私网 422 +
回环白名单 / 统计对账 + 校验杂项 + 权限负例 / 清理。

本地 receiver 按路径回平台语义:/wxok 200 errcode=0;/wxperm 200 errcode=
93000;/wxtrans 200 errcode=45009;/dtsign 记完整 URL(含 query)与 body;
/fssign 记 body;/ok 200 空体;/reject 404。

chat.refused 真栈链路 M17 已验收,M18 不重复(计划零 SKIP)。平台测试端点
events 固定 ["document.done"]:test 发送绕过订阅过滤不受影响,同时把 ⑥ 的
document.failed 噪声隔离在 per-KB 端点上,统计对账才确定。
"""
import asyncio
import base64
import hashlib
import hmac
import http.server
import json
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

# `python scripts/m18_acceptance.py` 直跑时 sys.path[0] 是 scripts 目录;
# app 是 editable 安装可导入,但 scripts.* 不是——补 backend 根(m17 同款)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8001"
API = f"{BASE}/api"
TIMEOUT = httpx.Timeout(120.0)
RESULTS = {"pass": [], "fail": [], "skip": []}

SUFFIX = uuid.uuid4().hex[:6]

# ---- 本地 receiver:daemon 线程 + 模块级收包表(锁保护并发写/读) ----
RECEIVED: list[dict] = []
LOCK = threading.Lock()

# 平台语义应答表(路径 → HTTP 码, body);路径按 query 前部分匹配
_RX_REPLY = {
    "/wxok": (200, b'{"errcode": 0}'),
    "/wxperm": (200, b'{"errcode": 93000}'),
    "/wxtrans": (200, b'{"errcode": 45009}'),
    "/dtsign": (200, b'{"errcode": 0}'),
    "/fssign": (200, b'{"code": 0}'),
    "/ok": (200, b""),
    "/reject": (404, b""),
}


class _RxHandler(http.server.BaseHTTPRequestHandler):
    """收包存 {"path"(含 query),"headers","body"};按路径回平台语义。"""

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        with LOCK:
            RECEIVED.append({"path": self.path, "headers": dict(self.headers),
                             "body": body})
        code, out = _RX_REPLY.get(self.path.split("?", 1)[0], (200, b""))
        self.send_response(code)
        self.send_header("Content-Length", str(len(out)))
        if out:
            self.send_header("Content-Type", "application/json")
        self.end_headers()
        if out:
            self.wfile.write(out)

    def log_message(self, *a):  # 安静:不污染验收输出
        pass


def start_receiver() -> http.server.ThreadingHTTPServer:
    """127.0.0.1 随机端口起 receiver(daemon 线程,随脚本退出)。"""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RxHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def packets() -> list[dict]:
    with LOCK:
        return list(RECEIVED)


def wait_for(pred, timeout_s: float = 30.0):
    """轮询收包表(0.5s 步进)直到谓词命中;超时返回 None。"""
    deadline = time.perf_counter() + timeout_s
    while True:
        got = pred()
        if got:
            return got
        if time.perf_counter() > deadline:
            return None
        time.sleep(0.5)


def _hdr(headers: dict, name: str) -> str:
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return ""


def _pkt_json(p: dict):
    try:
        return json.loads(p["body"].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def wait_packet(path: str, timeout_s: float = 10.0):
    """等首个命中路径(query 前部分相等)的收包;附带 "json" 解析结果。"""
    def pred():
        for p in packets():
            if p["path"].split("?", 1)[0] == path:
                out = dict(p)
                j = _pkt_json(p)
                if j is not None:
                    out["json"] = j
                return out
        return None
    return wait_for(pred, timeout_s)


def verify_sig(pkt: dict, secret: str) -> bool:
    """generic 面签名复算(M17 公式):hex(HMAC-SHA256(secret, f"{ts}.{body}"))。"""
    ts, sig = _hdr(pkt["headers"], "X-AIRag-Timestamp"), \
        _hdr(pkt["headers"], "X-AIRag-Signature")
    if not ts or not sig:
        return False
    expect = hmac.new(
        secret.encode(), f"{ts}.{pkt['body'].decode('utf-8')}".encode(),
        hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expect)


def sign_platform(secret: str, ts: str) -> str:
    """钉钉/飞书同构加签:base64(HMAC-SHA256(key=f"{ts}\\n{secret}", msg=b""))。"""
    digest = hmac.new(f"{ts}\n{secret}".encode(), b"", hashlib.sha256)
    return base64.b64encode(digest.digest()).decode()


def check(name, cond, detail=""):
    (RESULTS["pass"] if cond else RESULTS["fail"]).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + (f"  {detail}" if detail and not cond else ""))


def summary_and_exit():
    total = sum(len(v) for v in RESULTS.values())
    print(f"\nM18 ACCEPTANCE: {len(RESULTS['pass'])}/{total} PASS")
    if RESULTS["skip"]:
        print("SKIP:", *RESULTS["skip"], sep="\n  - ")
    if RESULTS["fail"]:
        print("FAILED:", *RESULTS["fail"], sep="\n  - ")
        sys.exit(1)


def _nullpool_sessionmaker():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core.config import settings

    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def promote_roles(usernames: dict[str, str]) -> None:
    from sqlalchemy import text

    engine, maker = _nullpool_sessionmaker()
    try:
        async with maker() as s:
            for username, role in usernames.items():
                await s.execute(
                    text("UPDATE users SET role = :r WHERE username = :u"),
                    {"r": role, "u": username},
                )
            await s.commit()
    finally:
        await engine.dispose()


async def cleanup(user_ids, kb_ids, usernames, endpoint_ids) -> None:
    from sqlalchemy import text

    engine, maker = _nullpool_sessionmaker()
    try:
        async with maker() as s:
            for uid in user_ids:  # 临时用户的 key(无 DELETE /api/keys 路由)
                await s.execute(
                    text("DELETE FROM api_keys WHERE user_id = :u"), {"u": uid})
            for kid in kb_ids:
                await s.execute(
                    text("DELETE FROM chunks WHERE kb_id = :k"), {"k": kid})
                await s.execute(
                    text("DELETE FROM documents WHERE kb_id = :k"), {"k": kid})
                await s.execute(
                    text("DELETE FROM kb_permissions WHERE kb_id = :k"),
                    {"k": kid})
                await s.execute(
                    text("DELETE FROM knowledge_bases WHERE id = :k"),
                    {"k": kid})
            for eid in endpoint_ids:  # API 删除失败时兜底;投递行 FK 级联
                await s.execute(
                    text("DELETE FROM webhook_endpoints WHERE id = :e"),
                    {"e": eid})
            for name in usernames:
                await s.execute(
                    text("DELETE FROM users WHERE username = :n"), {"n": name})
            await s.commit()
    finally:
        await engine.dispose()


async def make_user(c: httpx.AsyncClient, name: str, role: str) -> tuple[str, int]:
    r = await c.post(f"{API}/auth/register",
                     json={"username": name, "password": "secret123"})
    r.raise_for_status()
    await promote_roles({name: role})
    return name, r.json()["id"]


async def login(c: httpx.AsyncClient, username: str) -> dict:
    r = await c.post(f"{API}/auth/login",
                     json={"username": username, "password": "secret123"})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def wait_delivery(c: httpx.AsyncClient, jwt: dict, endpoint_id: int,
                        event_type: str, want: str = "succeeded",
                        timeout_s: float = 60.0):
    """轮询投递记录(服务端按 status=want 过滤)直到出现命中行;超时 None。"""
    deadline = time.perf_counter() + timeout_s
    while True:
        r = await c.get(f"{API}/admin/webhook-deliveries",
                        params={"endpoint_id": endpoint_id,
                                "event_type": event_type, "status": want,
                                "page_size": 50},
                        headers=jwt)
        items = r.json().get("items") or []
        if items or time.perf_counter() > deadline:
            return items[0] if items else None
        await asyncio.sleep(2)


async def wait_doc_status(c: httpx.AsyncClient, jwt: dict, doc_id: int,
                          want: str, timeout_s: int = 180):
    deadline = time.perf_counter() + timeout_s
    while True:
        r = await c.get(f"{API}/documents/{doc_id}", headers=jwt)
        st = r.json().get("status")
        if st == want or time.perf_counter() > deadline:
            return st
        await asyncio.sleep(2)


async def main() -> None:
    user_ids, kb_ids, usernames, endpoint_ids = [], [], [], []
    ledger: dict[int, int] = {}  # endpoint_id → 脚本累计投递行数(⑨ 对账用)
    srv = start_receiver()
    port = srv.server_address[1]
    print(f"[info] receiver on 127.0.0.1:{port}")
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        try:
            # ① admin 登录(m17 模式:栈上既有 admin 账号)+ 非 admin 负例账号
            admin = await login(c, "admin")
            check("admin login ok", bool(admin.get("Authorization")))
            v_name, v_id = await make_user(c, f"m18_{SUFFIX}_viewer", "viewer")
            usernames.append(v_name)
            user_ids.append(v_id)

            async def new_ep(name: str, path: str, events: list[str] | None = None,
                             **extra) -> httpx.Response:
                body = {"name": name, "url": f"http://127.0.0.1:{port}{path}",
                        "events": events if events is not None
                        else ["document.done"]} | extra
                return await c.post(f"{API}/admin/webhooks", json=body,
                                    headers=admin)

            # ② wecom 全链路:markdown 体 / 无签名头 / 投递行 succeeded
            r = await new_ep(f"m18wx{SUFFIX}", "/wxok", provider="wecom")
            check("wecom endpoint create 201 with one-time secret",
                  r.status_code == 201 and bool(r.json().get("secret")),
                  f"HTTP {r.status_code} {r.text[:200]}")
            r.raise_for_status()
            ep_wx = r.json()["id"]
            endpoint_ids.append(ep_wx)
            ledger[ep_wx] = 0
            r = await c.post(f"{API}/admin/webhooks/{ep_wx}/test", headers=admin)
            check("wecom test inline succeeded (http 200)",
                  r.status_code == 200 and r.json().get("status") == "succeeded"
                  and r.json().get("response_status") == 200, r.text[:200])
            ledger[ep_wx] += 1
            pkt = wait_packet("/wxok", 10)
            j = (pkt or {}).get("json") or {}
            check("wecom packet msgtype=markdown content has 测试消息",
                  pkt is not None and j.get("msgtype") == "markdown"
                  and "测试消息" in ((j.get("markdown") or {}).get("content")
                                     or ""),
                  str(pkt and pkt.get("json")))
            check("wecom packet no X-AIRag-Signature (X-AIRag-Event kept)",
                  pkt is not None
                  and _hdr(pkt["headers"], "X-AIRag-Signature") == ""
                  and _hdr(pkt["headers"], "X-AIRag-Event") == "test",
                  str(pkt and pkt["headers"]))
            row = await wait_delivery(c, admin, ep_wx, "test", timeout_s=30)
            check("wecom delivery row succeeded",
                  row is not None and row.get("response_status") == 200,
                  str(row))

            # ③ 平台错误码分类:两包均 HTTP 200,body 码决定命运
            r = await new_ep(f"m18wxperm{SUFFIX}", "/wxperm", provider="wecom")
            r.raise_for_status()
            ep_perm = r.json()["id"]
            endpoint_ids.append(ep_perm)
            ledger[ep_perm] = 0
            r = await c.post(f"{API}/admin/webhooks/{ep_perm}/test",
                             headers=admin)
            check("wxperm(93000) inline dead",
                  r.status_code == 200 and r.json().get("status") == "dead"
                  and "93000" in (r.json().get("error") or ""), r.text[:200])
            ledger[ep_perm] += 1
            row = await wait_delivery(c, admin, ep_perm, "test", want="dead",
                                      timeout_s=30)
            check("wxperm delivery dead immediately (http200, last_error 93000)",
                  row is not None and row.get("response_status") == 200
                  and "93000" in (row.get("last_error") or "")
                  and row.get("attempts") == 1 and not row.get("next_attempt_at"),
                  str(row))

            r = await new_ep(f"m18wxtrans{SUFFIX}", "/wxtrans", provider="wecom")
            r.raise_for_status()
            ep_trans = r.json()["id"]
            endpoint_ids.append(ep_trans)
            ledger[ep_trans] = 0
            r = await c.post(f"{API}/admin/webhooks/{ep_trans}/test",
                             headers=admin)
            check("wxtrans(45009) inline retrying",
                  r.status_code == 200 and r.json().get("status") == "retrying"
                  and "45009" in (r.json().get("error") or ""), r.text[:200])
            ledger[ep_trans] += 1
            row = await wait_delivery(c, admin, ep_trans, "test",
                                      want="retrying", timeout_s=30)
            check("wxtrans delivery retrying (attempts=1, next_attempt_at set)",
                  row is not None and row.get("attempts") == 1
                  and row.get("next_attempt_at") is not None
                  and row.get("response_status") == 200
                  and "45009" in (row.get("last_error") or ""), str(row))

            # ④ 钉钉加签:URL 带 timestamp(毫秒)+sign(query percent 编码)
            dt_secret = "m18dtsecret0123456"  # ≥16 字符(schema 校验)
            r = await new_ep(f"m18dt{SUFFIX}", "/dtsign", provider="dingtalk",
                             secret=dt_secret)
            check("dingtalk endpoint create 201 (secret echoed once)",
                  r.status_code == 201 and r.json().get("secret") == dt_secret,
                  f"HTTP {r.status_code} {r.text[:200]}")
            r.raise_for_status()
            ep_dt = r.json()["id"]
            endpoint_ids.append(ep_dt)
            ledger[ep_dt] = 0
            r = await c.post(f"{API}/admin/webhooks/{ep_dt}/test", headers=admin)
            check("dingtalk test inline succeeded",
                  r.status_code == 200 and r.json().get("status") == "succeeded",
                  r.text[:200])
            ledger[ep_dt] += 1
            pkt = wait_packet("/dtsign", 10)

            def _verify_dt(p) -> bool:
                if p is None:
                    return False
                q = parse_qs(urlparse(p["path"]).query)
                ts = (q.get("timestamp") or [""])[0]
                sign = (q.get("sign") or [""])[0]  # parse_qs 已 unquote
                if not ts.isdigit() or len(ts) != 13:  # 毫秒
                    return False
                if abs(int(ts) - int(time.time() * 1000)) > 60_000:
                    return False
                return hmac.compare_digest(sign, sign_platform(dt_secret, ts))

            check("dingtalk URL timestamp(ms)+sign verified",
                  _verify_dt(pkt), str(pkt and pkt["path"]))
            j = (pkt or {}).get("json") or {}
            check("dingtalk body markdown with title",
                  pkt is not None and j.get("msgtype") == "markdown"
                  and bool((j.get("markdown") or {}).get("title")),
                  str(pkt and pkt.get("json")))

            # ⑤ 飞书加签:body 带 timestamp(秒)+sign,msg_type=text
            fs_secret = "m18fssecret01234567"
            r = await new_ep(f"m18fs{SUFFIX}", "/fssign", provider="feishu",
                             secret=fs_secret)
            check("feishu endpoint create 201 (secret echoed once)",
                  r.status_code == 201 and r.json().get("secret") == fs_secret,
                  f"HTTP {r.status_code} {r.text[:200]}")
            r.raise_for_status()
            ep_fs = r.json()["id"]
            endpoint_ids.append(ep_fs)
            ledger[ep_fs] = 0
            r = await c.post(f"{API}/admin/webhooks/{ep_fs}/test", headers=admin)
            check("feishu test inline succeeded",
                  r.status_code == 200 and r.json().get("status") == "succeeded",
                  r.text[:200])
            ledger[ep_fs] += 1
            pkt = wait_packet("/fssign", 10)
            j = (pkt or {}).get("json") or {}
            ts = str(j.get("timestamp") or "")
            sign = str(j.get("sign") or "")
            check("feishu body msg_type=text",
                  pkt is not None and j.get("msg_type") == "text",
                  str(pkt and pkt.get("json")))
            check("feishu body timestamp(s)+sign verified",
                  ts.isdigit() and len(ts) == 10
                  and abs(int(ts) - int(time.time())) <= 60
                  and hmac.compare_digest(sign, sign_platform(fs_secret, ts)),
                  f"ts={ts} sign={sign}")

            # ⑥ per-KB:KB甲命中 / KB乙 零投递(损坏 PDF 触发 document.failed)
            ra = await c.post(f"{API}/kbs",
                              json={"name": f"m18验收甲{SUFFIX}"},
                              headers=admin)
            rb = await c.post(f"{API}/kbs",
                              json={"name": f"m18验收乙{SUFFIX}"},
                              headers=admin)
            check("KB甲/KB乙 created 201",
                  ra.status_code == 201 and rb.status_code == 201,
                  f"{ra.status_code}/{rb.status_code}")
            ra.raise_for_status()
            rb.raise_for_status()
            kb_a, kb_b = ra.json()["id"], rb.json()["id"]
            kb_ids.extend([kb_a, kb_b])
            r = await new_ep(f"m18kb{SUFFIX}", "/ok",
                             events=["document.failed"], kb_ids=[kb_a])
            check("per-KB endpoint create 201 (kb_ids=[KB甲])",
                  r.status_code == 201 and r.json().get("kb_ids") == [kb_a],
                  f"HTTP {r.status_code} {r.text[:200]}")
            r.raise_for_status()
            ep_kb = r.json()["id"]
            endpoint_ids.append(ep_kb)
            ledger[ep_kb] = 0
            gen_secret = r.json()["secret"]  # generic 自动 secret(明文仅此一次)

            bad_pdf = b"%PDF-1.4\n%\xe5\x9e\x8a bytes that are not a valid pdf"
            da = await c.post(f"{API}/kbs/{kb_a}/documents", files={"file": (
                "bad_a.pdf", bad_pdf, "application/pdf")}, headers=admin)
            db_ = await c.post(f"{API}/kbs/{kb_b}/documents", files={"file": (
                "bad_b.pdf", bad_pdf, "application/pdf")}, headers=admin)
            check("corrupt pdfs uploaded 201 (both KBs)",
                  da.status_code == 201 and db_.status_code == 201,
                  f"{da.status_code}/{db_.status_code}")
            da.raise_for_status()
            db_.raise_for_status()
            sta = await wait_doc_status(c, admin, da.json()["id"], "failed")
            stb = await wait_doc_status(c, admin, db_.json()["id"], "failed")
            check("corrupt pdfs status failed (both KBs)",
                  sta == "failed" and stb == "failed", f"{sta}/{stb}")
            row = await wait_delivery(c, admin, ep_kb, "document.failed",
                                      timeout_s=90)
            got_kb = ((row or {}).get("payload") or {}).get(
                "data", {}).get("document", {}).get("kb_id")
            check("KB甲 document.failed delivery succeeded",
                  row is not None and got_kb == kb_a, str(row))
            if row is not None:
                ledger[ep_kb] += 1
            pkt = wait_packet("/ok", 15)
            check("per-KB generic packet hmac verified",
                  pkt is not None and verify_sig(pkt, gen_secret),
                  "no /ok packet or sig mismatch")
            r = await c.get(f"{API}/admin/webhook-deliveries",
                            params={"endpoint_id": ep_kb,
                                    "event_type": "document.failed",
                                    "page_size": 100},
                            headers=admin)
            rows = r.json().get("items") or []
            seen = [((x.get("payload") or {}).get("data", {})
                     .get("document", {}) or {}).get("kb_id") for x in rows]
            check("KB乙 zero delivery rows (per-KB filter)",
                  kb_b not in seen and seen.count(kb_a) >= 1, str(seen))

            # ⑦ 重投:/reject → dead → PUT 改 URL /ok → redeliver → succeeded
            r = await new_ep(f"m18rej{SUFFIX}", "/reject")
            r.raise_for_status()
            ep_rej = r.json()["id"]
            endpoint_ids.append(ep_rej)
            ledger[ep_rej] = 0
            r = await c.post(f"{API}/admin/webhooks/{ep_rej}/test",
                             headers=admin)
            check("reject test inline dead (http 404)",
                  r.status_code == 200 and r.json().get("status") == "dead"
                  and r.json().get("response_status") == 404, r.text[:200])
            ledger[ep_rej] += 1
            row = await wait_delivery(c, admin, ep_rej, "test", want="dead",
                                      timeout_s=30)
            check("reject delivery dead row found", row is not None, str(row))
            if row is None:
                raise RuntimeError("reject dead delivery missing; abort step 7")
            did = row["id"]
            r = await c.put(f"{API}/admin/webhooks/{ep_rej}",
                            json={"url": f"http://127.0.0.1:{port}/ok"},
                            headers=admin)
            check("PUT url change to /ok 200",
                  r.status_code == 200 and (r.json().get("url") or "")
                  .endswith("/ok"), r.text[:200])
            r = await c.post(
                f"{API}/admin/webhooks/{ep_rej}/deliveries/{did}/redeliver",
                headers=admin)
            check("redeliver returns pending",
                  r.status_code == 200
                  and r.json() == {"status": "pending", "delivery_id": did},
                  r.text[:200])
            row = await wait_delivery(c, admin, ep_rej, "test", timeout_s=90)
            check("redelivered delivery succeeded attempts=1",
                  row is not None and row.get("status") == "succeeded"
                  and row.get("attempts") == 1, str(row))

            # ⑧ SSRF:私网 422;127.0.0.1 白名单显式建端点 201
            r = await c.post(f"{API}/admin/webhooks", json={
                "name": f"m18ssrfbad{SUFFIX}",
                "url": "http://10.255.255.1/x",
                "events": ["document.done"]}, headers=admin)
            check("SSRF private ip create 422 detail contains SSRF",
                  r.status_code == 422
                  and "SSRF" in str(r.json().get("detail")), r.text[:200])
            r = await new_ep(f"m18ssrfok{SUFFIX}", "/ok",
                             description=f"m18验收{SUFFIX}")
            check("127.0.0.1 endpoint create 201 (allowlist)",
                  r.status_code == 201, f"HTTP {r.status_code} {r.text[:200]}")
            r.raise_for_status()
            ep_ssrf = r.json()["id"]
            endpoint_ids.append(ep_ssrf)
            ledger[ep_ssrf] = 0

            # ⑨ 统计与杂项:stats 对账 / provider+kb_ids 回显 / 长度闸 /
            #    description 清空 / 非 admin 全 403
            r = await c.get(f"{API}/admin/webhooks", headers=admin)
            items = {w["id"]: w for w in r.json()}
            missing = [e for e in ledger if e not in items]
            mism = {e: ((items.get(e) or {}).get("stats") or {}).get("total")
                    for e in ledger
                    if (((items.get(e) or {}).get("stats") or {})
                        .get("total")) != ledger[e]}
            check("stats.total matches script ledger (all endpoints)",
                  not missing and not mism, f"missing={missing} mism={mism}")
            check("provider/kb_ids echoed in list",
                  (items.get(ep_wx) or {}).get("provider") == "wecom"
                  and (items.get(ep_dt) or {}).get("provider") == "dingtalk"
                  and (items.get(ep_fs) or {}).get("provider") == "feishu"
                  and (items.get(ep_kb) or {}).get("kb_ids") == [kb_a])
            long_url = "http://127.0.0.1/x" + "a" * (501 - len("http://127.0.0.1/x"))
            r = await c.post(f"{API}/admin/webhooks", json={
                "name": f"m18long{SUFFIX}", "url": long_url, "events": []},
                headers=admin)
            check("501-char url 422",
                  r.status_code == 422
                  and "500" in str(r.json().get("detail")), r.text[:200])
            r = await c.put(f"{API}/admin/webhooks/{ep_ssrf}",
                            json={"description": ""}, headers=admin)
            put_ok = r.status_code == 200 and r.json().get("description") is None
            r = await c.get(f"{API}/admin/webhooks", headers=admin)
            it = next((w for w in r.json() if w["id"] == ep_ssrf), None)
            check("PUT description='' then GET null",
                  put_ok and it is not None and it.get("description") is None)
            viewer = await login(c, v_name)
            r = await c.post(f"{API}/admin/webhooks", json={
                "name": "x", "url": f"http://127.0.0.1:{port}/ok",
                "events": []}, headers=viewer)
            check("viewer create webhook 403", r.status_code == 403,
                  f"HTTP {r.status_code}")
            r = await c.post(
                f"{API}/admin/webhooks/{ep_wx}/deliveries/1/redeliver",
                headers=viewer)
            check("viewer redeliver 403", r.status_code == 403,
                  f"HTTP {r.status_code}")
            r = await c.get(f"{API}/admin/webhooks", headers=viewer)
            check("viewer list webhooks 403", r.status_code == 403,
                  f"HTTP {r.status_code}")
            r = await c.get(f"{API}/admin/webhook-deliveries", headers=viewer)
            check("viewer list deliveries 403", r.status_code == 403,
                  f"HTTP {r.status_code}")

            # ⑩ 清理:全部验收端点 DELETE 204(投递行随 FK 级联删)
            codes = [(await c.delete(f"{API}/admin/webhooks/{eid}",
                                     headers=admin)).status_code
                     for eid in endpoint_ids]
            check("all acceptance endpoints deleted 204",
                  bool(codes) and all(x == 204 for x in codes), str(codes))
            r = await c.get(f"{API}/admin/webhooks", headers=admin)
            leftover = [w["id"] for w in r.json() if w["id"] in endpoint_ids]
            check("deleted endpoints absent from list", not leftover,
                  str(leftover))
        finally:
            await cleanup(user_ids, kb_ids, usernames, endpoint_ids)
            srv.shutdown()
    summary_and_exit()


if __name__ == "__main__":
    asyncio.run(main())
