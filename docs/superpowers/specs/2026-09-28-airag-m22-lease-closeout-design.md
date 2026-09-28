# AIRag M22 设计:eval 租约闭环 + 孤儿事件补全 + 治理小包

(2026-09-28;范围 = M21 终审 triage 回流的五候选;先例 M13/M19/M20/M21)

## 背景与目标

M21 落了心跳租约但留下残余:①排队等待 > 宽限(10min)的 run 仍会被 beat
sweep 误收口(solo FIFO 下真实可达:generation 评估可跑 15min+,期间另一 KB
触发的评估排在 sweep 消息后面,创建时心跳老化超宽限即被误杀,任务随后白跑、
双 miss 不修正)②sweep 收口不发 eval.failed,接收方对「孤儿死亡」无感知
③api/eval.py 派发失败路径仍是全库最后一个 ORM 无条件终态写④_finalize_run
的 cancelled+error 组合语义未定义(无生产调用方,留有 footgun)⑤前端
PROVIDER_HELP 无兜底 + docs 措辞 nit。大件(A2A/MinerU 本地化/LDAP)仍等输入。

## T1 — 两段式租约:任务启动领租 + 排队宽限(候选①,核心)

区分「已开跑」与「从未开跑」两个 stale 判据,**不新增列**(弃 M21 的
created-即-心跳初始化,heartbeat NULL 复活为「从未开跑」语义):

- **创建**:`trigger_run` 不再初始化 heartbeat_at(回 NULL);M21 的初始化
  注释与回归测试由本设计显式取代(测试改写为断言 NULL,非删除)。
- **任务启动领租**:`run_eval_task` 载入 run 后、加载题目前,条件 UPDATE
  `SET heartbeat_at=utcnow_naive() WHERE id=:id AND status IN
  ('running','cancelling')` + commit + refresh:
  - rowcount=0 → 行已被 sweep/端点收口终态 → log 后直接 return(杜绝
    「被误杀后白跑全程」,多 worker 下即标准 lease-claim 语义);
  - refresh 后 status='cancelling' → 排队期间已被取消:cancelled=True,
    跳过两循环,零子集经 _finalize_run 收口 cancelled(与 M19 检查点语义
    一致,只是提前到零题)。
- **sweep 两段谓词**(status IN (running,cancelling) 不变):
  - 已开跑(heartbeat 非空):`heartbeat_at <= naive_now - 
    EVAL_HEARTBEAT_GRACE_MINUTES(10)`(worker 死亡判据,不变);
  - 从未开跑(heartbeat NULL):`created_at <= aware_now - 
    EVAL_QUEUE_GRACE_MINUTES(60)`(消息丢失/队列彻底堵死的兜底判据)。
  - **时钟域**:heartbeat_at 是 naive TIMESTAMP 列,naive UTC 比较;
    created_at 是 timestamptz(server_default),比较参数必须用 aware UTC
    (`datetime.now(timezone.utc) - …`),由 asyncpg 正确发 timestamptz——
    两列各域同源,不混用。
- 新设置 `EVAL_QUEUE_GRACE_MINUTES: int = 60`(60 > solo 下任何现实排队
  时长;真多 worker 扩容时再评估 owner 列)。

## T2 — 孤儿收口发 eval.failed(候选②)

`_sweep_orphan_runs` 改 `UPDATE … RETURNING (id, kb_id, mode, item_count)`,
逐行 `emit_event("eval.failed", {"run": {id, kb_id, mode, item_count,
summary: None}, "error": <孤儿文案>})` 同事务 commit,`nudge()` 在 commit
后按 n>0 调一次。负载语义:孤儿 run 从未收口,summary 诚实为 null,
item_count 为创建时题数。docs/webhooks.md 的 eval.failed 说明补一句孤儿
来源与 summary=null 事实。行离开 running/cancelling 后不再入扫描集,无重复
发事件面。

## T3 — 派发失败条件收口(候选③)

`trigger_run` 的 `.delay()` 异常分支:删 ORM setattr,改调
`_finalize_run(db, run.id, run.kb_id, payload.mode, [], False,
error=f"dispatch failed: {e}"[:500])`,n>0 则 nudge——与任务失败路径完全
同构(条件 UPDATE、取消竞态用户赢、事件同事务)。api/eval.py 顶层 import
`_finalize_run`(services 不反向 import api,无环)。副作用说明:行上
item_count 由创建时题数改写为 0(已完成子集语义,与 M21 failed 一致)。
至此 eval_runs **全部**终态写路径皆条件 UPDATE。

## T4 — _finalize_run 互斥防护(候选④)

函数首行 `if cancelled and error: raise ValueError("cancelled 与 error
互斥:取消非失败(调用方二选一)")` + 用例。无生产调用方违反。

## T5 — 小顺手包(候选⑤)

- WebhooksPage.vue 模板绑定改 `PROVIDER_HELP[form.provider] ?? ''`
  (与相邻 URL_PLACEHOLDER 的兜底模式对齐)。
- docs/webhooks.md 44 行「generation 题目恒带 reference 键」→「generation
  单题结果(generation_item)恒带 reference 键」。

## 任务切分(SDD)

1. **T1** 两段式租约(创建回 NULL + 领租/abort/提前取消 + 两段谓词 +
   EVAL_QUEUE_GRACE_MINUTES + 测试改写/新增)。
2. **T2** sweep RETURNING + 逐行事件 + nudge + docs 一句。
3. **T3** 派发失败走 _finalize_run。
4. **T4** 互斥防护。
5. **T5** 前端兜底 + docs 措辞。

## 验收

- pytest/vitest/build 全绿(496P+85T 基线只增不减)。
- 真栈 m22_acceptance:①NULL+created 30min 前 → 存活 ≥2 beat tick;
  ②NULL+created 61min 前 → 收口 failed 且 eval.failed 真送达 receiver
  (负载 summary=null);③stale 心跳行 → 收口+事件;④fresh 行存活;
  ⑤真实 retrieval 运行 claim+续租;⑥docs 孤儿说明在。派发失败路径单测
  覆盖(真栈需断 broker,不值当)。

## 风险与不做

- 不做 owner 列/抢占:solo 部署无抢占场景;两段谓词+领租已消实测误杀面,
  多 worker 扩容时再加。
- created_at 用 DB 时钟、领租用 Python 时钟:同机部署假定同步(dev 事实);
  跨机部署时以 NTP 为前提(运维常识,不在码内处理)。
- M21 的创建即心跳被本设计取代——执行记录与测试改写须明示演化关系,
  防后人误回退。
