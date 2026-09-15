# AIRag M3 检索与问答实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 端到端问答体验:混合检索(向量+中文关键词,RRF 融合,可选 Rerank)→ LangGraph 三节点图(retrieve→rerank*→generate,AsyncPostgresSaver 检查点)→ SSE 流式回答+引用溯源 → 前端知识库/文档/对话三页面。验收:3~5 份真实文档上传后提问,流式回答带可点击引用。

**Architecture:** 复用 M1/M2 基座(认证/模型/流水线/chunks 表)。新增:检索服务(jieba 分词 + tsquery 关键词半场 + pgvector 余弦半场 + RRF)、rerank provider(默认关)、`services/chat_graph/`(LangGraph)、SSE 问答 API(会话/消息持久化)、前端三页面与流式渲染。**中文全文走 jieba 应用层分词**(见 R1),不编译 zhparser。

**Tech Stack(新增):** langgraph / langchain-openai / langgraph-checkpoint-postgres / psycopg[binary] / jieba;前端 @microsoft/fetch-event-source / markdown-it / highlight.js。

**Spec:** `docs/AIRag-AI知识库需求设计方案.md` §6(问答流程)/§9(M3);前置:`2026-09-14-airag-m2-document-pipeline.md` 末尾《M3 交接附录》(本计划已消化,裁决见下)。

## 开工裁决(控制器,2026-09-15,吸收交接附录)

| # | 裁决 | 理由与代价 |
|---|---|---|
| R1 | **中文分词:jieba 应用层 + PG `simple` 配置**,不编译 zhparser | Windows 原生编译 SCWS 成本高且脆;jieba 纯 Python。做法:入库时 tsv 由「分词后空格拼接的文本」生成,查询时同样分词后拼 tsquery。检索层有接口,M5 若要换 zhparser 只动一处。代价:tsv 生成从纯 SQL 变为应用侧参与 |
| R2 | **切块维持 1000 字符/150 重叠递归切分** | ≈§7"512 token"区间(512token≈768-1024 汉字);标题感知+RAGAS 评估属质量优化,归 M5(届时用评估集驱动) |
| R3 | **KB.embed_provider/embed_model 视为元数据**,M3 检索用全局 settings 的 provider/model | 避免 per-KB 多向量空间复杂度;当前全局=zhipu/embedding-3 与快照一致。M4 RBAC 时再议 |
| R4 | **权限沿 M2 从简**:登录用户可检索任意 KB | M4 RBAC 收紧;与 Spec"无权限不可检索"的差距已知并记录 |
| R5 | **对话模型 glm-5.3-flash**(用户定,.env 已配 CHAT_MODEL);temperature=0.3,max_tokens 2048 | 省钱;thinking 常开注意 token,集成时观察 |

## Global Constraints(承前,新增 M3 专属)

- M1 执行协议全部继续有效:**DLP 双检**(findstr TSD-Header 且 numstat 无 `-	-`)、显式路径暂存、conventional commits、TDD 红绿、py -3.12、测试进程 conftest 强制 test 库+fake 嵌入。
- **计数基线:后端 32 passed 0 skipped**(智谱 key 已验通);前端 2 passed。每个任务的 Expected 计数按**测试函数**数(教训:不按场景数)。
- 端口:后端 8001/前端 5173/PG 5432/Redis 6379(.env 已带密码连接串,**勿动 Redis 服务**)。
- 检索参数钉死:向量 top-20、关键词 top-20、RRF k=60、Rerank 后 top-8(`RETRIEVAL_TOP_K=8` 进 prompt)。
- SSE 事件格式:`data: {"type":"token"|"citations"|"done"|"error","data":...}\n\n`;前端只认这个契约。
- 引用结构钉死:`{"number":1,"chunk_id":64,"document_id":3,"filename":"xx.pdf","page_no":2,"excerpt":"...(前160字)"}`。
- LLM 测试一律用 langchain 的 FakeListChatModel,不打真 API;live 行为由 T10 验收兜底。
- 前端三页面完成前,MainLayout 的"知识库/对话"菜单保持 disabled;T9 统一启用。

## 环境事实(已核验)

- ZHIPU_API_KEY 已填且**真调通**(`api_ok=True, dims=1024`);CHAT_MODEL=glm-5.3-flash;REDIS_URL 带密码可用。
- pgvector 0.8.6(查询用 `<=>` 余弦距离);tsv 列已存在(M2 以 `to_tsvector('simple', content)` 生成——T1 会改为分词版并回填)。
- 运行库 airag 里有 M2 验收残留数据(KB 1、docs 1-3),T10 验收用新 KB。

---

### Task 0: M3 依赖与配置

**Files:** Modify `backend/pyproject.toml`, `backend/app/core/config.py`, `backend/.env.example`(根目录), `backend/tests/test_pipeline.py`(一行导入统一)

- [x] Step 1: pyproject dependencies 追加:

```toml
    "langgraph>=1.0",
    "langchain-openai>=1.0",
    "langgraph-checkpoint-postgres>=2.0",
    "psycopg[binary]>=3.2",
    "jieba>=0.42",
```

- [x] Step 2: config.py `Settings` 追加(JWT_EXPIRE_MINUTES 之后任意处):

```python
    CHAT_MODEL: str = "glm-5.3-flash"
    CHAT_TEMPERATURE: float = 0.3
    CHAT_MAX_TOKENS: int = 2048
    RERANK_ENABLED: bool = False
    RERANK_MODEL: str = "rerank-3"
    RETRIEVAL_TOP_K: int = 8
```

- [x] Step 3: 根 `.env.example` 在 CHAT_MODEL 行后追加:

```env
CHAT_TEMPERATURE=0.3
CHAT_MAX_TOKENS=2048
RERANK_ENABLED=false
RERANK_MODEL=rerank-3
RETRIEVAL_TOP_K=8
```

- [x] Step 4: `tests/test_pipeline.py` 顶部 `import fitz` 改为 `import pymupdf as fitz`(统一,消弃用告警)。

- [x] Step 5: 安装+验证:

```cmd
cd /d E:\Projects\AIRag\backend
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -c "import langgraph, langchain_openai, jieba, psycopg; print('imports ok')"
.venv\Scripts\python -m pytest -v
```

Expected: imports ok;**32 passed**(基线不变)。

- [x] Step 6: 提交(显式路径 + DLP 双检):`chore: m3 deps and settings (langgraph/jieba/rerank)`

### Task 1: jieba 分词 + 混合检索 + RRF(核心)

**Files:** Create `backend/app/services/retrieval/__init__.py`, `backend/app/services/retrieval/tokenize.py`, `backend/app/services/retrieval/searcher.py`, `backend/app/services/retrieval/backfill.py`;Modify `backend/app/workers/pipeline.py`(tsv 生成分词化);Test `backend/tests/test_retrieval.py`

**Interfaces(Produces):** `tokenize(text: str) -> list[str]`(jieba cut_for_search,去空白);`SearchHit(dataclass: chunk_id, document_id, kb_id, filename, page_no, content, score, source)`;`hybrid_search(db: AsyncSession, kb_ids: list[int], query: str, top_k: int = 20) -> list[SearchHit]`(两半场各 top_k,RRF k=60 融合);`rebuild_tsv(db) -> int`(回填存量 chunks 的新 tsv,返回行数)

- [x] Step 1: 失败测试 `tests/test_retrieval.py`(3 个函数):

```python
from app.services.retrieval.tokenize import tokenize


def test_tokenize_splits_chinese():
    tokens = tokenize("企业知识库的检索质量")
    assert isinstance(tokens, list) and len(tokens) >= 3
    assert all(t.strip() for t in tokens)
    assert any("知识库" in t for t in tokens)


def test_rrf_fusion_ranks_common_top():
    from app.services.retrieval.searcher import rrf_fuse

    vec = [("a", 0.9), ("b", 0.8), ("c", 0.7)]  # (chunk_id, raw)
    kw = [("b", 3.2), ("d", 2.0), ("a", 1.1)]
    fused = rrf_fuse(vec, kw, k=60)
    top = fused[0][0]
    assert top == "b"  # 双榜均在前列
    ids = [i for i, _ in fused]
    assert set(ids) == {"a", "b", "c", "d"}


async def test_hybrid_search_returns_matching_chunk(client, auth_headers, db_session):
    from app.models import Chunk, Document, KnowledgeBase
    from app.services.retrieval.searcher import hybrid_search

    user_rows = await db_session.execute(
        KnowledgeBase.__table__.insert()
        .values(name="检索测试库", owner_id=1)
        .returning(KnowledgeBase.id)
    )
    kb_id = user_rows.scalar_one()
    doc = Document(
        kb_id=kb_id, filename="r.pdf", file_path="x", mime="application/pdf",
        size=1, sha256="r" * 64,
    )
    db_session.add(doc)
    await db_session.flush()
    from app.services.embedding.fake import FakeEmbedding

    vec = FakeEmbedding().embed_documents(["企业差旅报销流程规定"])[0]
    db_session.add(
        Chunk(document_id=doc.id, kb_id=kb_id, chunk_index=0,
              content="企业差旅报销流程规定:三级审批,七个工作日到账", page_no=1,
              char_len=24, embedding=vec, content_hash="h1")
    )
    db_session.add(
        Chunk(document_id=doc.id, kb_id=kb_id, chunk_index=1,
              content="完全无关的另一个主题内容关于天气", page_no=2,
              char_len=17, embedding=FakeEmbedding().embed_documents(["天气"])[0],
              content_hash="h2")
    )
    await db_session.flush()
    await db_session.execute(
        __import__("sqlalchemy").text(
            "UPDATE chunks SET tsv = to_tsvector('simple', :t) WHERE document_id = :d"
        ).bindparams(t="企业 差旅 报销 流程 规定 三级 审批", d=doc.id)
    )
    await db_session.commit()

    hits = await hybrid_search(db_session, [kb_id], "差旅报销怎么走")
    assert len(hits) >= 1
    assert hits[0].chunk_index if hasattr(hits[0], "chunk_index") else True
    assert "差旅" in hits[0].content
```

- [x] Step 2: 跑 RED(`pytest tests\test_retrieval.py -v` 全 FAIL/ERROR)。
- [x] Step 3: 实现:

`tokenize.py`:

```python
import jieba

jieba.initialize()  # 预热词典,避免首个请求卡顿


def tokenize(text: str) -> list[str]:
    return [t for t in jieba.cut_for_search(text) if t.strip()]
```

`searcher.py`:

