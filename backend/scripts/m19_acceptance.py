"""M19 无头验收(真栈:8001 + worker + beat;worker/start_worker.bat 与
beat/start_beat.bat 必须已起;dev .env 需 WEBHOOK_SSRF_ALLOWLIST=127.0.0.1)。

覆盖:wecom secret 卫生(创建响应 secret=None 不回显 + 列表 secret_masked
空串)/ 评估取消全链路(触发→立即 cancel→cancelling→轮询 cancelled,
item_count 收缩为已完成子集;终态后同库同 mode 重触发不 409;终态再
cancel 409)/ 题集 export→bulk 回环(无 id 键 + Content-Disposition 附件
名;删 2 题后整包导回 created==3)/ bulk 部分成功(空白题干进
errors[index])/ viewer 负例(第二账号注册即 viewer,未授权对他人 run/库
统一 404)/ 清理(端点与 KB 走 API DELETE,SQL 兜底)。

取消窗口 mode 自动:.env 有 ZHIPU_API_KEY → generation(真 LLM 每题十数
秒,窗口宽,立即 cancel 必落在题间);否则 retrieval(检索秒级完成,
「立即 cancel」可能已终态→409,该情况记 SKIP 并注明,重触发/终态 409
分支照走)。

坏 allowlist 分支仅单测覆盖(T1 fail-open),真栈不做:allowlist 是进程级
settings,验收栈必须保持 127.0.0.1 放行(本脚本建端点依赖它),无法在
真栈上安全注入坏值。

M19 无投递断言(wecom 端点 events=[] 且不调 /test),不带 m18 的本地
receiver 线程。
"""
import asyncio
import sys
import time
import uuid
from pathlib import Path

import httpx

# `python scripts/m19_acceptance.py` 直跑时 sys.path[0] 是 scripts 目录;
# app 是 editable 安装可导入,但 scripts.* 不是——补 backend 根(m17/m18 同款)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8001"
API = f"{BASE}/api"
TIMEOUT = httpx.Timeout(120.0)
RESULTS = {"pass": [], "fail": [], "skip": []}

SUFFIX = uuid.uuid4().hex[:6]


def check(name, cond, detail=""):
    (RESULTS["pass"] if cond else RESULTS["fail"]).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + (f"  {detail}" if detail and not cond else ""))


def summary_and_exit():
    total = sum(len(v) for v in RESULTS.values())
    print(f"\nM19 ACCEPTANCE: {len(RESULTS['pass'])}/{total} PASS")
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
            # eval_questions 随 KB FK CASCADE;eval_runs 无 FK 按设计保留
            # 历史(m15 同款);上面 API 已删的库此处删 0 行,仅兜底
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


async def register_viewer(c: httpx.AsyncClient, name: str) -> tuple[str, int]:
    """注册即 viewer(User.role 默认 viewer),不提升——⑤ 负例正要用默认
    角色(与被授 viewer perm 的 403 分支不同,无授权行 → get_kb_perm None
    → 统一 404,eval.py 越权不可见语义)。"""
    r = await c.post(f"{API}/auth/register",
                     json={"username": name, "password": "secret123"})
    r.raise_for_status()
    return name, r.json()["id"]


async def add_questions(c: httpx.AsyncClient, jwt: dict, kb_id: int,
                        specs: list[dict]) -> list[int]:
    ids = []
    for spec in specs:
        r = await c.post(f"{API}/eval/questions",
                         json={**spec, "kb_id": kb_id}, headers=jwt)
        r.raise_for_status()
        ids.append(r.json()["id"])
    return ids


async def questions_total(c: httpx.AsyncClient, jwt: dict, kb_id: int) -> int:
    r = await c.get(f"{API}/eval/questions", params={"kb_id": kb_id},
                    headers=jwt)
    r.raise_for_status()
    return r.json()["total"]


