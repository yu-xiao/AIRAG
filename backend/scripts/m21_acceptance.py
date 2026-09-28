"""M21 无头验收(真栈:8001 + worker + beat;dev .env 已带
WEBHOOK_SSRF_ALLOWLIST=127.0.0.1,receiver 靠它)。

覆盖:①失败回路(M21 failed 条件收口真栈)——SQL 插 running run + 子进程
以 ZHIPU_API_KEY="" eager 直调 run_evaluation(env 变量压过 .env,conftest
同款先例):首题 RuntimeError→_finalize_run(error=…) 条件落库 failed +
eval.failed 事件 nudge→真 worker 投递→receiver 实收信封;断言负载 run
形状与 completed 统一(item_count/summary 在)+error 字段在 ②beat stale
sweep——SQL 插 running/cancelling 两行 NULL 心跳→beat 60s 周期收口
failed,孤儿文案「orphaned: heartbeat expired」③fresh 心跳行不被收口
(≥2 个 beat tick 后仍 running——stale-only 语义的真栈判别:旧代码会
无条件收口,此项过=新代码确在跑)④真实评估运行中心跳续租——API 触发
retrieval run,跑毕 heartbeat_at 非空且晚于触发前时刻 ⑤docs/webhooks.md
头部旧标签清除 + 逐事件负载示例节在 ⑥清理(端点/KB 走 API,run 行 SQL
兜底,eval_items 随 FK CASCADE)。

竞态窄窗(failed 撞取消等)单测+guard-flip seam 已覆盖,真栈验对外行为。
"""
import asyncio
import http.server
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import timedelta
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
    print(f"\nM21 ACCEPTANCE: {len(RESULTS['pass'])}/{total} PASS")
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
    engine, maker = _nullpool_sessionmaker()
    try:
        async with maker() as s:
            res = await s.execute(
                __import__("sqlalchemy").text(sql_text), params or {})
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


async def wait_delivery(c, jwt, endpoint_id, event_type, timeout_s=45):
    t0 = time.perf_counter()
    while True:
        r = await c.get(f"{API}/admin/webhook-deliveries",
                        params={"endpoint_id": endpoint_id,
                                "event_type": event_type, "page_size": 10},
                        headers=jwt)
        rows = r.json().get("items") or []
        if rows and rows[0].get("status") in ("succeeded", "dead") \
                or time.perf_counter() > t0 + timeout_s:
            return rows
        await asyncio.sleep(2)


def eager_fail_run(run_id: int) -> None:
    """子进程以 ZHIPU_API_KEY="" eager 直调任务(env 压过 .env):
    首题 RuntimeError(ZHIPU_API_KEY 未配置)→ M21 failed 条件收口。"""
    env = dict(os.environ)
    env["ZHIPU_API_KEY"] = ""
    code = (f"from app.workers.eval_tasks import run_evaluation; "
            f"run_evaluation.run({run_id}, 'generation', False, 8)")
    r = subprocess.run(
        [str(BACKEND_DIR / ".venv" / "Scripts" / "python"), "-c", code],
        cwd=str(BACKEND_DIR), env=env, capture_output=True, text=True,
        timeout=180)
    if r.returncode != 0:
        print(f"[warn] eager subprocess rc={r.returncode}: "
              f"{(r.stderr or r.stdout)[-400:]}")


