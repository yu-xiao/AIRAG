"""M17 无头验收(真栈:8001 + worker + beat + Redis;worker 必须 start_worker.bat、beat 必须 start_beat.bat 已起)。

覆盖:端点创建(secret 明文仅 POST 一次/列表 masked)/ eval.completed /
chat.refused(真 LLM 拒答;key 未配或未拒答记 SKIP)/ document.failed /
document.done / 4xx 永久 dead / beat 60s 兜底(直插 pending 行,不 nudge)/
权限负例 / 清理。本脚本自带本地 receiver(127.0.0.1 随机端口,/reject 回
404)逐包验签 hex(HMAC-SHA256(secret, f"{ts}.{body}"))。

注:无 DELETE /api/keys 路由——临时 key 随 temp 用户在收尾经 DB 直删。
"""
import asyncio
import hashlib
import hmac
import http.server
import io
import json
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

# `python scripts/m17_acceptance.py` 直跑时 sys.path[0] 是 scripts 目录;
# app 是 editable 安装可导入,但 scripts.* 不是——补 backend 根(m15 同款)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8001"
API = f"{BASE}/api"
TIMEOUT = httpx.Timeout(120.0)
RESULTS = {"pass": [], "fail": [], "skip": []}

SUFFIX = uuid.uuid4().hex[:6]

# ---- 本地 receiver:daemon 线程 + 模块级收包表(锁保护并发写/读) ----
RECEIVED: list[dict] = []
LOCK = threading.Lock()


class _RxHandler(http.server.BaseHTTPRequestHandler):
    """收包存 {"path","headers","body"};/reject 路径回 404(4xx 永久死信用)。"""

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        with LOCK:
            RECEIVED.append({"path": self.path, "headers": dict(self.headers),
                             "body": body})
        self.send_response(404 if self.path == "/reject" else 200)
        self.end_headers()

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


def verify_sig(pkt: dict, secret: str) -> bool:
    """按签名公式重算并与 X-AIRag-Signature 常时比对。"""
    ts, sig = _hdr(pkt["headers"], "X-AIRag-Timestamp"), \
        _hdr(pkt["headers"], "X-AIRag-Signature")
    if not ts or not sig:
        return False
    expect = hmac.new(
        secret.encode(), f"{ts}.{pkt['body'].decode('utf-8')}".encode(),
        hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expect)


def wait_packet(event_type: str, timeout_s: float = 30.0, match=None):
    """等指定事件的收包(可带 body 谓词);返回含 "json" 解析结果的包。"""
    def pred():
        for p in packets():
            if _hdr(p["headers"], "X-AIRag-Event") != event_type:
                continue
            try:
                body = json.loads(p["body"].decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if match is None or match(body):
                out = dict(p)
                out["json"] = body
                return out
        return None
    return wait_for(pred, timeout_s)


def check(name, cond, detail=""):
    (RESULTS["pass"] if cond else RESULTS["fail"]).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + (f"  {detail}" if detail and not cond else ""))


def summary_and_exit():
    total = sum(len(v) for v in RESULTS.values())
    print(f"\nM17 ACCEPTANCE: {len(RESULTS['pass'])}/{total} PASS")
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


async def wait_completed(c: httpx.AsyncClient, jwt: dict, run_id: int,
                         timeout_s: int) -> dict:
    """轮询 run 明细至 completed/failed(每 2s;m15 同款)。"""
    deadline = time.perf_counter() + timeout_s
    while True:
        r = await c.get(f"{API}/eval/runs/{run_id}", headers=jwt)
        d = r.json()
        if d.get("status") in ("completed", "failed") \
                or time.perf_counter() > deadline:
            print(f"[info] run {run_id} -> {d.get('status')}")
            return d
        await asyncio.sleep(2)


async def wait_delivery(c: httpx.AsyncClient, jwt: dict, endpoint_id: int,
                        event_type: str, want: str = "succeeded",
                        timeout_s: float = 30.0):
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
                          want: str, timeout_s: int = 120):
    deadline = time.perf_counter() + timeout_s
    while True:
        r = await c.get(f"{API}/documents/{doc_id}", headers=jwt)
        st = r.json().get("status")
        if st == want or time.perf_counter() > deadline:
            return st
        await asyncio.sleep(2)