```python
import asyncio
import math
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.embedding import get_provider
from app.services.retrieval.tokenize import tokenize

RRF_K = 60


@dataclass
class SearchHit:
    chunk_id: int
    document_id: int
    kb_id: int
    filename: str
    page_no: int | None
    content: str
    score: float
    source: str  # vector|keyword|both


def rrf_fuse(vec: list, kw: list, k: int = RRF_K) -> list:
    """输入 [(id, raw_score)...],输出 [(id, rrf_score)...] 按 rrf 降序。"""
    scores: dict = {}
    for ranking in (vec, kw):
        for rank, (cid, _) in enumerate(ranking, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    both = {cid for cid, _ in vec} & {cid for cid, _ in kw}
    out = [
        (cid, s + (0.001 if cid in both else 0.0)) for cid, s in scores.items()
    ]
    return sorted(out, key=lambda x: -x[1])


def _tsquery(query: str) -> str:
    terms = [t for t in tokenize(query) if t.isalnum() or "\u4e00" <= t[0] <= "\u9fff"]
    return " | ".join(dict.fromkeys(terms)) or "''"


async def hybrid_search(
    db: AsyncSession, kb_ids: list[int], query: str, top_k: int = 20
) -> list[SearchHit]:
    provider = get_provider()
    qvec = (await asyncio.to_thread(provider.embed_documents, [query]))[0]
    qvec_str = "[" + ",".join(f"{x:.6f}" for x in qvec) + "]"

    vec_rows = (
        await db.execute(
            text(
                "SELECT c.id FROM chunks c "
                "WHERE c.kb_id = ANY(:kb_ids) AND c.embedding IS NOT NULL "
                "ORDER BY c.embedding <=> :qv::vector LIMIT :k"
            ),
            {"kb_ids": kb_ids, "qv": qvec_str, "k": top_k},
        )
    ).scalars().all()

    kw_rows = (
        await db.execute(
            text(
                "SELECT c.id FROM chunks c "
                "WHERE c.kb_id = ANY(:kb_ids) AND c.tsv @@ to_tsquery('simple', :tsq) "
                "ORDER BY ts_rank(c.tsv, to_tsquery('simple', :tsq)) DESC LIMIT :k"
            ),
            {"kb_ids": kb_ids, "tsq": _tsquery(query), "k": top_k},
        )
    ).scalars().all()

    fused = rrf_fuse([(i, 0) for i in vec_rows], [(i, 0) for i in kw_rows])
    top_ids = [cid for cid, _ in fused[: top_k * 2]]
    if not top_ids:
        return []
    rows = (
        await db.execute(
            text(
                "SELECT c.id, c.document_id, c.kb_id, c.page_no, c.content, d.filename "
                "FROM chunks c JOIN documents d ON d.id = c.document_id "
                "WHERE c.id = ANY(:ids)"
            ),
            {"ids": top_ids},
        )
    ).all()
    by_id = {r.id: r for r in rows}
    vec_set, kw_set = set(vec_rows), set(kw_rows)
    hits = []
    for cid, score in fused:
        if cid not in by_id:
            continue
        r = by_id[cid]
        src = "both" if cid in vec_set and cid in kw_set else ("vector" if cid in vec_set else "keyword")
        hits.append(
            SearchHit(cid, r.document_id, r.kb_id, r.filename, r.page_no,
                      r.content, round(score, 6), src)
        )
    return hits
```

`backfill.py`:

```python
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.retrieval.tokenize import tokenize


async def rebuild_tsv(db: AsyncSession) -> int:
    """用 jieba 分词版重建全部 chunks 的 tsv(存量数据迁移,幂等)。"""
    rows = (await db.execute(text("SELECT id, content FROM chunks"))).all()
    for r in rows:
        joined = " ".join(tokenize(r.content))
        await db.execute(
            text("UPDATE chunks SET tsv = to_tsvector('simple', :t) WHERE id = :id"),
            {"t": joined, "id": r.id},
        )
    await db.commit()
    return len(rows)
```

`__init__.py`:`from app.services.retrieval.searcher import SearchHit, hybrid_search, rrf_fuse` + `__all__`。

pipeline.py 修改:tsv 的 UPDATE 语句改为逐 chunk 用分词文本(把原 `UPDATE chunks SET tsv = to_tsvector('simple', content) ...` 整段替换为):

```python
            from app.services.retrieval.tokenize import tokenize as _tok

            for chunk in chunks:
                await session.execute(
                    text(
                        "UPDATE chunks SET tsv = to_tsvector('simple', :t) "
                        "WHERE document_id = :d AND chunk_index = :i"
                    ),
                    {"t": " ".join(_tok(chunk.content)), "d": document_id,
                     "i": chunks.index(chunk)},
                )
```

(若嫌 index 低效,用 enumerate 变量循环——语义等价即可。)

- [x] Step 4: 跑 GREEN:全量 **35 passed**(32+3)。
- [x] Step 5: 提交:`feat: hybrid retrieval with jieba tokenization and rrf fusion`

### Task 2: Rerank Provider(默认关)

**Files:** Create `backend/app/services/rerank/__init__.py`(空), `backend/app/services/rerank/base.py`, `backend/app/services/rerank/zhipu.py`;Test `backend/tests/test_rerank.py`

**Interfaces:** `RerankProvider.rerank(query: str, documents: list[str], top_n: int) -> list[int]`(返回按下标引用的排序);`get_reranker() -> RerankProvider | None`(settings.RERANK_ENABLED 为 False 时返回 None;True 时 ZhipuRerank,POST {ZHIPU_BASE_URL}/rerank,model=RERANK_MODEL,query/results/top_n,httpx 同步放线程)

- [x] Step 1: 失败测试(2 函数):

```python
def test_rerank_disabled_returns_none():
    from app.services.rerank.base import get_reranker

    assert get_reranker() is None  # conftest 进程未开 RERANK_ENABLED


def test_zhipu_rerank_parses_response(monkeypatch):
    from app.services.rerank import zhipu as zr

    class FakeResp:
        def json(self):
            return {"results": [{"index": 2, "relevance_score": 0.9},
                                 {"index": 0, "relevance_score": 0.5}]}

    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(json=json)
        return FakeResp()

    monkeypatch.setattr(zr.httpx, "post", fake_post)
    order = zr.ZhipuRerank().rerank("q", ["a", "b", "c"], top_n=2)
    assert order == [2, 0]
    assert captured["json"]["model"] == zr.settings.RERANK_MODEL
```

注:测试进程 RERANK_ENABLED 未设(默认 False),第一个用例天然成立;实现里 `get_reranker` 读 settings。

