# AIRag M23 实施计划:检查点停止条件 + 孤儿文案分裂 + 测试卫生

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 清掉 M22 终审 triage:逐题检查点 any non-running 即 break(单题超宽限被 sweep 收口后不再白跑)、孤儿收口文案按分支分裂(heartbeat expired / never started)、测试卫生(RETURNING 序无关断言 + 影子 import + 验收模板括号)。

**Architecture:** 检查点改 `!= "running"`(cancelled 仅在 cancelling 时置位;failed 双 miss 不写不发,sweep 权威保持);sweep 单 UPDATE 拆两条同事务 UPDATE…RETURNING 各带自己 error 常量,事件负载仅 error 分流;docs 同步两种形态。

**Tech Stack:** FastAPI + SQLAlchemy async(asyncpg UPDATE…RETURNING)+ Celery beat。

**Spec:** `docs/superpowers/specs/2026-09-29-airag-m23-checkpoint-split-hygiene-design.md`

## Global Constraints

- 测试一律 `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest …`(Windows CMD;`&&` 可用,禁 bash 语法);vitest 一律 `cd E:\Projects\AIRag\frontend && pnpm vitest run …`。
- 时钟域铁律:heartbeat_at(naive TIMESTAMP)只比 naive UTC;created_at(timestamptz)只比 aware UTC。
- 事件与终态同事务 commit;`nudge()` 只在 n>0 且 commit 后;两常量均以 `orphaned:` 前缀开头(接收方前缀匹配兼容)。
- 基线只增不减(pytest 501P / vitest 85 / build 零错)。
- 每任务一个 commit,conventional 消息照 brief。

---

### Task 1: 检查点停止条件——any non-running 即 break

**Files:**
- Modify: `backend/app/services/eval_runner.py:280-282, 301-303`(两循环检查点)
- Test: `backend/tests/test_eval_cancel.py`(新增 1 用例)

**Interfaces:**
- Consumes: M22 领租与 `_finalize_run` 双 miss 语义。
- Produces: 检查点 `if run.status != "running": cancelled = run.status == "cancelling"; break`——cancelling 走原收口,failed(或任何他方终态)双 miss 不写不发。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_eval_cancel.py` 的 `test_run_task_cancelled_while_queued_closes_zero_subset` 之后追加:

```python
async def test_run_task_stops_when_swept_failed_mid_run(
        client, auth_headers, db_session, monkeypatch):
    """M23:单题超宽限被 sweep 收口 failed 后,循环在下一检查点停止
    ——不白跑剩余题;failed 终态与数据不被 _finalize_run 改写(双 miss,
    summary 保持 null、零事件;sweep 是唯一权威)。"""
    run_id = await _mk_run(client, auth_headers, db_session, n=3)
    calls = {"n": 0}

    async def flaky(db, kb_id, q, top_k, reranker):
        calls["n"] += 1
        if calls["n"] == 2:  # 第 2 题提交后模拟 sweep 已收口 failed
            await db.execute(update(EvalRun).where(EvalRun.id == run_id)
                             .values(status="failed"))
            await db.commit()
        return {"question": q.question, "hit_at_k": None, "mrr": None,
                "keyword_recall": None}

    monkeypatch.setattr(runner, "retrieval_item", flaky)
    monkeypatch.setattr(runner, "nudge", lambda: None)
    await run_eval_task(run_id, "retrieval", False, 8)
    assert calls["n"] == 2  # 第 2 题检查点见 failed 即停,第 3 题不跑
    db_session.expire_all()
    run = await db_session.get(EvalRun, run_id)
    assert run.status == "failed"  # sweep 终态保持
    assert run.summary is None  # 双 miss:无 summary 改写
    items = (await db_session.execute(
        select(EvalItem).where(EvalItem.run_id == run_id))).scalars().all()
    assert len(items) == 2
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert rows == []  # 双 miss:零事件
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_eval_cancel.py -k swept_failed_mid_run -v`
Expected: FAIL(`calls["n"] == 3`——现检查点只认 cancelling,failed 仍继续跑)。

- [ ] **Step 3: 实现**

两处循环的检查点(280-282 与 301-303,注释同步)均改为:

```python
                        await db.refresh(run)  # 检查点:拾取端点 cancelling / sweep failed
                        if run.status != "running":
                            # M23:any non-running 即停——cancelling 走取消
                            # 收口;failed(sweep 已收口+已发事件)双 miss
                            # 不写不发,最迟下一检查点停止白跑
                            cancelled = run.status == "cancelling"
                            break
