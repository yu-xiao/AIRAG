# AIRag M22 实施计划:eval 租约闭环 + 孤儿事件补全 + 治理小包

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 清掉 M21 终审 triage 五候选:两段式租约(排队误杀根除)、孤儿收口发 eval.failed、派发失败条件收口、_finalize_run 互斥防护、前端/docs 小顺手。

**Architecture:** ①heartbeat NULL 复活为「从未开跑」:创建不初始化心跳,任务启动条件 UPDATE 领租(零行命中即 abort,排队期取消零子集收口);sweep 两段判据——已开跑看心跳宽限(10min),从未开跑看创建龄 EVAL_QUEUE_GRACE_MINUTES(60min),不新增列。②sweep 改 UPDATE…RETURNING 逐行发 eval.failed(summary 诚实 null)。③trigger_run 派发失败改走 _finalize_run(最后一个 ORM 无条件终态写删除)。④cancelled/error 互斥 ValueError。⑤模板兜底+docs 措辞。

**Tech Stack:** FastAPI + SQLAlchemy async(asyncpg UPDATE…RETURNING)+ Celery beat / Vue3 + vitest。

**Spec:** `docs/superpowers/specs/2026-09-28-airag-m22-lease-closeout-design.md`

## Global Constraints

- 测试一律 `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest …`(Windows CMD;`&&` 可用,禁 bash 语法);vitest 一律 `cd E:\Projects\AIRag\frontend && pnpm vitest run …`。
- **时钟域铁律**:heartbeat_at 是 naive TIMESTAMP 列,只与 naive UTC(`utcnow_naive()`)比较;created_at 是 timestamptz(server_default),只与 aware UTC(`datetime.now(timezone.utc) - …`)比较——两列各域,绝不混用。
- 无新迁移、无新列(两段谓词复用 created_at)。
- 事件与终态同事务 commit;`nudge()` 只在 n>0 且 commit 后调。
- 既有基线只增不减(pytest 496P / vitest 85 / build 零错)。
- 每任务一个 commit,conventional 消息照 brief。

---

### Task 1: 两段式租约——领租 + 排队宽限(候选①,核心)

**Files:**
- Modify: `backend/app/api/eval.py:354-361`(trigger_run 删创建即心跳)
- Modify: `backend/app/services/eval_runner.py:233-243`(run_eval_task 领租块)
- Modify: `backend/app/workers/eval_tasks.py:30-66`(_sweep_orphan_runs 两段谓词)
- Modify: `backend/app/core/config.py`(EVAL_QUEUE_GRACE_MINUTES)
- Test: `backend/tests/test_eval_trigger.py`(改写 1 + 存量 1)、`backend/tests/test_eval_cancel.py`(改写 1 + 新增 2)

**Interfaces:**
- Consumes: M21 的 `EvalRun.heartbeat_at`、`utcnow_naive()`、`EVAL_HEARTBEAT_GRACE_MINUTES=10`。
- Produces: `EVAL_QUEUE_GRACE_MINUTES: int = 60`;run_eval_task 领租语义(闭行 abort / 排队取消零子集收口);sweep 谓词 `or_(and_(hb not null, hb<=naive_cutoff), and_(hb is null, created_at<=aware_cutoff))`。Task 2 在本任务改后的 _sweep_orphan_runs 上继续改 RETURNING。

- [ ] **Step 1: 写/改失败测试**

`backend/tests/test_eval_trigger.py`:将 `test_trigger_run_initializes_heartbeat_at_creation`(131-152 行)**整体替换**为:

```python
async def test_trigger_run_leaves_heartbeat_null_until_start(
        client, auth_headers, db_session, monkeypatch):
    """M22 两段式租约:创建时 heartbeat 保持 NULL(=从未开跑)——排队中
    的 run 由创建龄(EVAL_QUEUE_GRACE_MINUTES)保护;M21 的「创建即心跳」
    方案被本里程碑显式取代。patch .delay 拦住 eager 内联执行。"""
    from app.workers.eval_tasks import run_evaluation

    monkeypatch.setattr(run_evaluation, "delay", lambda *a, **k: None)
    kb_id = await _make_kb(client, auth_headers, "触发库E")
    await _add_question(db_session, kb_id)
    r = await client.post("/api/eval/runs", headers=auth_headers,
                          json={"kb_id": kb_id, "mode": "retrieval"})
    assert r.status_code == 201
    run = (await db_session.execute(
        select(EvalRun).where(EvalRun.kb_id == kb_id))).scalars().one()
    assert run.status == "running"
    assert run.heartbeat_at is None
```