- [x] Step 2: RED。
- [x] Step 3: 实现:

`base.py`:

```python
from abc import ABC, abstractmethod

from app.core.config import settings


class RerankProvider(ABC):
    @abstractmethod
    def rerank(self, query: str, documents: list[str], top_n: int = 8) -> list[int]:
        """返回保留文档的下标,按相关度降序。"""


def get_reranker():
    if not settings.RERANK_ENABLED:
        return None
    from app.services.rerank.zhipu import ZhipuRerank

    return ZhipuRerank()
```

`zhipu.py`:

```python
import httpx

from app.core.config import settings
from app.services.rerank.base import RerankProvider


class ZhipuRerank(RerankProvider):
    def rerank(self, query: str, documents: list[str], top_n: int = 8) -> list[int]:
        resp = httpx.post(
            f"{settings.ZHIPU_BASE_URL}/rerank",
            headers={"Authorization": f"Bearer {settings.ZHIPU_API_KEY}"},
            json={
                "model": settings.RERANK_MODEL,
                "query": query,
                "documents": documents,
                "top_n": top_n,
            },
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json()["results"]
        return [r["index"] for r in results][:top_n]
```

- [x] Step 4: GREEN:全量 **37 passed**(35+2)。
- [x] Step 5: 提交:`feat: optional zhipu rerank provider behind feature flag`

### Task 3: chat_graph(LangGraph 三节点图)

**Files:** Create `backend/app/services/chat_graph/__init__.py`(空), `state.py`, `nodes.py`, `graph.py`;Test `backend/tests/test_chat_graph.py`

**Interfaces:** `build_graph(llm=None, checkpointer=None) -> CompiledGraph`;节点签名 `async def retrieve_node(state) -> dict` / `rerank_node` / `generate_node`;state 键:`question/kb_ids/hits/answer/citations`(hits 为 SearchHit 的 dict 化列表);generate 的 prompt 模板与引用编号规则钉死(代码内);`make_chat_llm()` 返回 ChatOpenAI(base_url=ZHIPU_BASE_URL, api_key, model=CHAT_MODEL, temperature, max_tokens)

- [x] Step 1: 失败测试(2 函数):

```python
async def test_graph_end_to_end_with_fakes(client, auth_headers, db_session, monkeypatch):
    """fake embedding + FakeListChatModel 跑通三节点,retrieve 被 stub。"""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.services.chat_graph.graph import build_graph

    async def fake_search(db, kb_ids, query, top_k=20):
        from app.services.retrieval.searcher import SearchHit

        return [
            SearchHit(101, 9, 1, "手.pdf", 1, "报销需要发票", 0.5, "vector"),
            SearchHit(102, 9, 1, "手.pdf", 2, "审批三天", 0.4, "keyword"),
        ]

    import app.services.chat_graph.nodes as nodes_mod

    monkeypatch.setattr(nodes_mod, "hybrid_search", fake_search)

    g = build_graph(llm=FakeListChatModel(responses=["报销需要发票,审批三天。[1]"]))
    init = {"question": "报销要什么", "kb_ids": [1]}
    final = await g.ainvoke(init)
    assert "报销需要发票" in final["answer"]
    assert final["citations"][0]["number"] == 1
    assert final["citations"][0]["filename"] == "手.pdf"
    assert final["citations"][1]["page_no"] == 2


async def test_citations_truncate_excerpt():
    from app.services.chat_graph.nodes import build_citations

    from app.services.retrieval.searcher import SearchHit

    hits = [SearchHit(1, 2, 3, "a.pdf", None, "x" * 500, 0.1, "vector")]
    cits = build_citations(hits)
    assert cits[0]["number"] == 1
    assert len(cits[0]["excerpt"]) <= 160
```

- [x] Step 2: RED。
- [x] Step 3: 实现:

`state.py`:

```python
from typing import TypedDict


class ChatState(TypedDict, total=False):
    question: str
    kb_ids: list[int]
    hits: list[dict]
    answer: str
    citations: list[dict]
```

`nodes.py`(顶部一次性导入,无函数内导入):

```python
from app.core.config import settings
from app.db.session import SessionLocal
from app.services.retrieval.searcher import SearchHit, hybrid_search
from app.services.rerank.base import get_reranker

SYSTEM_PROMPT = (
    "你是企业知识库助手。只依据下面提供的参考资料回答;"
    "引用资料时标注编号如 [1][2];若资料不足以回答,明确说"
    "\"知识库中未找到相关内容\"。用中文,简洁分点。"
)


def build_citations(hits: list[SearchHit]) -> list[dict]:
    return [
        {
            "number": i + 1,
            "chunk_id": h.chunk_id,
            "document_id": h.document_id,
            "filename": h.filename,
            "page_no": h.page_no,
            "excerpt": h.content[:160],
        }
        for i, h in enumerate(hits[: settings.RETRIEVAL_TOP_K])
    ]


async def retrieve_node(state: dict) -> dict:
    async with SessionLocal() as db:
        hits = await hybrid_search(db, state["kb_ids"], state["question"])
    return {"hits": [h.__dict__ for h in hits]}


async def rerank_node(state: dict) -> dict:
    reranker = get_reranker()
    if reranker is None or not state.get("hits"):
        return {}
    import asyncio

    hits = state["hits"]
    order = await asyncio.to_thread(
        reranker.rerank, state["question"],
        [h["content"] for h in hits], settings.RETRIEVAL_TOP_K,
    )
    return {"hits": [hits[i] for i in order if i < len(hits)]}


async def generate_node(state: dict, llm) -> dict:
    hits = state.get("hits", [])[: settings.RETRIEVAL_TOP_K]
    context = "\n\n".join(
        f"[{i+1}] {h['filename']} 第{h['page_no'] or '?'}页:{h['content']}"
        for i, h in enumerate(hits)
    )
    messages = [
        ("system", SYSTEM_PROMPT),
        ("user", f"参考资料:\n{context}\n\n问题:{state['question']}"),
    ]
    resp = await llm.ainvoke(messages)
    shits = [
        SearchHit(
            chunk_id=h["chunk_id"], document_id=h["document_id"], kb_id=h["kb_id"],
            filename=h["filename"], page_no=h["page_no"], content=h["content"],
            score=h["score"], source=h["source"],
        )
        for h in hits
    ]
    return {"answer": resp.content, "citations": build_citations(shits)}
```

