"""M13 无头验收脚本(真栈:http://127.0.0.1:8001 + worker + Redis + 智谱真 key)。

用法(backend 目录,项目 venv,start_dev.bat / start_worker.bat 已起服务):
    .venv\\Scripts\\python scripts\\m13_acceptance.py

覆盖:Web 面上传→done / 拒答二审真 LLM(合成题+通识题 refused)/
正常事实题不拒答 / SSE 首包延迟记录([info],不判 PASS/FAIL)/
检索+生成评估入库(eval_runs ≥2 且 generation summary 键)/
KB 重命名(200 回显/重名 409/审计 kb_update)/ m12 配对断言真栈补验(子进程)。
"""
import asyncio
import io
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

# `python scripts/m13_acceptance.py` 直跑时 sys.path[0] 是 scripts 目录;
# app 是 editable 安装可导入,但 scripts.* 不是——补 backend 根
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8001"
API = f"{BASE}/api"
TIMEOUT = httpx.Timeout(120.0)
SSE_TIMEOUT = httpx.Timeout(600.0, connect=10.0)  # 真 LLM 全链路(含二审)宽限
RESULTS = {"pass": [], "fail": [], "skip": []}

SUFFIX = uuid.uuid4().hex[:6]
FACT = f"白鲸灯塔编号BJ-{SUFFIX}的塔高八十八米"
FACT_Q = f"白鲸灯塔编号BJ-{SUFFIX}的塔高多少米?"
Q_MISS_SYNTH = f"彗星捕手计划{SUFFIX}的发射窗口是哪天?"
Q_MISS_GENERAL = "红楼梦的作者是谁?"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

BACKEND_DIR = Path(__file__).resolve().parents[1]
EVAL_SET_PATH = BACKEND_DIR / "eval_sets" / "__KB__.json"


def check(name, cond, detail=""):
    (RESULTS["pass"] if cond else RESULTS["fail"]).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + (f"  {detail}" if detail and not cond else ""))


def summary_and_exit():
    total = sum(len(v) for v in RESULTS.values())
    print(f"\nM13 ACCEPTANCE: {len(RESULTS['pass'])}/{total} PASS")
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
            # m13 特有:ask SSE 会为本次用户建 conversations/messages(user_id
            # 有 FK,先于 users 删除;m12 cleanup 无此步因 m12 不触发对话)
            for uid in user_ids:
                await s.execute(
                    text("DELETE FROM messages WHERE conversation_id IN "
                         "(SELECT id FROM conversations WHERE user_id = :u)"),
                    {"u": uid})
                await s.execute(
                    text("DELETE FROM conversations WHERE user_id = :u"),
                    {"u": uid})
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


def make_docx_bytes() -> bytes:
    """纯文本 .docx 最小构造:用 python-docx(backend venv 已装,M2 解析依赖)。"""
    from docx import Document as Dx

    dx = Dx()
    dx.add_paragraph(FACT)
    buf = io.BytesIO()
    dx.save(buf)
    return buf.getvalue()


async def ask_sse(c: httpx.AsyncClient, jwt: dict, kb_id: int,
                  question: str) -> tuple[dict, float | None, float]:
    """POST /api/chat/ask SSE:逐行读 data: JSON 至 done 帧。

    返回 (done 负载, 首个 token 帧秒数|None, done 帧秒数);异常/断流时
    done 负载为 {"__error__": ...} 便于判定层报 detail。
    """
    t0 = time.perf_counter()
    first_token: float | None = None
    done: dict = {}
    try:
        async with c.stream(
            "POST", f"{API}/chat/ask",
            json={"kb_ids": [kb_id], "question": question},
            headers={**jwt, "Accept": "text/event-stream"},
            timeout=SSE_TIMEOUT,
        ) as r:
            if r.status_code != 200:
                body = (await r.aread()).decode("utf-8", "replace")
                return {"__error__": f"HTTP {r.status_code}: {body[:200]}"}, \
                    None, time.perf_counter() - t0
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    evt = json.loads(line[len("data: "):])
                except json.JSONDecodeError:
                    continue
                etype = evt.get("type")
                if etype == "token" and first_token is None:
                    first_token = time.perf_counter() - t0
                elif etype == "citations":
                    done["citations"] = evt.get("data") or []
                elif etype == "done":
                    done.update(evt.get("data") or {})
                    done["__done__"] = True
                    break
                elif etype == "error":
                    done["__error__"] = str(evt.get("data"))[:200]
                    break
    except Exception as exc:  # 网络/超时:归入判定层 detail
        done.setdefault("__error__", f"{type(exc).__name__}: {exc}")
    return done, first_token, time.perf_counter() - t0