同文件 `test_recover_orphan_runs_sweeps_running`(155-175 行)的两行插桩**改为带过期创建龄**(否则两段谓词下不再被收口):

```python
    db_session.add_all([
        EvalRun(kb_id=1, mode="retrieval", summary=None,
                item_count=1, status="running",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=90)),
        EvalRun(kb_id=2, mode="generation", summary={"hit": 1.0},
                item_count=2, status="completed"),
    ])
```

(文件头补 `from datetime import datetime, timedelta, timezone`。)

`backend/tests/test_eval_cancel.py`:将 `test_sweep_skips_fresh_heartbeat` **整体替换**为:

```python
async def test_sweep_two_stage_predicate(db_session):
    """M22 两段判据:已开跑看心跳宽限;从未开跑(NULL)看创建龄
    (EVAL_QUEUE_GRACE_MINUTES,默认 60)——排队中的新鲜行绝不收口,
    超龄 NULL 行兜底收口。created_at 为 timestamptz,回填用 aware UTC。"""
    from datetime import datetime, timezone

    aware_now = datetime.now(timezone.utc)
    db_session.add_all([
        EvalRun(kb_id=1, mode="retrieval", summary=None, item_count=0,
                status="running", heartbeat_at=utcnow_naive()),
        EvalRun(kb_id=2, mode="retrieval", summary=None, item_count=0,
                status="cancelling",
                heartbeat_at=utcnow_naive() - timedelta(minutes=30)),
        EvalRun(kb_id=3, mode="retrieval", summary=None, item_count=0,
                status="running", heartbeat_at=None,
                created_at=aware_now - timedelta(minutes=30)),
        EvalRun(kb_id=4, mode="retrieval", summary=None, item_count=0,
                status="running", heartbeat_at=None,
                created_at=aware_now - timedelta(minutes=90)),
    ])
    await db_session.commit()
    _recover_orphan_runs()
    db_session.expire_all()
    runs = (await db_session.execute(
        select(EvalRun).order_by(EvalRun.id))).scalars().all()
    assert [r.status for r in runs] == \
        ["running", "failed", "running", "failed"]
```

同文件 `test_sweep_collects_cancelling` 的两行在途插桩**同样补 created龄**:

```python
    db_session.add_all([
        EvalRun(kb_id=1, mode="retrieval", summary=None,
                item_count=1, status="cancelling",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=90)),
        EvalRun(kb_id=2, mode="retrieval", summary=None,
                item_count=1, status="running",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=90)),
        EvalRun(kb_id=3, mode="retrieval", summary={"hit": 1.0},
                item_count=1, status="completed"),
    ])
```

(该文件头已有 `from datetime import timedelta`;补 `datetime, timezone`。)

新增两个领租用例(放在 `test_run_task_exception_failed_via_finalize` 之后):

```python
async def test_run_task_aborts_when_closed_before_start(db_session,
                                                         monkeypatch):
    """M22 领租:行已被收口终态 → 条件 UPDATE 零行命中,任务直接退出
    ——零题被跑、终态不被覆盖(杜绝「被误杀后白跑全程」)。"""
    run_id = await _mk_bare_run(db_session, "failed")
    fake, calls = _fake_retrieval_item(run_id, flip_at=99)
    monkeypatch.setattr(runner, "retrieval_item", fake)
    await run_eval_task(run_id, "retrieval", False, 8)
    assert calls["n"] == 0
    items = (await db_session.execute(
        select(EvalItem).where(EvalItem.run_id == run_id))).scalars().all()
    assert items == []
    db_session.expire_all()
    run = await db_session.get(EvalRun, run_id)
    assert run.status == "failed"  # 终态未被覆盖


async def test_run_task_cancelled_while_queued_closes_zero_subset(
        db_session, monkeypatch):
    """M22:排队期间被取消(行 cancelling)→ 领租成功但提前收口:零子集
    cancelled + eval.cancelled 事件,一题不跑(M19 检查点语义提前到零题)。"""
    db_session.add(WebhookEndpoint(
        name=f"cq{time.time_ns()}", url="http://x/h", secret="wh_s",
        events=[], created_by=1))
    await db_session.commit()
    run_id = await _mk_bare_run(db_session, "cancelling")
    fake, calls = _fake_retrieval_item(run_id, flip_at=99)
    monkeypatch.setattr(runner, "retrieval_item", fake)
    await run_eval_task(run_id, "retrieval", False, 8)
    assert calls["n"] == 0
    db_session.expire_all()
    run = await db_session.get(EvalRun, run_id)
    assert run.status == "cancelled" and run.item_count == 0
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert [r.event_type for r in rows] == ["eval.cancelled"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_eval_trigger.py tests\test_eval_cancel.py -v`
Expected: FAIL(创建仍带心跳→`heartbeat_at is None` 断言败;NULL+30min 行被收口→谓词断言败;领租两用例败——闭行仍跑题/排队取消仍跑一题)。