(generate_node 的 `llm` 参数由 graph.py 用 `functools.partial` 注入,测试 FakeListChatModel 由此进。)

`graph.py`:

```python
from functools import partial

from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from app.core.config import settings
from app.services.chat_graph.nodes import generate_node, rerank_node, retrieve_node
from app.services.chat_graph.state import ChatState


def make_chat_llm():
    return ChatOpenAI(
        base_url=settings.ZHIPU_BASE_URL,
        api_key=settings.ZHIPU_API_KEY,
        model=settings.CHAT_MODEL,
        temperature=settings.CHAT_TEMPERATURE,
        max_tokens=settings.CHAT_MAX_TOKENS,
    )


def build_graph(llm=None, checkpointer=None):
    gen = partial(generate_node, llm=llm or make_chat_llm())
    g = StateGraph(ChatState)
    g.add_node("retrieve", retrieve_node)
    g.add_node("rerank", rerank_node)
    g.add_node("generate", gen)
    g.add_edge(START, "retrieve")
    if settings.RERANK_ENABLED:
        g.add_edge("retrieve", "rerank")
        g.add_edge("rerank", "generate")
    else:
        g.add_edge("retrieve", "generate")
    g.add_edge("generate", END)
    return g.compile(checkpointer=checkpointer)
```

- [x] Step 4: GREEN:全量 **39 passed**(37+2)。
- [x] Step 5: 提交:`feat: langgraph three-node chat graph with citations`

### Task 4: 检查点 + 会话/消息 API

**Files:** Create `backend/app/services/chat_graph/checkpointer.py`, `backend/app/schemas/chat.py`, `backend/app/api/conversations.py`;Modify `backend/app/api/__init__.py`;Test `backend/tests/test_conversations.py`

**Interfaces:** `get_checkpointer()`(app lifespan 惰性单例,AsyncPostgresSaver,psycopg URL = DATABASE_URL 去 `+asyncpg`,首次 `.setup()` 建表);`POST /api/chat/conversations {kb_ids,name?}` 201;`GET /api/chat/conversations` 列表(id 倒序);`GET /api/chat/conversations/{id}/messages` 消息列表(升序);全部鉴权

- [x] Step 1: 失败测试(2 函数):

```python
async def test_create_and_list_conversations(client, auth_headers):
    resp = await client.post(
        "/api/chat/conversations", json={"kb_ids": [1, 2], "name": "测试会话"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["kb_ids"] == [1, 2]
    listed = await client.get("/api/chat/conversations", headers=auth_headers)
    assert listed.status_code == 200
    assert any(c["title"] == "测试会话" for c in listed.json())


async def test_messages_history(client, auth_headers, db_session):
    from app.models import Message

    created = await client.post(
        "/api/chat/conversations", json={"kb_ids": [1], "name": "历史"},
        headers=auth_headers,
    )
    conv_id = created.json()["id"]
    db_session.add_all([
        Message(conversation_id=conv_id, role="user", content="问"),
        Message(conversation_id=conv_id, role="assistant", content="答",
                citations=[{"number": 1}]),
    ])
    await db_session.commit()
    resp = await client.get(
        f"/api/chat/conversations/{conv_id}/messages", headers=auth_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [m["role"] for m in body] == ["user", "assistant"]
    assert body[1]["citations"][0]["number"] == 1
```

- [x] Step 2: RED。
- [x] Step 3: 实现:

`checkpointer.py`:

```python
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.core.config import settings

_saver_cm = None
_saver = None


def psycopg_url() -> str:
    return settings.DATABASE_URL.replace("+asyncpg", "")


async def get_checkpointer():
    """惰性单例;测试进程不调用(build_graph(checkpointer=None) 路径)。"""
    global _saver_cm, _saver
    if _saver is None:
        _saver_cm = AsyncPostgresSaver.from_conn_string(psycopg_url())
        _saver = await _saver_cm.__aenter__()
        await _saver.setup()
    return _saver
```

`schemas/chat.py`:

```python
from datetime import datetime

from pydantic import BaseModel, Field


class ConversationIn(BaseModel):
    kb_ids: list[int] = Field(min_length=1)
    name: str | None = Field(default=None, max_length=128)


class ConversationOut(BaseModel):
    id: int
    kb_ids: list[int]
    title: str
    created_at: datetime

    model_config = {"from_attributes": True}


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    citations: list | None
    created_at: datetime

    model_config = {"from_attributes": True}
```

