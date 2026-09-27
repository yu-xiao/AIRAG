"""M20 无头验收(真栈:8001 + worker;beat 可选;dev .env 需
WEBHOOK_SSRF_ALLOWLIST=127.0.0.1——本脚本 receiver 与坏括号用例都靠它)。

覆盖:①坏括号 URL 建端点被 SSRF 预检拒(422,M20 urlparse 包裹)②新事件
类型 eval.cancelled 可订阅(注册表生效)/ 乱拼类型仍 422(回归)③取消回路
(generation 真 LLM 窗口;无 key 降 retrieval 并按 m19 先例记 SKIP)——触发→
立即 cancel→cancelling→轮询 cancelled→全订阅端点投递恰一行 eval.cancelled
且 succeeded、receiver 实收信封(run 子集诚实)、零 eval.completed 假事件
(M19 修复波语义)④cancel 幂等(cancelling 再调 200)/终态再 cancel 409
(M20 端点条件 UPDATE 真栈回归)⑤dingtalk 端点 im_secret_set 字段:未带
im_secret=False→PUT 带上→True(前端显示属走查,延后)⑥清理。

M20 竞态窄窗(A1/A2)单测+guard-flip seam 已覆盖,真栈不注入竞态——真栈
验的是条件 UPDATE 后的对外行为不回归(幂等/409/终态不被改写)。
"""
import asyncio
import http.server
import json
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx

# `python scripts/m20_acceptance.py` 直跑时 sys.path[0] 是 scripts 目录;
# app 是 editable 安装可导入,但 scripts.* 不是——补 backend 根(m17~m19 同款)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8001"
API = f"{BASE}/api"
TIMEOUT = httpx.Timeout(120.0)
RESULTS = {"pass": [], "fail": [], "skip": []}

SUFFIX = uuid.uuid4().hex[:6]

# ---- 本地 receiver(m18 同款):daemon 线程 + 收包表 ----
RECEIVED: list[dict] = []
LOCK = threading.Lock()


class _RxHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        with LOCK:
            RECEIVED.append({"path": self.path, "headers": dict(self.headers),
                             "body": body})
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):  # 安静:不污染验收输出
        pass


def start_receiver() -> http.server.ThreadingHTTPServer:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RxHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def packets() -> list[dict]:
    with LOCK:
        return list(RECEIVED)


def check(name, cond, detail=""):
    (RESULTS["pass"] if cond else RESULTS["fail"]).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + (f"  {detail}" if detail and not cond else ""))


def summary_and_exit():
    total = sum(len(v) for v in RESULTS.values())
    print(f"\nM20 ACCEPTANCE: {len(RESULTS['pass'])}/{total} PASS")
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


async def cleanup(user_ids, kb_ids, usernames, endpoint_ids) -> None:
    from sqlalchemy import text

    engine, maker = _nullpool_sessionmaker()
    try:
        async with maker() as s:
            for uid in user_ids:
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


async def login(c: httpx.AsyncClient, username: str) -> dict:
    r = await c.post(f"{API}/auth/login",
                     json={"username": username, "password": "secret123"})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def wait_run(c: httpx.AsyncClient, jwt: dict, run_id: int,
                   timeout_s: int) -> dict:
    t0 = time.perf_counter()
    deadline = t0 + timeout_s
    while True:
        r = await c.get(f"{API}/eval/runs/{run_id}", headers=jwt)
        d = r.json()
        if d.get("status") in ("completed", "failed", "cancelled") \
                or time.perf_counter() > deadline:
            print(f"[info] run {run_id} -> {d.get('status')} "
                  f"in {time.perf_counter() - t0:.1f}s")
            return d
        await asyncio.sleep(2)


async def wait_delivery(c: httpx.AsyncClient, jwt: dict, endpoint_id: int,
                        event_type: str, timeout_s: int = 30) -> list[dict]:
    """轮询投递记录至该端点该类型出现终态(succeeded/dead)或超时,返回行。"""
    t0 = time.perf_counter()
    while True:
        r = await c.get(f"{API}/admin/webhook-deliveries",
                        params={"endpoint_id": endpoint_id,
                                "event_type": event_type, "page_size": 10},
                        headers=jwt)
        rows = r.json().get("items") or []
        if rows and rows[0].get("status") in ("succeeded", "dead") \
                or time.perf_counter() > deadline(t0, timeout_s):
            return rows
        await asyncio.sleep(2)


def deadline(t0: float, timeout_s: int) -> float:
    return t0 + timeout_s


