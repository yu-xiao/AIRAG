# AIRag M21 设计:failed 条件收口 + 心跳租约 + 文档精装

(2026-09-28;范围 = M20 终审 triage 五候选全清 + 09-28 走查暴露的通道引导文案;先例 M13/M19/M20)

## 背景与目标

M20 把取消/完成竞态全部收口到行级条件 UPDATE,终审留下五项 M21 候选:
①异常路径 failed 仍是 ORM 无条件写(最后一个旁路终态写入)②sweep 无条件
UPDATE 建立在 solo worker 前提上③docs/webhooks.md 头部标签过时、缺逐事件
负载示例④_finalize_run None 分支空 commit⑤新测试局部 import 未上提。另
09-28 走查口头确认:generic 通道对企微假成功(企微业务错误也回 HTTP 200),
创建表单值得加通道引导文案。大件(A2A/MinerU 本地化/LDAP)仍等用户输入,
不在本里程碑。

## T1 — failed 条件收口 + None 分支空 commit(候选①+④)

`_finalize_run` 增加 `error: str | None = None` 参数:

- `final = "failed" if error else("cancelled" if cancelled else "completed")`,
  expect 语义不变(failed/completed 写在 running 行上,cancelled 写在
  cancelling 行上);failed 落库时同时写 `run.error`,cancelled/completed
  不写 error。
- 竞态结构不变:not-cancelled 路径首跳零行 → cancelling 兜底按「用户已赢」
  收口 cancelled(部分 summary 照写,数据诚实);cancelled 路径首跳零行 →
  None(他人收口)。**failed 与 completed 同享 A1 兜底**——异常撞上并发取消
  时用户赢,行终态 cancelled、发 `eval.cancelled` 而非 `eval.failed`。
- `run_eval_task` 异常分支:先 rollback(PendingRollback 防御保留),再调
  `_finalize_run(db, run_id, kb_id, mode, results, cancelled=False,
  error=str(e)[:500])`,彻底删掉 ORM setattr 终态写。最后一个无条件终态
  写入消失,M20「终态只经条件 UPDATE」语义闭环到失败路径。
- 事件负载统一:`eval.failed` 的 `run` 对象补齐 `item_count`/`summary`
  (与 completed/cancelled 同形,接收方可读部分汇总);`error` 字段保留。
- None 分支(两跳全零行):提前 `return None, 0`,不再走空 commit——此时
  事务内无 pending 变更,提前返回安全,session 由 async with 关闭时回滚。

## T2 — 心跳租约守卫(候选②)

现状:sweep 仅在 worker_ready 触发,无条件收口所有 running/cancelling 行,
正确性依赖「solo 池启动瞬间无在途任务」。多 worker 前必须让 sweep 能区分
「活任务」与「孤儿」。

设计(轻量租约 = 心跳时间戳,不加 owner 列):

- **迁移**:eval_runs 加 `heartbeat_at TIMESTAMP NULL`(naive UTC,与
  webhook next_attempt_at 同源约定)。NULL 视为 stale(兼容存量行与
  「已创建未开跑」孤儿)。
- **续租**:eval_runner 逐题事务里 `run.heartbeat_at = utcnow_naive()`
  ——与 EvalItem 插入同一 commit,零额外往返;每题续一次,粒度远小于宽限。
- **sweep 判定**:`WHERE status IN (running, cancelling) AND
  (heartbeat_at IS NULL OR heartbeat_at <= now - grace)`;
  `EVAL_HEARTBEAT_GRACE_MINUTES` 默认 10(单题 generation 三次 LLM 裁决
  远小于此;可调)。
- **触发**:`worker_ready` 钩子保留(判定改为 stale-only),**另注册 beat
  周期任务** `app.workers.eval_tasks.sweep_orphan_runs`(60s,与
  webhook-delivery-scan 同档)。收益:solo 场景下 worker 崩溃后孤儿不再
  「只能等下次重启」才收口,而是 ≤ grace+interval 内被 beat 兜底——比
  M15 现状更强,不是退化。