async def insert_beat_probe(endpoint_id: int) -> str:
    """直插一行 pending 投递行(绕过 emit/nudge)——只等 beat 60s 扫描。"""
    from app.models import WebhookDelivery

    event_id = uuid.uuid4().hex
    engine, maker = _nullpool_sessionmaker()
    try:
        async with maker() as s:
            s.add(WebhookDelivery(
                endpoint_id=endpoint_id, event_type="beat.check",
                event_id=event_id,
                payload={"event_id": event_id, "event_type": "beat.check",
                         "occurred_at": datetime.now(timezone.utc).isoformat(),
                         "data": {"note": "m17 beat fallback probe"}},
                status="pending", attempts=0))
            await s.commit()
    finally:
        await engine.dispose()
    return event_id


def print_verify_snippet(pkt: dict, secret: str) -> None:
    """走查辅助:外部复算签名的一行命令(用首个 eval.completed 收包)。
    body 走 base64——整行除外层双引号外无任何双引号/非 ASCII,CMD
    复制粘贴即用。"""
    import base64

    ts = _hdr(pkt["headers"], "X-AIRag-Timestamp")
    sig = _hdr(pkt["headers"], "X-AIRag-Signature")
    b64 = base64.b64encode(pkt["body"]).decode("ascii")
    print(f"\n[walkthrough] 外部验签示例(输出应等于收到的签名 {sig}):")
    print(f'  python -c "import base64,hmac,hashlib;ts={ts!r};'
          f"b=base64.b64decode({b64!r}).decode();"
          f"print(hmac.new({secret!r}.encode(),(ts+'.'+b).encode(),"
          'hashlib.sha256).hexdigest())"')


