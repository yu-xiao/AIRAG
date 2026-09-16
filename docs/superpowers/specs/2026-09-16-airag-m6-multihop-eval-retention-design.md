# AIRag M6 设计(多跳兜底 / 生成质量评估 / 审计留存)

日期:2026-09-16 | 状态:待用户审阅 | 前置:M5 已关账(main=a6654bb)

## 0. 背景与范围

M5 收官后用户勾选三个方向进入 M6(AskUserQuestion 定版):

| # | 项 | 一句话 |
|---|---|---|
| A | 生成式多跳问答 | 检索不足且一次重检仍不足时,LLM 拆子问题分别检索、合并去重后再生成(自适应兜底) |
| B | LLM-judge 生成质量评估 | 新 CLI:对 eval 集每题跑真图,用 LLM 评 faithfulness(忠实度)与 answer relevancy(切题度) |
| C | 审计留存策略 | `AUDIT_RETENTION_DAYS` 天自动清理 + admin 手动触发端点 |

**决策记录**(均来自用户明确选择):
1. 多跳触发 = **自适应兜底**:仅"insufficient 且 retries≥1"或"零命中"时进入 decompose;正常问题零额外成本。
2. 多跳拓扑 = **单图加 decompose 节点**(弃并行子图/API 层编排)。
3. LLM-judge = **自研轻量两指标**(faithfulness + relevancy,弃 RAGAS 库);指标范围 = 忠实度+切题度(弃全套)。
4. LDAP/SSO 仍无输入,MinerU 本地化按需搁置,均不在本里程碑。

**不在本里程碑**:MinerU 本地部署、LDAP/SSO、RAGAS 库、评估集 `reference_answer` 字段、评估结果入库/页面、检索类新指标(现有 hit@k/MRR/关键词 recall 不动)、前端任何改动(三特性均为后端/CLI 面)。

## 1. 生成式多跳(自适应兜底)

### 1.1 现状与约束

- M5 图拓扑(graph.py):`START→rewrite→retrieve→rerank→grade→cond(transform|generate)`,transform→retrieve,retries<1 有界一次。
- `route_after_grade` 现为二级:insufficient 且 retries<1 → transform,否则 generate。
- `rewrite_node` 每轮显式重置 `{"search_query","retries":0,"grade":""}`(checkpointer 残留安全,M6 需同步重置新增字段)。
- `retrieve_node` 单查询(`search_query or question`);`generate_node`/引用构建只消费 `hits`,与检索路径无关。
- SSE 靠 `tags=["answer"]` 过滤答案流——**decompose 的 LLM 调用不得带 answer tag**(与 rewrite/grade 同)。
- `AGENTIC_CRAG_ENABLED=false` 时 grade 直通返回 `{}`(grade 保持 ""),路由落 generate。

### 1.2 图拓扑(M6 后)

```
START → rewrite → retrieve → rerank ──(route_after_rerank)──→ grade ──(route_after_grade)──→ generate → END
                        ↑                │ hopped=True 直通 generate                     │
                        │                └─────────────(不回 grade,省一次评审调用)──────→ generate
                        ├── transform ←── (insufficient 且 retries<1,M5 现状不变)
                        └── decompose ←── (insufficient 且 retries≥1,或零命中;且开关开且未 hopped)
```

- **route_after_rerank**(新条件边,rerank 后):`hopped` 为真 → generate;否则 → grade。多跳检索合并后直接生成,不再消耗一次 grade 评审。
- **route_after_grade**(改三级):
  1. `grade=="insufficient" and retries<1` → **transform**(M5 现状不变);
  2. `MULTI_HOP_ENABLED and not hopped and (grade=="insufficient" or not hits)` → **decompose**(零命中也兜底:复合问题关键词与两份文档都不匹配时,首轮常为零命中;CRAG 关闭时该分支仍可用,零命中是客观信号不依赖 grade);
  3. 其余 → **generate**。
- 拓扑恒含全部节点(开关关闭时 decompose 不可达,节点仍在),沿用 M4/M5"拓扑恒定"断言模式。

### 1.3 状态与节点

`ChatState` 新增(`total=False` 不破坏存量):

```python
sub_queries: list[str]  # decompose 拆出的子问题(retrieve 多查询模式时存在)
hopped: bool            # 是否已走过 decompose(防环;rewrite 每轮重置为 False)
```

