"""M14 无头验收脚本(真栈:http://127.0.0.1:8001 + worker + Redis + 智谱真 key)。

用法(backend 目录,项目 venv,start_dev.bat / start_worker.bat 已起服务):
    .venv\\Scripts\\python scripts\\m14_acceptance.py

覆盖:评估只读 API 权限矩阵(admin 全量 / owner 自见 / 无关用户空集 /
不可见 404 / 可见非 owner 403 / 明细越权 404)/
eval CLI stdout 纯 JSON + saved 行 stderr(M14 C5)/
明细 items 与 item_count 一致 / KB 描述清空端到端(M14 C3)/
零命中问题 SSE 正常收尾(M14 C4 勘误后路由仍稳,首包延迟 [info] 不判 PASS/FAIL)。
"""
import asyncio
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

# `python scripts/m14_acceptance.py` 直跑时 sys.path[0] 是 scripts 目录;
# app 是 editable 安装可导入,但 scripts.* 不是——补 backend 根
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8001"
API = f"{BASE}/api"
TIMEOUT = httpx.Timeout(120.0)
SSE_TIMEOUT = httpx.Timeout(600.0, connect=10.0)  # 真 LLM 全链路(含二审)宽限
RESULTS = {"pass": [], "fail": [], "skip": []}

SUFFIX = uuid.uuid4().hex[:6]

BACKEND_DIR = Path(__file__).resolve().parents[1]
EVAL_SET_PATH = BACKEND_DIR / "eval_sets" / "__KB__.json"


def check(name, cond, detail=""):
    (RESULTS["pass"] if cond else RESULTS["fail"]).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + (f"  {detail}" if detail and not cond else ""))


def summary_and_exit():
    total = sum(len(v) for v in RESULTS.values())
    print(f"\nM14 ACCEPTANCE: {len(RESULTS['pass'])}/{total} PASS")
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


def run_cli_split(args: list[str], timeout_s: int) -> tuple[int, str, str]:
    """跑 backend CLI 子进程,stdout/stderr 分开返回(M14 验 C5)。"""
    proc = subprocess.run(
        [sys.executable, *args], cwd=BACKEND_DIR, timeout=timeout_s,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    print(f"--- {' '.join(args)} (exit={proc.returncode}) ---")
    return proc.returncode, proc.stdout or "", proc.stderr or ""


async def main() -> None:
    user_ids, kb_ids, usernames = [], [], []
    eval_path = None
    async with httpx.AsyncClient(timeout=TIMEOUT) as c:
        try:
            # ① admin 全量列表(基线)
            admin = await login(c, "admin")
            r = await c.get(f"{API}/eval/runs", headers=admin)
            check("admin list runs 200", r.status_code == 200,
                  f"total={r.json().get('total')}")
            baseline = r.json()["total"]

            # ② owner(editor)建库
            owner_name, owner_id = await make_user(
                c, f"m14_{SUFFIX}_owner", "editor")
            usernames.append(owner_name); user_ids.append(owner_id)
            owner = await login(c, owner_name)
            r = await c.post(f"{API}/kbs",
                             json={"name": f"m14验收库{SUFFIX}"},
                             headers=owner)
            r.raise_for_status()
            kb_id = r.json()["id"]; kb_ids.append(kb_id)

            # ③ 现场题集 → retrieval CLI --save --json(stdout 纯 JSON/saved 在 stderr)
            eval_path = Path(str(EVAL_SET_PATH).replace("__KB__", str(kb_id)))
            eval_path.parent.mkdir(parents=True, exist_ok=True)
            eval_path.write_text(json.dumps({
                "items": [
                    {"question": f"编号BJ-{SUFFIX}的灯塔高多少米?",
                     "expect_doc_ids": [], "expect_keywords": []},
                ]}, ensure_ascii=False))
            rc, out, err = run_cli_split(
                ["-m", "scripts.eval_retrieval", "--kb", str(kb_id),
                 "--save", "--json"], 180)
            check("cli exit 0", rc == 0, err[-120:])
            try:
                parsed = json.loads(out)
                check("cli stdout pure json", isinstance(parsed, list))
            except json.JSONDecodeError:
                check("cli stdout pure json", False, out[:120])
            check("saved line on stderr", "saved: run_id=" in err, err[-80:])

            # ④ owner 列表见新 run;明细 items 与 item_count 一致
            r = await c.get(f"{API}/eval/runs", headers=owner)
            body = r.json()
            ok_list = body["total"] == 1 and (
                body["items"][0]["kb_name"] == f"m14验收库{SUFFIX}"
                if body["items"] else False)
            check("owner sees own run", ok_list)
            run_id = body["items"][0]["id"] if body["items"] else -1
            r = await c.get(f"{API}/eval/runs/{run_id}", headers=owner)
            d = r.json()
            check("detail items match count",
                  r.status_code == 200 and
                  len(d["items"]) == d["item_count"])

            # ⑤ 权限矩阵:无关用户空集/不可见 404;授 viewer 后 403 + 明细 404
            stranger_name, stranger_id = await make_user(
                c, f"m14_{SUFFIX}_str", "viewer")
            usernames.append(stranger_name); user_ids.append(stranger_id)
            stranger = await login(c, stranger_name)
            r = await c.get(f"{API}/eval/runs", headers=stranger)
            check("stranger empty set", r.status_code == 200 and
                  r.json() == {"total": 0, "items": []})
            r = await c.get(f"{API}/eval/runs?kb_id={kb_id}",
                            headers=stranger)
            check("invisible kb 404", r.status_code == 404)
            r = await c.put(f"{API}/kbs/{kb_id}/permissions",
                            json={"username": stranger_name,
                                  "perm": "viewer"},
                            headers=owner)
            r.raise_for_status()
            r = await c.get(f"{API}/eval/runs?kb_id={kb_id}",
                            headers=stranger)
            check("visible non-owner 403", r.status_code == 403)
            r = await c.get(f"{API}/eval/runs/{run_id}", headers=stranger)
            check("detail non-owner 404", r.status_code == 404)

            # ⑥ admin 全量 +1
            r = await c.get(f"{API}/eval/runs", headers=admin)
            check("admin sees new run", r.json()["total"] == baseline + 1)

            # ⑦ 描述清空端到端
            r = await c.put(f"{API}/kbs/{kb_id}", json={"description": ""},
                            headers=owner)
            check("clear desc 200", r.status_code == 200)
            r = await c.get(f"{API}/kbs/{kb_id}", headers=owner)
            check("desc now null", r.json()["description"] is None)
            r = await c.put(f"{API}/kbs/{kb_id}",
                            json={"description": "m14新描述"}, headers=owner)
            r = await c.get(f"{API}/kbs/{kb_id}", headers=owner)
            check("desc set again", r.json()["description"] == "m14新描述")

            # ⑧ 零命中问题 SSE 正常收尾(勘误后路由仍稳)
            done, ft, total = await ask_sse(
                c, owner, kb_id, f"茄子梦计划{SUFFIX}的发射窗口是哪天?")
            check("zero-hit sse completes", "answer" in done,
                  str(done)[:120])
            print(f"[info] zero-hit first_token={ft:.1f}s total={total:.1f}s")
        finally:
            await cleanup(user_ids, kb_ids, usernames)
            if eval_path is not None:
                eval_path.unlink(missing_ok=True)
    summary_and_exit()


if __name__ == "__main__":
    asyncio.run(main())