async def main() -> None:
    from app.core.timeutil import utcnow_naive

    srv = start_receiver()
    port = srv.server_address[1]
    print(f"[info] receiver on 127.0.0.1:{port}")

    kb_ids, endpoint_ids, run_ids = [], [], []
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        try:
            admin = await login(c, "admin")
            check("admin login ok", bool(admin.get("Authorization")))

            # ---- ① 失败回路:eval.failed 条件落库 + 真投递 + 负载形状 ----
            ra = await c.post(f"{API}/kbs",
                              json={"name": f"m21失败库{SUFFIX}"},
                              headers=admin)
            ra.raise_for_status()
            kb_f = ra.json()["id"]
            kb_ids.append(kb_f)
            for i in range(2):
                await c.post(f"{API}/eval/questions", json={
                    "kb_id": kb_f, "question": f"m21{SUFFIX}-f{i} 失败题",
                    "expect_doc_ids": [], "expect_keywords": []},
                    headers=admin)
            r = await c.post(f"{API}/admin/webhooks", json={
                "name": f"m21fl{SUFFIX}",
                "url": f"http://127.0.0.1:{port}/fail",
                "provider": "generic", "events": ["eval.failed"]},
                headers=admin)
            r.raise_for_status()
            ep_fl = r.json()["id"]
            endpoint_ids.append(ep_fl)

            rows = await sql(
                "INSERT INTO eval_runs (kb_id, mode, summary, item_count, "
                "status, triggered_by) VALUES (:k, 'generation', NULL, 0, "
                "'running', 1) RETURNING id", {"k": kb_f}, fetch=True)
            run_f = rows[0]["id"]
            run_ids.append(run_f)
            eager_fail_run(run_f)
            d = await wait_status(c, admin, run_f, ("failed",), 60)
            check("scrubbed-key run settles failed", d.get("status") == "failed",
                  f"status={d.get('status')}")
            check("failed run error names ZHIPU_API_KEY",
                  "ZHIPU_API_KEY" in str(d.get("error") or ""),
                  f"err={str(d.get('error'))[:120]}")
            rows = await wait_delivery(c, admin, ep_fl, "eval.failed")
            check("delivery row eval.failed exists & succeeded",
                  len(rows) == 1 and rows[0].get("status") == "succeeded",
                  str(rows)[:200])
            envs = []
            for p in packets():
                if p["path"] != "/fail":
                    continue
                try:
                    env = json.loads(p["body"])
                    if env.get("event_type") == "eval.failed":
                        envs.append(env)
                except Exception:
                    pass
            if envs:
                dr = envs[0]["data"]
                run_obj = dr.get("run") or {}
                check("receiver envelope: error + unified run shape "
                      "(item_count/summary, M21)",
                      "ZHIPU_API_KEY" in str(dr.get("error") or "")
                      and run_obj.get("id") == run_f
                      and "item_count" in run_obj and "summary" in run_obj,
                      json.dumps(dr, ensure_ascii=False)[:300])
            else:
                check("receiver envelope: eval.failed received", False,
                      "no envelope captured")

            # ---- ② beat stale sweep + ③ fresh 心跳不被收口(判别项)----
            now = utcnow_naive()
            rows = await sql(
                "INSERT INTO eval_runs (kb_id, mode, summary, item_count, "
                "status, triggered_by, heartbeat_at) VALUES "
                "(:k, 'retrieval', NULL, 0, 'running', 1, NULL), "
                "(:k, 'retrieval', NULL, 0, 'cancelling', 1, :stale) "
                "RETURNING id, status", {"k": kb_f, "stale": now - timedelta(
                    minutes=30)}, fetch=True)
            for row in rows:
                run_ids.append(row["id"])
            stale_ids = [row["id"] for row in rows]
            rows = await sql(
                "INSERT INTO eval_runs (kb_id, mode, summary, item_count, "
                "status, triggered_by, heartbeat_at) VALUES "
                "(:k, 'retrieval', NULL, 0, 'running', 1, :fresh) "
                "RETURNING id", {"k": kb_f, "fresh": now}, fetch=True)
            fresh_id = rows[0]["id"]
            run_ids.append(fresh_id)
            print(f"[info] stale runs {stale_ids} + fresh run {fresh_id}; "
                  f"waiting for beat tick(s)...")

            d = await wait_status(c, admin, stale_ids[0], ("failed",), 150)
            check("beat sweep closes NULL-heartbeat running row",
                  d.get("status") == "failed", f"status={d.get('status')}")
            d = await wait_status(c, admin, stale_ids[1], ("failed",), 60)
            check("beat sweep closes stale-heartbeat cancelling row",
                  d.get("status") == "failed", f"status={d.get('status')}")
            err = str((await sql("SELECT error FROM eval_runs WHERE id = :i",
                                 {"i": stale_ids[0]}, fetch=True))[0]["error"])
            check("orphan error text updated",
                  err == "orphaned: heartbeat expired (worker died or "
                         "restarted)", f"err={err[:100]}")

            await asyncio.sleep(70)  # 再等 ≥1 个 beat tick,专攻 fresh 行
            d = await wait_status(c, admin, fresh_id, ("failed",), 1)
            check("fresh-heartbeat row survives beat sweep (stale-only)",
                  d.get("status") == "running", f"status={d.get('status')}")

            # ---- ④ 真实运行中心跳续租 ----
            ra = await c.post(f"{API}/kbs",
                              json={"name": f"m21心跳库{SUFFIX}"},
                              headers=admin)
            ra.raise_for_status()
            kb_h = ra.json()["id"]
            kb_ids.append(kb_h)
            for i in range(3):
                await c.post(f"{API}/eval/questions", json={
                    "kb_id": kb_h, "question": f"m21{SUFFIX}-h{i} 心跳题",
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
            hb = (await sql("SELECT heartbeat_at FROM eval_runs WHERE id = :i",
                            {"i": run_h}, fetch=True))[0]["heartbeat_at"]
            check("heartbeat renewed during real run (non-null, >= trigger)",
                  hb is not None and hb >= t0,
                  f"hb={hb} t0={t0}")

            # ---- ⑤ docs 事实 ----
            doc = (BACKEND_DIR.parent / "docs" / "webhooks.md").read_text(
                encoding="utf-8")
            check("webhooks.md stale M17/M18 header label gone",
                  "(AIRag M17/M18;" not in doc)
            sec_ok = ("事件负载示例" in doc and all(
                t in doc for t in ("document.done", "document.failed",
                                   "eval.completed", "eval.cancelled",
                                   "eval.failed", "chat.refused")))
            check("per-event payload examples section present (six events)",
                  sec_ok)

            # ---- ⑥ 清理 ----
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