async def main() -> None:
    from app.core.config import settings

    mode = "generation" if settings.ZHIPU_API_KEY else "retrieval"
    print(f"[info] cancel-flow mode={mode}"
          + (" (ZHIPU_API_KEY set)" if mode == "generation"
             else " (no key; immediate cancel may race to 409 -> SKIP)"))

    srv = start_receiver()
    port = srv.server_address[1]
    print(f"[info] receiver on 127.0.0.1:{port}")

    user_ids, kb_ids, usernames, endpoint_ids = [], [], [], []
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        try:
            admin = await login(c, "admin")
            check("admin login ok", bool(admin.get("Authorization")))

            # ① 坏括号 URL 建端点 → 422(M20 urlparse 包裹;真栈预检路径)
            r = await c.post(f"{API}/admin/webhooks", json={
                "name": f"m20bad{SUFFIX}", "url": "http://[::1/x",
                "provider": "generic", "events": []}, headers=admin)
            check("malformed bracket URL rejected 422",
                  r.status_code == 422, f"HTTP {r.status_code} {r.text[:160]}")

            # ② eval.cancelled 可订阅(注册表生效);乱拼类型仍 422(回归)
            r = await c.post(f"{API}/admin/webhooks", json={
                "name": f"m20sub{SUFFIX}",
                "url": f"http://127.0.0.1:{port}/ok",
                "provider": "generic", "events": ["eval.cancelled"]},
                headers=admin)
            check("events=['eval.cancelled'] accepted 201",
                  r.status_code == 201, f"HTTP {r.status_code} {r.text[:160]}")
            ep_ev = r.json()["id"]
            # ② 末尾就地删除并入断言;不进 endpoint_ids——清理段重复 DELETE
            # 会 404(首跑实测),清单只留生命周期跨越多步的端点
            r = await c.delete(f"{API}/admin/webhooks/{ep_ev}", headers=admin)
            check("event-sub endpoint deleted 204", r.status_code == 204)
            r = await c.post(f"{API}/admin/webhooks", json={
                "name": f"m20typo{SUFFIX}",
                "url": f"http://127.0.0.1:{port}/ok",
                "provider": "generic", "events": ["eval.cancelledd"]},
                headers=admin)
            check("unknown event type still 422 (validation regression)",
                  r.status_code == 422, f"HTTP {r.status_code}")

            # ③ 取消回路:全订阅端点(events=[])+ 触发→立即 cancel
            r = await c.post(f"{API}/admin/webhooks", json={
                "name": f"m20rx{SUFFIX}",
                "url": f"http://127.0.0.1:{port}/ok",
                "provider": "generic", "events": []}, headers=admin)
            r.raise_for_status()
            ep_rx = r.json()["id"]
            endpoint_ids.append(ep_rx)

            ra = await c.post(f"{API}/kbs",
                              json={"name": f"m20取消库{SUFFIX}"},
                              headers=admin)
            ra.raise_for_status()
            kb_c = ra.json()["id"]
            kb_ids.append(kb_c)
            for i in range(3):
                r = await c.post(f"{API}/eval/questions", json={
                    "kb_id": kb_c, "question": f"m20{SUFFIX}-c{i} 取消窗口题",
                    "expect_doc_ids": [], "expect_keywords": []},
                    headers=admin)
                r.raise_for_status()
            r = await c.post(f"{API}/eval/runs",
                             json={"kb_id": kb_c, "mode": mode},
                             headers=admin)
            check(f"trigger {mode} run 201", r.status_code == 201,
                  f"HTTP {r.status_code} {r.text[:120]}")
            r.raise_for_status()
            run1 = r.json()["run_id"]
            r = await c.post(f"{API}/eval/runs/{run1}/cancel", headers=admin)
            if r.status_code == 200:
                check("immediate cancel 200 -> cancelling",
                      r.json().get("status") == "cancelling", r.text[:200])
                if mode == "generation":  # 检索模式此刻可能已翻终态,免 409 竞态
                    r2 = await c.post(f"{API}/eval/runs/{run1}/cancel",
                                      headers=admin)
                    check("cancel while cancelling 200 idempotent",
                          r2.status_code == 200
                          and r2.json().get("status") == "cancelling",
                          f"HTTP {r2.status_code}")
                d = await wait_run(c, admin, run1, 120)
                check("run settles cancelled (<=120s)",
                      d.get("status") == "cancelled",
                      f"status={d.get('status')} "
                      f"err={str(d.get('error'))[:80]}")
                check("cancelled item_count < 3 (subset only)",
                      d.get("item_count", 3) < 3,
                      f"item_count={d.get('item_count')}")

                # 投递断言:该端点恰一行 eval.cancelled 且 succeeded;零
                # eval.completed(M19 修复波语义,M20 换发独立事件)
                rows = await wait_delivery(c, admin, ep_rx, "eval.cancelled")
                check("delivery row eval.cancelled exists & succeeded",
                      len(rows) == 1 and rows[0].get("status") == "succeeded",
                      str(rows)[:200])
                r = await c.get(f"{API}/admin/webhook-deliveries",
                                params={"endpoint_id": ep_rx,
                                        "event_type": "eval.completed"},
                                headers=admin)
                check("no eval.completed delivery for cancelled run",
                      r.json().get("total") == 0,
                      f"total={r.json().get('total')}")
                hits = [p for p in packets() if p["path"] == "/ok"]
                envs = []
                for p in hits:
                    try:
                        env = json.loads(p["body"])
                        if env.get("event_type") == "eval.cancelled":
                            envs.append(env)
                    except Exception:
                        pass
                ok_rx = (len(envs) == 1
                         and envs[0]["data"]["run"]["id"] == run1
                         and envs[0]["data"]["run"]["item_count"] < 3
                         and (envs[0]["data"]["run"].get("summary") or {})
                         .get("item_count")
                         == envs[0]["data"]["run"]["item_count"])
                check("receiver got exactly one eval.cancelled envelope "
                      "(subset honest)", ok_rx, f"envs={len(envs)}")
            else:
                RESULTS["skip"].append(
                    f"cancel roundtrip + delivery (immediate cancel -> HTTP "
                    f"{r.status_code}: {mode} run already terminal)")
                print(f"SKIP cancel roundtrip  (immediate cancel -> HTTP "
                      f"{r.status_code}; plan-sanctioned)")
                d = await wait_run(c, admin, run1, 300)

            # 终态后再 cancel → 409(条件 UPDATE 重读分流真栈回归)
            r = await c.post(f"{API}/eval/runs/{run1}/cancel", headers=admin)
            check("cancel on terminal run 409 (no overwrite)",
                  r.status_code == 409, f"HTTP {r.status_code}")

            # ④ dingtalk im_secret_set:未带=False → PUT 带上=True
            #    (URL 用本地 receiver:allowlist 覆盖 127.0.0.1,免真实 DNS)
            r = await c.post(f"{API}/admin/webhooks", json={
                "name": f"m20dt{SUFFIX}",
                "url": f"http://127.0.0.1:{port}/dt",
                "provider": "dingtalk", "events": []}, headers=admin)
            r.raise_for_status()
            ep_dt = r.json()["id"]
            endpoint_ids.append(ep_dt)
            r = await c.get(f"{API}/admin/webhooks", headers=admin)
            row = next((w for w in r.json() if w["id"] == ep_dt), None)
            check("dingtalk im_secret_set False (no im_secret)",
                  row is not None and row.get("im_secret_set") is False,
                  str(row)[:160])
            r = await c.put(f"{API}/admin/webhooks/{ep_dt}",
                            json={"im_secret": "SECnewhooksecret0123456789"},
                            headers=admin)
            check("PUT im_secret 200", r.status_code == 200,
                  f"HTTP {r.status_code} {r.text[:160]}")
            r = await c.get(f"{API}/admin/webhooks", headers=admin)
            row = next((w for w in r.json() if w["id"] == ep_dt), None)
            check("dingtalk im_secret_set True after PUT",
                  row is not None and row.get("im_secret_set") is True,
                  str(row)[:160])

            # ⑤ 清理:端点与 KB 走 API DELETE;SQL 兜底在 finally
            codes = [(await c.delete(f"{API}/admin/webhooks/{e}",
                                     headers=admin)).status_code
                     for e in endpoint_ids]
            check("acceptance endpoints deleted 204",
                  bool(codes) and all(x == 204 for x in codes), str(codes))
            codes = [(await c.delete(f"{API}/kbs/{k}", headers=admin)
                      ).status_code for k in kb_ids]
            check("acceptance KBs deleted 204 (API)",
                  bool(codes) and all(x == 204 for x in codes), str(codes))
        finally:
            await cleanup(user_ids, kb_ids, usernames, endpoint_ids)
    summary_and_exit()


if __name__ == "__main__":
    asyncio.run(main())
