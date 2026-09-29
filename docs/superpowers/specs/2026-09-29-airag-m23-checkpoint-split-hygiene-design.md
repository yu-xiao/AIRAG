# AIRag M23 设计:检查点停止条件 + 孤儿文案分裂 + 测试卫生

(2026-09-29;范围 = M22 终审 triage 回流候选;先例 M13/M19/M20/M21/M22)

## 背景与目标

M22 关闭了租约主环,终审留下:①逐题检查点只认 `cancelling`——单题超
EVAL_HEARTBEAT_GRACE_MINUTES(10min)时,mid-run sweep 已把行收口 failed 并发了
eval.failed,循环仍会跑完剩余题(LLM 白花 + 事件发出后 EvalItem 继续插入,数据
与事件口径漂移)②ORPHAN_ERROR 单一文案对「从未开跑」行不真实(没有心跳可
「过期」;真实原因是排队宽限超龄/消息丢失)③测试卫生顺手包(RETURNING 序
假设、影子 import、验收模板 wait_delivery 的 and/or 优先级坑)。排队>60min
残余(owner 列)仍显式 park 给多 worker 扩容。大件 A2A/MinerU 本地化/LDAP
仍等输入。

## T1 — 检查点停止条件:any non-running 即 break

两循环(retrieval/generation)的逐题检查点由:

```python
                        if run.status == "cancelling":
                            cancelled = True
                            break
```

改为:

```python
                        if run.status != "running":
                            cancelled = run.status == "cancelling"
                            break
```

语义:cancelling → 原行为(零/部分子集收口 cancelled);failed(sweep 收口)
→ break 后 `_finalize_run(final="completed", expect="running")` 双 miss →
不写不发——sweep 的 failed 终态与 eval.failed 事件保持唯一权威,任务最迟在
「收口后的下一个检查点」停止白跑(在飞的单题是上界,不可再省)。注释同步。

## T2 — 孤儿文案分裂

`ORPHAN_ERROR` 单常量分裂为两常量(均 `orphaned:` 前缀,接收方前缀匹配不受
影响):

```python
ORPHAN_HEARTBEAT_ERROR = "orphaned: heartbeat expired (worker died or restarted)"
ORPHAN_QUEUED_ERROR = "orphaned: never started (queue grace exceeded or message lost)"
```

`_sweep_orphan_runs` 由单条 UPDATE 改为**同事务两条 UPDATE…RETURNING**:先收
「已开跑+心跳过期」(HEARTBEAT 文案),再收「从未开跑+创建超龄」(QUEUED
文案);各自 RETURNING 后逐行发 eval.failed(负载不变:summary null +
item_count 创建题数,仅 error 分流)。两段谓词本身与 M22 完全一致,只是拆成
两条语句各带自己的 error。docs/webhooks.md 孤儿句更新为两种 error 形态。

## T3 — 测试卫生顺手包

- `test_sweep_emits_eval_failed_per_orphan` 断言改**按 run id 匹配**(消除
  UPDATE…RETURNING 顺序假设;T2 改写时一并落地)。
- 两个 sweep 测试内的局部 `from datetime import datetime, timezone` 影子
  import 删除(模块头已具备)。
- `backend/scripts/m22_acceptance.py` 的 `wait_delivery` 条件加显式括号并去
  冗余 `len(rows) >= 1`(防后世拷贝踩 and/or 优先级);m23 验收脚本沿用净版。
- api/eval.py 派发分支 `final` 未用变量:维持与兄弟调用点一致的命名,**不改**
  (一致性优先,裁定在案)。

## 任务切分(SDD)

1. **T1** 检查点停止条件(两循环同改)+ 中途 sweep 收口的停止测试。
2. **T2** 双 UPDATE 文案分裂 + docs + 存量断言更新 + 序无关断言。
3. **T3** 影子 import 删除 + m22 验收脚本条件括号。

## 验收

- pytest/vitest/build 全绿(501P+85T 基线只增不减;vitest/build 预期无变化,
  跑门禁确认)。
- 真栈 m23_acceptance:①超龄 never-started 行 → 收口 + receiver 收到 QUEUED
  文案 eval.failed;②stale 心跳行 → HEARTBEAT 文案;③真实运行正常完成;
  ④docs 两种文案在。检查点中途停止(单题>10min)不注入真栈,单测覆盖。

## 风险与不做

- 两条 UPDATE 非原子:两语句间崩溃会留半途(一行收口、事件未发)——下一
  beat tick 重扫到另一段,语义仍是 at-least-once 收口;事件只可能少发不可
  重复(行离场后不再入扫描集),与全站 at-least-once 哲学一致,可接受。
- 不做 owner 列/排队>60min(多 worker 前提);不做检查点之外的细粒度停止。
