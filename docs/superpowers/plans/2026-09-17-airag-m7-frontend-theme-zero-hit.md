# AIRag M7 实施计划(前端全站体验升级 / 零命中双门控)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 前端全站视觉/交互升级(设计令牌+亮暗双主题+7 页改造),后端零命中双门控(提示词收紧+rerank 相关度阈值)与 refused 全链路。

**Architecture:** 前端零新依赖,新增 `src/styles/` 令牌层与 `useTheme` composable,EP 官方 dark css-vars 驱动暗色;后端 rerank 提供方透传 relevance_score,`rerank_node` 阈值过滤,`generate_node` 产 `refused`,沿 SSE done 帧与 messages 表直达前端。

**Tech Stack:** Vue 3.5 + Element Plus 2.14(主色保持默认蓝)/ FastAPI + LangGraph + SQLAlchemy + alembic / pytest + vitest。

**Spec:** `docs/superpowers/specs/2026-09-17-airag-m7-frontend-theme-zero-hit-design.md`

## Global Constraints

- 后端命令在 `E:\Projects\AIRag\backend` 下执行,python 一律 `.venv\Scripts\python.exe`;前端命令在 `E:\Projects\AIRag\frontend` 下;git 在仓库根。
- 后端全量测试:`.venv\Scripts\python.exe -m pytest tests -q`;**红线:现有 127 passed 只增不减**。
- 前端:`npm run build`(含 vue-tsc)/ `npx vitest run`(现有 6 passed 不回归)/ `npm run lint`(oxlint+eslint 0/0)。
- 后端开发启动必须 `start_dev.bat`(--reload);worker 与 beat 分开两个 bat(Windows celery 拒 -B)。
- 固定拒答话术(全链路锚点,逐字一致):`知识库中未找到相关内容`。
- score 双语义:rerank 开启=智谱相关度分(0~1);关闭=RRF 融合分。阈值只在 rerank 开启路径生效。
- `RETRIEVAL_MIN_SCORE` 默认 `0.30`,`0` = 禁用过滤。
- 前端不引入任何新依赖;业务逻辑与 API 调用零改动(仅表现层+计划明列的交互补强)。
- 中文文案与现有风格一致(简体、半角逗号);conventional commits。
- 涉及真智谱调用的验收步骤:ZHIPU_API_KEY 为空时跳过并标注 SKIP(M5/M6 同款容错)。

---

### Task 1: 后端 — rerank 相关度透传 + 阈值门控

**Files:**
- Modify: `backend/app/services/rerank/base.py`
- Modify: `backend/app/services/rerank/zhipu.py`
- Modify: `backend/app/services/chat_graph/nodes.py`(仅 rerank_node)
- Modify: `backend/app/core/config.py`(RETRIEVAL_TOP_K 行后)
- Test: `backend/tests/test_rerank.py`、`backend/tests/test_chat_graph.py`

**Interfaces:**
- Produces: `RerankProvider.rerank(query: str, documents: list[str], top_n: int = 8) -> list[tuple[int, float]]`(index, 相关度分,乱序允许);`settings.RETRIEVAL_MIN_SCORE: float = 0.30`;`rerank_node` 过滤后 hits 的 `hit["score"]` = 相关度分(保留 6 位小数)。Task 2/3/12 依赖。

- [ ] **Step 1: 写失败测试(test_rerank.py)**

改 `test_zhipu_rerank_parses_response` 为带分断言(重命名),并新增缺分兜底,替换为以下两个测试:

```python
def test_zhipu_rerank_returns_scores(monkeypatch):
    from app.services.rerank import zhipu as zr

    class FakeResp:
        def json(self):
            return {"results": [{"index": 2, "relevance_score": 0.9},
                                 {"index": 0, "relevance_score": 0.5}]}

        def raise_for_status(self):
            return None

    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(json=json)
        return FakeResp()

    monkeypatch.setattr(zr.httpx, "post", fake_post)
    scored = zr.ZhipuRerank().rerank("q", ["a", "b", "c"], top_n=2)
    assert scored == [(2, 0.9), (0, 0.5)]
    assert captured["json"]["model"] == zr.settings.RERANK_MODEL


def test_zhipu_rerank_defaults_missing_score(monkeypatch):
    from app.services.rerank import zhipu as zr

    class FakeResp:
        def json(self):
            return {"results": [{"index": 1}]}

        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        zr.httpx, "post",
        lambda url, json=None, headers=None, timeout=None: FakeResp(),
    )
    assert zr.ZhipuRerank().rerank("q", ["a", "b"], top_n=2) == [(1, 0.0)]
```

(删除原 `test_zhipu_rerank_parses_response`,由上面两个测试替代。)

- [ ] **Step 2: 写失败测试(test_chat_graph.py,文件末尾追加)**

```python
def _hit(cid: int, content: str) -> dict:
    return {"chunk_id": cid, "document_id": 1, "kb_id": 1,
            "filename": "a.pdf", "page_no": 1, "content": content,
            "score": 0.01, "source": "vector"}


async def test_rerank_node_sorts_and_filters_by_threshold(monkeypatch):
    from app.core.config import settings
    from app.services.chat_graph import nodes as nodes_mod

    class FakeReranker:
        def rerank(self, query, documents, top_n=8):
            return [(0, 0.4), (1, 0.9), (2, 0.1)]  # 乱序,含低于阈值

    monkeypatch.setattr(nodes_mod, "get_reranker", lambda: FakeReranker())
    monkeypatch.setattr(settings, "RETRIEVAL_MIN_SCORE", 0.3)
    out = await nodes_mod.rerank_node(
        {"question": "q", "hits": [_hit(1, "甲"), _hit(2, "乙"), _hit(3, "丙")],
         "rerank": True})
    assert [h["chunk_id"] for h in out["hits"]] == [2, 1]  # 按分降序,丙被滤
    assert out["hits"][0]["score"] == 0.9  # relevance 回写 score
    assert out["hits"][1]["score"] == 0.4


async def test_rerank_node_threshold_zero_disables_filter(monkeypatch):
    from app.core.config import settings
    from app.services.chat_graph import nodes as nodes_mod

    class FakeReranker:
        def rerank(self, query, documents, top_n=8):
            return [(0, 0.05), (1, 0.02)]

    monkeypatch.setattr(nodes_mod, "get_reranker", lambda: FakeReranker())
    monkeypatch.setattr(settings, "RETRIEVAL_MIN_SCORE", 0.0)
    out = await nodes_mod.rerank_node(
        {"question": "q", "hits": [_hit(1, "甲"), _hit(2, "乙")], "rerank": True})
    assert [h["chunk_id"] for h in out["hits"]] == [1, 2]  # 全保留,仅按分排序


async def test_rerank_node_filters_all_to_empty(monkeypatch):
    from app.core.config import settings
    from app.services.chat_graph import nodes as nodes_mod

    class FakeReranker:
        def rerank(self, query, documents, top_n=8):
            return [(0, 0.1), (1, 0.2)]

    monkeypatch.setattr(nodes_mod, "get_reranker", lambda: FakeReranker())
    monkeypatch.setattr(settings, "RETRIEVAL_MIN_SCORE", 0.3)
    out = await nodes_mod.rerank_node(
        {"question": "q", "hits": [_hit(1, "甲"), _hit(2, "乙")], "rerank": True})
    assert out == {"hits": []}
```

同时改既有 `test_rerank_node_respects_state_flag` 的 FakeReranker(返回类型变了,不改必红):

```python
    class FakeReranker:
        def rerank(self, query, documents, top_n=8):
            return [(i, 0.9) for i in range(len(documents))][::-1]  # 全量倒序
```

(on 分支断言 `ids == [2, 1]` 不变。)