- [ ] **Step 3: 实现**

`backend/app/core/config.py`(`EVAL_HEARTBEAT_GRACE_MINUTES` 之后)加:

```python
    # M22 从未开跑(heartbeat NULL,含排队中)的兜底宽限:消息丢失/队列
    # 堵死才超此龄;solo 下任何现实排队时长 << 此值
    EVAL_QUEUE_GRACE_MINUTES: int = 60
```

`backend/app/api/eval.py` trigger_run 的 EvalRun 构造改回无心跳(354-361 一带):

```python
    # M22 两段式租约:创建不写心跳(NULL=从未开跑)——排队中的 run 由
    # 创建龄 EVAL_QUEUE_GRACE_MINUTES 保护;任务启动即领租(run_eval_task)
    run = EvalRun(kb_id=payload.kb_id, mode=payload.mode, summary=None,
                  item_count=q_count, status="running",
                  triggered_by=current.id)
```

(若删后 `from app.core.timeutil import utcnow_naive` 在本文件再无使用,一并删除该 import。)

`backend/app/services/eval_runner.py` `run_eval_task`:在 `if run is None: … return` 之后、`kb_id = run.kb_id` 之前插入领租块(函数内局部 import 区已有 `from sqlalchemy.ext.asyncio import …` 等,补 `from sqlalchemy import update`):

```python
            # M22 领租:启动即条件 UPDATE 续签——行已被 sweep/端点收口终态
            # 则零行命中直接退出(杜绝被误杀后白跑);排队期间被取消则
            # 零子集提前收口 cancelled(与 M19 检查点同语义,提前到零题)。
            claim = await db.execute(
                update(EvalRun)
                .where(EvalRun.id == run_id,
                       EvalRun.status.in_(("running", "cancelling")))
                .values(heartbeat_at=utcnow_naive()))
            await db.commit()
            await db.refresh(run)
            if claim.rowcount == 0:
                logger.info(f"eval run {run_id} closed before start, abort")
                return
            if run.status == "cancelling":
                final, n = await _finalize_run(db, run_id, run.kb_id,
                                               mode, [], True)
                if n:
                    nudge()
                return
```

`backend/app/workers/eval_tasks.py`:模块顶改 `from datetime import datetime, timedelta, timezone`;`_sweep_orphan_runs` 的判定段(51-62 行一带)替换为:

```python
            hb_cutoff = utcnow_naive() - timedelta(
                minutes=settings.EVAL_HEARTBEAT_GRACE_MINUTES)
            # created_at 是 timestamptz:比较参数必须 aware UTC(时钟域铁律)
            q_cutoff = datetime.now(timezone.utc) - timedelta(
                minutes=settings.EVAL_QUEUE_GRACE_MINUTES)
            result = await session.execute(
                update(EvalRun)
                .where(EvalRun.status.in_(("running", "cancelling")),
                       or_(
                           and_(EvalRun.heartbeat_at.is_not(None),
                                EvalRun.heartbeat_at <= hb_cutoff),
                           and_(EvalRun.heartbeat_at.is_(None),
                                EvalRun.created_at <= q_cutoff),
                       ))
                .values(
                    status="failed",
                    error="orphaned: heartbeat expired (worker died or restarted)",
                )
            )
```

(局部 import 区补 `and_`;docstring 的判据描述同步为两段式。)

