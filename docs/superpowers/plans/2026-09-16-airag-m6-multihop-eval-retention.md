# AIRag M6 实施计划(多跳兜底 / LLM-judge 评估 / 审计留存)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 spec 实现三项 M6 能力——自适应多跳兜底(decompose 节点)、生成质量 LLM-judge 评估 CLI、审计留存(自动+手动清理),前端零改动。

**Architecture:** 在 M5 图(`rewrite→retrieve→rerank→grade→…`)上加一个 `decompose` 节点与两条路由规则,检索节点支持多子查询合并;评估侧新增 judge 模块与 `eval_generation` CLI 复用现有 eval 集与 `make_chat_llm`;审计侧在 `app/services/audit.py` 加 `purge_expired` 核心函数,被 Celery beat 任务与 admin 端点共用。

**Tech Stack:** Python 3.12 / FastAPI / LangGraph / Celery+Redis / pytest(现状,零新依赖)。

**Spec:** `docs/superpowers/specs/2026-09-16-airag-m6-multihop-eval-retention-design.md`

## Global Constraints

- 运行命令一律用 `E:\Projects\AIRag\backend\.venv\Scripts\python.exe`(**不是** `py -3.12`,那是无 pytest 的全局解释器)。
- 测试基线:后端 **104 passed**、前端 build 绿 + vitest **6 passed** + oxlint **0 warnings 0 errors**——每任务结束不得回归。
- conftest 环境区必须先于任何 `app.*` import;新增 `MULTI_HOP_ENABLED=false` 同块(Task 3)。
- 图拓扑恒含全部节点(开关关闭时 decompose 不可达但节点仍在);新 LLM 调用一律**不带** `answer` tag(SSE 只放行 answer tag 的流)。
- 提交前 DLP 双检:`git diff --cached | findstr TSD-Header` 无命中,且 `git diff --cached --numstat` 无 `-  -` 二进制占位(误报判据:内容本身提到 TSD-Header 的文档类文件,看大小是否恰 8192/是否可读)。
- 工作目录:后端命令在 `E:\Projects\AIRag\backend` 下执行;git 操作在仓库根 `E:\Projects\AIRag`。

---

### Task 1: 多跳基建——config + state 字段 + decompose_node

**Files:**
- Modify: `backend/app/core/config.py`(RETRIEVAL_TOP_K 行附近加 2 个配置)
- Modify: `backend/app/services/chat_graph/state.py`(加 2 个字段)
- Modify: `backend/app/services/chat_graph/nodes.py`(加 DECOMPOSE_SYSTEM + decompose_node;rewrite_node 重置字典加 hopped)
- Modify: `E:\Projects\AIRag\.env.example`(加 2 个 M6 变量)
- Test: `backend/tests/test_chat_graph.py`(追加;并改 1 个存量断言)