```

- [ ] **Step 4: 跑测试**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_eval_cancel.py tests\test_eval_trigger.py tests\test_eval_runner.py -v`
Expected: 全 PASS(存量取消用例的 cancelling 路径行为不变)。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/eval_runner.py backend/tests/test_eval_cancel.py
git commit -m "fix(eval): checkpoint breaks on any non-running status — no work after sweep close (m23)"
```

---

### Task 2: 孤儿文案分裂 + docs + 序无关断言

**Files:**
- Modify: `backend/app/workers/eval_tasks.py:11, 62-86`(常量分裂 + 双 UPDATE)
- Modify: `docs/webhooks.md`(孤儿句两形态)
- Test: `backend/tests/test_eval_cancel.py`(改写 1 + 存量断言 1)、`backend/tests/test_eval_trigger.py`(存量断言 1)

**Interfaces:**
- Consumes: M22 的两段谓词与 RETURNING 事件面。
- Produces: 常量 `ORPHAN_HEARTBEAT_ERROR = "orphaned: heartbeat expired (worker died or restarted)"` 与 `ORPHAN_QUEUED_ERROR = "orphaned: never started (queue grace exceeded or message lost)"`(删旧 `ORPHAN_ERROR`);sweep 两条同事务 UPDATE…RETURNING 各带自己 error,事件负载仅 error 分流。

- [ ] **Step 1: 改写/改失败测试**

`backend/tests/test_eval_cancel.py` 将 `test_sweep_emits_eval_failed_per_orphan` **整体替换**为(按 run id 匹配,断言两分支文案;影子 import 不再写):

```python
async def test_sweep_emits_eval_failed_per_orphan(db_session):
    """M22/M23:孤儿收口逐行发 eval.failed;error 按分支分流(已开跑=
    heartbeat expired / 从未开跑=never started);断言按 run id 匹配,
    不依赖 UPDATE…RETURNING 顺序;summary 诚实 null;completed 行零事件。"""
    db_session.add(WebhookEndpoint(
        name=f"sw{time.time_ns()}", url="http://x/h", secret="wh_s",
        events=[], created_by=1))
    q_row = EvalRun(kb_id=1, mode="retrieval", summary=None, item_count=7,
                    status="running", heartbeat_at=None,
                    created_at=datetime.now(timezone.utc)
                    - timedelta(minutes=90))
    hb_row = EvalRun(kb_id=1, mode="generation", summary=None, item_count=3,
                     status="cancelling",
                     heartbeat_at=utcnow_naive() - timedelta(minutes=30))
    done = EvalRun(kb_id=1, mode="retrieval", summary={"hit": 1.0},
                   item_count=2, status="completed")
    db_session.add_all([q_row, hb_row, done])
    await db_session.commit()
    _recover_orphan_runs()
    db_session.expire_all()
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert len(rows) == 2
    by_run = {r.payload["data"]["run"]["id"]: r.payload["data"]
              for r in rows}
    assert set(by_run) == {q_row.id, hb_row.id}
    assert all(d["run"]["summary"] is None for d in by_run.values())
    assert "never started" in by_run[q_row.id]["error"]
    assert "heartbeat expired" in by_run[hb_row.id]["error"]
    assert by_run[q_row.id]["run"]["item_count"] == 7
```

同文件 `test_sweep_collects_cancelling` 的 error 断言(约 448 行)改为:

```python
    assert runs[0].error == ("orphaned: never started "
                             "(queue grace exceeded or message lost)")
```

(该测试两行在途均为 NULL+90min 创建龄 → QUEUED 文案;`runs[2].error is None` 不变。)

`backend/tests/test_eval_trigger.py` 的对应断言(约 203 行)同样改为 QUEUED 文案串。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_eval_cancel.py tests\test_eval_trigger.py -k "sweep or orphan" -v`
Expected: FAIL(改写用例的两分支文案断言败——现单一 ORPHAN_ERROR;存量 QUEUED 断言败)。