- [ ] **Step 4: 跑测试 + 相邻面**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_eval_trigger.py tests\test_eval_cancel.py tests\test_eval_runner.py tests\test_webhook_events.py -v`
Expected: 全 PASS(含既有 `test_heartbeat_renewed_per_item`——领租+逐题续租后 hb≥before 仍成立)。

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/eval.py backend/app/services/eval_runner.py backend/app/workers/eval_tasks.py backend/app/core/config.py backend/tests/test_eval_trigger.py backend/tests/test_eval_cancel.py
git commit -m "feat(eval): two-stage lease — start-claim + queue grace supersedes creation heartbeat (m22)"
```

---

### Task 2: 孤儿收口发 eval.failed(候选②)

**Files:**
- Modify: `backend/app/workers/eval_tasks.py`(_sweep_orphan_runs 改 RETURNING+事件)
- Modify: `docs/webhooks.md`(eval.failed 说明补一句)
- Test: `backend/tests/test_eval_cancel.py`(新增 1 用例)

**Interfaces:**
- Consumes: Task 1 改后的 `_sweep_orphan_runs`(谓词不变,只换收口后的动作);`emit_event`/`nudge`(app.services.outbound)。
- Produces: sweep 对每个收口行发 `eval.failed`,负载 `{"run": {id, kb_id, mode, item_count, summary: None}, "error": <孤儿文案>}`;模块级常量 `ORPHAN_ERROR`。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_eval_cancel.py` 追加(头补 `import pytest` 供后续任务,本任务可先不加):

```python
async def test_sweep_emits_eval_failed_per_orphan(db_session):
    """M22:孤儿收口不再静默——每个被收口行恰一条 eval.failed,负载
    summary 诚实为 null、item_count 为创建时题数;completed 行零事件。"""
    from datetime import datetime, timezone

    db_session.add(WebhookEndpoint(
        name=f"sw{time.time_ns()}", url="http://x/h", secret="wh_s",
        events=[], created_by=1))
    db_session.add_all([
        EvalRun(kb_id=1, mode="retrieval", summary=None, item_count=7,
                status="running", heartbeat_at=None,
                created_at=datetime.now(timezone.utc)
                - timedelta(minutes=90)),
        EvalRun(kb_id=1, mode="generation", summary=None, item_count=3,
                status="cancelling",
                heartbeat_at=utcnow_naive() - timedelta(minutes=30)),
        EvalRun(kb_id=1, mode="retrieval", summary={"hit": 1.0},
                item_count=2, status="completed"),
    ])
    await db_session.commit()
    _recover_orphan_runs()
    db_session.expire_all()
    rows = (await db_session.execute(
        select(WebhookDelivery).order_by(WebhookDelivery.id))
    ).scalars().all()
    assert [r.event_type for r in rows] == ["eval.failed", "eval.failed"]
    assert all(r.payload["data"]["run"]["summary"] is None
               for r in rows)
    assert rows[0].payload["data"]["run"]["item_count"] == 7
    assert "orphaned" in rows[0].payload["data"]["error"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_eval_cancel.py -k sweep_emits -v`
Expected: FAIL(WebhookDelivery 查询为空——现 sweep 不发事件)。

- [ ] **Step 3: 实现**

`backend/app/workers/eval_tasks.py` 模块顶(imports 之后)加常量:

```python
ORPHAN_ERROR = "orphaned: heartbeat expired (worker died or restarted)"
```

`_sweep_orphan_runs` 执行段替换为(RETURNING 拿行、逐行发事件、commit 后 nudge):

```python
            result = await session.execute(
                update(EvalRun)
                .where(EvalRun.status.in_(("running", "cancelling")),
                       or_(
                           and_(EvalRun.heartbeat_at.is_not(None),
                                EvalRun.heartbeat_at <= hb_cutoff),
                           and_(EvalRun.heartbeat_at.is_(None),
                                EvalRun.created_at <= q_cutoff),
                       ))
                .values(status="failed", error=ORPHAN_ERROR)
                .returning(EvalRun.id, EvalRun.kb_id, EvalRun.mode,
                           EvalRun.item_count)
            )
            swept = result.all()
            n = 0
            for rid, kb_id, mode, item_count in swept:
                # 孤儿从未收口:summary 诚实 null,item_count 为创建时题数
                n += await emit_event(session, "eval.failed", {
                    "run": {"id": rid, "kb_id": kb_id, "mode": mode,
                            "item_count": item_count, "summary": None},
                    "error": ORPHAN_ERROR})
            await session.commit()
            if n:
                nudge()
            return len(swept)
```

模块顶加 `from app.services.outbound import emit_event, nudge`(outbound 不反向 import 本模块,无环);两处旧 `error="orphaned: …"` 字面量与测试断言统一走 `ORPHAN_ERROR`(`test_sweep_collects_cancelling`/`test_recover_orphan_runs_sweeps_running` 的断言字符串不变,常量值相同)。

`docs/webhooks.md` 的 eval.failed 条目(负载示例节)末尾补一行:

```markdown
  孤儿收口(worker 崩溃后被清扫)也发 eval.failed:`summary` 为 null、
  `item_count` 为创建时题数、`error` 为 `orphaned: heartbeat expired …`。
```

- [ ] **Step 4: 跑测试**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_eval_cancel.py tests\test_eval_trigger.py tests\test_webhook_events.py -v`
Expected: 全 PASS(存量 sweep 用例无订阅端点→n=0 不 nudge,不受影响)。

- [ ] **Step 5: Commit**

```bash
git add backend/app/workers/eval_tasks.py docs/webhooks.md backend/tests/test_eval_cancel.py
git commit -m "feat(eval): orphan sweep emits eval.failed per closed run (m22)"
```

---

### Task 3: 派发失败条件收口(候选③)

**Files:**
- Modify: `backend/app/api/eval.py:365-377`(trigger_run except 分支)
- Test: `backend/tests/test_eval_trigger.py`(存量 1 扩断言 + 新增 1)

**Interfaces:**
- Consumes: `_finalize_run(db, run_id, kb_id, mode, results, cancelled, error=None)`(M21 签名)。
- Produces: 无新接口(端点行为:502 不变,行 failed 条件落库 + eval.failed 事件)。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_eval_trigger.py` 新增(存量 `test_trigger_dispatch_failure_marks_run_failed` 保留不动):

```python
async def test_trigger_dispatch_failure_emits_eval_failed(
        client, auth_headers, db_session, monkeypatch):
    """M22:派发失败走 _finalize_run——eval.failed 事件照发(此前静默)、
    item_count 收口为 0(零子集),与任务失败路径同构。"""
    from app.models import WebhookDelivery, WebhookEndpoint
    from app.workers.eval_tasks import run_evaluation

    def _boom(*args, **kwargs):
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(run_evaluation, "delay", _boom)
    db_session.add(WebhookEndpoint(
        name=f"df{time.time_ns()}", url="http://x/h", secret="wh_s",
        events=["eval.failed"], created_by=1))
    await db_session.commit()
    kb_id = await _make_kb(client, auth_headers, "触发库F")
    await _add_question(db_session, kb_id)
    r = await client.post("/api/eval/runs", headers=auth_headers,
                          json={"kb_id": kb_id, "mode": "retrieval"})
    assert r.status_code == 502
    run = (await db_session.execute(
        select(EvalRun).where(EvalRun.kb_id == kb_id))).scalars().one()
    assert run.status == "failed" and run.item_count == 0
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert [x.event_type for x in rows] == ["eval.failed"]
    d = rows[0].payload["data"]
    assert "dispatch failed" in d["error"] and d["run"]["item_count"] == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_eval_trigger.py -k dispatch_failure_emits -v`
Expected: FAIL(WebhookDelivery 为空——现路径不发事件,item_count 仍为创建题数)。

- [ ] **Step 3: 实现**

`backend/app/api/eval.py` 模块顶 import 区加(按字母序插入既有 app.* 组):

```python
from app.services.eval_runner import _finalize_run
from app.services.outbound import nudge
```

except 分支(367-376 一带)替换为:

```python
    try:
        run_evaluation.delay(run.id, payload.mode, payload.rerank, top_k)
    except Exception as e:
        # I-1:.delay() 抛异常(broker 不可达/连接断)时消息已丢,run 若留
        # running 则同 kb+mode 永久 409、前端轮询永不停 → 收口 failed。
        # M22:同走 _finalize_run 条件收口——全库最后一个 ORM 无条件终态
        # 写删除;事件/nudge/取消竞态(用户赢)与任务失败路径完全同构。
        final, n = await _finalize_run(
            db, run.id, run.kb_id, payload.mode, [], False,
            error=f"dispatch failed: {e}"[:500])
        if n:
            nudge()
        raise HTTPException(
            status_code=502, detail="evaluation dispatch failed") from e
```

- [ ] **Step 4: 跑测试**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_eval_trigger.py tests\test_eval_cancel.py -v`
Expected: 全 PASS(存量 dispatch 用例的 `status failed`+`"dispatch failed" in error` 断言不受影响)。

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/eval.py backend/tests/test_eval_trigger.py
git commit -m "fix(eval): dispatch failure closes via _finalize_run — last unconditional terminal write gone (m22)"
```

---

### Task 4: _finalize_run 互斥防护(候选④)

**Files:**
- Modify: `backend/app/services/eval_runner.py`(_finalize_run 首行)
- Test: `backend/tests/test_eval_cancel.py`(新增 1;头补 `import pytest`)

**Interfaces:**
- Consumes: 无。
- Produces: `cancelled=True` 且 `error` 非空 → `ValueError`,先于任何 DB 写。

- [ ] **Step 1: 写失败测试**

```python
async def test_finalize_rejects_cancelled_and_error(db_session):
    """M22 互斥防护:取消与失败二选一——组合调用即 ValueError,且防护
    先于任何写(行保持 running、零事件)。"""
    run_id = await _mk_bare_run(db_session, "running")
    with pytest.raises(ValueError):
        await _finalize_run(db_session, run_id, 1, "retrieval", [],
                            True, error="x")
    db_session.expire_all()
    run = await db_session.get(EvalRun, run_id)
    assert run.status == "running"
    rows = (await db_session.execute(
        select(WebhookDelivery))).scalars().all()
    assert rows == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_eval_cancel.py -k reject -v`
Expected: FAIL(现不抛 ValueError)。

- [ ] **Step 3: 实现**

`_finalize_run` docstring 之后、`summary = summarize(results)` 之前加:

```python
    if cancelled and error:
        raise ValueError(
            "cancelled 与 error 互斥:取消非失败(调用方二选一)")
```

- [ ] **Step 4: 跑测试**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_eval_cancel.py tests\test_eval_runner.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/eval_runner.py backend/tests/test_eval_cancel.py
git commit -m "fix(eval): _finalize_run rejects cancelled+error combo upfront (m22)"
```

---

### Task 5: 前端兜底 + docs 措辞(候选⑤)

**Files:**
- Modify: `frontend/src/pages/WebhooksPage.vue`(模板绑定加兜底)
- Modify: `docs/webhooks.md:44`(主语措辞)

**Interfaces:**
- Consumes: M21 的 `PROVIDER_HELP` 映射。
- Produces: 无(防御性模板改动,form.provider 为封闭枚举,兜底仅对不可能值生效——不加测试,既有 vitest 证明合法值渲染不回归)。

- [ ] **Step 1: 改模板绑定**

```html
          <div class="form-help">{{ PROVIDER_HELP[form.provider] ?? '' }}</div>
```

- [ ] **Step 2: 改 docs 措辞**

`docs/webhooks.md` 44 行一带「generation 题目恒带 `reference` 键」改为「generation 单题结果(`generation_item`)恒带 `reference` 键」。

- [ ] **Step 3: 验证**

Run: `cd E:\Projects\AIRag\frontend && pnpm vitest run src\pages\__tests__\WebhooksPage.spec.ts`
Expected: 全 PASS(19/19)。

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/WebhooksPage.vue docs/webhooks.md
git commit -m "chore(ui,docs): provider-help fallback + wording nit (m22)"
```

---

## 收尾(控制端执行,不派子代理)

1. **全量门禁**:后端 pytest -q(≥501P 预期:496 + T1 净增 2 + T2/T3/T4 各 1)、前端 vitest(85)+ build 零错。
2. **真栈验收** `backend/scripts/m22_acceptance.py`(照 m21 模式):①NULL+created 30min 前 running 行存活 ≥2 beat tick;②NULL+created 61min 前行 → beat 收口 failed + eval.failed 真送达 receiver(summary=null);③stale 心跳行 → 收口+事件;④fresh 心跳行存活;⑤真实 retrieval 运行领租+续租(hb 非空);⑥docs 孤儿说明在。派发失败路径单测已覆盖,真栈不模拟断 broker。
3. **终审 whole-branch** + M23 候选回流。
4. **执行记录** + 记忆更新(推送与 M21 的 11 个提交一起,等用户批量走查)。