**Interfaces:**
- Produces: `decompose_node(state: dict, llm) -> dict`,返回 `{"sub_queries": list[str], "hopped": True}`;`ChatState` 新键 `sub_queries: list[str]`、`hopped: bool`;`settings.MULTI_HOP_ENABLED: bool`(默认 True)、`settings.MULTI_HOP_MAX_SUBQ: int`(默认 3)。Task 2/3 消费这些名字。
- Consumes: 现有 `_extract_json(text) -> str`(nodes.py,剥离 ```json 围栏)、`settings`(config.py 单例)。

- [ ] **Step 1: 写失败测试(追加到 test_chat_graph.py 末尾)**

```python
async def test_decompose_parses_truncates_and_dedupes():
    from app.services.chat_graph.nodes import decompose_node

    out = await decompose_node(
        {"question": "复合", "search_query": "A 和 B", "proposed_query": "改写"},
        llm=_LLMScript(['["子问题一", "子问题二", "子问题三", "子问题四"]']),
    )
    assert out["sub_queries"] == ["子问题一", "子问题二", "子问题三"]  # 截断到 MAX_SUBQ=3
    assert out["hopped"] is True


async def test_decompose_filters_and_fenced_json():
    from app.services.chat_graph.nodes import decompose_node

    out = await decompose_node(
        {"question": "复合", "search_query": "改写后查询"},
        llm=_LLMScript(['```json\n["甲", 42, " ", "甲", "乙"]\n```']),
    )
    assert out["sub_queries"] == ["甲", "乙"]  # 非字符串/空白被滤,重复去重,围栏剥离


async def test_decompose_bad_json_falls_back():
    from app.services.chat_graph.nodes import decompose_node

    # 解析失败:退化为 [proposed_query or search_query or question]
    out = await decompose_node(
        {"question": "原问题", "search_query": "改写后", "proposed_query": "提议词"},
        llm=_LLMScript(["不是 json"]),
    )
    assert out["sub_queries"] == ["提议词"]
    assert out["hopped"] is True
    out2 = await decompose_node(
        {"question": "原问题"}, llm=_LLMScript(["[]"])  # 空数组也走兜底
    )
    assert out2["sub_queries"] == ["原问题"]


async def test_decompose_llm_exception_falls_back():
    from app.services.chat_graph.nodes import decompose_node

    out = await decompose_node(
        {"question": "原问题", "search_query": "改写后"},
        llm=Boom(),
    )
    assert out == {"sub_queries": ["改写后"], "hopped": True}
```

同时修改存量 `test_rewrite_disabled_resets_state`(reset 字典新增 hopped 会破坏精确断言):

```python
    assert out == {"search_query": "它是什么", "retries": 0, "grade": "", "hopped": False}
```

注:`_LLMScript`/`Boom` 已在该文件定义(`test_grade_json_parsing_paths`/`test_rewrite_llm_failure_falls_back` 同款);若 Boom 在该测试之前未定义,把类体 `async def ainvoke(self, msgs, config=None): raise RuntimeError("llm down")` 内联即可。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /d E:\Projects\AIRag\backend && .venv\Scripts\python.exe -m pytest tests/test_chat_graph.py -q`
Expected: 新增 4 个 FAIL(`cannot import name 'decompose_node'`),存量 rewrite 断言 FAIL。

- [ ] **Step 3: 最小实现**

`config.py` 在 `CHECKPOINTER_ENABLED` 块后加:

```python
    # M6 多跳兜底:检索不足且重检仍不足(或零命中)时拆子问题再检索
    MULTI_HOP_ENABLED: bool = True
    MULTI_HOP_MAX_SUBQ: int = 3
```

`state.py` 的 ChatState 追加(文件末尾、citations 之后):

```python
    # M6 多跳兜底
    sub_queries: list[str]  # decompose 拆出的子问题(存在即多查询检索)
    hopped: bool  # 是否已走过 decompose(防环;rewrite 每轮重置)
```

`nodes.py`:`rewrite_node` 的 reset 行改为

```python
    reset = {"search_query": question, "retries": 0, "grade": "", "hopped": False}
```

文件末尾(transform_node 之后)加:

```python
DECOMPOSE_SYSTEM = (
    "你是问题分解器。把复合问题拆成2~3个各自独立、无指代、可直接用于检索的子问题;"
    '只输出 JSON 字符串数组,如 ["子问题1","子问题2"]。'
    "问题本身简单时,输出只含该问题的单元素数组。"
)


async def decompose_node(state: dict, llm) -> dict:
    base = state.get("search_query") or state["question"]
    user = f"问题:{base}"
    hint = state.get("proposed_query")
    if hint:
        user += f"\n(检索改写提示:{hint})"
    try:
        resp = await llm.ainvoke([("system", DECOMPOSE_SYSTEM), ("user", user)])
        parsed = json.loads(_extract_json(resp.content))
        subs = (
            [str(q).strip() for q in parsed if str(q).strip()]
            if isinstance(parsed, list) else []
        )
        subs = list(dict.fromkeys(subs))[: settings.MULTI_HOP_MAX_SUBQ]
        if subs:
            return {"sub_queries": subs, "hopped": True}
    except Exception:
        logger.exception("decompose failed; fallback to single query")
    return {"sub_queries": [hint or base], "hopped": True}
```

`.env.example` 末尾追加:

```
# M6:多跳兜底(检索不足且重检仍不足/零命中时拆子问题再检索);MAX_SUBQ 为子问题上限
MULTI_HOP_ENABLED=true
MULTI_HOP_MAX_SUBQ=3
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_chat_graph.py -q`
Expected: 全 PASS(存量 15 + 新 4 = 19)。

- [ ] **Step 5: 提交**

```bash
cd /d E:\Projects\AIRag
git add backend/app/core/config.py backend/app/services/chat_graph/state.py backend/app/services/chat_graph/nodes.py backend/tests/test_chat_graph.py .env.example
git commit -m "feat: m6 decompose node, state fields and config"
```

(提交前执行 Global Constraints 的 DLP 双检;后续每个提交步骤同此,不再重复。)

---

### Task 2: retrieve_node 多子查询合并

**Files:**
- Modify: `backend/app/services/chat_graph/nodes.py`(retrieve_node)
- Test: `backend/tests/test_chat_graph.py`(追加 2 个)

**Interfaces:**
- Consumes: Task 1 的 `sub_queries` 状态键;`settings.RETRIEVAL_TOP_K`(现有,int,默认 8)。
- Produces: `retrieve_node(state)` 在 `sub_queries` 存在时逐查询检索并合并——Task 3 端到端依赖此行为。

- [ ] **Step 1: 写失败测试**

```python
async def test_retrieve_multi_query_merges_round_robin(monkeypatch):
    import app.services.chat_graph.nodes as nodes_mod
    from app.services.retrieval.searcher import SearchHit

    async def fake_search(db, kb_ids, query, top_k=20):
        data = {
            "甲": [(1, "甲一", 0.9), (2, "甲二", 0.7)],
            "乙": [(2, "乙一", 0.8), (3, "乙二", 0.6)],
        }[query]
        return [
            SearchHit(cid, 1, 1, "f.pdf", i + 1, txt, s, "vector")
            for i, (cid, txt, s) in enumerate(data)
        ]

    monkeypatch.setattr(nodes_mod, "hybrid_search", fake_search)
    out = await nodes_mod.retrieve_node(
        {"question": "q", "kb_ids": [1], "sub_queries": ["甲", "乙"]}
    )
    # 轮转交错:甲1→乙1(chunk 2)→甲2(chunk 2 去重跳过)→乙2
    assert [h["chunk_id"] for h in out["hits"]] == [1, 2, 3]


async def test_retrieve_multi_query_caps_at_2x_topk(monkeypatch):
    import app.services.chat_graph.nodes as nodes_mod
    from app.core.config import settings
    from app.services.retrieval.searcher import SearchHit

    async def fake_search(db, kb_ids, query, top_k=20):
        return [
            SearchHit(query == "甲" and i or 100 + i, 1, 1, "f.pdf", i + 1,
                      f"{query}-{i}", 0.9, "vector")
            for i in range(8)
        ]

    monkeypatch.setattr(nodes_mod, "hybrid_search", fake_search)
    monkeypatch.setattr(settings, "RETRIEVAL_TOP_K", 2)  # 合并上限 = 2*2 = 4
    out = await nodes_mod.retrieve_node(
        {"question": "q", "kb_ids": [1], "sub_queries": ["甲", "乙"]}
    )
    assert len(out["hits"]) == 4
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_chat_graph.py -q -k multi_query`
Expected: 2 FAIL(现 retrieve_node 忽略 sub_queries,只搜一个查询)。

- [ ] **Step 3: 最小实现(retrieve_node 整体替换)**

```python
async def retrieve_node(state: dict) -> dict:
    queries = state.get("sub_queries") or [
        state.get("search_query") or state["question"]
    ]
    per_query = []
    async with SessionLocal() as db:
        for q in queries:
            hits = await hybrid_search(db, state["kb_ids"], q)
            per_query.append(hits)
    # 跨查询轮转交错(chunk_id 去重),合并上限 2*top_k,留给 rerank 全局重排
    merged, seen = [], set()
    cap = settings.RETRIEVAL_TOP_K * 2
    depth = 0
    while len(merged) < cap and any(depth < len(hs) for hs in per_query):
        for hs in per_query:
            if len(merged) >= cap:
                break
            if depth < len(hs):
                h = hs[depth]
                if h.chunk_id not in seen:
                    seen.add(h.chunk_id)
                    merged.append(h)
        depth += 1
    return {"hits": [h.__dict__ for h in merged]}
```

- [ ] **Step 4: 跑测试确认通过(含全文件回归)**

Run: `.venv\Scripts\python.exe -m pytest tests/test_chat_graph.py tests/test_ask.py -q`
Expected: 全 PASS(单查询路径行为不变:现有 test_retrieve_uses_search_query / test_retrieve_falls_back_to_question 仍绿)。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/chat_graph/nodes.py backend/tests/test_chat_graph.py
git commit -m "feat: m6 retrieve node merges multi sub-query hits"
```

---

### Task 3: 图接线——三级路由 + hopped 直通 + conftest 隔离 + 端到端

**Files:**
- Modify: `backend/app/services/chat_graph/graph.py`(route_after_grade 改三级、新增 route_after_rerank、build_graph 加 decompose)
- Modify: `backend/tests/conftest.py`(env 区加一行)
- Test: `backend/tests/test_chat_graph.py`(追加 4 个)

**Interfaces:**
- Consumes: Task 1 `decompose_node`/`settings.MULTI_HOP_ENABLED`、Task 2 多查询 retrieve。
- Produces: `route_after_rerank(state: dict) -> str`("generate"|"grade")、改版 `route_after_grade(state: dict) -> str`("transform"|"decompose"|"generate")。SSE/ask.py 零改动(decompose 的 LLM 调用不带 answer tag)。

- [ ] **Step 1: 写失败测试**

```python
async def test_route_after_grade_multihop_branches(monkeypatch):
    from app.core.config import settings
    from app.services.chat_graph.graph import route_after_grade

    monkeypatch.setattr(settings, "MULTI_HOP_ENABLED", True)
    assert route_after_grade(
        {"grade": "insufficient", "retries": 0}) == "transform"  # 重检优先(M5)
    assert route_after_grade(
        {"grade": "insufficient", "retries": 1, "hits": [{}]}) == "decompose"
    assert route_after_grade({"hits": []}) == "decompose"  # 零命中兜底
    assert route_after_grade({"hits": [{}]}) == "generate"  # 正常
    assert route_after_grade(
        {"grade": "insufficient", "retries": 1, "hits": [{}], "hopped": True}
    ) == "generate"  # 防环


async def test_route_after_grade_multihop_disabled(monkeypatch):
    from app.core.config import settings
    from app.services.chat_graph.graph import route_after_grade

    monkeypatch.setattr(settings, "MULTI_HOP_ENABLED", False)
    assert route_after_grade(
        {"grade": "insufficient", "retries": 1, "hits": [{}]}) == "generate"
    assert route_after_grade({"hits": []}) == "generate"  # 回到 M5 行为


async def test_route_after_rerank_bypasses_grade_when_hopped():
    from app.services.chat_graph.graph import route_after_rerank

    assert route_after_rerank({"hopped": True}) == "generate"
    assert route_after_rerank({}) == "grade"
    assert route_after_rerank({"hopped": False}) == "grade"


async def test_graph_topology_contains_multihop_edges():
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.services.chat_graph.graph import build_graph

    g = build_graph(llm=FakeListChatModel(responses=["x"]))
    edges = {(e.source, e.target) for e in g.get_graph().edges}
    assert ("decompose", "retrieve") in edges
    assert ("rerank", "generate") in edges  # hopped 直通(条件边可达)
    assert ("rerank", "grade") in edges  # 条件边两分支都在图结构里
    assert ("grade", "decompose") in edges


async def test_multihop_fallback_end_to_end(monkeypatch):
    """CRAG+多跳全开:首轮不足→transform 重检→仍不足→decompose 拆两问→合并检索→直通 generate。"""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.core.config import settings
    from app.services.chat_graph.graph import build_graph
    from app.services.retrieval.searcher import SearchHit

    calls = []

    async def fake_search(db, kb_ids, query, top_k=20):
        calls.append(query)
        return [SearchHit(len(calls), 1, 1, "a.pdf", 1, f"内容-{query}", 0.5, "vector")]

    import app.services.chat_graph.nodes as nodes_mod

    monkeypatch.setattr(nodes_mod, "hybrid_search", fake_search)
    monkeypatch.setattr(settings, "AGENTIC_CRAG_ENABLED", True)
    monkeypatch.setattr(settings, "MULTI_HOP_ENABLED", True)

    llm = FakeListChatModel(responses=[
        '{"verdict": "insufficient", "query": "重检词"}',   # grade 第 1 轮
        '{"verdict": "insufficient", "query": "再改写"}',   # grade 第 2 轮(重检后)
        '["子问题A", "子问题B"]',                            # decompose
        "最终答案[1]",                                       # generate
    ])
    g = build_graph(llm=llm)
    final = await g.ainvoke({"question": "复合问题", "kb_ids": [1]})
    assert "子问题A" in calls and "子问题B" in calls  # 两个子查询都检索了
    assert final["hopped"] is True
    assert final["sub_queries"] == ["子问题A", "子问题B"]
    assert "最终答案" in final["answer"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_chat_graph.py -q -k "route_after or multihop"`
Expected: 新增用例 FAIL(route_after_grade 无 decompose 分支 / route_after_rerank 未定义 / 拓扑缺边)。

- [ ] **Step 3: 最小实现**

conftest.py 在 `os.environ["CHECKPOINTER_ENABLED"] = "false"` 行后加(必须保留"先 env 后 import app"次序):

```python
# M6:多跳兜底默认关闭(存量用例的 FakeListChatModel 只为 generate 准备了响应)
os.environ["MULTI_HOP_ENABLED"] = "false"
```

graph.py:导入区补 `decompose_node`;替换 `route_after_grade` 并新增 `route_after_rerank`:

```python
def route_after_rerank(state: dict) -> str:
    if state.get("hopped"):
        return "generate"  # 多跳合并后直通生成,省一次 grade 评审
    return "grade"


def route_after_grade(state: dict) -> str:
    if state.get("grade") == "insufficient" and state.get("retries", 0) < 1:
        return "transform"
    if (
        settings.MULTI_HOP_ENABLED
        and not state.get("hopped")
        and (state.get("grade") == "insufficient" or not state.get("hits"))
    ):
        return "decompose"
    return "generate"
```

`build_graph` 内:节点注册区加 `dec = partial(decompose_node, llm=chat_llm)` 与 `g.add_node("decompose", dec)`;边区把 `g.add_edge("rerank", "grade")` 替换为:

```python
    g.add_conditional_edges(
        "rerank", route_after_rerank, {"grade": "grade", "generate": "generate"}
    )
```

并在 `g.add_edge("transform", "retrieve")` 后加:

```python
    g.add_edge("decompose", "retrieve")
```

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: **115 passed**(104 基线 + T1 4 + T2 2 + T3 5;conftest 隔离使存量用例不受零命中新路由影响——`test_route_after_grade_branches` 的 `retries:1→generate` 断言在 MULTI_HOP 关闭下仍成立)。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/chat_graph/graph.py backend/tests/conftest.py backend/tests/test_chat_graph.py
git commit -m "feat: m6 wire decompose into graph with three-tier routing"
```

---

### Task 4: LLM-judge 模块(faithfulness / relevancy)

**Files:**
- Create: `backend/app/services/eval_judge.py`
- Test: `backend/tests/test_eval_judge.py`(新文件)

**Interfaces:**
- Consumes: `nodes._extract_json(text) -> str`(Task 1 已确认存在)、任意 `llm`(实现 `async ainvoke(messages, config=None)` 返回带 `.content` 的对象)。
- Produces: `async faithfulness_score(llm, question: str, answer: str, contexts: list[str]) -> dict`、`async relevancy_score(llm, question: str, answer: str) -> dict`,均返回 `{"score": float | None, "reasons": str}`(score 已 clamp 到 [0,1];两次解析失败 → score=None)。Task 5 消费。

- [ ] **Step 1: 写失败测试(新文件 tests/test_eval_judge.py)**

```python
class _Resp:
    def __init__(self, text):
        self.content = text


class _LLMScript:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def ainvoke(self, msgs, config=None):
        self.calls.append(msgs)
        return _Resp(self.replies.pop(0))


async def test_judge_parses_and_clamps():
    from app.services.eval_judge import faithfulness_score, relevancy_score

    f = await faithfulness_score(
        _LLMScript(['{"score": 1.7, "reasons": "全有依据"}']),
        "预算多少", "三千万[1]", ["预算三千万"],
    )
    assert f == {"score": 1.0, "reasons": "全有依据"}
    v = await relevancy_score(
        _LLMScript(['```json\n{"score": 0.42, "reasons": "部分回答"}\n```']),
        "预算多少", "不清楚",
    )
    assert v["score"] == 0.42


async def test_judge_retries_once_then_none():
    from app.services.eval_judge import relevancy_score

    ok = await relevancy_score(
        _LLMScript(["不是 json", '{"score": 0.8}']), "q", "a"
    )
    assert ok["score"] == 0.8
    bad = await relevancy_score(
        _LLMScript(["坏输出", "还是坏"]), "q", "a"
    )
    assert bad["score"] is None


async def test_judge_prompt_shape():
    from app.services.eval_judge import faithfulness_score, relevancy_score

    fl = _LLMScript(['{"score": 1.0}'])
    await faithfulness_score(fl, "q", "a", ["参考资料甲"])
    assert "参考资料" in fl.calls[0][1][1]  # user 消息带上下文
    rl = _LLMScript(['{"score": 1.0}'])
    await relevancy_score(rl, "q", "a")
    assert "参考资料" not in rl.calls[0][1][1]  # 切题度只看问题与答案
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_eval_judge.py -q`
Expected: FAIL(`ModuleNotFoundError: app.services.eval_judge`)。

- [ ] **Step 3: 最小实现(app/services/eval_judge.py 全文)**

```python
"""M6 生成质量 LLM-judge:faithfulness(忠实度)与 answer relevancy(切题度)。

与 RAGAS 的取舍:只要两个指标,自研中文 prompt 零依赖、输出可控
(spec §2.1);坏输出重试一次,仍坏记 None 不伪装成 0。
"""
import json

from loguru import logger

from app.services.chat_graph.nodes import _extract_json

FAITHFULNESS_SYSTEM = (
    "你是答案忠实度评审。对照参考资料判断答案中的陈述是否都有依据:"
    "全部有依据接近1.0,部分有依据取中间值,存在编造接近0.0;"
    '答案明确表示"知识库中未找到相关内容"类拒答时按1.0。'
    '只输出 JSON:{"score": <0.0~1.0 的数值>, "reasons": "<一句话依据>"}'
)

RELEVANCY_SYSTEM = (
    "你是答案切题度评审。判断答案是否直接回答了所问问题:"
    "完整回答接近1.0,部分回答取中间值,答非所问接近0.0;"
    "问题确实无法回答且答案礼貌拒答时按0.5。"
    '只输出 JSON:{"score": <0.0~1.0 的数值>, "reasons": "<一句话依据>"}'
)


async def _judge(llm, system: str, user: str) -> dict:
    for _ in range(2):  # 坏输出重试一次
        try:
            resp = await llm.ainvoke([("system", system), ("user", user)])
            parsed = json.loads(_extract_json(resp.content))
            score = max(0.0, min(1.0, float(parsed["score"])))
            return {"score": score, "reasons": str(parsed.get("reasons", ""))[:200]}
        except Exception:
            logger.warning("judge output unparseable, retrying")
    return {"score": None, "reasons": "parse failed"}


async def faithfulness_score(llm, question: str, answer: str, contexts: list[str]) -> dict:
    ctx = "\n\n".join(f"[{i + 1}] {c}" for i, c in enumerate(contexts))
    user = f"问题:{question}\n参考资料:\n{ctx}\n答案:{answer}"
    return await _judge(llm, FAITHFULNESS_SYSTEM, user)


async def relevancy_score(llm, question: str, answer: str) -> dict:
    user = f"问题:{question}\n答案:{answer}"
    return await _judge(llm, RELEVANCY_SYSTEM, user)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_eval_judge.py -q`
Expected: 3 PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/eval_judge.py backend/tests/test_eval_judge.py
git commit -m "feat: m6 llm-judge module for faithfulness and relevancy"
```

---

### Task 5: eval_generation CLI

**Files:**
- Create: `backend/scripts/eval_generation.py`
- Test: `backend/tests/test_eval_generation.py`(新文件)

**Interfaces:**
- Consumes: Task 4 `faithfulness_score`/`relevancy_score`;`scripts.eval_metrics.load_eval_set(path) -> dict`;`build_graph(llm=...)`/`make_chat_llm()`(graph.py 现有);`settings.ZHIPU_API_KEY`/`settings.RETRIEVAL_TOP_K`。
- Produces: `async run(kb_id: int, use_rerank: bool) -> list[dict]`(元素含 `question/faithfulness/relevancy/citations`,前两者为 judge dict);`main()` 支持 `--kb/--rerank/--json`。Task 8 验收以子进程方式调用本 CLI。

- [ ] **Step 1: 写失败测试(新文件 tests/test_eval_generation.py)**

```python
import json


class _FakeGraph:
    async def ainvoke(self, init, config=None):
        return {
            "answer": "三千万[1]",
            "hits": [{"content": "预算三千万"}],
            "citations": [{"number": 1}],
        }


async def test_eval_generation_run(tmp_path, monkeypatch, db_session):
    import scripts.eval_generation as eg
    from app.core.config import settings
    from app.core.security import hash_password
    from app.models import KnowledgeBase, User

    monkeypatch.setattr(settings, "ZHIPU_API_KEY", "k")

    u = User(username="evalgen", password_hash=hash_password("x"))
    db_session.add(u)
    await db_session.flush()
    kb = KnowledgeBase(name="评估库", owner_id=u.id)
    db_session.add(kb)
    await db_session.commit()

    (tmp_path / f"{kb.id}.json").write_text(
        json.dumps({"kb_id": kb.id,
                    "items": [{"question": "预算多少", "expect_doc_ids": [1]}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(eg, "EVAL_DIR", tmp_path)

    import app.services.chat_graph.graph as graph_mod
    monkeypatch.setattr(graph_mod, "build_graph",
                        lambda llm=None, checkpointer=None: _FakeGraph())

    import app.services.eval_judge as ej

    async def fake_f(llm, q, a, contexts):
        return {"score": 0.9, "reasons": "ok"}

    async def fake_r(llm, q, a):
        return {"score": 0.8, "reasons": "ok"}

    monkeypatch.setattr(ej, "faithfulness_score", fake_f)
    monkeypatch.setattr(ej, "relevancy_score", fake_r)

    results = await eg.run(kb.id, False)
    assert len(results) == 1
    assert results[0]["faithfulness"]["score"] == 0.9
    assert results[0]["relevancy"]["score"] == 0.8
    assert results[0]["citations"] == 1


async def test_eval_generation_requires_key(monkeypatch):
    from app.core.config import settings
    import scripts.eval_generation as eg

    monkeypatch.setattr(settings, "ZHIPU_API_KEY", "")
    try:
        await eg.run(1, False)
        raised = False
    except SystemExit:
        raised = True
    assert raised
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_eval_generation.py -q`
Expected: FAIL(模块不存在)。

- [ ] **Step 3: 最小实现(scripts/eval_generation.py 全文)**

```python
"""生成质量评估 CLI(LLM-judge)。

用法(backend 目录下):
    .venv\\Scripts\\python -m scripts.eval_generation --kb 3 [--rerank] [--json]

复用 eval_sets/{kb_id}.json(格式见 eval_sets/README.md,expect_* 字段透传不使用);
对每题跑完整问答图,LLM 评 faithfulness(忠实度)与 relevancy(切题度)。
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from scripts.eval_metrics import load_eval_set

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval_sets"


def _fmt(score):
    return f"{score:.2f}" if score is not None else "N/A"


async def run(kb_id: int, use_rerank: bool) -> list[dict]:
    from app.core.config import settings
    from app.db.session import SessionLocal
    from app.models import KnowledgeBase
    from app.services.chat_graph.graph import build_graph, make_chat_llm
    from app.services.eval_judge import faithfulness_score, relevancy_score

    if not settings.ZHIPU_API_KEY:
        sys.exit("ZHIPU_API_KEY 未配置:桩答案的 LLM-judge 评估无意义,拒绝运行")

    set_path = EVAL_DIR / f"{kb_id}.json"
    if not set_path.exists():
        sys.exit(f"eval set not found: {set_path}(格式见 eval_sets/README.md)")
    data = load_eval_set(set_path)

    async with SessionLocal() as db:
        kb = await db.get(KnowledgeBase, kb_id)
    if kb is None:
        sys.exit(f"knowledge base {kb_id} not found")

    llm = make_chat_llm()  # 生成与评审共用同一实例(单例)
    graph = build_graph(llm=llm)
    results = []
    for item in data["items"]:
        final = await graph.ainvoke(
            {"question": item["question"], "kb_ids": [kb_id],
             "rerank": use_rerank, "history": []}
        )
        answer = final.get("answer") or ""
        contexts = [
            h["content"]
            for h in (final.get("hits") or [])[: settings.RETRIEVAL_TOP_K]
        ]
        results.append(
            {
                "question": item["question"],
                "faithfulness": await faithfulness_score(
                    llm, item["question"], answer, contexts),
                "relevancy": await relevancy_score(llm, item["question"], answer),
                "citations": len(final.get("citations") or []),
            }
        )
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", type=int, required=True)
    ap.add_argument("--rerank", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    results = asyncio.run(run(args.kb, args.rerank))
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    print(f"{'问题':<28} 忠实度  切题度  引用数")
    for r in results:
        print(f"{r['question'][:26]:<28} "
              f"{_fmt(r['faithfulness']['score']):<7} "
              f"{_fmt(r['relevancy']['score']):<7} {r['citations']}")
    n = len(results)
    parse_errors = sum(
        1 for r in results
        if r["faithfulness"]["score"] is None or r["relevancy"]["score"] is None
    )
    for key, label in (("faithfulness", "忠实度"), ("relevancy", "切题度")):
        vals = [r[key]["score"] for r in results if r[key]["score"] is not None]
        avg = f"{sum(vals) / len(vals):.2f}" if vals else "N/A"
        print(f"\n汇总:n={n}  {label}={avg}", end="")
    print(f"  (parse_errors={parse_errors})")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_eval_generation.py -q`
Expected: 2 PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/scripts/eval_generation.py backend/tests/test_eval_generation.py
git commit -m "feat: m6 generation-quality eval cli with llm-judge"
```

---

### Task 6: 审计 purge_expired + admin 手动端点

**Files:**
- Modify: `backend/app/core/config.py`(加 AUDIT_RETENTION_DAYS)
- Modify: `backend/app/services/audit.py`(加 purge_expired)
- Modify: `backend/app/api/admin.py`(加 POST /audit-logs/purge)
- Modify: `E:\Projects\AIRag\.env.example`(加 1 个 M6 变量)
- Test: `backend/tests/test_audit_retention.py`(新文件)

**Interfaces:**
- Produces: `async purge_expired(db: AsyncSession) -> int`(禁用返回 0;删除后**在函数内**落一条 `audit_purge` 摘要,不自行 commit);端点 `POST /api/admin/audit-logs/purge` → `{"deleted": int, "retention_days": int}`(admin-only)。Task 7 的 Celery 任务复用 purge_expired。
- Consumes: 现有 `audit(db, username, action, target, detail)`;`AuditLog.created_at`(timestamptz, server_default=now())。

- [ ] **Step 1: 写失败测试(新文件 tests/test_audit_retention.py)**

```python
from sqlalchemy import select, text

from app.models import AuditLog


async def _make_admin(client, db_session, username="ret_admin"):
    created = await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    await db_session.execute(
        text("UPDATE users SET role = 'admin' WHERE id = :i"),
        {"i": created.json()["id"]},
    )
    await db_session.commit()
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_purge_deletes_only_expired(db_session, monkeypatch):
    from app.core.config import settings
    from app.services.audit import audit, purge_expired

    await audit(db_session, "u1", "login_success", "t")
    await db_session.commit()
    await db_session.execute(
        text("UPDATE audit_logs SET created_at = now() - interval '200 days'")
    )
    await audit(db_session, "u2", "login_fail", "t")
    await db_session.commit()

    monkeypatch.setattr(settings, "AUDIT_RETENTION_DAYS", 180)
    deleted = await purge_expired(db_session)
    await db_session.commit()

    assert deleted == 1
    actions = (await db_session.execute(select(AuditLog.action))).scalars().all()
    assert "login_fail" in actions
    assert "audit_purge" in actions  # 摘要留在同事务
    assert "login_success" not in actions


async def test_purge_disabled_returns_zero(db_session, monkeypatch):
    from app.core.config import settings
    from app.services.audit import audit, purge_expired

    await audit(db_session, "u1", "login_success", "t")
    await db_session.commit()
    await db_session.execute(
        text("UPDATE audit_logs SET created_at = now() - interval '900 days'")
    )
    await db_session.commit()

    monkeypatch.setattr(settings, "AUDIT_RETENTION_DAYS", 0)
    assert await purge_expired(db_session) == 0
    await db_session.commit()
    assert await (
        await db_session.execute(select(AuditLog.action))
    ).scalars().one() == "login_success"


async def test_purge_endpoint_admin_only(client, db_session, auth_headers, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "AUDIT_RETENTION_DAYS", 180)
    admin = await _make_admin(client, db_session)
    resp = await client.post("/api/admin/audit-logs/purge", headers=admin)
    assert resp.status_code == 200
    assert set(resp.json()) == {"deleted", "retention_days"}
    assert resp.json()["retention_days"] == 180

    denied = await client.post("/api/admin/audit-logs/purge", headers=auth_headers)
    assert denied.status_code == 403
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_audit_retention.py -q`
Expected: FAIL(`cannot import name 'purge_expired'`;端点 404)。

- [ ] **Step 3: 最小实现**

`config.py` 加(OCR 块附近):

```python
    # M6 审计留存:超过天数自动/手动清理;0 = 禁用
    AUDIT_RETENTION_DAYS: int = 180
```

`app/services/audit.py` 整体改为:

```python
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import AuditLog


async def audit(
    db: AsyncSession,
    username: str,
    action: str,
    target: str = "",
    detail=None,
    ip: str | None = None,
) -> None:
    """追加一条审计记录;与业务同事务(不自行 commit)。"""
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False)
    db.add(
        AuditLog(
            username=username[:64],
            action=action[:32],
            target=target[:128],
            detail=detail,
            ip=ip,
        )
    )


async def purge_expired(db: AsyncSession) -> int:
    """按 AUDIT_RETENTION_DAYS 清理过期审计;禁用返回 0。

    删除动作自身在本事务落一条 audit_purge 摘要(调用方负责 commit)。
    """
    days = settings.AUDIT_RETENTION_DAYS
    if days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await db.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
    deleted = result.rowcount or 0
    await audit(
        db, "system", "audit_purge", "audit_logs",
        {"deleted": deleted, "retention_days": days},
    )
    return deleted
```

(注:`action=action[:32]` 是顺手对齐模型列宽,不改行为。)

`app/api/admin.py`:导入区加 `from app.core.config import settings` 与 `from app.services.audit import audit, purge_expired`(audit 原已导入,合并);文件末尾加:

```python
@router.post("/audit-logs/purge")
async def purge_audit_logs(
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    deleted = await purge_expired(db)
    await db.commit()
    return {"deleted": deleted, "retention_days": settings.AUDIT_RETENTION_DAYS}
```

`.env.example` 末尾追加:

```
# M6:审计日志留存天数(超期自动清理,worker 带 -B 时每日 03:00 执行);0 = 禁用
AUDIT_RETENTION_DAYS=180
```

- [ ] **Step 4: 跑测试确认通过 + 存量回归**

Run: `.venv\Scripts\python.exe -m pytest tests/test_audit_retention.py tests/test_audit.py tests/test_admin_users.py -q`
Expected: 全 PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/core/config.py backend/app/services/audit.py backend/app/api/admin.py backend/tests/test_audit_retention.py .env.example
git commit -m "feat: m6 audit retention purge with admin endpoint"
```

---

### Task 7: Celery beat 任务 + 运行文档(-B)

**Files:**
- Modify: `backend/app/workers/celery_app.py`(include + beat_schedule)
- Create: `backend/app/workers/maintenance.py`
- Modify: `backend/start_worker.bat`(celery 命令行加 `-B`)
- Modify: `E:\Projects\AIRag\README.md`(worker 一节加 beat 说明)
- Test: `backend/tests/test_maintenance.py`(新文件)

**Interfaces:**
- Consumes: Task 6 `purge_expired`;pipeline.py 现有 `_run_async(coro)` 与 `_engine(db_url)`;`celery_app`(现有实例)。
- Produces: Celery 任务 `purge_expired_audit_logs`(任务名 `app.workers.maintenance.purge_expired_audit_logs`,返回删除行数);beat 条目键 `purge-expired-audit-logs`(每日 03:00,Asia/Shanghai)。

- [ ] **Step 1: 写失败测试(新文件 tests/test_maintenance.py)**

```python
from sqlalchemy import text


async def test_beat_schedule_targets_purge():
    from app.workers.celery_app import celery_app

    sched = celery_app.conf.beat_schedule
    assert "purge-expired-audit-logs" in sched
    entry = sched["purge-expired-audit-logs"]
    assert entry["task"] == "app.workers.maintenance.purge_expired_audit_logs"


async def test_purge_task_deletes_expired_rows(db_session):
    from app.services.audit import audit
    from app.workers.maintenance import purge_expired_audit_logs

    await audit(db_session, "u1", "login_success", "t")
    await db_session.commit()
    await db_session.execute(
        text("UPDATE audit_logs SET created_at = now() - interval '400 days'")
    )
    await db_session.commit()

    deleted = purge_expired_audit_logs()  # sync 壳;测试进程 env 指向 airag_test
    assert deleted >= 1
    rows = await db_session.execute(text("SELECT action FROM audit_logs"))
    assert "login_success" not in [r[0] for r in rows]
    assert "audit_purge" in [r[0] for r in rows]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_maintenance.py -q`
Expected: FAIL(beat_schedule 空 / maintenance 模块不存在)。

- [ ] **Step 3: 最小实现**

`app/workers/maintenance.py` 全文:

```python
"""审计留存定时任务(beat 每日 03:00,见 celery_app.beat_schedule)。"""

from loguru import logger
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.services.audit import purge_expired
from app.workers.celery_app import celery_app
from app.workers.pipeline import _engine, _run_async


@celery_app.task(name="app.workers.maintenance.purge_expired_audit_logs")
def purge_expired_audit_logs():
    async def _inner():
        engine = _engine(settings.DATABASE_URL)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                deleted = await purge_expired(session)
                await session.commit()
                return deleted
        finally:
            await engine.dispose()

    deleted = _run_async(_inner())
    logger.info("audit retention purge deleted {} rows", deleted)
    return deleted
```

`celery_app.py`:`include` 加 `"app.workers.maintenance"`;conf.update 增补 beat(顶部加 `from celery.schedules import crontab`):

```python
celery_app = Celery(
    "airag",
    broker=settings.REDIS_URL,
    include=["app.workers.pipeline", "app.workers.maintenance"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    beat_schedule={
        "purge-expired-audit-logs": {
            "task": "app.workers.maintenance.purge_expired_audit_logs",
            "schedule": crontab(hour=3, minute=0),
        }
    },
)
```

`start_worker.bat`:只改 celery 命令行(该文件注释为 GBK 编码,勿动其余行):

```bat
.venv\Scripts\python -m celery -A app.workers.celery_app worker -B --pool=solo --loglevel=info
```

README "开发启动" 的 worker 段(Redis 说明行之后)加一行:

```markdown
Worker 带 `-B` 内嵌 beat(M6 起):每日 03:00 自动清理超期审计日志(`AUDIT_RETENTION_DAYS`,默认 180 天,0=禁用)。
```

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: **125 passed**(104 基线 + T1 4 + T2 2 + T3 5 + T4 3 + T5 2 + T6 3 + T7 2;若个别任务实增测试数不同,以全绿为准并回填此处)。

- [ ] **Step 5: 提交**

```bash
git add backend/app/workers/maintenance.py backend/app/workers/celery_app.py backend/start_worker.bat README.md backend/tests/test_maintenance.py
git commit -m "feat: m6 celery beat audit purge task and worker -B"
```

---

### Task 8: M6 验收脚本 + 全量回归 + 收尾

**Files:**
- Create: `backend/scripts/m6_acceptance.py`
- Create: `backend/eval_sets/`(验收轮新增 `{kb_id}.json`,运行时生成)
- Modify: 本计划文件(勾选同步)

**Interfaces:**
- Consumes: 真后端 `http://127.0.0.1:8001/api`(uvicorn 启动);Task 5 CLI 子进程;Task 6 端点;`m5_acceptance.py` 的既有 helper 模式(注册/SQL 晋升/轮询,复制不 import)。
- Produces: 终端输出 `M6 ACCEPTANCE: N/M PASS` 与失败明细。

- [ ] **Step 1: 写验收脚本(步骤级清单,断言完整;沿用 M5 模式,httpx 调真后端,asyncpg 直连开发库执行 SQL)**

脚本骨架与 helper(从 m5_acceptance.py 复制改写):

```python
"""M6 验收:多跳兜底 / 生成质量评估 / 审计留存。对真后端 127.0.0.1:8001 执行。"""
import asyncio
import io
import json
import subprocess
import sys

import asyncpg
import httpx

BASE = "http://127.0.0.1:8001/api"
DB_DSN = "postgresql://airag:airag_dev_password@localhost:5432/airag"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


# helpers:register_and_login / promote_sql(asyncpg 执行 UPDATE users SET role)
#          / wait_doc_status(轮询 GET /kbs/{kb}/documents 直到 done/failed,上限 120s)
#          / sse_ask(POST /chat/ask 收集 token/citations/done 帧,返回 (answer, done_data))
#          / make_docx_bytes(paragraphs: list[str])(python-docx 生成真 docx)
```

场景与断言(按序执行,两个断言连续失败才算 FAIL——M5 容错同款):

1. **基线**:注册 `m6_user`(升 admin)与 `m6_owner`(升 editor);`GET /health` 200。
2. **零命中多跳路径(确定性,验证兜底不炸)**:`m6_owner` 建库"M6验收库" → 上传含事实的 docx("猎户座反应堆功率五十兆瓦")等 done → ask"企鹅的巡游路线和考拉的食谱是什么"(库内零命中词) → SSE 正常走完:done 帧存在、answer 含"未找到"(零命中→decompose→仍空→拒答,全链路无 error 帧)。
3. **复合问题质量(真 LLM,重试 1 次)**:再传第二份 docx("北极星货运飞船载重三十吨")等 done → ask"猎户座反应堆的功率和北极星飞船的载重分别是多少" → answer 同时含"五十"与"三十"。
4. **eval_generation(真智谱)**:对"M6验收库"写 `eval_sets/{kb_id}.json`(3 题:功率/载重/组合,expect_doc_ids=[两文档 id])→ 子进程 `.venv\Scripts\python.exe -m scripts.eval_generation --kb {kb_id} --json` → 解析 stdout JSON:3 条、faithfulness/relevancy 均非 None、忠实度均值 ≥0.8。`ZHIPU_API_KEY` 为空时本项与第 3 项记 SKIP 并结尾提示。
5. **审计清理**:asyncpg 直插 2 条 `created_at = now() - interval '200 days'` 的 audit_logs → `POST /admin/audit-logs/purge`(m6_user)→ `deleted >= 2` 且 `retention_days == 180`;`GET /admin/audit-logs?action=audit_purge` total≥1;`m6_owner` 调 purge 403。
6. **汇总**:打印 `M6 ACCEPTANCE: {pass}/{total} PASS` 与 FAIL 明细;非零 FAIL 时 exit 1。

- [ ] **Step 2: 本地起三件套跑验收**

Run(三个窗口,或按 M5 惯例):后端 `cd backend && .venv\Scripts\python.exe -m uvicorn app.main:app --port 8001`、worker `start_worker.bat`、PG/Redis 已有服务;然后 `cd backend && .venv\Scripts\python.exe scripts/m6_acceptance.py`
Expected: 全 PASS(无智谱 key 时第 3/4 项 SKIP)。跑前 `GET /health` 探活(uvicorn 热重载窗口偶发退出,M5 已知)。

- [ ] **Step 3: 全量回归**

Run: `cd backend && .venv\Scripts\python.exe -m pytest -q`;`cd frontend && pnpm build && pnpm test && pnpm oxlint`
Expected: 后端全绿(≥120 passed);前端 build 绿 + 6 passed + 0 warnings 0 errors(前端零改动,防意外)。

- [ ] **Step 4: 浏览器走查留给用户**(M5 同模式):多跳复合问题对话、审计页无回归;清进程(后端/worker)。

- [ ] **Step 5: 收尾提交与交接**

```bash
git add backend/scripts/m6_acceptance.py backend/eval_sets
git commit -m "chore: m6 acceptance script and eval set"
git commit --allow-empty -m "chore: m6 complete - acceptance verified"
```

推送 main;本计划勾选同步;记忆更新:M6 完成状态 + M7 交接(MinerU 本地化、LDAP 仍开放、reference_answer/评估入库、多跳子问题并行检索)。

---

## 计划自审记录(写完即检)

1. **Spec 覆盖**:§1.2 拓扑/三级路由/零命中兜底=Task 3;§1.3 decompose/retrieve 合并/hopped 重置=Task 1+2;§1.4 配置与 conftest=Task 1+3;§2.1 judge 模块=Task 4;§2.2 CLI=Task 5;§3.1 purge_expired=Task 6;§3.2 任务+beat+`-B`=Task 7;§3.3 端点=Task 6;§3.4 配置=Task 6;§4.1 单测=各任务 Step 1;§4.2 验收=Task 8。无遗漏。
2. **占位符扫描**:Task 8 为步骤级清单+断言列项(M4/M5 同模式,helper 给了复制来源与签名);其余任务代码全量给出。无 TBD/"类似 Task N"。
3. **类型一致性**:`decompose_node(state, llm)->{"sub_queries","hopped"}` 与 Task 3 接线、e2e 断言一致;`route_after_grade/route_after_rerank` 定义与拓扑断言一致;`purge_expired(db)->int` 在 Task 6 端点与 Task 7 任务两处调用一致且均由调用方 commit;`faithfulness_score(llm,q,a,contexts)`/`relevancy_score(llm,q,a)` 与 Task 5 run() 调用一致;eval_generation `run(kb_id, use_rerank)` 与测试/验收一致;任务名 `app.workers.maintenance.purge_expired_audit_logs` 与 beat/测试一致。
4. **计数一致性**(按测试函数):104 基线 → T1 +4(改 1)=108 → T2 +2=110 → T3 +5=115 → T4 +3=118 → T5 +2=120 → T6 +3=123 → T7 +2=125;test_chat_graph.py 存量 15 个。
5. **风险预置**:存量 `test_rewrite_disabled_resets_state` 因 reset 加 hopped 需同步改断言(T1 显式列出);conftest 隔离 MULTI_HOP 必须先于 Task 3 端到端(T3 Step 3 置顶强调);`_LLMScript`/`Boom` 复用现有测试类(T1 注明内联兜底);start_worker.bat 为 GBK 编码只改 ASCII 命令行;验收第 3 项为真 LLM 断言,按 M5 惯例一次重试;`python-docx` 已随 M2 解析器在 venv 中(验收造 docx 用)。
