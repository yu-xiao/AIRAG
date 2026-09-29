"""M22 无头验收(真栈:8001 + worker + beat;dev .env 已带
WEBHOOK_SSRF_ALLOWLIST=127.0.0.1,receiver 靠它)。

覆盖(M22 两段式租约+孤儿事件):①从未开跑+创建 30min(NULL 心跳)→
存活 ≥2 个 beat tick——排队误杀根除的真栈判别(M21 语义下 10min 即被
收口)②从未开跑+创建 61min → beat 收口 failed 且 eval.failed 真送达
receiver(负载 summary=null、item_count=创建题数、error=orphaned)——
孤儿事件补全的真栈回路 ③已开跑+stale 心跳(30min)→ 收口+事件
④fresh 心跳行存活(回归)⑤真实 retrieval 运行:领租+逐题续租(跑毕
heartbeat 非空且晚于触发前)⑥docs 孤儿收口说明在。派发失败路径(502+
条件收口+事件)单测已覆盖,真栈不模拟断 broker。清理:端点/KB 走 API,
run 行 SQL 兜底(eval_items 随 FK CASCADE)。
"""
import asyncio
import http.server
import json
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8001"
API = f"{BASE}/api"
TIMEOUT = httpx.Timeout(120.0)
RESULTS = {"pass": [], "fail": [], "skip": []}
SUFFIX = uuid.uuid4().hex[:6]
BACKEND_DIR = Path(__file__).resolve().parents[1]

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

    def log_message(self, *a):
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
    print(f"\nM22 ACCEPTANCE: {len(RESULTS['pass'])}/{total} PASS")
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


async def sql(sql_text, params=None, fetch=False):
    import sqlalchemy

    engine, maker = _nullpool_sessionmaker()
    try:
        async with maker() as s:
            res = await s.execute(sqlalchemy.text(sql_text), params or {})
            if fetch:
                out = res.mappings().all()
                await s.commit()
                return out
            await s.commit()
            return res
    finally:
        await engine.dispose()


async def login(c: httpx.AsyncClient, username: str) -> dict:
    r = await c.post(f"{API}/auth/login",
                     json={"username": username, "password": "secret123"})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def wait_status(c, jwt, run_id, want, timeout_s):
    t0 = time.perf_counter()
    while True:
        r = await c.get(f"{API}/eval/runs/{run_id}", headers=jwt)
        d = r.json()
        if d.get("status") in want or time.perf_counter() > t0 + timeout_s:
            return d
        await asyncio.sleep(2)


async def wait_delivery(c, jwt, endpoint_id, event_type, timeout_s=60):
    t0 = time.perf_counter()
    while True:
        r = await c.get(f"{API}/admin/webhook-deliveries",
                        params={"endpoint_id": endpoint_id,
                                "event_type": event_type, "page_size": 20},
                        headers=jwt)
        rows = r.json().get("items") or []
        if (rows and all(x.get("status") in ("succeeded", "dead")
                         for x in rows)) \
                or time.perf_counter() > t0 + timeout_s:
            return rows
        await asyncio.sleep(2)