- [ ] **Step 3: 跑测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_rerank.py tests/test_chat_graph.py -v`
Expected: 新增 4 个 FAIL(类型不匹配/断言失败),`test_rerank_node_respects_state_flag` FAIL。

- [ ] **Step 4: 实现**

`base.py` 抽象方法签名与 docstring:

```python
class RerankProvider(ABC):
    @abstractmethod
    def rerank(
        self, query: str, documents: list[str], top_n: int = 8
    ) -> list[tuple[int, float]]:
        """返回 (保留文档下标, 相关度分 0~1) 列表;顺序不保证,调用方自行排序。"""
```

`zhipu.py` rerank 返回段替换为:

```python
        results = resp.json()["results"]
        return [
            (int(r["index"]), float(r.get("relevance_score", 0.0)))
            for r in results
        ][:top_n]
```

`config.py` 在 `RETRIEVAL_TOP_K: int = 8` 之后加:

```python
    # M7:rerank 相关度阈值(仅 rerank 开启路径生效;0 = 禁用过滤)
    RETRIEVAL_MIN_SCORE: float = 0.30
```

`nodes.py` 的 `rerank_node` 整体替换为:

```python
async def rerank_node(state: dict) -> dict:
    reranker = get_reranker()
    if reranker is None or not state.get("hits") or not state.get("rerank"):
        return {}
    import asyncio

    hits = state["hits"]
    scored = await asyncio.to_thread(
        reranker.rerank, state["question"],
        [h["content"] for h in hits], settings.RETRIEVAL_TOP_K,
    )
    # M7:排序取 top_k 后按相关度阈值过滤;relevance 回写 score
    # (rerank 开启时 score 语义=相关度分;关闭时保持 RRF 融合分)
    ranked = sorted(scored, key=lambda p: p[1], reverse=True)[: settings.RETRIEVAL_TOP_K]
    out = []
    for idx, rel in ranked:
        if not 0 <= idx < len(hits):
            continue
        if settings.RETRIEVAL_MIN_SCORE > 0 and rel < settings.RETRIEVAL_MIN_SCORE:
            continue
        h = dict(hits[idx])
        h["score"] = round(float(rel), 6)
        out.append(h)
    return {"hits": out}
```

- [ ] **Step 5: 跑测试确认通过 + 全量回归**

Run: `.venv\Scripts\python.exe -m pytest tests/test_rerank.py tests/test_chat_graph.py -v` → PASS
Run: `.venv\Scripts\python.exe -m pytest tests -q` → ≥127 passed(127 基线 + 本任务净增约 3)。

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/rerank backend/app/services/chat_graph/nodes.py backend/app/core/config.py backend/tests/test_rerank.py backend/tests/test_chat_graph.py
git commit -m "feat: rerank relevance scores with min-score threshold gating"
```

---

### Task 2: 后端 — 提示词收紧 + refused 判定

**Files:**
- Modify: `backend/app/services/chat_graph/nodes.py`(SYSTEM_PROMPT + generate_node)
- Modify: `backend/app/services/chat_graph/state.py`
- Test: `backend/tests/test_chat_graph.py`

**Interfaces:**
- Produces: `generate_node` 返回 dict 增 `refused: bool`;`ChatState` 增 `refused: bool` 键;模块常量 `REFUSAL_PHRASE = "知识库中未找到相关内容"`。Task 3(ask.py 读 final_state["refused"])与 Task 12 依赖。

- [ ] **Step 1: 写失败测试(test_chat_graph.py 追加)**

```python
async def test_generate_marks_refusal():
    from app.services.chat_graph.nodes import generate_node

    out = await generate_node(
        {"question": "q", "hits": []},
        llm=_LLMScript(["知识库中未找到相关内容"]),
    )
    assert out["refused"] is True
    assert out["citations"] == []


async def test_generate_refusal_startswith_semantics():
    from app.services.chat_graph.nodes import generate_node

    out = await generate_node(
        {"question": "q", "hits": [_hit(1, "不相关资料")]},
        llm=_LLMScript(["知识库中未找到相关内容。"]),
    )
    assert out["refused"] is True  # 以话术开头即拒答(含尾标点变体)


async def test_generate_normal_answer_not_refused():
    from app.services.chat_graph.nodes import generate_node

    out = await generate_node(
        {"question": "q", "hits": [_hit(1, "答案内容")]},
        llm=_LLMScript(["答案内容[1]"]),
    )
    assert out["refused"] is False
    assert out["citations"][0]["number"] == 1
```

注:`_hit` 与 `_LLMScript` 均已存在于本文件(Task 1/既有),复用。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_chat_graph.py -v -k generate`
Expected: 3 FAIL(KeyError 'refused' / 断言失败)。

- [ ] **Step 3: 实现**

`nodes.py` SYSTEM_PROMPT 替换为:

```python
REFUSAL_PHRASE = "知识库中未找到相关内容"

SYSTEM_PROMPT = (
    "你是企业知识库助手。只依据下面提供的参考资料回答;"
    "引用资料时标注编号如 [1][2];若资料与问题不相关或不足以回答,"
    f'只回复"{REFUSAL_PHRASE}",不得罗列、摘要或拼凑返回的资料;'
    "用中文,简洁分点。"
)
```

`generate_node` 的 return 语句替换为:

```python
    answer = resp.content
    return {
        "answer": answer,
        "citations": build_citations(shits),
        "refused": (answer or "").strip().startswith(REFUSAL_PHRASE),
    }
```

`state.py` 在 `hopped: bool` 行后加:

```python
    # M7 零命中:generate 判定的拒答标记(随 done 帧与消息落库)
    refused: bool
```

- [ ] **Step 4: 跑测试确认通过 + 全量**

Run: `.venv\Scripts\python.exe -m pytest tests/test_chat_graph.py -v` → PASS
Run: `.venv\Scripts\python.exe -m pytest tests -q` → 全绿(既有端到端用例的正常答案不触话术,不受影响)。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chat_graph/nodes.py backend/app/services/chat_graph/state.py backend/tests/test_chat_graph.py
git commit -m "feat: tighten refusal prompt and emit refused flag from generate"
```

---

### Task 3: 后端 — refused 持久化全链路(迁移/模型/schema/ask 帧)

**Files:**
- Create: `backend/alembic/versions/a7b8c9d0e1f2_m7_messages_refused.py`
- Modify: `backend/app/models/chat.py`(Message)
- Modify: `backend/app/schemas/chat.py`(MessageOut)
- Modify: `backend/app/api/ask.py`(落库 + done 帧)
- Test: `backend/tests/test_ask.py`、`backend/tests/test_conversations.py`

**Interfaces:**
- Consumes: Task 2 的 `final_state["refused"]`。
- Produces: `messages.refused boolean not null default false`;`MessageOut.refused: bool`;SSE done 帧 `{"conversation_id", "answer", "refused"}`。Task 9(前端 DonePayload/MessageItem)与 Task 12 依赖。

- [ ] **Step 1: 写失败测试(test_ask.py 追加)**

```python
async def test_ask_done_frame_and_persistence_carry_refused(
    client, auth_headers, monkeypatch, db_session
):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    import app.services.chat_graph.nodes as nodes_mod
    from app.services.chat_graph.graph import build_graph

    async def fake_search(db, kb_ids, query, top_k=20):
        return []  # 零命中(conftest 已关 MULTI_HOP → 直通 generate)

    monkeypatch.setattr(nodes_mod, "hybrid_search", fake_search)
    real_build = build_graph
    monkeypatch.setattr(
        "app.api.ask.build_graph",
        lambda **kw: real_build(
            llm=FakeListChatModel(responses=["知识库中未找到相关内容"])
        ),
    )

    kb = await client.post("/api/kbs", json={"name": "拒答链路库"}, headers=auth_headers)
    resp = await client.post(
        "/api/chat/ask",
        json={"kb_ids": [kb.json()["id"]], "question": "无关问题"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.text
    assert '"type": "done"' in body or '"type":"done"' in body
    assert '"refused": true' in body  # done 帧带拒答标记

    from app.models import Message

    rows = (await db_session.execute(select_(Message))).scalars().all()
    assistant = [m for m in rows if m.role == "assistant"][-1]
    assert assistant.refused is True
    assert assistant.citations in (None, [])
```