- [ ] **Step 3: 实现**

`backend/app/workers/eval_tasks.py` 常量替换(11 行):

```python
ORPHAN_HEARTBEAT_ERROR = ("orphaned: heartbeat expired "
                          "(worker died or restarted)")
ORPHAN_QUEUED_ERROR = ("orphaned: never started "
                       "(queue grace exceeded or message lost)")
```

`_sweep_orphan_runs` 执行段(62-86 行)替换为(两条同事务 UPDATE 各带文案,事件分流;谓词两段保持 M22 原样):

```python
            started = (await session.execute(
                update(EvalRun)
                .where(EvalRun.status.in_(("running", "cancelling")),
                       EvalRun.heartbeat_at.is_not(None),
                       EvalRun.heartbeat_at <= hb_cutoff)
                .values(status="failed", error=ORPHAN_HEARTBEAT_ERROR)
                .returning(EvalRun.id, EvalRun.kb_id, EvalRun.mode,
                           EvalRun.item_count, EvalRun.error)
            )).all()
            queued = (await session.execute(
                update(EvalRun)
                .where(EvalRun.status.in_(("running", "cancelling")),
                       EvalRun.heartbeat_at.is_(None),
                       EvalRun.created_at <= q_cutoff)
                .values(status="failed", error=ORPHAN_QUEUED_ERROR)
                .returning(EvalRun.id, EvalRun.kb_id, EvalRun.mode,
                           EvalRun.item_count, EvalRun.error)
            )).all()
            n = 0
            for rid, kb_id, mode, item_count, err in [*started, *queued]:
                # 孤儿从未收口:summary 诚实 null,item_count 为创建时题数
                n += await emit_event(session, "eval.failed", {
                    "run": {"id": rid, "kb_id": kb_id, "mode": mode,
                            "item_count": item_count, "summary": None},
                    "error": err})
            await session.commit()
            if n:
                nudge()
            return len(started) + len(queued)
```

(RETURNING 带 `EvalRun.error` 作第五列——事件文案直接取库里真值,免得
Python 侧再按分支映射。docstring 补一句「两条同事务语句各带分支文案,
语句间崩溃最少数发不重发,at-least-once 一致」。)

`docs/webhooks.md` 孤儿句(「孤儿收口(worker 崩溃后被清扫)也发
eval.failed:…`error` 为 `orphaned: heartbeat expired …`」)替换为:

```markdown
  孤儿收口也发 eval.failed:`summary` 为 null、`item_count` 为创建时题数;
  `error` 按分支两形态——已开跑 `orphaned: heartbeat expired (worker died
  or restarted)` / 从未开跑 `orphaned: never started (queue grace exceeded
  or message lost)`。
```

- [ ] **Step 4: 跑测试**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_eval_cancel.py tests\test_eval_trigger.py tests\test_webhook_events.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/workers/eval_tasks.py docs/webhooks.md backend/tests/test_eval_cancel.py backend/tests/test_eval_trigger.py
git commit -m "feat(eval): orphan sweep splits error text by lease branch (m23)"
```

---

### Task 3: 测试卫生顺手包

**Files:**
- Modify: `backend/tests/test_eval_cancel.py:489`(影子 import 删除)
- Modify: `backend/scripts/m22_acceptance.py`(wait_delivery 条件括号)

**Interfaces:**
- Consumes: 无。
- Produces: 无(纯卫生;`api/eval.py` 派发分支 `final` 命名维持与兄弟调用点一致——spec 已裁定不改)。

- [ ] **Step 1: 删影子 import**

`test_sweep_two_stage_predicate` 内的 `from datetime import datetime, timezone` 局部 import 删除(模块头已具备;测试体直接用)。

- [ ] **Step 2: 修验收模板条件**

`backend/scripts/m22_acceptance.py` 的 `wait_delivery` 条件:

```python
        if rows and all(x.get("status") in ("succeeded", "dead") for x in rows) \
                and len(rows) >= 1 \
                or time.perf_counter() > t0 + timeout_s:
```

改为(显式括号,去冗余):

```python
        if (rows and all(x.get("status") in ("succeeded", "dead")
                         for x in rows)) \
                or time.perf_counter() > t0 + timeout_s:
```

- [ ] **Step 3: 验证**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_eval_cancel.py -q && .venv\Scripts\python -m py_compile scripts\m22_acceptance.py && echo COMPILE_OK`
Expected: 全 PASS + COMPILE_OK。

- [ ] **Step 4: Commit**

```bash
git add backend/tests/test_eval_cancel.py backend/scripts/m22_acceptance.py
git commit -m "chore(test): drop shadow import; parenthesize acceptance wait condition (m23)"
```

---

## 收尾(控制端执行,不派子代理)

1. **全量门禁**:后端 pytest -q(≥502P 预期:501+T1 新 1);前端 vitest(85)+ build 零错(前端未触,门禁确认)。
2. **真栈验收** `backend/scripts/m23_acceptance.py`(照 m22 模式+净版 wait_delivery,worker 重启后跑):①超龄 never-started 行 → 收口 + receiver 收到 `never started` 文案 eval.failed;②stale 心跳行 → `heartbeat expired` 文案;③真实 retrieval 运行正常完成(领租+续租不回归);④docs 两形态文案在。
3. **终审 whole-branch**(4746383 起)+ M24 候选回流。
4. **执行记录** + 记忆更新(与 M21/M22 的 21 个提交同批,等用户批量走查后推送)。

---

## 执行记录(2026-09-29,SDD 三任务零修复轮 + 终审一波修复)

**交付(4746383..f295973,8 提交含 spec/plan,main 本地未推)**:
- 0d4379b spec / 9f24302 plan
- T1 4679e19 检查点 any non-running 即 break(两循环;cancelled 仅 cancelling 置位;
  failed 双 miss 不写不发,sweep 唯一权威;单题>10min 被 sweep 收口后不再白跑)
- T2 339a34b 孤儿文案分裂:ORPHAN_HEARTBEAT_ERROR / ORPHAN_QUEUED_ERROR 两常量,
  两条同事务 UPDATE…RETURNING(第五列 EvalRun.error,事件文案取库中真值),
  断言序无关化;docs 两形态;实现者抓到 brief 测试代码 MissingGreenlet 坑
  (expire_all 后访问 ORM id 同步惰性加载),最小修复裁定追认
- T3 dc03959 影子 import 删除 + m22 验收 wait_delivery 条件括号(真值表等价核验)
- d3a3650 m23_acceptance(10 检查)/ f295973 终审修复(日志尾缀 lease expired
  + docstring 单 commit 原子性——终审证实两语句+事件行单 commit,崩溃整体回滚,
  事件只延迟不重复,比 spec 记载更强)

**门禁**:pytest 501→**502P/0F**、vitest **85/85**、build 零错(前端未触,门禁确认)。

**真栈验收 m23_acceptance 10/10 PASS 0 SKIP**:超龄 never-started 行收口且
receiver 实收 `never started` 文案、stale 心跳行收口且实收 `heartbeat expired`
(分支分流真栈判别);真实 retrieval 运行零回归(领租+续租);docs 两形态在。

**终审(whole-branch,4746383..d3a3650)**:**Ready to merge — Yes**,零
Critical/Important。对抗核验:检查点对状态域路由全枚举(cancelling→取消收口/
failed→双 miss/他值→保守 no-op);两语句在 heartbeat NULL 性上不相交,恰一次
收口恰一次事件;RETURNING 后值即所写常量无漂移;时钟域全程同律。triage:日志
尾缀 fix-now(随修复波落地),验收清理不对称 park(dev-only 脚本)。

**环境备忘(新)**:start_dev.bat 的 alembic 步在分离窗口偶发卡死(前台直跑秒过,
库已在 head)——重启栈时若 API 不起,先查 alembic 进程,直接拉 uvicorn 绕过;
wmic /format:value 输出行尾带 CR,for 循环取 PID 须去 CR(字面量杀最稳)。

**M24 候选**:大件仍等输入(A2A/MinerU 本地化/LDAP);小项:验收脚本早失败时
端点/KB 清理不对称(finally 只删 run 行)、sweep docstring 与 spec 的原子性措辞
对齐已完成、其余无积压——**M17 以来的 triage 队列首次清零**。

**栈态**:8001(直接 uvicorn)/beat/worker(已重启带 f295973)/5173;dev 库
head=c2d3e4f5a6b7(M23 无迁移)。