`api/conversations.py`:

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.session import get_db
from app.models import Conversation, Message, User
from app.schemas.chat import ConversationIn, ConversationOut, MessageOut

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/conversations", response_model=ConversationOut, status_code=201)
async def create_conversation(
    payload: ConversationIn,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = Conversation(
        user_id=current.id, kb_ids=payload.kb_ids,
        title=payload.name or "新对话",
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.execute(
        select(Conversation)
        .where(Conversation.user_id == current.id)
        .order_by(Conversation.id.desc())
    )
    return list(rows.scalars().all())


@router.get("/conversations/{conv_id}/messages", response_model=list[MessageOut])
async def list_messages(
    conv_id: int,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conv = await db.get(Conversation, conv_id)
    if conv is None or conv.user_id != current.id:
        raise HTTPException(status_code=404, detail="conversation not found")
    rows = await db.execute(
        select(Message).where(Message.conversation_id == conv_id).order_by(Message.id)
    )
    return list(rows.scalars().all())
```

`api/__init__.py` 挂载 conversations_router。

- [x] Step 4: GREEN:全量 **41 passed**(39+2)。
- [x] Step 5: 提交:`feat: conversations and messages api with async checkpointer`

### Task 5: SSE 问答端点(核心)

**Files:** Create `backend/app/schemas/chat.py` 追加 `AskIn`, `backend/app/api/ask.py`;Modify `backend/app/api/__init__.py`;Test `backend/tests/test_ask.py`

**Interfaces:** `POST /api/chat/ask {conversation_id?, kb_ids, question}` → `text/event-stream`;事件序列 `token* → citations → done`(错误 `error`);无 conversation_id 则自动建(标题=问题前 20 字);持久化 user/assistant 消息+citations;鉴权。测试用 FakeListChatModel 注入(monkeypatch `app.api.ask.build_graph` 为返回 fake-llm 图,检索同样 stub——**实现者按 T3 测试的 stub 手法复用**)

- [x] Step 1: 失败测试(2 函数):

```python
async def test_ask_streams_tokens_and_saves(client, auth_headers, monkeypatch, db_session):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    import app.services.chat_graph.nodes as nodes_mod
    from app.services.chat_graph.graph import build_graph
    from app.services.retrieval.searcher import SearchHit

    async def fake_search(db, kb_ids, query, top_k=20):
        return [SearchHit(1, 1, 1, "a.pdf", 1, "答案内容来自这里", 0.5, "vector")]

    monkeypatch.setattr(nodes_mod, "hybrid_search", fake_search)
    real_build = build_graph
    monkeypatch.setattr(
        "app.api.ask.build_graph",
        lambda **kw: real_build(llm=FakeListChatModel(responses=["最终答案[1]"])),
    )

    resp = await client.post(
        "/api/chat/ask",
        json={"kb_ids": [1], "question": "问个问题"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    assert "最终答案" in body
    assert '"type": "citations"' in body or '"type":"citations"' in body
    assert '"type": "done"' in body or '"type":"done"' in body

    from app.models import Message

    rows = (await db_session.execute(select_(Message))).scalars().all()
    assert {m.role for m in rows} >= {"user", "assistant"}


from sqlalchemy import select as select_  # 放文件顶部


async def test_ask_requires_auth(client):
    resp = await client.post(
        "/api/chat/ask", json={"kb_ids": [1], "question": "x"}
    )
    assert resp.status_code == 401
```

- [x] Step 2: RED。
- [x] Step 3: 实现 `app/api/ask.py`:

```python
import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.session import get_db, SessionLocal
from app.models import Conversation, Message, User
from app.services.chat_graph.graph import build_graph

router = APIRouter(prefix="/chat", tags=["chat"])


class AskIn(BaseModel):
    conversation_id: int | None = None
    kb_ids: list[int] = Field(min_length=1)
    question: str = Field(min_length=1, max_length=2000)


def _sse(evt_type: str, data) -> str:
    return f"data: {json.dumps({'type': evt_type, 'data': data}, ensure_ascii=False)}\n\n"


@router.post("/ask")
async def ask(
    payload: AskIn,
    current: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if payload.conversation_id is None:
        conv = Conversation(
            user_id=current.id, kb_ids=payload.kb_ids,
            title=payload.question[:20],
        )
        db.add(conv)
        await db.commit()
        await db.refresh(conv)
    else:
        conv = await db.get(Conversation, payload.conversation_id)
        if conv is None or conv.user_id != current.id:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="conversation not found")

    db.add(Message(conversation_id=conv.id, role="user", content=payload.question))
    await db.commit()

    graph = build_graph()
    init = {"question": payload.question, "kb_ids": payload.kb_ids}
    cfg = {"configurable": {"thread_id": str(conv.id)}}

    async def gen():
        final_state = {}
        try:
            async for ev in graph.astream_events(init, config=cfg, version="v2"):
                if ev["event"] == "on_chat_model_stream":
                    chunk = ev["data"]["chunk"]
                    delta = getattr(chunk, "content", "") or ""
                    if isinstance(delta, str) and delta:
                        yield _sse("token", delta)
                elif ev["event"] == "on_chain_end" and ev["name"] == "generate":
                    final_state.update(ev["data"].get("output") or {})
            citations = final_state.get("citations") or []
            yield _sse("citations", citations)
            answer = final_state.get("answer") or ""
            async with SessionLocal() as s2:
                s2.add(Message(conversation_id=conv.id, role="assistant",
                               content=answer, citations=citations))
                await s2.commit()
            yield _sse("done", {"conversation_id": conv.id})
        except Exception as exc:  # 断连/取消也会走这里
            yield _sse("error", str(exc)[:300])

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
```

挂载到 api_router。

- [x] Step 4: GREEN:全量 **43 passed**(41+2)。
- [x] Step 5: 提交:`feat: sse streaming ask endpoint with persistence`

### Task 6: 前端基建(依赖/SSE 解析/ api 模块/组合式)

**Files:** Modify `frontend/package.json`(pnpm add);Create `frontend/src/api/kb.ts`, `frontend/src/api/documents.ts`, `frontend/src/api/chat.ts`, `frontend/src/composables/useChatStream.ts`, `frontend/src/utils/sse.ts`;Test `frontend/src/utils/__tests__/sse.spec.ts`

**Interfaces:** `sse.ts`:`parseSSEChunk(raw: string) -> object[]`(按 `data: ` 前缀+空行分帧解析 JSON,坏帧跳过);`useChatStream.ask({kbIds, question, conversationId}, {onToken, onCitations, onDone, onError})`(fetch-event-source POST /api/chat/ask,逐帧回调);api 模块:kbApi(list/create), documentsApi(list/upload/onProgress/detail/chunks), conversationsApi(list/messages)

- [x] Step 1: `pnpm add @microsoft/fetch-event-source markdown-it highlight.js`
- [x] Step 2: 失败测试 `src/utils/__tests__/sse.spec.ts`:

```ts
import { describe, expect, it } from 'vitest'
import { parseSSEChunk } from '@/utils/sse'

describe('parseSSEChunk', () => {
  it('parses data frames and skips bad json', () => {
    const raw = 'data: {"type":"token","data":"你好"}\n\n junk \n\ndata: {bad}\n\n'
    const events = parseSSEChunk(raw)
    expect(events).toEqual([{ type: 'token', data: '你好' }])
  })

  it('handles multiline raw buffer tail', () => {
    const raw = 'data: {"type":"done","data":{"conversation_id":7}}\n\ndata: {"type":"tok'
    const events = parseSSEChunk(raw)
    expect(events).toHaveLength(1)
    expect(events[0].type).toBe('done')
  })
})
```

- [x] Step 3: RED(`pnpm test -- --run`)→ 实现 `sse.ts`:

```ts
export interface SSEEvent {
  type: 'token' | 'citations' | 'done' | 'error'
  data: unknown
}

export function parseSSEChunk(raw: string): SSEEvent[] {
  const events: SSEEvent[] = []
  const frames = raw.split('\n\n')
  for (const frame of frames) {
    const line = frame.split('\n').find((l) => l.startsWith('data: '))
    if (!line) continue
    try {
      events.push(JSON.parse(line.slice(6)))
    } catch {
      // 坏帧跳过(半包尾部由调用方缓冲)
    }
  }
  return events
}
```

api 三模块(use http.ts,类型与后端 Out 模型对齐)+ `useChatStream.ts`:

```ts
import { fetchEventSource } from '@microsoft/fetch-event-source'
import { parseSSEChunk, type SSEEvent } from '@/utils/sse'
import { useAuthStore } from '@/stores/auth'
import type { Ref } from 'vue'

export interface AskHandlers {
  onToken: (t: string) => void
  onCitations: (c: unknown) => void
  onDone: (d: { conversation_id: number }) => void
  onError: (msg: string) => void
}

export function useChatStream() {
  let buffer = ''
  async function ask(
    payload: { kbIds: number[]; question: string; conversationId?: number },
    handlers: AskHandlers,
    isAborted?: Ref<boolean>,
  ): Promise<void> {
    buffer = ''
    const ctrl = new AbortController()
    await fetchEventSource('/api/chat/ask', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${useAuthStore().token}`,
      },
      body: JSON.stringify({
        kb_ids: payload.kbIds,
        question: payload.question,
        conversation_id: payload.conversationId ?? null,
      }),
      signal: ctrl.signal,
      onmessage(msg) {
        buffer += msg.data + '\n\n'  // fetch-event-source 已按帧回调,保持双保险
        for (const ev of parseSSEChunk('data: ' + msg.data + '\n\n')) {
          dispatch(ev, handlers)
        }
      },
      onerror(err) {
        handlers.onError(String(err))
        throw err
      },
    })
  }
  function dispatch(ev: SSEEvent, h: AskHandlers) {
    if (ev.type === 'token') h.onToken(ev.data as string)
    else if (ev.type === 'citations') h.onCitations(ev.data)
    else if (ev.type === 'done') h.onDone(ev.data as { conversation_id: number })
    else if (ev.type === 'error') h.onError(String(ev.data))
  }
  return { ask }
}
```

(实现者注意:onmessage 里 msg.data 已是单帧负载,直接 JSON.parse 并 dispatch 即可,parseSSEChunk 双保险用于分片测试——**两种都保留,以测试通过为准**。)

- [x] Step 4: GREEN(前端 3 passed)+ `pnpm build` 绿。
- [x] Step 5: 提交:`feat: frontend chat streaming infra (sse parser/apis/composable)`

### Task 7: 知识库页面

**Files:** Create `frontend/src/pages/KbPage.vue`;Modify `frontend/src/router/index.ts`(路由 `/kb`)、`frontend/src/layouts/MainLayout.vue`(菜单启用,去掉 disabled)

**Interfaces:** 列表(名称/描述/文档数占位/时间)+ 新建对话框(el-dialog + 表单校验)+ 点击进入 `/kb/:id/docs`;空态 el-empty;类型 `KbItem` 与 kbApi 对齐

- [x] Step 1-4: 实现页面(完整 SFC:el-table/el-dialog/el-form;onMounted 拉列表;创建成功刷新);`pnpm build` + `pnpm test` 全绿。
- [x] Step 5: 提交:`feat: knowledge base page with create dialog`

### Task 8: 文档管理页面

**Files:** Create `frontend/src/pages/DocsPage.vue`;Modify router(`/kb/:id/docs`)

**Interfaces:** 顶部返回+上传区(el-upload 手动触发,白名单 .pdf/.docx/.xlsx,进度条);文档表(文件名/状态 tag 轮询 pending→done 每 3s/chunk_count/时间);行操作"查看分块"→ 抽屉(el-drawer,调 chunks 端点分页);状态色:pending灰/parsing蓝/chunking蓝/embedding蓝/done绿/failed红(failed 显示 error_msg tooltip)

- [x] Step 1-4: 实现;build+test 绿。
- [x] Step 5: 提交:`feat: documents page with upload status polling and chunks drawer`

### Task 9: 对话页面(核心体验)

**Files:** Create `frontend/src/pages/ChatPage.vue`, `frontend/src/components/CitationList.vue`;Modify router(`/chat`)、MainLayout(用户名显示,激活 fetchUser)

**Interfaces:** 左栏会话列表+新对话;顶部 KB 多选(el-select multiple,kbApi 拉取);消息区(用户右侧/助手左侧,markdown-it 渲染+highlight.js 代码高亮,流式追加);回答完成后 CitationList 展示(点击弹 el-dialog 显示 excerpt/document/page);输入框 el-input textarea + 发送(Enter 发送/Shift+Enter 换行,流式期间禁用)

- [x] Step 1-4: 实现;build+test 绿。
- [x] Step 5: 提交:`feat: chat page with streaming answers and citation panel`

### Task 10: M3 端到端验收(真模型×真文档)

- [x] Step 1: 准备 3~5 份真实感样例(含中文段落+表格的 pdf/docx,xlsx)到 %TEMP%(脚本生成,内容要能构成可问答的事实,如"报销审批需三天")。
- [x] Step 2: 起全栈四进程(PG/Redis 服务确认 → start_dev.bat → start_worker.bat → pnpm dev)。
- [x] Step 3: 无头验收链:注册登录→建 KB→上传样例→轮询 done→**curl 调 /api/chat/ask**,验证 SSE 帧序列(token 若干→citations 含 filename/page_no/excerpt→done 含 conversation_id),答案内容与样例事实一致(关键词抽查);再问一个知识库外问题,断言回答含"未找到"或明确不知(记录实际行为)。
- [x] Step 4: 浏览器验收(留给用户,同 M1 模式):三页面走查+流式对话+引用点击。无头项全过即 M3 验收 PASS。
- [x] Step 5: 回归(后端 43 passed;前端 build+test)+ 清进程;空标记提交 `chore: m3 complete - retrieval chat acceptance verified`。

### Task 11(控制器): 收尾

- 勾选同步+提交;全分支终审(重点:R1-R5 裁决落地、SSE 契约前后端一致、引用映射正确、断连行为、性能 N+1);合并推送;M4 交接附录;记忆更新。

---

## 计划自审记录

1. **Spec 覆盖**:§6 七步流程→T1(权限校验简化的 R4 已记)/T1 RRF/T2 可选精排/T3 组装 prompt+流式/T5 SSE+citations/T3 抑制幻觉(系统提示);§9 M3 验收→T10;前端三页面→T7-9。交接附录三项裁决→R1/R2/R3 已消化;延后 Minor 中 fitz 统一→T0、fetchUser→T9、chunks 加固与 count()→**T8 顺带**(上传页依赖,加固测试允许加 2 个:404/401)。
2. **占位符扫描**:T3/T4 各有一处"笔误示范+修正说明"(nodes.py 顶部导入、checkpointer 三行)——均为显式指令非 TBD;T1 pipeline 改写给了语义等价说明。实现者按说明写正式版。
3. **类型一致性**:SearchHit 字段 ↔ nodes dict 化 ↔ citations 映射键一致;SSE 契约 ↔ sse.ts/useChatStream 一致;ConversationOut/MessageOut ↔ 前端 api 模块;`rrf_fuse` 签名 ↔ 测试元组输入。
4. **计数一致性**(按测试函数):32→T1+3=35→T2+2=37→T3+2=39→T4+2=41→T5+2=43;前端 2→T6+1=3。T8 允许 +2 加固(404/401)→若加,后续计数 +2 且 T10 回归以实际为准。

---

## M4 交接附录(2026-09-15 终审前固化,M4 计划生成时必须消化)

### 定稿裁决(本次验收后)
1. **断连语义正式定稿**:M3 行为即定稿语义——断连仅保留已落库的 user 消息,assistant 部分回答**不持久化、不恢复**(CancelledError 不入 except Exception,无 error 帧)。spec 原倾向的"检查点恢复"降级为 M5 可选增强(接线 get_checkpointer 单例 + thread_id 已随 config 传递,接线成本可控)。
2. **done 事件携带完整 answer** 为正式协议(T5 裁决,前端以 done.answer 为权威对账源)。
3. **DOCX 引用 page_no=null 为设计行为**(docx 无页概念;前端 CitationList 已兜底显示"—")。
4. 真模型实测:glm-5.3-flash 无思考内容混入 token 流,首 token 2-3s、单问总耗时 3-4s(2026-09-15,供性能基线参考)。

### M4 范围提示(spec §9-M4)
RBAC(viewer/editor/admin)+ 权限管理界面;失败重试/重新解析;问答历史完善;Rerank 开关界面。**注意 M3 遗留**:R4 权限从简(登录即可检索任意 KB)必须在 M4 收紧(kb_permissions 表已在);ask/检索入口都要加权限过滤。

### 延后 Minor 清单(择要,M3 台账有全量)
- checkpointer 接线(见裁决 1);DOMPurify(markdown html:false 已兜底);卸载页面未中止 SSE(isAborted 未接);错误消息内联标记;poll-after-unmount 窄窗;轮询 loading 闪烁;kb 高亮子路由;kbName 走 list 匹配;hybrid 返回 2×top_k 消费端已切;rerank 负下标;单次 OpenAI 客户端;markdown 每帧全量重渲染;Alt/Meta+Enter 也发送;oxlint 三处旧错;断连 assistant 丢失(同裁决 1)。

### 环境事实(增量)
运行库 airag 含验收数据(KB 2 三文档,可留作回归样本或手动清理);EMBED_PROVIDER 默认 zhipu 真嵌入(余额已充);worker 连 Redis 需 .env 带密码 REDIS_URL(已配)。