test_conversations.py 追加:

```python
async def test_messages_include_refused_flag(client, auth_headers, db_session):
    from app.models import Message

    kb = await client.post("/api/kbs", json={"name": "refused库"}, headers=auth_headers)
    conv = await client.post(
        "/api/chat/conversations",
        json={"kb_ids": [kb.json()["id"]], "name": "c"},
        headers=auth_headers,
    )
    conv_id = conv.json()["id"]
    db_session.add(Message(conversation_id=conv_id, role="user", content="q"))
    db_session.add(Message(conversation_id=conv_id, role="assistant",
                           content="知识库中未找到相关内容", refused=True))
    db_session.add(Message(conversation_id=conv_id, role="assistant",
                           content="正常回答"))
    await db_session.commit()

    resp = await client.get(
        f"/api/chat/conversations/{conv_id}/messages", headers=auth_headers
    )
    items = resp.json()
    refused_flags = [m["refused"] for m in items if m["role"] == "assistant"]
    assert refused_flags == [True, False]  # 旧数据/未传 → 默认 False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_ask.py tests/test_conversations.py -v -k refused`
Expected: 2 FAIL(Message() 无 refused 参数 / done 帧无字段)。

- [ ] **Step 3: 实现**

`app/models/chat.py`:import 行加 `Boolean`,Message 类 citations 行后加:

```python
    refused: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
```

新迁移 `alembic/versions/a7b8c9d0e1f2_m7_messages_refused.py`(对齐 M5 文件头格式):

```python
"""m7 messages refused flag

Revision ID: a7b8c9d0e1f2
Revises: a1b2c3d4e5f6
Create Date: 2026-09-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'messages',
        sa.Column('refused', sa.Boolean(), nullable=False, server_default='false'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('messages', 'refused')
```

`app/schemas/chat.py` MessageOut 加字段(citations 行后):

```python
    refused: bool = False
```

`app/api/ask.py` gen() 内,`answer = final_state.get("answer") or ""` 行后加 `refused` 读取,落库与 done 帧带上:

```python
            answer = final_state.get("answer") or ""
            refused = bool(final_state.get("refused"))
            async with SessionLocal() as s2:
                s2.add(Message(conversation_id=conv.id, role="assistant",
                               content=answer, citations=citations, refused=refused))
```

done 帧:

```python
            yield _sse("done", {"conversation_id": conv.id, "answer": answer,
                                "refused": refused})
```

- [ ] **Step 4: 跑测试确认通过 + 迁移验证 + 全量**

Run: `.venv\Scripts\python.exe -m pytest tests/test_ask.py tests/test_conversations.py -v` → PASS
Run: `.venv\Scripts\python.exe -m alembic upgrade head` → 开发库加列成功
Run: `.venv\Scripts\python.exe -m alembic downgrade -1 && .venv\Scripts\python.exe -m alembic upgrade head` → up/down 可逆
Run: `.venv\Scripts\python.exe -m pytest tests -q` → 全绿。

- [ ] **Step 5: Commit**

```bash
git add backend/alembic/versions/a7b8c9d0e1f2_m7_messages_refused.py backend/app/models/chat.py backend/app/schemas/chat.py backend/app/api/ask.py backend/tests/test_ask.py backend/tests/test_conversations.py
git commit -m "feat: persist and stream refused flag end to end"
```

---

### Task 4: 前端 — 设计令牌层 + 亮暗双主题

**Files:**
- Create: `frontend/src/styles/tokens.css`
- Create: `frontend/src/composables/useTheme.ts`
- Create: `frontend/src/composables/__tests__/useTheme.spec.ts`
- Modify: `frontend/src/main.ts`、`frontend/src/App.vue`

**Interfaces:**
- Produces: `useTheme()` → `{ isDark: Ref<boolean>, init(): void, toggle(): void }`;CSS 变量 `--app-bg/--app-bg-soft/--app-card-bg/--app-card-border/--app-radius/--app-radius-sm/--app-shadow-card/--app-spacing-{xs,sm,md,lg,xl}`;localStorage 键 `airag_theme`('light'|'dark',默认 light);`<html class="dark">`。Task 5-10 全部依赖。

- [ ] **Step 1: 写失败测试**

`frontend/src/composables/__tests__/useTheme.spec.ts`:

```ts
import { beforeEach, describe, expect, it } from 'vitest'
import { useTheme } from '@/composables/useTheme'

describe('useTheme', () => {
  beforeEach(() => {
    localStorage.clear()
    document.documentElement.classList.remove('dark')
  })

  it('init defaults to light theme', () => {
    const { init, isDark } = useTheme()
    init()
    expect(isDark.value).toBe(false)
    expect(document.documentElement.classList.contains('dark')).toBe(false)
  })

  it('init restores dark from storage', () => {
    localStorage.setItem('airag_theme', 'dark')
    const { init, isDark } = useTheme()
    init()
    expect(isDark.value).toBe(true)
    expect(document.documentElement.classList.contains('dark')).toBe(true)
  })

  it('toggle flips and persists', () => {
    const { init, toggle, isDark } = useTheme()
    init()
    toggle()
    expect(isDark.value).toBe(true)
    expect(localStorage.getItem('airag_theme')).toBe('dark')
    expect(document.documentElement.classList.contains('dark')).toBe(true)
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npx vitest run src/composables/__tests__/useTheme.spec.ts`
Expected: FAIL(模块不存在)。

- [ ] **Step 3: 实现**

`useTheme.ts`:

```ts
import { ref } from 'vue'

const KEY = 'airag_theme'
/** 模块级单例:多处 useTheme() 共享同一份 isDark */
const isDark = ref(false)

function apply(dark: boolean) {
  isDark.value = dark
  document.documentElement.classList.toggle('dark', dark)
}

export function useTheme() {
  /** App 挂载时调用:按 localStorage 恢复主题(默认亮色) */
  function init() {
    apply(localStorage.getItem(KEY) === 'dark')
  }

  function toggle() {
    const next = !isDark.value
    localStorage.setItem(KEY, next ? 'dark' : 'light')
    apply(next)
  }

  return { isDark, init, toggle }
}
```

`tokens.css`:

```css
/* M7 设计令牌:主色/文字/状态色直接沿用 EP 变量(--el-*),此处只补 EP 未覆盖的面 */
:root {
  --app-bg: #f5f7fa;
  --app-bg-soft: #eef1f6;
  --app-card-bg: #ffffff;
  --app-card-border: var(--el-border-color-lighter);
  --app-radius: 8px;
  --app-radius-sm: 6px;
  --app-shadow-card: 0 1px 4px rgba(0, 21, 41, 0.08);
  --app-spacing-xs: 4px;
  --app-spacing-sm: 8px;
  --app-spacing-md: 12px;
  --app-spacing-lg: 16px;
  --app-spacing-xl: 24px;
}

html.dark {
  --app-bg: #0a0c10;
  --app-bg-soft: #141721;
  --app-card-bg: #141721;
  --app-card-border: var(--el-border-color-dark);
  --app-shadow-card: 0 1px 4px rgba(0, 0, 0, 0.4);
}

body {
  margin: 0;
  background: var(--app-bg);
  transition: background-color 0.2s;
}

/* 细滚动条 */
* {
  scrollbar-width: thin;
}
*::-webkit-scrollbar {
  width: 6px;
  height: 6px;
}
*::-webkit-scrollbar-thumb {
  background: var(--el-border-color);
  border-radius: 3px;
}
```

`main.ts` import 区(element-plus css 行后)加两行,并在 mount 前初始化主题:

```ts
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import '@/styles/tokens.css'

import App from './App.vue'
import router from './router'
import { useTheme } from '@/composables/useTheme'

const app = createApp(App)

app.use(createPinia())
app.use(router)
app.use(ElementPlus)

useTheme().init() // 挂载前恢复主题,避免暗色用户看到亮色闪帧

app.mount('#app')
```

`App.vue` 整体替换为:

```vue
<script setup lang="ts">
// 主题恢复在 main.ts 挂载前完成;此处仅承载 router-view
</script>

<template>
  <router-view />
</template>
```

- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run src/composables/__tests__/useTheme.spec.ts` → 3 PASS
Run: `npm run build` → 绿(vue-tsc 通过)。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/styles frontend/src/composables frontend/src/main.ts frontend/src/App.vue
git commit -m "feat: design tokens and light/dark theme foundation"
```

---

### Task 5: 前端 — 布局壳重做 + PageHeader + 路由标题

**Files:**
- Modify: `frontend/src/layouts/MainLayout.vue`(整体重写)
- Create: `frontend/src/components/PageHeader.vue`
- Modify: `frontend/src/router/index.ts`(meta.title + afterEach)

**Interfaces:**
- Produces: `PageHeader` 组件 props `{ title: string; description?: string }` + slot `actions`;路由 meta `title`(afterEach 同步 `document.title = "<title> · AIRag"`)。Task 6-10 使用。视觉任务,无新增单测(Task 12 双主题走查覆盖);验证=build+lint。

- [ ] **Step 1: 实现 router 标题**

`router/index.ts` 每个 route 对象加 `meta: { title: '...' }`:home=首页、kb=知识库、kb-docs=知识库文档、chat=对话、admin-users=用户管理、admin-audit-logs=审计日志;文件末尾 `router.beforeEach` 后追加:

```ts
router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} · AIRag` : 'AIRag'
})
```

- [ ] **Step 2: 实现 PageHeader 组件**

```vue
<script setup lang="ts">
defineProps<{ title: string; description?: string }>()
</script>

<template>
  <div class="page-header">
    <div class="page-header-text">
      <h2>{{ title }}</h2>
      <p v-if="description" class="page-header-desc">{{ description }}</p>
    </div>
    <div class="page-header-actions">
      <slot name="actions" />
    </div>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--app-spacing-lg);
  margin-bottom: var(--app-spacing-lg);
}
.page-header-text h2 {
  margin: 0;
  font-size: 20px;
}
.page-header-desc {
  margin: 4px 0 0;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.page-header-actions {
  display: flex;
  align-items: center;
  gap: var(--app-spacing-sm);
}
</style>
```

- [ ] **Step 3: 重写 MainLayout**

逻辑红线:菜单 index/路由、admin 可见性 `v-if`、fetchUser 兜底、登出行为全部不变。

```vue
<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ChatDotRound,
  Collection,
  Document,
  HomeFilled,
  Moon,
  Sunny,
  User,
} from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { useTheme } from '@/composables/useTheme'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const { isDark, toggle } = useTheme()

// kb 子路由(/kb/3/docs)时菜单仍高亮"知识库"
const activeIndex = computed(() => (route.path.startsWith('/kb') ? '/kb' : route.path))
const pageTitle = computed(() => (route.meta.title as string) ?? '')
const roleLabel = computed(() =>
  auth.user?.role === 'admin' ? '管理员' : auth.user?.role === 'editor' ? '编辑' : '只读',
)

onMounted(() => {
  // 头部显示当前用户名:M1 的 fetchUser 死代码至此激活。
  // 401 由 http 响应拦截器统一清 token 跳登录;其余失败不阻塞布局。
  if (auth.isLoggedIn && !auth.user) {
    auth.fetchUser().catch(() => {})
  }
})

function onLogout() {
  auth.logout()
  router.push({ name: 'login' })
}
</script>

<template>
  <el-container class="app-shell">
    <el-aside width="220px" class="app-aside">
      <div class="brand">
        <span class="brand-mark">AI</span>
        <span class="brand-name">AIRag 知识库</span>
      </div>
      <el-menu :default-active="activeIndex" router class="app-menu">
        <el-menu-item index="/">
          <el-icon><HomeFilled /></el-icon><span>首页</span>
        </el-menu-item>
        <el-menu-item index="/kb">
          <el-icon><Collection /></el-icon><span>知识库</span>
        </el-menu-item>
        <el-menu-item index="/chat">
          <el-icon><ChatDotRound /></el-icon><span>对话</span>
        </el-menu-item>
        <el-menu-item-group v-if="auth.user?.role === 'admin'" title="管理">
          <el-menu-item index="/admin/users">
            <el-icon><User /></el-icon><span>用户管理</span>
          </el-menu-item>
          <el-menu-item index="/admin/audit-logs">
            <el-icon><Document /></el-icon><span>审计日志</span>
          </el-menu-item>
        </el-menu-item-group>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="app-header">
        <span class="page-title">{{ pageTitle }}</span>
        <div class="header-actions">
          <el-button :icon="isDark ? Sunny : Moon" circle @click="toggle" />
          <el-dropdown v-if="auth.user">
            <span class="user-chip">
              <span class="avatar">{{ auth.user.username.slice(0, 1).toUpperCase() }}</span>
              <span class="username">{{ auth.user.username }}</span>
              <el-tag size="small" type="info">{{ roleLabel }}</el-tag>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="onLogout">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>
      <el-main class="app-main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.app-shell {
  height: 100vh;
}
.app-aside {
  border-right: 1px solid var(--app-card-border);
  background: var(--app-card-bg);
}
.brand {
  display: flex;
  align-items: center;
  gap: var(--app-spacing-sm);
  padding: var(--app-spacing-lg);
}
.brand-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: var(--app-radius-sm);
  background: var(--el-color-primary);
  color: #fff;
  font-size: 14px;
  font-weight: 700;
}
.brand-name {
  font-weight: 600;
  font-size: 15px;
}
.app-menu {
  border-right: none;
}
.app-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid var(--app-card-border);
  background: var(--app-card-bg);
}
.page-title {
  font-size: 15px;
  font-weight: 600;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: var(--app-spacing-md);
}
.user-chip {
  display: inline-flex;
  align-items: center;
  gap: var(--app-spacing-sm);
  cursor: pointer;
  outline: none;
}
.avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: var(--el-color-primary-light-8);
  color: var(--el-color-primary);
  font-size: 13px;
  font-weight: 600;
}
.username {
  font-size: 14px;
  color: var(--el-text-color-regular);
}
.app-main {
  background: var(--app-bg);
}
</style>
```

- [ ] **Step 4: 构建与静态检查**

Run: `npm run build` → 绿;Run: `npm run lint` → 0/0。
手动(可选,dev 起前端):侧栏品牌/图标/管理分组、头部标题/主题切换/用户下拉、`document.title` 跟随。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/layouts/MainLayout.vue frontend/src/components/PageHeader.vue frontend/src/router/index.ts
git commit -m "feat: redesigned app shell with brand, icons, theme toggle and page titles"
```

---

### Task 6: 前端 — HomePage 工作台

**Files:**
- Modify: `frontend/src/pages/HomePage.vue`(整体重写)

**Interfaces:**
- Consumes: `kbApi.list()`(既有)、`conversationsApi.list()`(既有)、Task 4 令牌。
- Produces: 快捷入口路由 `/kb?create=1`、`/chat`、`/kb`(Task 7 需支持 `?create=1`;Task 9 需支持 `/chat?conv=`)。零后端改动;无单测(Task 12 走查),验证=build+lint。

- [ ] **Step 1: 实现**

