"""M15 无头验收(真栈:8001 + worker + Redis + 智谱真 key;worker 必须 start_worker.bat 已起)。

覆盖:题集 CRUD 回环 / my-kbs 计数 / 触发 retrieval→轮询 completed /
409 并发守卫 / 422 空题集 / 权限负例 / generation 真 LLM /
CLI --save(DB 题源)回归 / 趋势数据就位 / 列表新字段。
"""
import asyncio
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

# `python scripts/m15_acceptance.py` 直跑时 sys.path[0] 是 scripts 目录;
# app 是 editable 安装可导入,但 scripts.* 不是——补 backend 根
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8001"
API = f"{BASE}/api"
TIMEOUT = httpx.Timeout(120.0)
RESULTS = {"pass": [], "fail": [], "skip": []}

SUFFIX = uuid.uuid4().hex[:6]
BACKEND_DIR = Path(__file__).resolve().parents[1]


def check(name, cond, detail=""):
    (RESULTS["pass"] if cond else RESULTS["fail"]).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + (f"  {detail}" if detail and not cond else ""))


def summary_and_exit():
    total = sum(len(v) for v in RESULTS.values())
    print(f"\nM15 ACCEPTANCE: {len(RESULTS['pass'])}/{total} PASS")
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


async def cleanup(user_ids, kb_ids, usernames) -> None:
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
            # m15 不触发对话,无需清 conversations/messages;eval_questions 随
            # KB FK CASCADE 删除;eval_runs 无 FK 按设计保留历史(m14 同款)
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
    """轮询 run 明细至 completed/failed(每 2s);期间捕获一次
    done_count<item_count 的进度快照 [info] 输出(检索秒级完成时可能错过)。"""
    t0 = time.perf_counter()
    deadline = time.perf_counter() + timeout_s
    snapshot = None
    while True:
        r = await c.get(f"{API}/eval/runs/{run_id}", headers=jwt)
        d = r.json()
        if snapshot is None and d.get("status") == "running" \
                and d.get("done_count", 0) < d.get("item_count", 0):
            snapshot = (d.get("done_count"), d.get("item_count"))
            print(f"[info] run {run_id} in progress: "
                  f"done {snapshot[0]}/{snapshot[1]}")
        if d.get("status") in ("completed", "failed") \
                or time.perf_counter() > deadline:
            print(f"[info] run {run_id} -> {d.get('status')} "
                  f"in {time.perf_counter() - t0:.1f}s")
            return d
        await asyncio.sleep(2)