- **时间源**:outbound 的 `_utcnow_naive` 提升为 `app/core/timeutil.py
  utcnow_naive()` 公共助手;outbound 内保留 `_utcnow_naive` 名字(别名
  或薄包装)兼容既有 import 与测试;eval_runner/eval_tasks 用公共助手。
- eager 测试下续租同样生效(纯 ORM 写);sweep 测试直接构造
  stale/fresh/NULL 心跳行断言收口选择性。

## T3 — docs/webhooks.md 精装(候选③)

- 头部「(AIRag M17/M18;…)」刷新为 M17~M21 事实(引入/平台适配+per-KB/
  密钥生命周期/eval.cancelled/本里程碑负载统一)。
- 新增「事件负载示例」节:六个事件各一段真实形状的信封 JSON(generic 通道
  全量信封;平台通道是 markdown 摘要已述)——
  - `document.done`:`document{id, kb_id, filename, chunk_count}`
  - `document.failed`:`document{id, kb_id, filename}` + `error`
  - `eval.completed` / `eval.cancelled`:`run{id, kb_id, mode,
    item_count, summary}`(cancelled 的 item_count 是子集)
  - `eval.failed`:`run{id, kb_id, mode, item_count, summary}` + `error`
    (M21 起 run 形状与 completed 统一)
  - `chat.refused`:`source(web|rest|mcp)` + `kb_ids[]` + `question`
  - summary 双模式形状:retrieval(item_count/hit/mrr/keyword_recall)与
    generation(item_count/faithfulness_avg/relevancy_avg/refused_count/
    reference_avg,未测量字段缺席)。

## T5 — 测试局部 import 上提(候选⑤,先做)

test_eval_cancel.py 九处函数内 `from app.services.eval_runner import …`
/ `from app.workers.eval_tasks import …` 上提模块级(conftest 先于测试模块
加载,env 钉扎不受影响);跑全套证明无 import 顺序问题。为新测试先铺平。

## T6 — 通道引导文案(走查候选)

WebhooksPage 创建/编辑对话框:平台类型下方帮助文案改为按所选 provider
联动的 `PROVIDER_HELP` 映射——

- generic:自签 HMAC 密钥;仅按 HTTP 状态码判定成功(2xx 即成功)。
- wecom:企微群机器人请用本通道;URL 取「群设置→群机器人→新建→复制
  Webhook」;无需密钥。**提示:通用通道只看 HTTP 状态码,企微业务错误也
  回 200,用通用通道会误报成功。**
- dingtalk / feishu:平台加签密钥(可选)。

vitest 断言:wecom 选中时帮助含「群机器人」与误报提示;generic 选中时含
HMAC/状态码语义。

## 任务切分(SDD)

1. **T5** import 上提(机械,先铺平)。
2. **T1** _finalize_run error 参数 + 异常路径条件收口 + None 早返回。
3. **T2a** timeutil 助手 + heartbeat_at 列 + 迁移 + 逐题续租。
4. **T2b** sweep stale-only + beat 任务注册 + worker_ready 复用。
5. **T3** docs/webhooks.md 头部刷新 + 逐事件负载示例节。
6. **T6** 前端 PROVIDER_HELP + vitest。

## 验收

- pytest/vitest/build 全绿(489P+ 基线只增不减)。
- 真栈 m21_acceptance:①generation 评估在无 key 下触发→failed 条件落库
  且 eval.failed 事件真送达、负载含部分 summary;②running 行 stale 心跳
  →beat/sweep 收口 failed,fresh 心跳行不动;③真实运行中 heartbeat_at
  随题推进;④docs 检查人工。
- 终审 whole-branch 后 M22 候选回流;推送仍等用户走查(既定节奏)。

## 风险与不做

- 不做完整 lease(owner 列/抢占):心跳+宽限已满足「区分活任务与孤儿」,
  多 worker 真落地时再评估是否加 owner(solo 池序列化下无抢占场景)。
- eval.failed 负载变化是接收方兼容性放宽(新增字段),at-least-once 消费者
  按事件类型分发不受影响;docs 同步说明。
- 续租粒度 = 单题时长:grace 10min >> 最慢单题(三次 LLM 裁决,实测
  <2min);若未来单题可超 10min,调 env 即可,无需改码。