```vue
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ChatDotRound, Collection, Plus } from '@element-plus/icons-vue'
import { conversationsApi, type ConversationItem } from '@/api/chat'
import { kbApi, type KbItem } from '@/api/kb'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()

const kbs = ref<KbItem[]>([])
const conversations = ref<ConversationItem[]>([])
const loading = ref(false)

const greeting = computed(() => {
  const h = new Date().getHours()
  if (h < 6) return '夜深了'
  if (h < 12) return '早上好'
  if (h < 18) return '下午好'
  return '晚上好'
})

const totalDocs = computed(() => kbs.value.reduce((s, k) => s + (k.doc_count ?? 0), 0))
const recentConversations = computed(() => conversations.value.slice(0, 5))

const stats = computed(() => [
  { label: '知识库', value: kbs.value.length, icon: Collection },
  { label: '文档总数', value: totalDocs.value, icon: Document },
  { label: '会话数', value: conversations.value.length, icon: ChatDotRound },
])

onMounted(async () => {
  loading.value = true
  try {
    ;[kbs.value, conversations.value] = await Promise.all([kbApi.list(), conversationsApi.list()])
  } catch {
    /* 首页统计加载失败不阻塞,展示 0 */
  } finally {
    loading.value = false
  }
})

function fmtTime(iso: string) {
  return iso.replace('T', ' ').slice(0, 16)
}
</script>

<template>
  <div class="home-page" v-loading="loading">
    <section class="hero">
      <h2>{{ greeting }},{{ auth.user?.username ?? '' }}</h2>
      <p>从知识库或对话开始,构建你的私有知识问答。</p>
      <div class="quick-actions">
        <el-button type="primary" :icon="Plus" @click="router.push('/kb?create=1')">
          新建知识库
        </el-button>
        <el-button :icon="ChatDotRound" @click="router.push('/chat')">开始提问</el-button>
        <el-button :icon="Collection" @click="router.push('/kb')">查看知识库</el-button>
      </div>
    </section>

    <section class="stat-cards">
      <el-card v-for="s in stats" :key="s.label" shadow="never" class="stat-card">
        <div class="stat-body">
          <el-icon :size="22" class="stat-icon"><component :is="s.icon" /></el-icon>
          <div>
            <div class="stat-value">{{ s.value }}</div>
            <div class="stat-label">{{ s.label }}</div>
          </div>
        </div>
      </el-card>
    </section>

    <el-card shadow="never" class="recent-card">
      <template #header>最近会话</template>
      <div v-if="recentConversations.length === 0" class="recent-empty">
        还没有会话,去<a @click.prevent="router.push('/chat')" href="/chat">对话页</a>提第一个问题吧。
      </div>
      <div
        v-for="c in recentConversations"
        :key="c.id"
        class="recent-item"
        @click="router.push(`/chat?conv=${c.id}`)"
      >
        <span class="recent-title">{{ c.title }}</span>
        <span class="recent-time">{{ fmtTime(c.created_at) }}</span>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.hero {
  margin-bottom: var(--app-spacing-xl);
}
.hero h2 {
  margin: 0 0 4px;
}
.hero p {
  margin: 0 0 var(--app-spacing-lg);
  color: var(--el-text-color-secondary);
}
.quick-actions {
  display: flex;
  gap: var(--app-spacing-sm);
}
.stat-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: var(--app-spacing-md);
  margin-bottom: var(--app-spacing-lg);
}
.stat-card {
  border-radius: var(--app-radius);
}
.stat-body {
  display: flex;
  align-items: center;
  gap: var(--app-spacing-md);
}
.stat-icon {
  color: var(--el-color-primary);
}
.stat-value {
  font-size: 24px;
  font-weight: 700;
  line-height: 1.2;
}
.stat-label {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.recent-card {
  border-radius: var(--app-radius);
}
.recent-empty {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
.recent-item {
  display: flex;
  justify-content: space-between;
  padding: var(--app-spacing-sm) var(--app-spacing-xs);
  border-radius: var(--app-radius-sm);
  cursor: pointer;
}
.recent-item:hover {
  background: var(--el-fill-color-light);
}
.recent-title {
  max-width: 70%;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 14px;
}
.recent-time {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
</style>
```

注:`stats` 里 Document 图标需从 `@element-plus/icons-vue` 一并 import(script 顶部 import 行补 `Document`)。

- [ ] **Step 2: 构建与静态检查**

Run: `npm run build` → 绿;Run: `npm run lint` → 0/0;Run: `npx vitest run` → 既有测试不回归。

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/HomePage.vue
git commit -m "feat: home workspace with stats, quick actions and recent chats"
```

---

### Task 7: 前端 — KbPage 卡片网格 + 搜索 + ?create=1

**Files:**
- Modify: `frontend/src/pages/KbPage.vue`(script 小增,template 表格→卡片)

**Interfaces:**
- Consumes: Task 6 的 `/kb?create=1`;既有 kbApi 全套;Task 4 令牌。
- Produces: 无下游依赖。成员/创建对话框、权限逻辑全部不动。

- [ ] **Step 1: script 增量改动**

在既有 script(保持 load/openCreate/submit/openDocs/成员管理全部不动)上:

```ts
import { useRoute } from 'vue-router' // router 已有,补 useRoute
const route = useRoute()
import { computed } from 'vue' // vue import 行补 computed
import { Search } from '@element-plus/icons-vue'

const keyword = ref('')
const filteredList = computed(() => {
  const k = keyword.value.trim().toLowerCase()
  if (!k) return list.value
  return list.value.filter(
    (kb) =>
      kb.name.toLowerCase().includes(k) ||
      (kb.description ?? '').toLowerCase().includes(k),
  )
})
const totalDocs = computed(() => list.value.reduce((s, kb) => s + (kb.doc_count ?? 0), 0))
```

`onMounted(load)` 改为:

```ts
onMounted(() => {
  load()
  // 工作台快捷入口:自动打开新建对话框(viewer 无权创建,跳过)
  if (route.query.create === '1' && !isViewer()) {
    openCreate()
  }
})
```

- [ ] **Step 2: template 替换(表格式页头+表格 → PageHeader+统计+搜索+卡片网格)**

页头与列表区替换为(两个对话框模板原样保留在文件末尾):

```vue
<template>
  <div class="kb-page">
    <PageHeader title="知识库" description="共 {{ list.length }} 个库 · {{ totalDocs }} 篇文档">
      <template #actions>
        <el-input
          v-model="keyword"
          :prefix-icon="Search"
          placeholder="搜索名称/描述"
          clearable
          class="kb-search"
        />
        <el-button v-if="!isViewer()" type="primary" @click="openCreate">新建知识库</el-button>
      </template>
    </PageHeader>

    <div v-loading="loading" class="kb-grid">
      <el-empty v-if="filteredList.length === 0" description="暂无知识库,点击右上角新建" class="kb-empty" />
      <el-card v-for="row in filteredList" :key="row.id" shadow="never" class="kb-card" @click="openDocs(row)">
        <div class="kb-card-head">
          <span class="kb-card-name">{{ row.name }}</span>
          <el-tag :type="permMeta(row.my_perm).type" size="small">
            {{ permMeta(row.my_perm).label }}
          </el-tag>
        </div>
        <p class="kb-card-desc">{{ row.description ?? '暂无描述' }}</p>
        <div class="kb-card-meta">
          <span>{{ row.doc_count }} 篇文档</span>
          <span>{{ fmtTime(row.created_at) }}</span>
        </div>
        <div class="kb-card-actions" @click.stop>
          <el-button size="small" type="primary" plain @click="openDocs(row)">进入</el-button>
          <el-button v-if="row.my_perm === 'owner'" size="small" plain @click="openMembers(row)">
            成员
          </el-button>
        </div>
      </el-card>
    </div>

    <!-- 新建/成员两个 el-dialog 原样保留 -->
  </div>