def run_cli(args: list[str], timeout_s: int) -> tuple[int, str]:
    """跑 backend CLI 子进程(venv python;cwd=backend),输出透传。"""
    proc = subprocess.run(
        [sys.executable, *args], cwd=BACKEND_DIR, timeout=timeout_s,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    print(f"--- {' '.join(args)} (exit={proc.returncode}) ---")
    print(out.rstrip() or "(no output)")
    return proc.returncode, out


async def main() -> None:
    user_ids, kb_ids, usernames = [], [], []
    kb_in = None
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        try:
            # ① u1(editor)建库 → Web 面上传含 FACT 的 docx → 轮询 done
            u1_name, u1_id = await make_user(c, f"m13_{SUFFIX}_u1", "editor")
            usernames.append(u1_name)
            user_ids.append(u1_id)
            jwt1 = await login(c, u1_name)

            r = await c.post(f"{API}/kbs", json={"name": f"m13验收库{SUFFIX}"},
                             headers=jwt1)
            r.raise_for_status()
            kb_in = r.json()["id"]
            kb_ids.append(kb_in)

            up = await c.post(
                f"{API}/kbs/{kb_in}/documents",
                files={"file": (f"m13-{SUFFIX}.docx", make_docx_bytes(),
                                DOCX_MIME)},
                data={"ocr": "off"}, headers=jwt1,
            )
            check("web upload 201", up.status_code == 201,
                  f"status={up.status_code} body={up.text[:200]}")
            doc_id = up.json()["id"]
            status = None
            for _ in range(60):
                g = await c.get(f"{API}/documents/{doc_id}", headers=jwt1)
                status = g.json()["status"]
                if status in ("done", "failed"):
                    break
                await asyncio.sleep(3)
            check("web poll done", status == "done", f"status={status}")

            # ② 拒答二审(真 LLM):库内无答案的两题,done 帧 refused 均 True
            d1, _, _ = await ask_sse(c, jwt1, kb_in, Q_MISS_SYNTH)
            check("refusal recheck synthetic",
                  d1.get("__done__") and d1.get("refused") is True,
                  f"done={ {k: d1.get(k) for k in ('refused', '__error__', '__done__')} }")
            d2, _, _ = await ask_sse(c, jwt1, kb_in, Q_MISS_GENERAL)
            check("refusal recheck general",
                  d2.get("__done__") and d2.get("refused") is True,
                  f"done={ {k: d2.get(k) for k in ('refused', '__error__', '__done__')} }")

            # ③ 正常事实题:不拒答,且答案含"八十八"或 citations 非空
            d3, _, _ = await ask_sse(c, jwt1, kb_in, FACT_Q)
            ok3 = (d3.get("__done__") and d3.get("refused") is False
                   and ("八十八" in (d3.get("answer") or "")
                        or d3.get("citations")))
            check("fact answer not refused", bool(ok3),
                  f"done={ {k: d3.get(k) for k in ('refused', 'answer', 'citations', '__error__')} }")

            # ④ 延迟记录([info],不判 PASS/FAIL):FACT_Q 再问一次
            d4, first_tok, total = await ask_sse(c, jwt1, kb_in, FACT_Q)
            ft = f"{first_tok:.1f}" if first_tok is not None else "N/A"
            print(f"[info] SSE first-token={ft}s total={total:.1f}s"
                  f" (done={d4.get('__done__')})")

            # ⑤ 评估入库:现场写 eval_sets/{kb_in}.json(2 题)→ 两个 CLI --save
            #    → eval_runs 查询断言(行保留是设计意图,cleanup 不删)
            eval_path = Path(str(EVAL_SET_PATH).replace("__KB__", str(kb_in)))
            eval_path.parent.mkdir(parents=True, exist_ok=True)
            eval_path.write_text(json.dumps({
                "kb_id": kb_in,
                "items": [
                    {"question": FACT_Q,
                     "expect_doc_ids": [doc_id],
                     "expect_keywords": ["白鲸", "八十八"],
                     "reference_answer": "八十八米"},
                    {"question": Q_MISS_GENERAL,
                     "expect_doc_ids": [],
                     "expect_keywords": []},
                ],
            }, ensure_ascii=False, indent=2), encoding="utf-8")

            rc_r, _ = run_cli(["-m", "scripts.eval_retrieval",
                               "--kb", str(kb_in), "--save"], 300)
            check("eval retrieval saved", rc_r == 0)
            rc_g, _ = run_cli(["-m", "scripts.eval_generation",
                               "--kb", str(kb_in), "--save"], 900)
            check("eval generation saved", rc_g == 0)

            from scripts.eval_runs import query as eval_query

            rows = await eval_query(kb_in, None, 10)
            check("eval runs >=2 rows", len(rows) >= 2,
                  f"rows={[(r['id'], r['mode']) for r in rows]}")
            gen = [r for r in rows if r["mode"] == "generation"]
            skeys = (gen[0]["summary"].keys() if gen else set())
            check("generation summary keys",
                  bool(gen) and {"faithfulness_avg", "reference_avg"} <= skeys,
                  f"summary_keys={sorted(skeys)}")

            # ⑥ KB 重命名:200 回显新名 → 重名 409 → 审计 kb_update 绑定
            kb_other_name = f"m13占位库{SUFFIX}"
            r = await c.post(f"{API}/kbs", json={"name": kb_other_name},
                             headers=jwt1)
            r.raise_for_status()
            kb_ids.append(r.json()["id"])

            new_name = f"m13改名库{SUFFIX}"
            rn = await c.put(f"{API}/kbs/{kb_in}", json={"name": new_name},
                             headers=jwt1)
            check("kb rename 200 echo", rn.status_code == 200
                  and rn.json().get("name") == new_name,
                  f"status={rn.status_code} body={rn.text[:200]}")
            dup = await c.put(f"{API}/kbs/{kb_in}",
                              json={"name": kb_other_name}, headers=jwt1)
            check("kb rename duplicate 409", dup.status_code == 409,
                  f"status={dup.status_code} body={dup.text[:200]}")

            from sqlalchemy import select

            from app.models import AuditLog

            engine, maker = _nullpool_sessionmaker()
            try:
                async with maker() as s6:
                    acts = (await s6.execute(
                        select(AuditLog.action).where(
                            AuditLog.action == "kb_update",
                            AuditLog.target == f"kb:{kb_in}",
                        )
                    )).scalars().all()
            finally:
                await engine.dispose()
            check("audit kb_update bound", len(acts) >= 1,
                  f"actions={acts}")

            # ⑦ m12 配对断言真栈补验(T6⑥):子进程跑 m12 验收,退出码 0
            rc_m, _ = run_cli(["scripts/m12_acceptance.py"], 600)
            check("m12 acceptance subprocess", rc_m == 0,
                  "see m12 output above")
        finally:
            await cleanup(user_ids, kb_ids, usernames)
            if kb_in is not None:
                eval_path = Path(str(EVAL_SET_PATH).replace("__KB__",
                                                            str(kb_in)))
                eval_path.unlink(missing_ok=True)
            try:
                from scripts.eval_runs import query as eval_query

                kept = len(await eval_query(kb_in, None, 100))
            except Exception as exc:
                kept = -1
                print(f"[warn] eval run count failed: {exc}")
            print(f"[info] eval run rows kept: {kept}")
    summary_and_exit()


if __name__ == "__main__":
    asyncio.run(main())