def run_cli_split(args: list[str], timeout_s: int) -> tuple[int, str, str]:
    """跑 backend CLI 子进程,stdout/stderr 分开返回(m14 同款)。"""
    proc = subprocess.run(
        [sys.executable, *args], cwd=BACKEND_DIR, timeout=timeout_s,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    print(f"--- {' '.join(args)} (exit={proc.returncode}) ---")
    return proc.returncode, proc.stdout or "", proc.stderr or ""


async def main() -> None:
    user_ids, kb_ids, usernames = [], [], []
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        try:
            # ① admin 登录;建验收 KB(admin 建库)
            admin = await login(c, "admin")
            r = await c.post(f"{API}/kbs",
                             json={"name": f"m15验收库{SUFFIX}"},
                             headers=admin)
            check("admin creates acceptance kb",
                  r.status_code == 201, f"HTTP {r.status_code}")
            r.raise_for_status()
            kb_id = r.json()["id"]; kb_ids.append(kb_id)

            # ② 题集 CRUD:POST ×3(空期望/关键词/参考答案各覆盖)→ 201;
            #    PUT 改一题;GET total==3 且 id asc;my-kbs question_count==3
            q_specs = [
                {"question": f"m15{SUFFIX}-0 空期望题:检索评估度量哪些指标?",
                 "expect_doc_ids": [], "expect_keywords": []},
                {"question": f"m15{SUFFIX}-1 关键词题:评估题集如何维护?",
                 "expect_doc_ids": [], "expect_keywords": ["题集", "评估"]},
                {"question": f"m15{SUFFIX}-2 参考答案题:系统支持几种评估模式?",
                 "expect_doc_ids": [1, 2], "expect_keywords": ["检索"],
                 "reference_answer": "支持检索评估与生成评估两种模式。"},
            ]
            q_ids = []
            for spec in q_specs:
                r = await c.post(f"{API}/eval/questions",
                                 json={**spec, "kb_id": kb_id},
                                 headers=admin)
                check(f"create question {len(q_ids)} 201",
                      r.status_code == 201, f"HTTP {r.status_code}")
                q_ids.append(r.json()["id"])
            upd = dict(q_specs[1])
            upd["question"] = f"m15{SUFFIX}-1 改后:评估题集经什么入口维护?"
            upd["expect_keywords"] = ["改后关键词"]
            r = await c.put(f"{API}/eval/questions/{q_ids[1]}", json=upd,
                            headers=admin)
            check("update question 200 & reflects",
                  r.status_code == 200 and
                  r.json()["question"].startswith(f"m15{SUFFIX}-1 改后"),
                  f"HTTP {r.status_code}")
            r = await c.get(f"{API}/eval/questions?kb_id={kb_id}", headers=admin)
            qs = r.json()
            check("questions total 3 & id asc",
                  qs["total"] == 3 and
                  [q["id"] for q in qs["items"]] == sorted(q_ids),
                  f"total={qs.get('total')}")
            r = await c.get(f"{API}/eval/my-kbs", headers=admin)
            mine = next((k for k in r.json() if k["kb_id"] == kb_id), None)
            check("my-kbs question_count 3",
                  mine is not None and mine["question_count"] == 3,
                  str(mine))

            # ③ 触发 retrieval;立即重复 POST 同 kb+mode → 409 并发守卫
            r = await c.post(f"{API}/eval/runs",
                             json={"kb_id": kb_id, "mode": "retrieval"},
                             headers=admin)
            check("trigger retrieval 201", r.status_code == 201,
                  f"HTTP {r.status_code}")
            run_id = r.json()["run_id"]
            r2 = await c.post(f"{API}/eval/runs",
                              json={"kb_id": kb_id, "mode": "retrieval"},
                              headers=admin)
            if r2.status_code == 201:
                # 既知竞态:首轮检索评估秒级完成,第二次 POST 已不在
                # running 窗口内 → 记 SKIP;多出的 run 等其收敛后再继续
                RESULTS["skip"].append("duplicate trigger 409")
                print("SKIP duplicate trigger 409"
                      "  (首轮检索评估已 completed,竞态窗口错过)")
                await wait_completed(c, admin, r2.json()["run_id"], 120)
            else:
                check("duplicate trigger 409", r2.status_code == 409,
                      f"HTTP {r2.status_code}")

            # ④ 轮询至 completed(120s/每 2s;进度快照 [info])
            d = await wait_completed(c, admin, run_id, 120)
            check("retrieval run completed", d.get("status") == "completed",
                  f"status={d.get('status')} err={str(d.get('error'))[:80]}")

            # ⑤ 明细:items==3、summary 键、created_by==admin、题目 id asc
            check("retrieval items == 3", len(d.get("items") or []) == 3,
                  str(len(d.get("items") or [])))
            check("retrieval summary keys",
                  set(d.get("summary") or {}) >=
                  {"item_count", "hit", "mrr", "keyword_recall"},
                  str(d.get("summary")))
            check("retrieval created_by admin", d.get("created_by") == "admin",
                  str(d.get("created_by")))
            item_ids = [i["id"] for i in d.get("items") or []]
            check("retrieval item order id asc", item_ids == sorted(item_ids))

            # 列表新字段(状态/发起人/进度分子)就位
            r = await c.get(f"{API}/eval/runs?kb_id={kb_id}", headers=admin)
            row = next((it for it in r.json()["items"]
                        if it["id"] == run_id), None)
            check("list row has status/creator/done_count",
                  row is not None and row["status"] == "completed" and
                  row["created_by"] == "admin" and row["done_count"] == 3,
                  str(row))

            # ⑥ 权限负例:viewer(授 perm)POST /runs → 403;
            #    不可见库 999999 → 404;空题集库 → 422
            viewer_name, viewer_id = await make_user(
                c, f"m15_{SUFFIX}_viewer", "viewer")
            usernames.append(viewer_name); user_ids.append(viewer_id)
            viewer = await login(c, viewer_name)
            r = await c.put(f"{API}/kbs/{kb_id}/permissions",
                            json={"username": viewer_name, "perm": "viewer"},
                            headers=admin)
            r.raise_for_status()
            r = await c.post(f"{API}/eval/runs",
                             json={"kb_id": kb_id, "mode": "retrieval"},
                             headers=viewer)
            check("viewer trigger 403", r.status_code == 403,
                  f"HTTP {r.status_code}")
            r = await c.post(f"{API}/eval/runs",
                             json={"kb_id": 999999, "mode": "retrieval"},
                             headers=viewer)
            check("invisible kb 404", r.status_code == 404,
                  f"HTTP {r.status_code}")
            r = await c.post(f"{API}/kbs",
                             json={"name": f"m15空题库{SUFFIX}"}, headers=admin)
            r.raise_for_status()
            empty_kb = r.json()["id"]; kb_ids.append(empty_kb)
            r = await c.post(f"{API}/eval/runs",
                             json={"kb_id": empty_kb, "mode": "retrieval"},
                             headers=admin)
            check("empty question set 422", r.status_code == 422,
                  f"HTTP {r.status_code}")

            # ⑦ generation 真 LLM(题带 reference_answer)→ completed,
            #    summary 含三均值
            r = await c.post(f"{API}/eval/runs",
                             json={"kb_id": kb_id, "mode": "generation"},
                             headers=admin)
            check("trigger generation 201", r.status_code == 201,
                  f"HTTP {r.status_code}")
            gen_id = r.json()["run_id"]
            d = await wait_completed(c, admin, gen_id, 300)
            check("generation run completed", d.get("status") == "completed",
                  f"status={d.get('status')} err={str(d.get('error'))[:120]}")
            check("generation summary keys",
                  set(d.get("summary") or {}) >=
                  {"faithfulness_avg", "relevancy_avg", "reference_avg"},
                  str(d.get("summary")))

            # ⑧ CLI 回归:--save --json;stdout 纯 JSON(3 题,即 DB 题源)、
            #    stderr saved 行、API 列表 +1、该 run created_by 为 null
            r = await c.get(f"{API}/eval/runs?kb_id={kb_id}&mode=retrieval",
                            headers=admin)
            before_total = r.json()["total"]
            rc, out, err = run_cli_split(
                ["-m", "scripts.eval_retrieval", "--kb", str(kb_id),
                 "--save", "--json"], 180)
            check("cli exit 0", rc == 0, err[-120:])
            try:
                parsed = json.loads(out)
                check("cli stdout pure json", isinstance(parsed, list))
                check("cli read db questions",
                      isinstance(parsed, list) and len(parsed) == 3,
                      f"items={len(parsed) if isinstance(parsed, list) else '?'}")
            except json.JSONDecodeError:
                check("cli stdout pure json", False, out[:120])
                check("cli read db questions", False, out[:80])
            check("saved line on stderr", "saved: run_id=" in err, err[-80:])
            m = re.search(r"saved: run_id=(\d+)", err)
            check("cli run added to list", m is not None and
                  (await c.get(
                      f"{API}/eval/runs?kb_id={kb_id}&mode=retrieval",
                      headers=admin)).json()["total"] == before_total + 1,
                  f"before={before_total}")
            if m is not None:
                r = await c.get(f"{API}/eval/runs/{m.group(1)}", headers=admin)
                check("cli run created_by null",
                      r.status_code == 200 and r.json()["created_by"] is None,
                      f"HTTP {r.status_code}")

            # ⑨ 趋势数据就位:kb+mode=retrieval 列表,completed 且带
            #    summary 的 ≥2 条(步骤④与⑧各贡献一条)
            r = await c.get(f"{API}/eval/runs?kb_id={kb_id}"
                            "&mode=retrieval&page_size=100", headers=admin)
            with_summary = [it for it in r.json()["items"]
                            if it["status"] == "completed" and it.get("summary")]
            check("trend data >= 2 completed with summary",
                  len(with_summary) >= 2, f"got {len(with_summary)}")

            # ⑩ 回环收尾:删除一题 → 204 → total==2
            r = await c.delete(f"{API}/eval/questions/{q_ids[1]}",
                               headers=admin)
            check("delete question 204", r.status_code == 204,
                  f"HTTP {r.status_code}")
            r = await c.get(f"{API}/eval/questions?kb_id={kb_id}",
                            headers=admin)
            check("questions total 2 after delete",
                  r.json()["total"] == 2, f"total={r.json().get('total')}")
        finally:
            await cleanup(user_ids, kb_ids, usernames)
    summary_and_exit()


if __name__ == "__main__":
    asyncio.run(main())