async def main() -> None:
    srv = start_receiver()
    port = srv.server_address[1]
    print(f"[info] receiver on 127.0.0.1:{port}")

    kb_ids, endpoint_ids, run_ids = [], [], []
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        try:
            admin = await login(c, "admin")
            check("admin login ok", bool(admin.get("Authorization")))

            ra = await c.post(f"{API}/kbs",
                              json={"name": f"m22租约库{SUFFIX}"},
                              headers=admin)
            ra.raise_for_status()
            kb = ra.json()["id"]
            kb_ids.append(kb)

            # 事件面:订阅 eval.failed 的 receiver 端点
            r = await c.post(f"{API}/admin/webhooks", json={
                "name": f"m22rx{SUFFIX}",
                "url": f"http://127.0.0.1:{port}/orphan",
                "provider": "generic", "events": ["eval.failed"]},
                headers=admin)
            r.raise_for_status()
            ep = r.json()["id"]
            endpoint_ids.append(ep)

            # ---- ①②③④ 两段谓词四象限(beat 真触发) ----
            now = datetime.now(timezone.utc)
            from app.core.timeutil import utcnow_naive

            rows = await sql(
                "INSERT INTO eval_runs (kb_id, mode, summary, item_count, "
                "status, triggered_by, heartbeat_at, created_at) VALUES "
                "(:k, 'retrieval', NULL, 4, 'running', 1, NULL, :c30), "
                "(:k, 'retrieval', NULL, 5, 'running', 1, NULL, :c61), "
                "(:k, 'retrieval', NULL, 6, 'cancelling', 1, :hb_stale, "
                "DEFAULT), "
                "(:k, 'retrieval', NULL, 7, 'running', 1, :hb_fresh, DEFAULT) "
                "RETURNING id",
                {"k": kb,
                 "c30": now - timedelta(minutes=30),
                 "c61": now - timedelta(minutes=61),
                 "hb_stale": utcnow_naive() - timedelta(minutes=30),
                 "hb_fresh": utcnow_naive()}, fetch=True)
            for row in rows:
                run_ids.append(row["id"])
            q30, q61, st_stale, st_fresh = (row["id"] for row in rows)
            print(f"[info] rows queued={q30} overage={q61} stale={st_stale} "
                  f"fresh={st_fresh}; waiting for beat tick(s)...")

            # ②③:超龄 NULL 与 stale 心跳应被收口(60s beat + 宽裕)
            d = await wait_status(c, admin, q61, ("failed",), 150)
            check("beat sweep closes over-age never-started row (61min)",
                  d.get("status") == "failed", f"status={d.get('status')}")
            d = await wait_status(c, admin, st_stale, ("failed",), 60)
            check("beat sweep closes stale-heartbeat cancelling row",
                  d.get("status") == "failed", f"status={d.get('status')}")
            err = str((await sql("SELECT error FROM eval_runs WHERE id=:i",
                                 {"i": q61}, fetch=True))[0]["error"])
            check("orphan error text on never-started close",
                  err.startswith("orphaned: heartbeat expired"),
                  f"err={err[:80]}")

            # 孤儿事件真投递:恰两封 eval.failed(q61 与 stale),summary=null
            dl = await wait_delivery(c, admin, ep, "eval.failed")
            check("delivery rows eval.failed x2 succeeded",
                  len(dl) == 2 and all(x.get("status") == "succeeded"
                                       for x in dl), str(dl)[:200])
            envs = []
            for p in packets():
                if p["path"] != "/orphan":
                    continue
                try:
                    env = json.loads(p["body"])
                    if env.get("event_type") == "eval.failed":
                        envs.append(env)
                except Exception:
                    pass
            if envs:
                ok_env = (len(envs) == 2
                          and all(e["data"]["run"]["summary"] is None
                                  for e in envs)
                          and {e["data"]["run"]["id"] for e in envs}
                          == {q61, st_stale}
                          and all("orphaned" in e["data"]["error"]
                                  for e in envs))
                check("receiver got 2 orphan eval.failed envelopes "
                      "(summary null, honest counts)",
                      ok_env, json.dumps(envs, ensure_ascii=False)[:400])
            else:
                check("receiver got orphan eval.failed envelopes", False,
                      "none captured")

            # ①④:排队中(30min)与 fresh 心跳行须存活 ≥2 tick
            await asyncio.sleep(70)
            for rid, name in ((q30, "queued 30min never-started"),
                              (st_fresh, "fresh-heartbeat")):
                d = await wait_status(c, admin, rid, ("failed",), 1)
                check(f"{name} row survives beat sweep (stale-only)",
                      d.get("status") == "running", f"status={d.get('status')}")

            # ---- ⑤ 真实运行:领租 + 逐题续租 ----
            ra = await c.post(f"{API}/kbs",
                              json={"name": f"m22心跳库{SUFFIX}"},
                              headers=admin)
            ra.raise_for_status()
            kb_h = ra.json()["id"]
            kb_ids.append(kb_h)
            for i in range(3):
                await c.post(f"{API}/eval/questions", json={
                    "kb_id": kb_h, "question": f"m22{SUFFIX}-h{i} 心跳题",
                    "expect_doc_ids": [], "expect_keywords": []},
                    headers=admin)
            t0 = utcnow_naive()
            r = await c.post(f"{API}/eval/runs",
                             json={"kb_id": kb_h, "mode": "retrieval"},
                             headers=admin)
            r.raise_for_status()
            run_h = r.json()["run_id"]
            run_ids.append(run_h)
            d = await wait_status(c, admin, run_h,
                                  ("completed", "failed", "cancelled"), 180)
            check("real retrieval run completes", d.get("status") == "completed",
                  f"status={d.get('status')} err={str(d.get('error'))[:80]}")
            hb = (await sql("SELECT heartbeat_at FROM eval_runs WHERE id=:i",
                            {"i": run_h}, fetch=True))[0]["heartbeat_at"]
            check("claim+renewal: heartbeat non-null after real run",
                  hb is not None and hb >= t0, f"hb={hb} t0={t0}")

            # ---- ⑥ docs 孤儿说明 ----
            doc = (BACKEND_DIR.parent / "docs" / "webhooks.md").read_text(
                encoding="utf-8")
            check("docs mention orphan-sweep eval.failed (summary null)",
                  "孤儿收口" in doc and "orphaned: heartbeat expired" in doc)

            # ---- 清理 ----
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
            for rid in run_ids:
                await sql("DELETE FROM eval_runs WHERE id = :i", {"i": rid})
    summary_and_exit()


if __name__ == "__main__":
    asyncio.run(main())