async def wait_run(c: httpx.AsyncClient, jwt: dict, run_id: int,
                   timeout_s: int) -> dict:
    """轮询 run 明细至终态(completed/failed/cancelled;每 2s);超时返回
    最后一次响应体,由调用方按 status 断言(m15 wait_completed 改款:
    终态集扩 M19 的 cancelled)。"""
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


async def main() -> None:
    from app.core.config import settings

    # 取消窗口 mode:有真 key 走 generation(LLM 慢,窗口宽);否则 retrieval
    mode = "generation" if settings.ZHIPU_API_KEY else "retrieval"
    settle_s = 300 if mode == "generation" else 120
    print(f"[info] cancel-flow mode={mode}"
          + (" (ZHIPU_API_KEY set)" if mode == "generation"
             else " (no key; immediate cancel may race to 409 -> SKIP)"))

    user_ids, kb_ids, usernames, endpoint_ids = [], [], [], []
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        try:
            # ① admin 登录 + wecom secret 卫生(创建不回显 / 列表 masked 空)
            admin = await login(c, "admin")
            check("admin login ok", bool(admin.get("Authorization")))
            r = await c.post(f"{API}/admin/webhooks", json={
                "name": f"m19wx{SUFFIX}",
                "url": "http://127.0.0.1:1/never-delivered",
                "provider": "wecom", "events": []}, headers=admin)
            j = r.json() if r.status_code == 201 else {}
            check("wecom create 201 secret is None (never echoed)",
                  r.status_code == 201 and j.get("secret") is None,
                  f"HTTP {r.status_code} {r.text[:200]}")
            r.raise_for_status()
            ep_wx = j["id"]
            endpoint_ids.append(ep_wx)
            r = await c.get(f"{API}/admin/webhooks", headers=admin)
            row = next((w for w in r.json() if w["id"] == ep_wx), None)
            check("wecom list secret_masked == ''",
                  row is not None and row.get("secret_masked") == "",
                  str(row))

            # ② 取消全链路:建 KB+3 题(全不带期望,检索路径快)→ 触发 →
            #    立即 cancel → cancelling → 轮询 cancelled(≤120s)
            ra = await c.post(f"{API}/kbs",
                              json={"name": f"m19取消库{SUFFIX}"},
                              headers=admin)
            ra.raise_for_status()
            kb_c = ra.json()["id"]
            kb_ids.append(kb_c)
            qids = await add_questions(c, admin, kb_c, [
                {"question": f"m19{SUFFIX}-c0 取消窗口题一",
                 "expect_doc_ids": [], "expect_keywords": []},
                {"question": f"m19{SUFFIX}-c1 取消窗口题二",
                 "expect_doc_ids": [], "expect_keywords": []},
                {"question": f"m19{SUFFIX}-c2 取消窗口题三",
                 "expect_doc_ids": [], "expect_keywords": []},
            ])
            check("cancel kb + 3 bare questions created", len(qids) == 3)
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
                      r.json().get("id") == run1
                      and r.json().get("status") == "cancelling",
                      r.text[:200])
                if mode == "generation":
                    # 幂等分支(cancelling 再 cancel 200):仅生成模式做——
                    # 检索模式此刻可能已翻终态,二次 cancel 会 409 竞态
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
                check("cancelled subset honest (items & summary agree)",
                      len(d.get("items") or []) == d.get("item_count")
                      and (d.get("summary") or {}).get("item_count")
                      == d.get("item_count"),
                      f"items={len(d.get('items') or [])} "
                      f"ic={d.get('item_count')} "
                      f"summary={str(d.get('summary'))[:80]}")
            else:
                # retrieval 秒级完成:cancel 落在终态后 → 409,记 SKIP 并注明
                RESULTS["skip"].append(
                    f"cancel roundtrip (immediate cancel -> HTTP "
                    f"{r.status_code}: {mode} run already terminal)")
                print(f"SKIP cancel roundtrip  (immediate cancel -> HTTP "
                      f"{r.status_code}; run finished within the race "
                      "window, plan-sanctioned)")
                d = await wait_run(c, admin, run1, 120)

            # 终态后同库同 mode 重触发:dup 守卫仅拦 running → 201 不 409
            r = await c.post(f"{API}/eval/runs",
                             json={"kb_id": kb_c, "mode": mode},
                             headers=admin)
            check("re-trigger same kb+mode 201 (dup guard running-only)",
                  r.status_code == 201, f"HTTP {r.status_code} {r.text[:120]}")
            r.raise_for_status()
            run2 = r.json()["run_id"]
            # 第二轮只为 201 断言:cancel 赶上窗口就用它终结(省 LLM 时),
            # 否则等自然完成——断言只要求收敛到终态,不留孤儿 running
            r2c = await c.post(f"{API}/eval/runs/{run2}/cancel",
                               headers=admin)
            d2 = await wait_run(c, admin, run2, settle_s)
            check("second run settles terminal (no orphan running)",
                  d2.get("status") in ("completed", "cancelled", "failed"),
                  f"cancel HTTP {r2c.status_code} status={d2.get('status')}")
            # 终态 run 再 cancel → 409
            r = await c.post(f"{API}/eval/runs/{run1}/cancel", headers=admin)
            check("cancel on terminal run 409", r.status_code == 409,
                  f"HTTP {r.status_code}")

            # ③ export→bulk 回环:3 题带各种期望字段 → 导出(无 id 键/
            #    disposition/字段保真)→ 删 2 题 → 整包导回 created==3
            rb = await c.post(f"{API}/kbs",
                              json={"name": f"m19回环库{SUFFIX}"},
                              headers=admin)
            rb.raise_for_status()
            kb_r = rb.json()["id"]
            kb_ids.append(kb_r)
            specs = [
                {"question": f"m19{SUFFIX}-r0 空期望题:导出包含哪些字段?",
                 "expect_doc_ids": [], "expect_keywords": []},
                {"question": f"m19{SUFFIX}-r1 关键词题:批量导入上限多少条?",
                 "expect_doc_ids": [], "expect_keywords": ["导入", "上限"]},
                {"question": f"m19{SUFFIX}-r2 参考答案题:取消是何种机制?",
                 "expect_doc_ids": [1, 2], "expect_keywords": ["协作"],
                 "reference_answer": "协作式取消:逐题检查点收口。"},
            ]
            qids = await add_questions(c, admin, kb_r, specs)
            check("roundtrip kb + 3 questions (mixed expectation fields)",
                  len(qids) == 3)
            r = await c.get(f"{API}/eval/questions/export",
                            params={"kb_id": kb_r}, headers=admin)
            disp = r.headers.get("content-disposition", "")
            body = r.json() if r.status_code == 200 else {}
            exported = body.get("questions") or []
            check("export 200: count==3 kb_name, no id keys, disposition",
                  r.status_code == 200 and body.get("count") == 3
                  and body.get("kb_name") == f"m19回环库{SUFFIX}"
                  and f"eval-questions-kb{kb_r}.json" in disp
                  and len(exported) == 3
                  and all(set(q) == {"question", "expect_doc_ids",
                                     "expect_keywords", "reference_answer"}
                          for q in exported),
                  f"HTTP {r.status_code} disp={disp} "
                  f"count={body.get('count')}")
            r.raise_for_status()
            codes = [(await c.delete(f"{API}/eval/questions/{qid}",
                                     headers=admin)).status_code
                     for qid in qids[:2]]
            check("delete 2 questions 204", codes == [204, 204], str(codes))
            check("total==1 after deletes",
                  await questions_total(c, admin, kb_r) == 1)
            r = await c.post(f"{API}/eval/questions/bulk",
                             json={"kb_id": kb_r, "questions": exported},
                             headers=admin)
            j = r.json() if r.status_code == 201 else {}
            check("bulk re-import full export 201 created==3 errors==[]",
                  r.status_code == 201 and j.get("created") == 3
                  and j.get("errors") == [],
                  f"HTTP {r.status_code} {r.text[:200]}")
            r.raise_for_status()
            check("total==4 after re-import (1 kept + 3 imported)",
                  await questions_total(c, admin, kb_r) == 4)
            r = await c.get(f"{API}/eval/questions",
                            params={"kb_id": kb_r, "page_size": 100},
                            headers=admin)
            back = next((i for i in r.json()["items"]
                         if i["question"] == specs[2]["question"]), None)
            check("re-imported fields intact (docs/keywords/reference)",
                  back is not None and back.get("expect_doc_ids") == [1, 2]
                  and back.get("expect_keywords") == ["协作"]
                  and back.get("reference_answer")
                  == specs[2]["reference_answer"],
                  str(back))

            # ④ bulk 部分成功:空白题干进 errors[index],合法条照常创建
            r = await c.post(f"{API}/eval/questions/bulk", json={
                "kb_id": kb_r,
                "questions": [
                    {"question": f"m19{SUFFIX}-p0 部分成功合法题一"},
                    {"question": "   "},
                    {"question": f"m19{SUFFIX}-p2 部分成功合法题二"},
                ]}, headers=admin)
            j = r.json() if r.status_code == 201 else {}
            errs = j.get("errors") or []
            check("bulk partial success: created==2, errors[0].index==1 "
                  "detail has 题干",
                  r.status_code == 201 and j.get("created") == 2
                  and len(errs) == 1 and errs[0].get("index") == 1
                  and "题干" in str(errs[0].get("detail")),
                  f"HTTP {r.status_code} {r.text[:200]}")
            check("total==6 after partial bulk (4 + 2)",
                  await questions_total(c, admin, kb_r) == 6)

            # ⑤ viewer 负例:注册即 viewer、无任何授权 → 他人 run/库统一
            #    404(get_kb_perm None;授 viewer perm 才是 403 分支,不在
            #    本脚本造)——cancel / run 明细 / export / bulk 四面
            v_name, v_id = await register_viewer(c, f"m19_{SUFFIX}_viewer")
            usernames.append(v_name)
            user_ids.append(v_id)
            viewer = await login(c, v_name)
            r = await c.post(f"{API}/eval/runs/{run1}/cancel", headers=viewer)
            check("viewer cancel other's run 404 (invisible kb)",
                  r.status_code == 404, f"HTTP {r.status_code}")
            r = await c.get(f"{API}/eval/runs/{run1}", headers=viewer)
            check("viewer get other's run 404", r.status_code == 404,
                  f"HTTP {r.status_code}")
            r = await c.get(f"{API}/eval/questions/export",
                            params={"kb_id": kb_r}, headers=viewer)
            check("viewer export 404", r.status_code == 404,
                  f"HTTP {r.status_code}")
            r = await c.post(f"{API}/eval/questions/bulk",
                             json={"kb_id": kb_r,
                                   "questions": [{"question": "x"}]},
                             headers=viewer)
            check("viewer bulk 404", r.status_code == 404,
                  f"HTTP {r.status_code}")

            # ⑥ 清理:验收端点 DELETE 204 → 列表消隐;KB 走 API DELETE
            #    (eval_questions 随库级联);SQL 兜底在 finally
            r = await c.delete(f"{API}/admin/webhooks/{ep_wx}", headers=admin)
            check("wecom endpoint deleted 204", r.status_code == 204,
                  f"HTTP {r.status_code}")
            r = await c.get(f"{API}/admin/webhooks", headers=admin)
            check("deleted endpoint absent from list",
                  all(w["id"] != ep_wx for w in r.json()))
            codes = [(await c.delete(f"{API}/kbs/{k}", headers=admin)
                      ).status_code for k in kb_ids]
            check("acceptance KBs deleted 204 (API)",
                  bool(codes) and all(x == 204 for x in codes), str(codes))
        finally:
            await cleanup(user_ids, kb_ids, usernames, endpoint_ids)
    summary_and_exit()


if __name__ == "__main__":
    asyncio.run(main())