- **rewrite_node**:重置字典增加 `"hopped": False`(checkpointer 残留安全,与 M5 同理)。
- **decompose_node(llm)**(新,放 nodes.py):
  - 输入以 `search_query`(已消解指代)为主、`question` 兜底、`proposed_query`(grade 提议)作改写提示;一次 LLM 调用,system prompt 要求拆成 **2~3 个各自独立、可直接检索的子问题**,只输出 JSON 字符串数组(如 `["子问题1","子问题2"]`);
  - 解析(复用 `_extract_json` 同款剥离逻辑):非空字符串过滤、去重、**硬截断到 `MULTI_HOP_MAX_SUBQ`**;
  - 兜底:LLM 异常/解析失败/结果为空 → `sub_queries=[proposed_query or search_query or question]`(等效多一次重检,行为安全);
  - 返回 `{"sub_queries": [...], "hopped": True}`;LLM 调用**不带 answer tag**;异常 logger.exception 后走兜底。
- **retrieve_node**(改多查询):`queries = state.get("sub_queries") or [state.get("search_query") or state["question"]]`;逐查询 `hybrid_search`,**按 chunk_id 去重、跨查询轮转交错**(每查询第 1 名先取,再第 2 名……),合并上限 `RETRIEVAL_TOP_K * 2`;单查询路径行为与 M5 完全一致。
- rerank/grade/generate/citations/SSE/checkpointer:零改动(合并后的 hits 走既有 rerank→generate 管道)。

### 1.4 配置

```python
# M6 多跳兜底(默认开;conftest 关闭保测试确定性)
MULTI_HOP_ENABLED: bool = True
MULTI_HOP_MAX_SUBQ: int = 3
```

`.env.example` 同步;`tests/conftest.py` 顶部环境区补 `MULTI_HOP_ENABLED=false`(与 AGENTIC_*/CHECKPOINTER 同块),多跳专项测试用 monkeypatch 打开。

### 1.5 成本与有界性

- 正常问题(首轮 sufficient):**零额外调用**,与 M5 相同。
- 兜底路径最多新增:1 次 decompose LLM + ≤3 次 hybrid_search + 1 次 rerank;**decompose 全程至多一次**(hopped 防环),不回 grade,总调用数有上界。
- 零命中且无指望的问题:多花 1 次 LLM + ≤3 次检索后仍以"知识库中未找到相关内容"收尾(可接受;`MULTI_HOP_ENABLED=false` 可整体关闭回到 M5 行为)。

## 2. LLM-judge 生成质量评估

### 2.1 judge 模块(app/services/eval_judge.py,新)

```python
async def faithfulness_score(llm, question: str, answer: str, contexts: list[str]) -> dict
    # {"score": float 0~1, "reasons": str}
async def relevancy_score(llm, question: str, answer: str) -> dict
```

- 各一次 LLM 调用,system prompt 中文明确评审定义:faithfulness = 答案中的陈述是否都有参考资料支撑(防幻觉,答案拒答"未找到"时按 1.0 计);relevancy = 答案是否直接回答了所问问题(答非所问打低分)。**只输出 JSON `{"score": <0.0~1.0>, "reasons": "<一句话依据>"}`**。
- 复用 `make_chat_llm()`(graph.py 单例);解析复用 `_extract_json` 模式(从 nodes.py 导入或提取共享)。
- score 解析后 clamp 到 [0,1];**坏输出重试一次,仍坏 → 返回 `{"score": None, "reasons": ...}`**(该题该指标记 None,汇总时跳过并计入 parse_errors,不整体失败)。
- 测试以 FakeListChatModel 注入(与 M5 图测试同模式)。

### 2.2 CLI(scripts/eval_generation.py,新)

```
用法(backend 目录): .venv\Scripts\python -m scripts.eval_generation --kb N [--rerank] [--json]
```

- 复用 `eval_sets/{kb}.json`(格式不变:`question/expect_doc_ids/expect_keywords`,后两者在本脚本仅透传不使用);文件缺失/KB 不存在 → 明确报错退出(对齐 eval_retrieval)。
- 每题:`build_graph(checkpointer=None)` 后 `ainvoke({"question","kb_ids","rerank","history":[]})`,取最终 state 的 `answer` 与 `hits[:RETRIEVAL_TOP_K]` **全文内容**(不是 citations 的 160 字 excerpt)作 faithfulness 的 contexts;再依次调两个 judge。
- 输出:表格 `问题 / 忠实度 / 切题度 / 引用数` + 汇总行(两指标均值,None 不计入);`--json` 输出数组(含 reasons)。
- 前置检查:`ZHIPU_API_KEY` 为空 → 退出并提示(fake 桩答案评估无意义),不静默跑。

## 3. 审计留存策略

### 3.1 核心函数(app/services/audit.py 增补)

```python
async def purge_expired(db: AsyncSession) -> int
    # AUDIT_RETENTION_DAYS<=0 → 直接返回 0(禁用)
    # 否则 DELETE WHERE created_at < now() - retention 天,返回删除行数
    # 删除后写一条 audit_purge 摘要:detail={"deleted": N, "retention_days": X}
```

- `created_at` 来自 TimestampMixin,已有,无迁移。
- 任务与端点共用此函数,避免两份删除逻辑。