</template>
```

import 行补:`import PageHeader from '@/components/PageHeader.vue'`。scoped style 删除旧 `.kb-table` 相关,新增:

```css
.kb-search {
  width: 220px;
}
.kb-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: var(--app-spacing-md);
  min-height: 120px;
}
.kb-empty {
  grid-column: 1 / -1;
}
.kb-card {
  cursor: pointer;
  border-radius: var(--app-radius);
  transition: box-shadow 0.2s;
}
.kb-card:hover {
  box-shadow: var(--app-shadow-card);
}
.kb-card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--app-spacing-sm);
}
.kb-card-name {
  font-weight: 600;
  font-size: 15px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.kb-card-desc {
  margin: var(--app-spacing-sm) 0;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  min-height: 40px;
}
.kb-card-meta {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-bottom: var(--app-spacing-md);
}
.kb-card-actions {
  display: flex;
  gap: var(--app-spacing-sm);
}
```

- [ ] **Step 3: 构建与静态检查**

Run: `npm run build` → 绿;`npm run lint` → 0/0;`npx vitest run` 不回归。
手动(可选):`/kb?create=1` 自动弹新建对话框;搜索过滤;viewer 不见新建按钮。

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/KbPage.vue
git commit -m "feat: kb card grid with search and create deep-link"
```

---

### Task 8: 前端 — DocsPage 工具行 + 客户端分页

**Files:**
- Modify: `frontend/src/pages/DocsPage.vue`

**Interfaces:**
- Consumes: Task 5 PageHeader。轮询/上传/分块抽屉/重新解析逻辑全部不动。无下游依赖。

- [ ] **Step 1: script 增量**

```ts
import { watch } from 'vue' // vue import 行补 watch
import { Search } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'

const keyword = ref('')
const statusFilter = ref<string>('')
const page = reactive({ page: 1, pageSize: 20 })

const processingCount = computed(
  () => docs.value.filter((d) => NON_TERMINAL.includes(d.status)).length,
)

const filteredDocs = computed(() => {
  const k = keyword.value.trim().toLowerCase()
  return docs.value.filter((d) => {
    const okKw = !k || d.filename.toLowerCase().includes(k)
    const okStatus = !statusFilter.value || d.status === statusFilter.value
    return okKw && okStatus
  })
})

const pagedDocs = computed(() =>
  filteredDocs.value.slice((page.page - 1) * page.pageSize, page.page * page.pageSize),
)

watch([keyword, statusFilter], () => {
  page.page = 1
})
```

- [ ] **Step 2: template 替换页头与表格**

`page-header` 区块替换为:

```vue
<PageHeader :title="kbName" :description="`共 ${docs.length} 篇 · ${processingCount} 个处理中`">
  <template #actions>
    <el-select v-model="statusFilter" placeholder="全部状态" clearable class="status-filter">
      <el-option
        v-for="(meta, key) in STATUS_META"
        :key="key"
        :label="meta.label"
        :value="key"
      />
    </el-select>
    <el-input
      v-model="keyword"
      :prefix-icon="Search"
      placeholder="搜索文件名"
      clearable
      class="doc-search"
    />
    <el-button :icon="ArrowLeft" @click="router.push({ name: 'kb' })">返回</el-button>
  </template>
</PageHeader>
```

表格 `:data="docs"` 改 `:data="pagedDocs"`;表格后追加分页(总数为过滤后数量):

```vue
<el-pagination
  v-if="filteredDocs.length > page.pageSize"
  v-model:current-page="page.page"
  :page-size="page.pageSize"
  class="docs-pagination"
  layout="total, prev, pager, next"
  :total="filteredDocs.length"
/>
```

scoped style 补:

```css
.doc-search {
  width: 200px;
}
.status-filter {
  width: 130px;
}
.docs-pagination {
  margin-top: var(--app-spacing-md);
  justify-content: flex-end;
}
```

- [ ] **Step 3: 构建与静态检查**

Run: `npm run build` → 绿;`npm run lint` → 0/0;`npx vitest run` 不回归。
手动(可选):搜索/状态筛选联动分页重置;处理中徽章数字随轮询刷新。

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/DocsPage.vue
git commit -m "feat: docs toolbar with search, status filter and client pagination"
```

---

### Task 9: 前端 — ChatPage 打磨 + refused 渲染 + 停止生成 + ?conv= + 精排默认开

**Files:**
- Modify: `frontend/src/api/chat.ts`(MessageItem)
- Modify: `frontend/src/composables/useChatStream.ts`(DonePayload)
- Create: `frontend/src/components/AssistantMessage.vue`
- Create: `frontend/src/components/__tests__/AssistantMessage.spec.ts`
- Modify: `frontend/src/pages/ChatPage.vue`

**Interfaces:**
- Consumes: Task 3 的 `MessageOut.refused` 与 done 帧 `refused`;Task 4 令牌。
- Produces: `MessageItem.refused?: boolean`、`DonePayload.refused?: boolean`、`AssistantMessage` 组件(props: html/content/pending/refused/citations)。

- [ ] **Step 1: 写失败组件测试**

`frontend/src/components/__tests__/AssistantMessage.spec.ts`:

```ts
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import AssistantMessage from '@/components/AssistantMessage.vue'
import CitationList from '@/components/CitationList.vue'
import type { Citation } from '@/api/chat'

const citation: Citation = {
  number: 1,
  chunk_id: 1,
  document_id: 1,
  filename: 'a.pdf',
  page_no: 1,
  excerpt: '摘录',
}

describe('AssistantMessage', () => {
  it('renders refusal style and hides citations when refused', () => {
    const w = mount(AssistantMessage, {
      props: {
        content: '知识库中未找到相关内容',
        refused: true,
        citations: [citation],
      },
    })
    expect(w.find('.refusal').exists()).toBe(true)
    expect(w.findComponent(CitationList).exists()).toBe(false)
  })

  it('renders markdown and citations when not refused', () => {
    const w = mount(AssistantMessage, {
      props: { html: '<p>答案</p>', content: '答案', citations: [citation] },
    })
    expect(w.find('.refusal').exists()).toBe(false)
    expect(w.find('.markdown-body').exists()).toBe(true)
    expect(w.findComponent(CitationList).exists()).toBe(true)
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npx vitest run src/components/__tests__/AssistantMessage.spec.ts`
Expected: FAIL(组件不存在)。

- [ ] **Step 3: 实现 AssistantMessage.vue(最终形态:含 AI 头像/拒答样式/打字动画/markdown 深度样式,后者从 ChatPage 原样迁入)**

```vue
<script setup lang="ts">
import { InfoFilled } from '@element-plus/icons-vue'
import type { Citation } from '@/api/chat'
import CitationList from '@/components/CitationList.vue'

defineProps<{
  html?: string
  content: string
  pending?: boolean
  refused?: boolean
  citations?: Citation[] | null
}>()
</script>

<template>
  <div class="assistant-row">
    <span class="avatar bot-avatar">AI</span>
    <div class="bubble" :class="{ refused }">
      <div v-if="refused" class="refusal">
        <el-icon><InfoFilled /></el-icon>
        <span>{{ content }}</span>
      </div>
      <div v-else class="markdown-body" v-html="html" />
      <div v-if="pending && !content" class="typing">
        <span class="dot" /><span class="dot" /><span class="dot" />
      </div>
      <CitationList
        v-if="!pending && !refused && citations && citations.length > 0"
        :citations="citations"
      />
    </div>
  </div>
</template>

<style scoped>
.assistant-row {
  display: flex;
  gap: var(--app-spacing-sm);
  align-items: flex-start;
}
.avatar {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  font-size: 12px;
  font-weight: 600;
}
.bot-avatar {
  background: var(--el-color-primary);
  color: #fff;
}
.bubble {
  max-width: 78%;
  padding: var(--app-spacing-sm) var(--app-spacing-md);
  border-radius: var(--app-radius);
  font-size: 14px;
  line-height: 1.6;
  word-break: break-word;
  background: var(--el-fill-color-light);
}
.bubble.refused {
  background: var(--el-fill-color-lighter);
  border: 1px dashed var(--el-border-color);
}
.refusal {
  display: flex;
  align-items: center;
  gap: var(--app-spacing-sm);
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.typing {
  display: inline-flex;
  gap: 4px;
  align-items: center;
  padding: 4px 0;
}
.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--el-text-color-secondary);
  animation: typing-blink 1.2s infinite ease-in-out;
}
.dot:nth-child(2) {
  animation-delay: 0.2s;
}
.dot:nth-child(3) {
  animation-delay: 0.4s;
}
@keyframes typing-blink {
  0%,
  80%,
  100% {
    opacity: 0.3;
  }
  40% {
    opacity: 1;
  }
}

/* markdown 渲染区(自 ChatPage 原样迁入) */
.markdown-body :deep(p) {
  margin: 0 0 8px;
}
.markdown-body :deep(p:last-child) {
  margin-bottom: 0;
}
.markdown-body :deep(pre.hljs) {
  margin: 8px 0;
  padding: 10px 12px;
  border-radius: var(--app-radius-sm);
  overflow-x: auto;
  font-size: 13px;
}
.markdown-body :deep(code) {
  font-family: var(--el-font-family);
}
.markdown-body :deep(table) {
  border-collapse: collapse;
  margin: 8px 0;
}
.markdown-body :deep(th),
.markdown-body :deep(td) {
  border: 1px solid var(--el-border-color);
  padding: 4px 10px;
}
</style>
```

(el-icon/InfoFilled 走 Element Plus 全局注册,组件内不显式 import el-icon 本体。)

- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run src/components/__tests__/AssistantMessage.spec.ts` → 2 PASS

- [ ] **Step 5: 类型与 ChatPage 接线**

`api/chat.ts` MessageItem 加字段(citations 行后):

```ts
  /** M7:拒答标记(后端 MessageOut.refused;旧数据缺省 false) */
  refused?: boolean
```

`useChatStream.ts` DonePayload 加(answer 行后):

```ts
  /** M7:拒答标记(refused=true 时前端隐藏引用) */
  refused?: boolean
```

`ChatPage.vue` 改动(逐点):

1. import 行:`import { useRoute } from 'vue-router'` 补 useRoute(`const route = useRoute()`);`import AssistantMessage from '@/components/AssistantMessage.vue'`。
2. 精排默认开:

```ts
/** M4:请求级精排开关;M7 起默认开(阈值门控生效);localStorage 记忆用户选择 */
const rerankEnabled = ref(
  localStorage.getItem('airag_rerank') === null
    ? true
    : localStorage.getItem('airag_rerank') === '1',
)
```

3. ChatMessage 接口加 `refused?: boolean`;`toChatMessage` 返回对象加 `refused: m.refused ?? false`;onDone 加 `assistant.refused = d.refused ?? false`。
4. 消息渲染替换为组件(用户气泡保持原样):

```vue
<div v-for="(m, i) in messages" :key="m.id ?? `local-${i}`" class="msg-row" :class="m.role">
  <span v-if="m.role === 'user'" class="avatar user-avatar">
    {{ auth.user?.username?.slice(0, 1).toUpperCase() ?? '?' }}
  </span>
  <AssistantMessage
    v-if="m.role === 'assistant'"
    :html="m.html"
    :content="m.content"
    :pending="m.pending"
    :refused="m.refused"
    :citations="m.citations"
  />
  <div v-else class="bubble">{{ m.content }}</div>
</div>
```

需要 `import { useAuthStore } from '@/stores/auth'` 与 `const auth = useAuthStore()`(ChatPage 目前未引 auth)。
5. 停止生成:chat-input 区按钮替换:

```vue
<el-button
  v-if="streaming"
  type="danger"
  plain
  class="send-btn"
  @click="abort()"
>
  停止
</el-button>
<el-button v-else type="primary" class="send-btn" :disabled="!canSend()" @click="onSend">
  发送
</el-button>
```

6. `?conv=` 定位:onMounted 改:

```ts
onMounted(async () => {
  loadKbs()
  await loadConversations()
  const cv = Number(route.query.conv)
  if (cv) {
    const target = conversations.value.find((c) => c.id === cv)
    if (target) openConversation(target)
  }
})
```

7. 样式清理(ChatPage 侧):删除已迁入组件的旧样式——`.typing`、`.markdown-body` 全部 `:deep()` 规则、助手气泡分支(`.msg-row.assistant .bubble`);保留用户气泡(`.msg-row.user .bubble` 背景主色白字);`.msg-row` 与新增用户头像样式:

```css
.msg-row {
  display: flex;
  gap: var(--app-spacing-sm);
  align-items: flex-start;
}
.msg-row.user {
  justify-content: flex-end;
}
.msg-row.user .bubble {
  background: var(--el-color-primary);
  color: #fff;
  max-width: 78%;
  padding: var(--app-spacing-sm) var(--app-spacing-md);
  border-radius: var(--app-radius);
  font-size: 14px;
  line-height: 1.6;
  word-break: break-word;
}
.avatar {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  font-size: 12px;
  font-weight: 600;
}
.user-avatar {
  background: var(--el-color-primary-light-8);
  color: var(--el-color-primary);
  order: 2; /* 用户消息头像在气泡右侧 */
}
```

- [ ] **Step 6: 构建与全量前端检查**

Run: `npx vitest run` → ≥11 PASS(6 既有 + useTheme 3 + AssistantMessage 2);`npm run build` → 绿;`npm run lint` → 0/0。

- [ ] **Step 7: Commit**

```bash
git add frontend/src/api/chat.ts frontend/src/composables/useChatStream.ts frontend/src/components/AssistantMessage.vue frontend/src/components/__tests__/AssistantMessage.spec.ts frontend/src/pages/ChatPage.vue
git commit -m "feat: chat polish with refusal rendering, stop button, deep link and rerank default on"
```

---

### Task 10: 前端 — Users/AuditLog/Login 统一换肤

**Files:**
- Modify: `frontend/src/pages/UsersPage.vue`
- Modify: `frontend/src/pages/AuditLogPage.vue`
- Modify: `frontend/src/pages/LoginPage.vue`

**Interfaces:**
- Consumes: Task 5 PageHeader、Task 4 令牌。逻辑零改动红线(角色切换/启停/审计筛选/清理按钮/登录注册全部不动)。

- [ ] **Step 1: UsersPage 页头替换**

`<div class="page-header"><h2>用户管理</h2></div>` 替换为:

```vue
<PageHeader title="用户管理" description="管理系统用户角色与启用状态" />
```

import `PageHeader`;scoped 删旧 `.page-header` 规则;`.users-table` 加圆角:

```css
.users-table {
  width: 100%;
  max-width: 720px;
  border-radius: var(--app-radius);
}
```

- [ ] **Step 2: AuditLogPage 页头替换**

`page-header` 区块替换为(筛选行移入 actions):

```vue
<PageHeader title="审计日志" description="用户操作与系统动作记录">
  <template #actions>
    <div class="filters">
      <!-- 原 filters 内五个控件原样搬入:username 输入/action 选择/查询按钮/清理按钮 -->
    </div>
  </template>
</PageHeader>
```

import `PageHeader`;scoped 保留 `.filters/.filter-input/.filter-select` 规则,删旧 `.page-header` 规则;`.audit-table` 加 `border-radius: var(--app-radius);`。

- [ ] **Step 3: LoginPage 品牌化**

template 替换(表单逻辑/校验/按钮全部不动,仅外壳):

```vue
<template>
  <div class="login-page">
    <el-card class="login-card" shadow="never">
      <div class="login-brand">
        <span class="brand-mark">AI</span>
        <h2>AIRag 知识库</h2>
      </div>
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
        <!-- 两个 el-form-item 与两个按钮原样保留 -->
      </el-form>
    </el-card>
  </div>
</template>
```

scoped 替换:

```css
.login-page {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100vh;
  background: var(--app-bg);
}
.login-card {
  width: 360px;
  border-radius: var(--app-radius);
}
.login-brand {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--app-spacing-sm);
  margin-bottom: var(--app-spacing-lg);
}
.brand-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border-radius: var(--app-radius);
  background: var(--el-color-primary);
  color: #fff;
  font-weight: 700;
}
h2 {
  margin: 0;
}
```

- [ ] **Step 4: 构建与静态检查**

Run: `npm run build` → 绿;`npm run lint` → 0/0;`npx vitest run` 不回归。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/UsersPage.vue frontend/src/pages/AuditLogPage.vue frontend/src/pages/LoginPage.vue
git commit -m "feat: unified page headers and themed login"
```

---

### Task 11: 后端 — eval 零命中支持 + 配置文档

**Files:**
- Modify: `backend/scripts/eval_generation.py`(结果增 refused 字段)
- Modify: `backend/.env.example`、`README.md`(仓库根)

**Interfaces:**
- Produces: eval_generation JSON 结果每项含 `"refused": bool`,汇总行含拒答计数。Task 12 验收断言依赖。

- [ ] **Step 1: eval_generation.py 结果增 refused**

`results.append({...})` 内 `"citations": ...` 行后加:

```python
                "refused": bool(final.get("refused")),
```

汇总打印段,`parse_errors = ...` 行后加:

```python
    refused_n = sum(1 for r in results if r["refused"])
```

末行 print 改为:

```python
    print(f"  (parse_errors={parse_errors}  refused={refused_n}/{n})")
```

- [ ] **Step 2: 配置与文档**

`backend/.env.example` 在 RETRIEVAL 相关配置区(若无则 RERANK 区)加:

```
# M7:rerank 相关度阈值(仅 rerank 开启时生效;0=禁用)
RETRIEVAL_MIN_SCORE=0.30
```

`README.md` 两处增补:
1. 检索/问答配置说明处:`RETRIEVAL_MIN_SCORE`(默认 0.30;rerank 关闭时该阈值不生效,仅提示词兜底;前端精排开关 M7 起默认开,收益=阈值门控+排序质量,代价=每问一次 rerank 调用)。
2. 部署节 Windows 注意事项:裸 uvicorn(非 --reload)在 Windows 走 ProactorEventLoop 会打断 psycopg checkpointer 导致 ask 500;后端启动必须 `start_dev.bat`(M6 遗留文档化)。

- [ ] **Step 3: 全量回归 + Commit**

Run: `.venv\Scripts\python.exe -m pytest tests -q` → 全绿(既有 test_eval_generation 不读新字段,不回归)。

```bash
git add backend/scripts/eval_generation.py backend/.env.example README.md
git commit -m "feat: eval refused metric and retrieval threshold docs"
```

---

### Task 12: 验收 — 无头脚本 + eval 对比 + 双主题走查 + 记录固化

**Files:**
- Create: `backend/scripts/m7_acceptance.py`
- Create: `backend/eval_sets/9.json`(验收库动态建,eval set 用 KB id=9 段;若 id 被占,脚本按实际 id 写临时 eval 文件并在结束后删除——见 Step 1 说明)
- Modify: 本计划(勾选 + 执行记录)、spec 附录(阈值终值)

**Interfaces:**
- Consumes: Task 1-11 全部。

- [ ] **Step 1: 写验收脚本**

对齐 `scripts/m6_acceptance.py` 的结构复用其 helpers(注册登录/建库/传文档/轮询 done/SSE 解析/asyncpg 直连;首行注释写明对真后端 127.0.0.1:8001 执行、ZHIPU_API_KEY 空则打印 SKIP 退出)。场景骨架:

```python
"""M7 验收:零命中双门控 / refused 全链路 / 主题前端走查前置数据。对真后端 127.0.0.1:8001 执行。"""
# 复用 m6_acceptance.py 的 register_and_login / create_kb / upload_doc / wait_doc_done / ask_sse helpers
# (按需复制到本文件,不 import m6 模块,保持脚本自包含)

# 场景 1:rerank on + 零命中问题 → refused
#   r = ask_sse(kb_id, "珠穆朗玛峰有多高?", rerank=True)   # 与验收库内容无关
#   断言 r.done["refused"] is True 且 r.done 引用为空(citations 事件 data == [])
# 场景 2:rerank on + 正常问题(库内文档内容) → 不误伤
#   断言 r.done["refused"] is False 且 citations 长度 >= 1
# 场景 3:rerank off + 零命中问题 → 提示词兜底
#   断言 answer.startswith("知识库中未找到相关内容")
# 场景 4:GET /chat/conversations/{id}/messages → 助手消息 refused 字段与场景 1/2 一致
# 场景 5:eval_generation --kb {kb_id} --rerank --json
#   断言 zero-hit 题 refused==True 比例 100%;正常题 faithfulness 均值 >= 0.8(M6 同门槛)
#   若场景 2/5 出现误拒:记录并把 RETRIEVAL_MIN_SCORE 按 0.05 下调重启后端重跑,终值回填 spec 附录
```

eval set 内容(上传 2 篇内容明确的 txt→pdf 或复用 m6 的验收文档生成方式;items 前 2 题正常、后 2 题零命中):

```json
{"kb_id": 9, "items": [
  {"question": "<库内事实题1>", "expect_keywords": ["<关键词>"]},
  {"question": "<库内事实题2>", "expect_keywords": ["<关键词>"]},
  {"question": "珠穆朗玛峰的海拔是多少米?", "expect_keywords": []},
  {"question": "世界杯足球赛每几年举办一次?", "expect_keywords": []}
]}
```

(具体库内题以实际上传文档内容为准,实现者按 m6 验收文档同款造两篇含明确事实的 PDF;两道零命中题固定用上面两题。)

- [ ] **Step 2: 跑无头验收(真后端+真智谱)**

前置:后端 start_dev.bat、worker/beat bat 启动;.env 有 ZHIPU_API_KEY 与 RERANK_ENABLED=true。
Run: `.venv\Scripts\python.exe -m scripts.m7_acceptance`
Expected: 全场景 PASS(0 SKIP);若阈值误伤按脚本提示下调重跑并记录终值。

- [ ] **Step 3: 全量回归双栈**

Run(backend): `.venv\Scripts\python.exe -m pytest tests -q` → ≥127 基线只增不减,记录终值。
Run(frontend): `npx vitest run` + `npm run build` + `npm run lint` → 全绿记录终值。

- [ ] **Step 4: 用户浏览器双主题走查(交给用户)**

走查清单(用户执行,亮/暗各一遍):
- 登录页 / 首页工作台(统计+快捷入口+最近会话点击跳转)
- 知识库卡片(搜索/新建对话框/?create=1/成员管理)
- 文档页(上传/筛选/分页/分块抽屉/处理中徽章)
- 对话页(精排默认开/停止生成/拒答样式无引用/历史会话 refused 样式/头像/打字动画)
- 用户管理/审计日志(含清理按钮)/主题切换往返/侧栏高亮

- [ ] **Step 5: 收尾提交与交接**

勾选本计划全部步骤;追加"M7 执行记录"节(结果/偏差与修正,对齐 M6 计划格式);spec 附录回填阈值终值与 eval 数据;记忆更新(M7 完成状态 + M8 交接)。

```bash
git add docs/superpowers/plans/2026-09-17-airag-m7-frontend-theme-zero-hit.md docs/superpowers/specs/2026-09-17-airag-m7-frontend-theme-zero-hit-design.md backend/scripts/m7_acceptance.py
git commit -m "chore: m7 acceptance script, plan checkboxes and execution record"
git push
```

---

## 计划自审记录(写完即检)

1. **Spec 覆盖**:§1 令牌/主题=Task 4;§2 布局壳/PageHeader=Task 5;§3 七页=Task 6/7/8/9/10(HomePage 快捷入口三项与 ?create=1 在 Task 6/7 对齐);§4.1 提示词=Task 2;§4.2 阈值=Task 1;§4.3 refused=Task 2/3;§4.4 精排默认开=Task 9;§4.5 README=Task 11;§5 eval=Task 11/12;§6 测试验收=各任务+Task 12;§6.4 双主题走查=Task 12 Step 4。无缺口。
2. **占位扫描**:Task 6 stats 数组引用 Document 图标已在注中要求补 import;Task 10 两个"原样保留"注释指向的是既有代码区块而非计划缺失;Task 12 场景为骨架+断言语句(验收脚本含真实断言语义,零命中两题给定)。无 TBD。
3. **类型一致性**:`rerank -> list[tuple[int, float]]` 在 base/zhipu/nodes/测试四处一致;`refused` 布尔流经 state→generate→ask.py 落库→done 帧→DonePayload/MessageItem→AssistantMessage props,命名一致;`RETRIEVAL_MIN_SCORE` 拼写全文一致。
