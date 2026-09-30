"""M23 无头验收(真栈:8001 + worker + beat;dev .env 已带
WEBHOOK_SSRF_ALLOWLIST=127.0.0.1,receiver 靠它)。

覆盖(M23 检查点停止+文案分裂):①超龄 never-started 行(NULL 心跳+
创建 61min)→ beat 收口 failed 且 receiver 收到 `never started` 文案的
eval.failed ②stale 心跳行(30min)→ `heartbeat expired` 文案(两分支
分流真栈判别;M22 单一文案下两者同文)③真实 retrieval 运行正常完成
(领租+续租+检查点不回归)④docs 两形态文案在。检查点中途停止(单题
>10min 被 sweep 收口后循环即停)不注入真栈,单测覆盖(时序不可控)。
清理:端点/KB 走 API,run 行 SQL 兜底。
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
    print(f"\nM23 ACCEPTANCE: {len(RESULTS['pass'])}/{total} PASS")
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


async def wait_delivery(c, jwt, endpoint_id, event_type, expect_n=1,
                        timeout_s=60):
    """净版条件(M23):显式括号,(全终态且够数)或超时。"""
    t0 = time.perf_counter()
    while True:
        r = await c.get(f"{API}/admin/webhook-deliveries",
                        params={"endpoint_id": endpoint_id,
                                "event_type": event_type, "page_size": 20},
                        headers=jwt)
        rows = r.json().get("items") or []
        settled = [x for x in rows if x.get("status") in ("succeeded", "dead")]
        if (len(settled) >= expect_n) or (time.perf_counter() > t0 + timeout_s):
            return rows
        await asyncio.sleep(2)


async def main() -> None:
    srv = start_receiver()
    port = srv.server_address[1]
    print(f"[info] receiver on 127.0.0.1:{port}")

    kb_ids, endpoint_ids, run_ids = [], [], []
    admin: dict | None = None  # 前置绑定:login 即失败时 finally 仍可走 SQL 兜底
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        async def _cleanup(c, jwt):
            """幂等兜底:API 优先(逐个吞错),SQL 按 FK 序清残;
            正常路径的 204 检查在 try 内不变,这里只兜早失败。"""
            for e in endpoint_ids:
                try:
                    await c.delete(f"{API}/admin/webhooks/{e}",
                                   headers=jwt)
                except Exception:
                    pass
            for k in kb_ids:
                try:
                    await c.delete(f"{API}/kbs/{k}", headers=jwt)
                except Exception:
                    pass
            for rid in run_ids:
                try:
                    await sql("DELETE FROM eval_runs WHERE id = :i",
                              {"i": rid})
                except Exception:
                    pass
            for k in kb_ids:  # SQL 兜底(API 删失败时)
                try:
                    await sql(
                        "DELETE FROM chunks WHERE kb_id = :k", {"k": k})
                    await sql(
                        "DELETE FROM documents WHERE kb_id = :k", {"k": k})
                    await sql(
                        "DELETE FROM eval_questions WHERE kb_id = :k",
                        {"k": k})
                    await sql(
                        "DELETE FROM kb_permissions WHERE kb_id = :k",
                        {"k": k})
                    await sql(
                        "DELETE FROM knowledge_bases WHERE id = :k",
                        {"k": k})
                except Exception:
                    pass
            for e in endpoint_ids:
                try:
                    await sql(
                        "DELETE FROM webhook_endpoints WHERE id = :e",
                        {"e": e})
                except Exception:
                    pass

        try:
            admin = await login(c, "admin")
            check("admin login ok", bool(admin.get("Authorization")))

            ra = await c.post(f"{API}/kbs",
                              json={"name": f"m23分支库{SUFFIX}"},
                              headers=admin)
            ra.raise_for_status()
            kb = ra.json()["id"]
            kb_ids.append(kb)

            r = await c.post(f"{API}/admin/webhooks", json={
                "name": f"m23rx{SUFFIX}",
                "url": f"http://127.0.0.1:{port}/split",
                "provider": "generic", "events": ["eval.failed"]},
                headers=admin)
            r.raise_for_status()
            ep = r.json()["id"]
            endpoint_ids.append(ep)

            now = datetime.now(timezone.utc)
            from app.core.timeutil import utcnow_naive

            rows = await sql(
                "INSERT INTO eval_runs (kb_id, mode, summary, item_count, "
                "status, triggered_by, heartbeat_at, created_at) VALUES "
                "(:k, 'retrieval', NULL, 5, 'running', 1, NULL, :c61), "
                "(:k, 'generation', NULL, 6, 'cancelling', 1, :hb_stale, "
                "DEFAULT) RETURNING id",
                {"k": kb,
                 "c61": now - timedelta(minutes=61),
                 "hb_stale": utcnow_naive() - timedelta(minutes=30)},
                fetch=True)
            for row in rows:
                run_ids.append(row["id"])
            q61, st_stale = rows[0]["id"], rows[1]["id"]
            print(f"[info] never-started={q61} stale-hb={st_stale}; "
                  f"waiting for beat tick(s)...")

            d = await wait_status(c, admin, q61, ("failed",), 150)
            check("beat sweep closes never-started over-age row",
                  d.get("status") == "failed", f"status={d.get('status')}")
            d = await wait_status(c, admin, st_stale, ("failed",), 60)
            check("beat sweep closes stale-heartbeat row",
                  d.get("status") == "failed", f"status={d.get('status')}")

            dl = await wait_delivery(c, admin, ep, "eval.failed", expect_n=2)
            check("delivery rows eval.failed x2 succeeded",
                  len(dl) == 2 and all(x.get("status") == "succeeded"
                                       for x in dl), str(dl)[:200])
            envs = []
            for p in packets():
                if p["path"] != "/split":
                    continue
                try:
                    env = json.loads(p["body"])
                    if env.get("event_type") == "eval.failed":
                        envs.append(env)
                except Exception:
                    pass
            if envs:
                by_id = {e["data"]["run"]["id"]: e["data"] for e in envs}
                ok_env = (
                    set(by_id) == {q61, st_stale}
                    and "never started" in by_id[q61]["error"]
                    and "heartbeat expired" in by_id[st_stale]["error"]
                    and all(d_["run"]["summary"] is None
                            for d_ in by_id.values()))
                check("receiver got branch-split orphan envelopes "
                      "(never started vs heartbeat expired)",
                      ok_env, json.dumps(list(by_id.values()),
                                         ensure_ascii=False)[:400])
            else:
                check("receiver got orphan envelopes", False, "none captured")

            # 真实运行不回归(领租+续租+检查点)
            ra = await c.post(f"{API}/kbs",
                              json={"name": f"m23回归库{SUFFIX}"},
                              headers=admin)
            ra.raise_for_status()
            kb_h = ra.json()["id"]
            kb_ids.append(kb_h)
            for i in range(3):
                await c.post(f"{API}/eval/questions", json={
                    "kb_id": kb_h, "question": f"m23{SUFFIX}-h{i} 回归题",
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
            check("real retrieval run completes (no regression)",
                  d.get("status") == "completed",
                  f"status={d.get('status')} err={str(d.get('error'))[:80]}")
            hb = (await sql("SELECT heartbeat_at FROM eval_runs WHERE id=:i",
                            {"i": run_h}, fetch=True))[0]["heartbeat_at"]
            check("claim+renewal intact after real run",
                  hb is not None and hb >= t0, f"hb={hb} t0={t0}")

            doc = (BACKEND_DIR.parent / "docs" / "webhooks.md").read_text(
                encoding="utf-8")
            check("docs carry both orphan error forms",
                  "heartbeat expired (worker died" in doc
                  and "never started (queue grace exceeded" in doc)

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
            await _cleanup(c, admin)
    summary_and_exit()


if __name__ == "__main__":
    asyncio.run(main())