### 3.2 Celery 定时任务

- `app/workers/maintenance.py`(新):`purge_expired_audit_logs` 任务,sync 壳 + `_run_async`(与 pipeline.py 同模式)+ 独立 NullPool session;logger 记录删除数。
- `celery_app.py` 增 `beat_schedule`:每日 03:00(Asia/Shanghai,timezone 已配)`crontab(hour=3, minute=0)`。
- **Windows 运行方式**:worker 启动命令加 `-B` 内嵌 beat(单 worker 场景标准做法),README 开发启动一节同步。

### 3.3 手动触发端点(admin)

- `POST /api/admin/audit-logs/purge`:`require_admin`;直接调 `purge_expired(db)` 同步执行,返回 `{"deleted": N, "retention_days": X}`(`audit_purge` 审计摘要由 purge_expired 内部落一条,端点不重复写)。
- 非 admin 403(复用现有依赖行为);便于验收与运维即时清理。

### 3.4 配置

```python
AUDIT_RETENTION_DAYS: int = 180  # 0 = 禁用自动与手动清理
```

`.env.example` 同步。

## 4. 测试与验收

### 4.1 后端单测(pytest,目标 +12~16 个测试函数)

- **多跳**:decompose_node 正常解析/截断/去重/坏 JSON 兜底/异常兜底;route_after_grade 三级(insufficient+retries<1→transform;insufficient+retries≥1→decompose;零命中→decompose;hopped→不再 decompose;开关关→generate);route_after_rerank(hopped 直通);retrieve_node 多查询合并去重与上限;rewrite 重置 hopped;开关关时图拓扑仍含 decompose 节点。
- **judge**:faithfulness/relevancy 好 JSON 解析与 clamp;坏输出重试一次;两次皆坏 → score None。
- **审计留存**:purge_expired 只删过期(种 3 条:2 旧 1 新,删 2 留 1);`AUDIT_RETENTION_DAYS=0` 禁用;手动端点 admin 200 且返回删除数、editor 403;beat_schedule 存在且指向正确任务。
- conftest 环境区补 `MULTI_HOP_ENABLED=false` 不破坏存量 103+ 用例(全量回归绿为验收前提)。

### 4.2 验收脚本(scripts/m6_acceptance.py,对齐 M4/M5 模式)

1. **多跳真图**:建 KB 上传两份文档,复合问题(单独检索/一次重检不足,拆两个子问题各自可命中)→ ask SSE → 断言答案含两处事实且 done 正常;临时关 `MULTI_HOP_ENABLED` 重启对照,同问题答案显著变差或"未找到"(证明兜底生效)。
2. **eval_generation**:对 M5 验收库(id 5/6,已有 eval 集)跑 `--kb` → 两指标均非 None 且忠实度 ≥0.8(真智谱;无 key 时记 SKIP)。
3. **审计留存**:SQL 种过期/新鲜审计行 → `POST /admin/audit-logs/purge` → 过期被删、audit_purge 摘要存在;`AUDIT_RETENTION_DAYS=0` 时端点返回 deleted=0。
4. 基线回归:`.venv\Scripts\python -m pytest -q` 全绿;`pnpm build && pnpm test && pnpm oxlint` 绿(前端零改动应保持 6 passed/0 errors)。
5. 浏览器走查留给用户(多跳对话页/审计页无回归)。

## 5. 风险与退化

1. **decompose LLM 输出不守格式**:兜底单查询(≈多一次重检),不阻塞回答;单测覆盖。
2. **合并检索后 rerank 未开时引用质量下降**:合并已按跨查询轮转交错,截断 2×top_k;rerank 请求级开关维持现状,不为本特性强开。
3. **零命中进兜底拖慢 hopeless 问题**:有界(1 LLM + ≤3 检索);整体可用 `MULTI_HOP_ENABLED=false` 回 M5 行为。
4. **judge 评分主观性/漂移**:reasons 必输出留痕;parse 失败记 None 不伪装成 0;评估结论看均值与趋势,不设硬阈值拦截(验收阈值仅用于验收脚本的 sanity 断言)。
5. **beat 在 Windows 的运行形态**:`-B` 内嵌单 worker 足够(单机单 worker);若未来多 worker 需独立 beat 进程,超出本里程碑。
6. **审计删除不可恢复**:策略天数默认 180 保守;删除动作自身落 audit_purge 留痕;`AUDIT_RETENTION_DAYS=0` 可完全禁用。

---

## 交接(M7 候选,按需)

MinerU 本地化(数据不出内网,client 已隔离可换)、LDAP/SSO(仍待用户提供服务器信息)、评估集 `reference_answer` 与评估结果入库、多跳子问题并行检索(若单轮兜底召回仍不足)。