async def main() -> None:
    user_ids, kb_ids, usernames, endpoint_ids = [], [], [], []
    srv = start_receiver()
    port = srv.server_address[1]
    print(f"[info] receiver on 127.0.0.1:{port}")
    sample = None  # 验签示例用首个收包
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        try:
            # ① 端点创建:订阅全部(events=[]);secret 明文仅此一次;列表 masked
            admin = await login(c, "admin")
            r = await c.post(f"{API}/admin/webhooks", json={
                "name": f"m17acc{SUFFIX}",
                "url": f"http://127.0.0.1:{port}/hook", "events": []},
                headers=admin)
            check("create webhook 201 with plaintext secret",
                  r.status_code == 201 and bool(r.json().get("secret")),
                  f"HTTP {r.status_code}")
            r.raise_for_status()
            ep1 = r.json()["id"]; endpoint_ids.append(ep1)
            secret = r.json()["secret"]
            r = await c.get(f"{API}/admin/webhooks", headers=admin)
            row = next((w for w in r.json() if w["id"] == ep1), None)
            check("webhook list secret masked",
                  row is not None and "****" in row.get("secret_masked", "")
                  and secret not in json.dumps(r.json()), str(row))

            # ② eval.completed:KB+3 题+retrieval run → completed →
            #    收包验签 / 信封 event_id / 投递行 succeeded
            r = await c.post(f"{API}/kbs",
                             json={"name": f"m17验收库{SUFFIX}"}, headers=admin)
            r.raise_for_status()
            kb_id = r.json()["id"]; kb_ids.append(kb_id)
            for i in range(3):
                r = await c.post(f"{API}/eval/questions", json={
                    "kb_id": kb_id,
                    "question": f"m17{SUFFIX}-{i} 出站推送验收题{i}?",
                    "expect_doc_ids": [1, 2] if i == 0 else [],
                    "expect_keywords": []}, headers=admin)
                r.raise_for_status()
            r = await c.post(f"{API}/eval/runs",
                             json={"kb_id": kb_id, "mode": "retrieval"},
                             headers=admin)
            check("trigger retrieval 201", r.status_code == 201,
                  f"HTTP {r.status_code}")
            run_id = r.json()["run_id"]
            d = await wait_completed(c, admin, run_id, 120)
            check("retrieval run completed", d.get("status") == "completed",
                  f"status={d.get('status')}")
            pkt = wait_packet("eval.completed", 30, match=lambda b: (
                b.get("data", {}).get("run", {}).get("id") == run_id))
            check("eval.completed packet received", pkt is not None)
            if pkt:
                sample = pkt
                check("eval.completed hmac verified", verify_sig(pkt, secret))
                check("eval.completed envelope event_id 32",
                      len(pkt["json"].get("event_id") or "") == 32)
            row = await wait_delivery(c, admin, ep1, "eval.completed")
            check("eval.completed delivery succeeded", row is not None,
                  str(row))

            # ③ chat.refused:viewer 账号发临时 key → /agent/ask 问库外
            #    完全无关问题(此时库无文档,零命中);未拒答/异常记 SKIP
            v_name, v_id = await make_user(c, f"m17_{SUFFIX}_viewer", "viewer")
            usernames.append(v_name); user_ids.append(v_id)
            r = await c.put(f"{API}/kbs/{kb_id}/permissions",
                            json={"username": v_name, "perm": "viewer"},
                            headers=admin)
            r.raise_for_status()
            r = await c.post(f"{API}/admin/keys", json={
                "user_id": v_id, "name": f"m17acc{SUFFIX}",
                "expires_in_days": 1}, headers=admin)
            check("issue temp api key 201", r.status_code == 201,
                  f"HTTP {r.status_code}")
            r.raise_for_status()
            raw_key = r.json()["key"]
            q = "m17验收无关问题:夸父逐日时佩戴了什么款式的护目镜xyzzy?"
            r = await c.post(f"{API}/agent/ask",
                             json={"kb_ids": [kb_id], "query": q},
                             headers={"Authorization": f"Bearer {raw_key}"})
            refused = r.status_code == 200 and bool(r.json().get("refused"))
            if refused:
                check("agent ask refused", True)
                pkt = wait_packet("chat.refused", 30)
                check("chat.refused packet received", pkt is not None)
                if pkt:
                    check("chat.refused hmac verified", verify_sig(pkt, secret))
                    data = pkt["json"].get("data", {})
                    check("chat.refused source rest",
                          data.get("source") == "rest", str(data))
                    check("chat.refused question echoed <=500",
                          data.get("question") == q
                          and len(data.get("question") or "") <= 500)
                row = await wait_delivery(c, admin, ep1, "chat.refused")
                check("chat.refused delivery succeeded", row is not None,
                      str(row))
            else:
                RESULTS["skip"].append("chat.refused events")
                print(f"SKIP chat.refused events  (ask HTTP {r.status_code}, "
                      f"refused={r.json().get('refused') if r.status_code == 200 else '?'}; "
                      "真 LLM 拒答不可用——ZHIPU key 未配或模型未按提示拒答)")

            # ④ document.failed:损坏 PDF(上传 201,worker 解析炸)→ failed
            r = await c.post(f"{API}/kbs/{kb_id}/documents", files={"file": (
                "bad.pdf", b"%PDF-1.4\n%\xe5\x9e\x8a bytes that are not a valid pdf",
                "application/pdf")}, headers=admin)
            check("corrupt pdf upload 201", r.status_code == 201,
                  f"HTTP {r.status_code}")
            r.raise_for_status()
            bad_id = r.json()["id"]
            st = await wait_doc_status(c, admin, bad_id, "failed", 120)
            check("corrupt pdf status failed", st == "failed", f"status={st}")
            pkt = wait_packet("document.failed", 30, match=lambda b: (
                b.get("data", {}).get("document", {}).get("id") == bad_id))
            check("document.failed packet received", pkt is not None)
            if pkt:
                check("document.failed hmac verified", verify_sig(pkt, secret))
                check("document.failed error present",
                      bool(pkt["json"].get("data", {}).get("error")))
            row = await wait_delivery(c, admin, ep1, "document.failed")
            check("document.failed delivery succeeded", row is not None,
                  str(row))

            # ⑤ document.done:pymupdf 最小 PDF(test_pipeline 模式)→ done
            import pymupdf as fitz
            doc = fitz.open()
            page = doc.new_page()
            page.insert_text((72, 72), f"m17 acceptance pdf {SUFFIX}")
            buf = io.BytesIO(); doc.save(buf); doc.close()
            r = await c.post(f"{API}/kbs/{kb_id}/documents", files={"file": (
                "good.pdf", buf.getvalue(), "application/pdf")}, headers=admin)
            check("good pdf upload 201", r.status_code == 201,
                  f"HTTP {r.status_code}")
            r.raise_for_status()
            good_id = r.json()["id"]
            st = await wait_doc_status(c, admin, good_id, "done", 120)
            check("good pdf status done", st == "done", f"status={st}")
            pkt = wait_packet("document.done", 30, match=lambda b: (
                b.get("data", {}).get("document", {}).get("id") == good_id))
            check("document.done packet received", pkt is not None)
            if pkt:
                check("document.done hmac verified", verify_sig(pkt, secret))
                check("document.done chunk_count >= 1",
                      (pkt["json"].get("data", {}).get("document", {})
                       .get("chunk_count") or 0) >= 1)
            row = await wait_delivery(c, admin, ep1, "document.done")
            check("document.done delivery succeeded", row is not None,
                  str(row))

            # ⑥ 4xx 永久:/reject 端点 + 复用库再跑一次 eval(nudge 即投)
            #    → dead + response_status=404 + last_error 含 permanent
            r = await c.post(f"{API}/admin/webhooks", json={
                "name": f"m17rej{SUFFIX}",
                "url": f"http://127.0.0.1:{port}/reject", "events": []},
                headers=admin)
            check("reject endpoint created 201", r.status_code == 201,
                  f"HTTP {r.status_code}")
            r.raise_for_status()
            ep2 = r.json()["id"]; endpoint_ids.append(ep2)
            r = await c.post(f"{API}/eval/runs",
                             json={"kb_id": kb_id, "mode": "retrieval"},
                             headers=admin)
            r.raise_for_status()
            await wait_completed(c, admin, r.json()["run_id"], 120)
            row = await wait_delivery(c, admin, ep2, "eval.completed",
                                      want="dead", timeout_s=30)
            check("reject delivery dead", row is not None, str(row))
            if row:
                check("reject response_status 404",
                      row.get("response_status") == 404, str(row))
                check("reject last_error permanent",
                      "permanent" in (row.get("last_error") or ""), str(row))

            # ⑦ beat 兜底:直插 pending 行(不 nudge)→ ≤90s 等 beat 60s 扫描
            probe_id = await insert_beat_probe(ep1)
            row = await wait_delivery(c, admin, ep1, "beat.check",
                                      timeout_s=90)
            check("beat fallback delivery succeeded", row is not None,
                  str(row))
            pkt = wait_packet("beat.check", 10,
                              match=lambda b: b.get("event_id") == probe_id)
            check("beat fallback receiver packet", pkt is not None)

            # ⑧ 权限负例:viewer POST/DELETE webhooks 403、GET deliveries 403
            viewer = await login(c, v_name)
            r = await c.post(f"{API}/admin/webhooks", json={
                "name": "x", "url": "http://127.0.0.1:1/x", "events": []},
                headers=viewer)
            check("viewer create webhook 403", r.status_code == 403,
                  f"HTTP {r.status_code}")
            r = await c.delete(f"{API}/admin/webhooks/{ep1}", headers=viewer)
            check("viewer delete webhook 403", r.status_code == 403,
                  f"HTTP {r.status_code}")
            r = await c.get(f"{API}/admin/webhook-deliveries", headers=viewer)
            check("viewer list deliveries 403", r.status_code == 403,
                  f"HTTP {r.status_code}")

            # ⑨ 清理:两端点 DELETE 204(投递行随 FK 级联删)
            codes = [(await c.delete(f"{API}/admin/webhooks/{eid}",
                                     headers=admin)).status_code
                     for eid in (ep1, ep2)]
            check("webhook endpoints deleted 204", codes == [204, 204],
                  str(codes))
        finally:
            await cleanup(user_ids, kb_ids, usernames, endpoint_ids)
            srv.shutdown()
    if sample is not None:
        print_verify_snippet(sample, secret)
    summary_and_exit()


if __name__ == "__main__":
    asyncio.run(main())
