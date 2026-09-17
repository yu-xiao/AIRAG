# AIRag M8 拒答加固 + 一致性收口 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 拒答判定子串化(包裹型加固)+ refused 每轮复位 + KB 重名 409 与下拉区分 + 暗色 hljs-regexp + 孤儿 eval_sets 清理脚本与真栈验收。

**Architecture:** 五个相互独立的小改动,后端三处(nodes.py 拒答判定与 rewrite 复位、kbs.py 重名校验)+ 前端三处(KbPage 内联错误、ChatPage 下拉区分、tokens.css 一行)+ 两个脚本(孤儿清理、无头验收)。无新依赖、无迁移、无接口变更(新增 409 语义)。

**Tech Stack:** FastAPI + LangGraph(TypedDict state)+ SQLAlchemy async + pytest;Vue3 + Element Plus + vitest;验收脚本 httpx 真栈(127.0.0.1:8001 + worker + 智谱 key)。

**Spec:** `docs/superpowers/specs/2026-09-17-airag-m8-refusal-hardening-design.md`(本计划从 spec 出发,执行者需同读)

## Global Constraints

- 后端命令一律在 `E:\Projects\AIRag\backend` 下用项目 venv:`.venv\Scripts\python -m pytest ...`(conda 环境不含依赖)。
- conftest 已显式封 `RERANK_ENABLED=false`,单测不得依赖 rerank 真调用。
- 前端命令在 `E:\Projects\AIRag\frontend`:`pnpm vitest run <path>` / `pnpm build` / `pnpm lint`。
- `REFUSAL_PHRASE = "知识库中未找到相关内容"`(`nodes.py:10`),任何断言/文案逐字复制,不得改写。
- 错误 detail 用英文短语(对齐现有 "knowledge base not found" 风格);面向用户的文案用中文。
- m7_acceptance.py 是历史记录,保留其 startswith 语义,不回改。
- 真栈验收前置:PG+Redis 服务已启 → `backend\start_dev.bat`(8001 端口)→ `start_worker.bat`;`.env` 已有 ZHIPU_API_KEY/REDIS_URL。key 未配置时验收脚本整体 SKIP 退出。
- Windows CMD 环境:无 `head/tail/ls`;`.venv\Scripts\python` 路径分隔用反斜杠。

---

### Task 1: 拒答判定子串化 + 提示词收紧

**Files:**
- Modify: `backend/app/services/chat_graph/nodes.py:12-17`(SYSTEM_PROMPT)、`:109`(refused 判定)
- Test: `backend/tests/test_chat_graph.py:586-593`(改造既有用例 + 新增)

**Interfaces:**
- Consumes: `REFUSAL_PHRASE` 常量(nodes.py:10)、`generate_node(state, llm)` 既有签名。
- Produces: `generate_node` 返回的 `refused` 语义变为"话术子串命中即 True";下游(SSE/落库/前端/导出)零改动。

- [x] **Step 1: 改造与新增失败测试**

把 `backend/tests/test_chat_graph.py:586` 的用例整体替换为以下三个(保留 `test_generate_marks_refusal`:575 与 `test_generate_normal_answer_not_refused`:596 不动):

```python
async def test_generate_refusal_containment_semantics():
    """M8:话术出现在回答任意位置即拒答(含包裹型礼貌前缀)。"""
    from app.services.chat_graph.nodes import generate_node

    out = await generate_node(
        {"question": "q", "hits": [_hit(1, "不相关资料")]},
        llm=_LLMScript(["知识库中未找到相关内容。"]),
    )
    assert out["refused"] is True  # 开头命中(原 startswith 语义兼容)

    out2 = await generate_node(
        {"question": "q", "hits": [_hit(1, "不相关资料")]},
        llm=_LLMScript(["很抱歉,知识库中未找到相关内容,建议换个问法。"]),
    )
    assert out2["refused"] is True  # 包裹型:话术居中


async def test_generate_near_phrase_not_refused():
    """近似措辞(缺字/改字)不含完整话术,不得误判。"""
    from app.services.chat_graph.nodes import generate_node

    out = await generate_node(
        {"question": "q", "hits": []},
        llm=_LLMScript(["知识库中未找到相关文档"]),
    )
    assert out["refused"] is False

    out2 = await generate_node(
        {"question": "q", "hits": []},
        llm=_LLMScript(["关于知识库的使用说明如下[1]"]),
    )
    assert out2["refused"] is False
```

- [x] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_chat_graph.py -k "refusal or refused or near_phrase" -v`
Expected: `test_generate_refusal_containment_semantics` FAIL(包裹型变体 refused=False),`test_generate_near_phrase_not_refused` PASS(旧逻辑恰好也通过,作回归锚)。

- [x] **Step 3: 最小实现**

`nodes.py:12-17` SYSTEM_PROMPT 第二行改为(原单行拆两行):

```python
SYSTEM_PROMPT = (
    "你是企业知识库助手。只依据下面提供的参考资料回答;"
    "引用资料时标注编号如 [1][2];若资料与问题不相关或不足以回答,"
    f'只回复"{REFUSAL_PHRASE}"本身,不得添加任何前后缀或礼貌用语,'
    "不得罗列、摘要或拼凑返回的资料;"
    "用中文,简洁分点。"
)
```

`nodes.py:109` 改为:

```python
        "refused": REFUSAL_PHRASE in (answer or "").strip(),
```

- [x] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_chat_graph.py -v`
Expected: 全 PASS(含既有 575/596 两用例)。

- [x] **Step 5: Commit**

```bash
git add backend/app/services/chat_graph/nodes.py backend/tests/test_chat_graph.py
git commit -m "feat: refusal detection upgraded to substring containment"
```

---

### Task 2: refused 入 rewrite 每轮复位

**Files:**
- Modify: `backend/app/services/chat_graph/nodes.py:137-144`(reset 字典)
- Test: `backend/tests/test_chat_graph.py:100-116`(既有断言)+ 新增用例

**Interfaces:**
- Consumes: `ChatState.refused: bool` 已声明(`app/services/chat_graph/state.py`),channel 存在,reset 合法。
- Produces: `rewrite_node` 返回字典新增键 `refused: False`。

- [x] **Step 1: 改造既有断言并新增失败测试**

`test_rewrite_disabled_resets_state`(test_chat_graph.py:109-116)的期望字典追加一行:

```python
    assert out == {
        "search_query": "它是什么",
        "retries": 0,
        "grade": "",
        "hopped": False,
        "sub_queries": [],  # M6:每轮清空,防 checkpointer 跨轮残留
        "proposed_query": "",
        "refused": False,  # M8:拒答标记同样防跨轮残留
    }
```

紧随其后新增:

```python
async def test_rewrite_clears_stale_refused():
    """上一轮残留的 refused 必须在本轮 rewrite 被复位(M8 拓扑护栏)。"""
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from app.services.chat_graph.nodes import rewrite_node

    out = await rewrite_node(
        {"question": "新问题", "refused": True},
        llm=FakeListChatModel(responses=["不该被调用"]),
    )
    assert out["refused"] is False
```

- [x] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_chat_graph.py -k "rewrite" -v`
Expected: `test_rewrite_disabled_resets_state` 与 `test_rewrite_clears_stale_refused` FAIL(返回字典无 refused 键)。

- [x] **Step 3: 最小实现**

`nodes.py:137-144` reset 字典追加(置于 `"proposed_query": "",` 之后):

```python
    reset = {
        "search_query": question,
        "retries": 0,
        "grade": "",
        "hopped": False,
        "sub_queries": [],
        "proposed_query": "",
        "refused": False,  # M8:异常中断路径下防上一轮拒答标记跨轮残留
    }
```

- [x] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_chat_graph.py -v`
Expected: 全 PASS。

- [x] **Step 5: Commit**

```bash
git add backend/app/services/chat_graph/nodes.py backend/tests/test_chat_graph.py
git commit -m "fix: reset refused flag at turn start in rewrite node"
```

---

### Task 3: KB 创建重名 409(strip 归一化)

**Files:**
- Modify: `backend/app/api/kbs.py:15-33`(create_kb)
- Test: `backend/tests/test_kbs.py`(文末追加)

**Interfaces:**
- Consumes: conftest fixtures `client` / `auth_headers`(editor);`KBIn.name`(str, 1-128)。
- Produces: `POST /api/kbs` 新语义——strip 后为空 → 422 `"knowledge base name cannot be blank"`;strip 后重名 → 409 `"knowledge base name already exists"`;入库 name 为 strip 后形态。Task 4 前端依赖 409 状态码与该 detail。

- [x] **Step 1: 写失败测试**

`backend/tests/test_kbs.py` 文末追加:

```python
async def test_create_kb_duplicate_name_409(client, auth_headers):
    first = await client.post("/api/kbs", json={"name": "重名库"}, headers=auth_headers)
    dup = await client.post("/api/kbs", json={"name": "重名库"}, headers=auth_headers)
    assert first.status_code == 201
    assert dup.status_code == 409
    assert dup.json()["detail"] == "knowledge base name already exists"


async def test_create_kb_duplicate_after_strip_409(client, auth_headers):
    await client.post("/api/kbs", json={"name": "归一库"}, headers=auth_headers)
    dup = await client.post("/api/kbs", json={"name": "  归一库  "}, headers=auth_headers)
    assert dup.status_code == 409
    listed = await client.get("/api/kbs", headers=auth_headers)
    names = [k["name"] for k in listed.json() if k["name"].strip() == "归一库"]
    assert names == ["归一库"]  # 入库即 strip 后形态,仅一条


async def test_create_kb_blank_after_strip_422(client, auth_headers):
    resp = await client.post("/api/kbs", json={"name": "   "}, headers=auth_headers)
    assert resp.status_code == 422
    assert resp.json()["detail"] == "knowledge base name cannot be blank"
```

- [x] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_kbs.py -k "duplicate or blank" -v`
Expected: 三个新用例 FAIL(现在返回 201)。

- [x] **Step 3: 最小实现**

`kbs.py` `create_kb` 在 viewer 检查之后改为(替换 21-29 行对应片段;`select` 已在文件头导入):

```python
    if current.role == "viewer":
        raise HTTPException(status_code=403, detail="viewers cannot create knowledge bases")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="knowledge base name cannot be blank")
    dup = (
        await db.execute(select(KnowledgeBase).where(KnowledgeBase.name == name))
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(status_code=409, detail="knowledge base name already exists")
    kb = KnowledgeBase(
        name=name, description=payload.description, owner_id=current.id
    )
    db.add(kb)
    await db.flush()
    await audit(db, current.username, "kb_create", f"kb:{kb.id}", {"name": name})
```

(后续 commit/refresh/返回不变。)

- [x] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_kbs.py tests/test_kb_permissions.py -v`
Expected: 全 PASS(权限套件不受影响)。

- [x] **Step 5: Commit**

```bash
git add backend/app/api/kbs.py backend/tests/test_kbs.py
git commit -m "feat: reject duplicate knowledge base names with 409"
```

---

### Task 4: KbPage 创建 409 内联错误

**Files:**
- Modify: `frontend/src/pages/KbPage.vue`(submit catch、name 表单项、新增 `serverNameError`)
- Test: `frontend/src/pages/__tests__/KbPage.spec.ts`(新建,目录不存在则一并创建)

**Interfaces:**
- Consumes: Task 3 的 409 响应(`response.status === 409`);`kbApi.create` 抛 axios 形态错误。
- Produces: 纯 UI 行为,无导出接口。

- [x] **Step 1: 写失败测试**

新建 `frontend/src/pages/__tests__/KbPage.spec.ts`:

```typescript
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import KbPage from '@/pages/KbPage.vue'
import { kbApi } from '@/api/kb'

const push = vi.fn()

vi.mock('vue-router', () => ({
  useRoute: () => ({ query: { create: '1' } }),  // 挂载即开创建对话框
  useRouter: () => ({ push }),
}))
vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ user: { id: 1, username: 'ed', role: 'editor' } }),
}))
vi.mock('@/api/kb', () => ({
  kbApi: { list: vi.fn(), create: vi.fn() },
}))

const nameInput = 'input[placeholder="请输入知识库名称"]'
const createBtn = (w: ReturnType<typeof mount>) =>
  w.findAll('button').find((b) => b.text() === '创建')!

describe('KbPage create dialog', () => {
  beforeEach(() => {
    vi.mocked(kbApi.list).mockResolvedValue([])
    push.mockClear()
  })

  it('409 shows inline name error and keeps dialog open with input', async () => {
    vi.mocked(kbApi.create).mockRejectedValue({
      response: { status: 409, data: { detail: 'knowledge base name already exists' } },
    })
    const w = mount(KbPage)
    await flushPromises()
    await w.find(nameInput).setValue('重名库')
    await createBtn(w).trigger('click')
    await flushPromises()
    const err = w.find('.el-form-item__error')
    expect(err.exists()).toBe(true)
    expect(err.text()).toContain('已存在')
    expect((w.find(nameInput).element as HTMLInputElement).value).toBe('重名库')
  })

  it('non-409 falls back to global message without inline error', async () => {
    vi.mocked(kbApi.create).mockRejectedValue({
      response: { status: 500, data: { detail: 'boom' } },
    })
    const w = mount(KbPage)
    await flushPromises()
    await w.find(nameInput).setValue('普通库')
    await createBtn(w).trigger('click')
    await flushPromises()
    expect(w.find('.el-form-item__error').exists()).toBe(false)
  })
})
```

(若对话框内容因 teleport 在 `w.find` 下取不到,给 mount 加 `attachTo: document.body` 并改用 `document.querySelector`;先按默认尝试。)

- [x] **Step 2: 跑测试确认失败**

Run(frontend 目录): `pnpm vitest run src/pages/__tests__/KbPage.spec.ts`
Expected: 用例 1 FAIL(无内联错误节点),用例 2 PASS(现状恰好不显示内联)。

- [x] **Step 3: 最小实现**

`KbPage.vue` script 增加(挨着 `const form = reactive(...)`):

```typescript
const serverNameError = ref('')
```

`openCreate()` 首行加 `serverNameError.value = ''`;`submit()` 的 catch 整体替换为:

```typescript
  } catch (e) {
    const resp = (e as { response?: { status?: number; data?: { detail?: string } } })?.response
    if (resp?.status === 409) {
      serverNameError.value = '该名称已存在,请换一个名称'
    } else {
      ElMessage.error(resp?.data?.detail ?? '创建失败')
    }
  } finally {
```

template 名称表单项与输入加联动清除:

```html
        <el-form-item label="名称" prop="name" :error="serverNameError || undefined">
          <el-input
            v-model="form.name"
            placeholder="请输入知识库名称"
            maxlength="128"
            @input="serverNameError = ''"
          />
        </el-form-item>
```

- [x] **Step 4: 跑测试确认通过**

Run: `pnpm vitest run src/pages/__tests__/KbPage.spec.ts`
Expected: 2 PASS。

- [x] **Step 5: Commit**

```bash
git add frontend/src/pages/KbPage.vue frontend/src/pages/__tests__/KbPage.spec.ts
git commit -m "feat: inline duplicate-name error on kb create dialog"
```

---

### Task 5: ChatPage 下拉重名区分(id 后缀)

**Files:**
- Create: `frontend/src/utils/kbLabel.ts`
- Modify: `frontend/src/pages/ChatPage.vue:363`(el-option label)
- Test: `frontend/src/utils/__tests__/kbLabel.spec.ts`(新建)

**Interfaces:**
- Consumes: `KbItem`(id: number, name: string;frontend/src/api/kb.ts)。
- Produces: `disambiguateKbNames(kbs: Pick<KbItem, 'id' | 'name'>[]): Map<number, string>`——碰撞名映射为 `` `${name} ·#${id}` ``,唯一名映射纯名。

- [x] **Step 1: 写失败测试**

新建 `frontend/src/utils/__tests__/kbLabel.spec.ts`:

```typescript
import { describe, expect, it } from 'vitest'
import { disambiguateKbNames } from '@/utils/kbLabel'

describe('disambiguateKbNames', () => {
  it('uniquely named kbs keep plain name', () => {
    const m = disambiguateKbNames([
      { id: 1, name: '甲库' },
      { id: 2, name: '乙库' },
    ])
    expect(m.get(1)).toBe('甲库')
    expect(m.get(2)).toBe('乙库')
  })

  it('duplicate names get id suffix on every collision member', () => {
    const m = disambiguateKbNames([
      { id: 3, name: '同名库' },
      { id: 7, name: '乙库' },
      { id: 5, name: '同名库' },
    ])
    expect(m.get(3)).toBe('同名库 ·#3')
    expect(m.get(5)).toBe('同名库 ·#5')
    expect(m.get(7)).toBe('乙库')
  })

  it('empty list yields empty map', () => {
    expect(disambiguateKbNames([]).size).toBe(0)
  })
})
```

- [x] **Step 2: 跑测试确认失败**

Run: `pnpm vitest run src/utils/__tests__/kbLabel.spec.ts`
Expected: FAIL,模块不存在。

- [x] **Step 3: 最小实现**

新建 `frontend/src/utils/kbLabel.ts`:

```typescript
import type { KbItem } from '@/api/kb'

/** M8:重名知识库在下拉/tag 中追加 id 后缀,唯一者保持纯名(仅碰撞时视觉变化)。 */
export function disambiguateKbNames(
  kbs: Pick<KbItem, 'id' | 'name'>[],
): Map<number, string> {
  const counts = new Map<string, number>()
  for (const k of kbs) counts.set(k.name, (counts.get(k.name) ?? 0) + 1)
  const labels = new Map<number, string>()
  for (const k of kbs) {
    labels.set(k.id, (counts.get(k.name) ?? 0) > 1 ? `${k.name} ·#${k.id}` : k.name)
  }
  return labels
}
```

- [x] **Step 4: 接线 ChatPage 并跑测试**

`ChatPage.vue` script:`import { disambiguateKbNames } from '@/utils/kbLabel'`(挨着 kbApi import),挨着 `const kbs = ref<KbItem[]>([])`(67 行)加:

```typescript
const kbLabels = computed(() => disambiguateKbNames(kbs.value))
```

template 363 行改为:

```html
          <el-option v-for="k in kbs" :key="k.id" :label="kbLabels.get(k.id) ?? k.name" :value="k.id" />
```

Run: `pnpm vitest run src/utils/__tests__/kbLabel.spec.ts`
Expected: 3 PASS。

- [x] **Step 5: Commit**

```bash
git add frontend/src/utils/kbLabel.ts frontend/src/utils/__tests__/kbLabel.spec.ts frontend/src/pages/ChatPage.vue
git commit -m "feat: disambiguate duplicate kb names in chat selector"
```

---

### Task 6: 暗色 hljs-regexp

**Files:**
- Modify: `frontend/src/styles/tokens.css:64-67`

**Interfaces:**
- Consumes: 暗色 string 组色值 `#98c379`(亮色 github.css 中 regexp 与 string 同为 #032f62,暗色保持同组)。
- Produces: 无(纯样式)。

- [x] **Step 1: 修改规则组**

`tokens.css:64-67` 改为:

```css
html.dark .hljs-string,
html.dark .hljs-attr,
html.dark .hljs-regexp {
  color: #98c379;
}
```

- [x] **Step 2: 验证构建**

Run: `pnpm build`
Expected: 成功(vue-tsc + vite 无错)。

- [x] **Step 3: Commit**

```bash
git add frontend/src/styles/tokens.css
git commit -m "fix: dark theme hljs-regexp color"
```

---

### Task 7: 孤儿 eval_sets 清理脚本

**Files:**
- Create: `backend/scripts/purge_orphan_evalsets.py`
- Test: `backend/tests/test_purge_evalsets.py`(新建)

**Interfaces:**
- Consumes: `backend/eval_sets/*.json` 文件名约定 `{kb_id}.json`(README.md 不匹配);`app.db.session.SessionLocal`(真栈运行库)。
- Produces: 纯函数 `find_orphans(existing_ids: set[int], files: list[Path]) -> list[Path]`;CLI `--apply` 开关(默认 dry-run)。Task 8 验收调用 dry-run 输出。

- [x] **Step 1: 写失败测试**

新建 `backend/tests/test_purge_evalsets.py`:

```python
from pathlib import Path

from scripts.purge_orphan_evalsets import find_orphans


def _p(name: str) -> Path:
    return Path("eval_sets") / name


def test_find_orphans_by_kb_ids():
    files = [_p("5.json"), _p("6.json"), _p("9.json")]
    assert find_orphans({9}, files) == [_p("5.json"), _p("6.json")]


def test_find_orphans_ignores_non_numeric_and_readme():
    files = [_p("README.md"), _p("abc.json"), _p("9.json")]
    assert find_orphans({9}, files) == []
    assert find_orphans(set(), files) == [_p("9.json")]
```

- [x] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python -m pytest tests/test_purge_evalsets.py -v`
Expected: FAIL,`ModuleNotFoundError: No module named 'scripts.purge_orphan_evalsets'`。

- [x] **Step 3: 最小实现**

新建 `backend/scripts/purge_orphan_evalsets.py`:

```python
"""对照运行库清理孤儿评估集(eval_sets/{kb_id}.json 中 KB 已不存在者)。

用法(backend 目录,项目 venv):
    .venv\\Scripts\\python scripts\\purge_orphan_evalsets.py           # dry-run,仅打印
    .venv\\Scripts\\python scripts\\purge_orphan_evalsets.py --apply   # 真删(磁盘 unlink)

M8 背书:验收脚本按 kb_id 留存评估集供复评(git 版本化);系统无 KB 删除端点,
KB 本体经人工清理后文件残留,本脚本按 DB 真值收口。README.md 不在匹配范围。
删除产生的工作区变更随里程碑提交入库。
"""
import argparse
import asyncio
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent.parent / "eval_sets"


def find_orphans(existing_ids: set[int], files: list[Path]) -> list[Path]:
    """文件名主干为纯数字且不在现存 KB id 集合中者即孤儿。"""
    return [
        f for f in files if f.stem.isdigit() and int(f.stem) not in existing_ids
    ]


async def fetch_kb_ids() -> set[int]:
    from sqlalchemy import text

    from app.db.session import SessionLocal

    async with SessionLocal() as s:
        rows = await s.execute(text("SELECT id FROM knowledge_bases"))
        return {r[0] for r in rows.all()}


async def main(apply: bool) -> None:
    ids = await fetch_kb_ids()
    files = sorted(EVAL_DIR.glob("*.json")) if EVAL_DIR.is_dir() else []
    orphans = find_orphans(ids, files)
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"[{mode}] 现存 KB {len(ids)} 个;eval_sets/*.json {len(files)} 个;孤儿 {len(orphans)} 个")
    for f in orphans:
        print(f"  - {f.name}")
    if not orphans:
        print("无可清理文件")
        return
    if apply:
        for f in orphans:
            f.unlink()
            print(f"deleted: {f.name}")
        print("清理完成(工作区变更随里程碑提交)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真删(默认 dry-run)")
    asyncio.run(main(ap.parse_args().apply))
```

- [x] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python -m pytest tests/test_purge_evalsets.py -v`
Expected: 2 PASS。

- [x] **Step 5: Commit**

```bash
git add backend/scripts/purge_orphan_evalsets.py backend/tests/test_purge_evalsets.py
git commit -m "feat: orphan eval_sets purge script"
```

---

### Task 8: m8 无头验收脚本(真智谱)

**Files:**
- Create: `backend/scripts/m8_acceptance.py`

**Interfaces:**
- Consumes: Task 1 的子串判定语义(断言用 `REFUSAL_PHRASE in answer`)、Task 3 的 409、Task 7 的 dry-run 输出;真栈 http://127.0.0.1:8001/api。
- Produces: 退出码 0/1 与 PASS/FAIL 清单;收尾直接清 DB(系统无删除端点)。

- [x] **Step 1: 写脚本**

新建 `backend/scripts/m8_acceptance.py`:

```python
"""M8 无头验收脚本(真栈:http://127.0.0.1:8001 + worker + 智谱 key)。

用法(backend 目录,项目 venv):
    .venv\\Scripts\\python scripts\\m8_acceptance.py

覆盖 M8:拒答子串判定(零命中 refused + 话术包含断言)/ 正常题不误伤回归 /
KB 重名 409 / 孤儿 eval_sets dry-run。ZHIPU_API_KEY 未配置时打印 SKIP 整体退出。
收尾直接清 DB(chunks/documents/kb_permissions/knowledge_bases,FK 顺序)。
"""
import asyncio
import json
import subprocess
import sys
import time

import httpx
import pymupdf as fitz

BASE = "http://127.0.0.1:8001/api"
TIMEOUT = httpx.Timeout(300.0)
RESULTS = {"pass": [], "fail": [], "skip": []}

KB_NAME = "M8验收库"
KB_NAME_B = "M8验收库B"
FACT1 = "青鸾号高空气船的巡航升限为八千五百米"
NORMAL_Q1 = "青鸾号高空气船的巡航升限是多少米?"
ZERO_HIT_Q1 = "珠穆朗玛峰的海拔是多少米?"
ZERO_HIT_Q2 = "世界杯足球赛每几年举办一次?"
REFUSAL_PHRASE = "知识库中未找到相关内容"
PDF_MIME = "application/pdf"


def check(name, cond, detail=""):
    if cond:
        RESULTS["pass"].append(name)
        print(f"PASS {name}")
    else:
        RESULTS["fail"].append(name)
        print(f"FAIL {name}  {detail}")


def skip(name, why):
    RESULTS["skip"].append(name)
    print(f"SKIP {name}  ({why})")


def summary_and_exit():
    print()
    total = sum(len(v) for v in RESULTS.values())
    print(f"M8 ACCEPTANCE: {len(RESULTS['pass'])}/{total} PASS")
    if RESULTS["skip"]:
        print("SKIP:", *RESULTS["skip"], sep="\n  - ")
    if RESULTS["fail"]:
        print("FAILED:", *RESULTS["fail"], sep="\n  - ")
        sys.exit(1)


def promote_roles(usernames: dict[str, str]) -> None:
    async def _run():
        from sqlalchemy import text

        from app.db.session import SessionLocal

        async with SessionLocal() as s:
            for username, role in usernames.items():
                await s.execute(
                    text("UPDATE users SET role = :r WHERE username = :u"),
                    {"r": role, "u": username},
                )
            await s.commit()

    asyncio.run(_run())


def cleanup_kbs(kb_ids: list[int]) -> None:
    async def _run():
        from sqlalchemy import text

        from app.db.session import SessionLocal

        async with SessionLocal() as s:
            for kid in kb_ids:
                await s.execute(text("DELETE FROM chunks WHERE kb_id = :k"), {"k": kid})
                await s.execute(text("DELETE FROM documents WHERE kb_id = :k"), {"k": kid})
                await s.execute(text("DELETE FROM kb_permissions WHERE kb_id = :k"), {"k": kid})
                await s.execute(text("DELETE FROM knowledge_bases WHERE id = :k"), {"k": kid})
            await s.commit()

    asyncio.run(_run())


def register_and_login(client: httpx.Client, username: str) -> dict:
    client.post(f"{BASE}/auth/register",
                json={"username": username, "password": "secret123"})
    r = client.post(f"{BASE}/auth/login",
                    json={"username": username, "password": "secret123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def make_pdf_bytes(title: str, paragraphs: list[str]) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    y = 96
    page.insert_text((72, y), title, fontname="china-s", fontsize=16)
    for p in paragraphs:
        y += 28
        page.insert_text((72, y), p, fontname="china-s", fontsize=12)
    return doc.tobytes()


def upload_doc(client: httpx.Client, headers: dict, kb_id: int,
               filename: str, content: bytes) -> dict:
    r = client.post(
        f"{BASE}/kbs/{kb_id}/documents",
        files={"file": (filename, content, PDF_MIME)},
        data={"ocr": "auto"},
        headers=headers,
    )
    return {"status": r.status_code, "body": r.json(), "text": r.text[:200]}


def wait_doc_status(client: httpx.Client, headers: dict, doc_id: int,
                    terminal=("done", "failed"), timeout_s=120) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = client.get(f"{BASE}/documents/{doc_id}", headers=headers)
        doc = r.json()
        if doc["status"] in terminal:
            return doc
        time.sleep(2)
    return {"status": "timeout", "error_msg": "poll timeout"}


def sse_ask(client: httpx.Client, headers: dict, kb_ids: list[int],
            question: str, rerank: bool = True) -> dict:
    out = {"tokens": "", "citations": None, "done": None, "error": None}
    payload = {"kb_ids": kb_ids, "question": question, "rerank": rerank}
    with client.stream("POST", f"{BASE}/chat/ask", json=payload,
                       headers=headers, timeout=TIMEOUT) as resp:
        out["status"] = resp.status_code
        buf = b""
        for chunk in resp.iter_bytes():
            buf += chunk
            while b"\n\n" in buf:
                frame, buf = buf.split(b"\n\n", 1)
                line = frame.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                evt = json.loads(line[5:])
                if evt["type"] == "token":
                    out["tokens"] += str(evt["data"])
                elif evt["type"] == "citations":
                    out["citations"] = evt["data"]
                elif evt["type"] == "done":
                    out["done"] = evt["data"]
                elif evt["type"] == "error":
                    out["error"] = evt["data"]
                    return out
    return out


def final_answer(res: dict) -> str:
    return ((res.get("done") or {}).get("answer") or res["tokens"]) or ""


def ask_with_retry(client, headers, kb_id, question, rerank, judge, attempts=2):
    res, ok = None, False
    for _ in range(attempts):
        res = sse_ask(client, headers, [kb_id], question, rerank=rerank)
        ok = judge(res)
        if ok:
            break
    return res, ok


def main():
    from app.core.config import settings

    if not settings.ZHIPU_API_KEY:
        skip("M8 acceptance(全部场景)", "ZHIPU_API_KEY 未配置")
        summary_and_exit()
        return

    client = httpx.Client(timeout=60.0)
    kb_ids = []

    # ---- 0. 基线与账号 ----
    r = client.get(f"{BASE}/health")
    check("health 200", r.status_code == 200, r.text[:100])
    if r.status_code != 200:
        summary_and_exit()
        return

    client.post(f"{BASE}/auth/register",
                json={"username": "m8_owner", "password": "secret123"})
    promote_roles({"m8_owner": "editor"})
    owner = register_and_login(client, "m8_owner")
    me = client.get(f"{BASE}/auth/me", headers=owner).json()
    check("m8_owner promoted editor", me["role"] == "editor", str(me))

    # ---- S1: KB 重名 409 / 异名 201 ----
    r = client.post(f"{BASE}/kbs", json={"name": KB_NAME}, headers=owner)
    kb_id = r.json().get("id")
    kb_ids.append(kb_id)
    check("kb created 201", r.status_code == 201 and kb_id > 0, r.text[:200])

    dup = client.post(f"{BASE}/kbs", json={"name": KB_NAME}, headers=owner)
    check("S1 duplicate name 409",
          dup.status_code == 409
          and dup.json().get("detail") == "knowledge base name already exists",
          dup.text[:200])

    strip_dup = client.post(f"{BASE}/kbs", json={"name": f"  {KB_NAME}  "}, headers=owner)
    check("S1 duplicate after strip 409", strip_dup.status_code == 409,
          strip_dup.text[:200])

    rb = client.post(f"{BASE}/kbs", json={"name": KB_NAME_B}, headers=owner)
    kb_b = rb.json().get("id")
    kb_ids.append(kb_b)
    check("S1 distinct name 201", rb.status_code == 201 and kb_b > 0, rb.text[:200])

    # ---- S2: 文档就绪(真 pdf,worker 解析)----
    up = upload_doc(client, owner, kb_id, "m8青鸾号.pdf",
                    make_pdf_bytes("青鸾号高空气船简介", [FACT1, "青鸾号以氦气提供升力。"]))
    doc = wait_doc_status(client, owner, up["body"].get("id"))
    check("pdf upload & processed done",
          up["status"] == 201 and doc["status"] == "done" and doc["chunk_count"] > 0,
          f"{up} {doc}")

    # ---- S3: 零命中 → refused + 话术子串命中(M8 核心断言)----
    s3, s3_ok = ask_with_retry(
        client, owner, kb_id, ZERO_HIT_Q1, rerank=True,
        judge=lambda r: (r["done"] is not None and r["error"] is None
                         and r["done"].get("refused") is True
                         and REFUSAL_PHRASE in final_answer(r).strip()))
    check("S3 zero-hit(rerank on) refused + phrase contained", s3_ok, str(s3)[:300])

    s3b, s3b_ok = ask_with_retry(
        client, owner, kb_id, ZERO_HIT_Q2, rerank=False,
        judge=lambda r: (r["done"] is not None and r["error"] is None
                         and REFUSAL_PHRASE in final_answer(r).strip()))
    check("S3b zero-hit(rerank off) prompt fallback phrase contained", s3b_ok,
          final_answer(s3b)[:200])

    # ---- S4: 正常题不误伤(提示词收紧回归)----
    s4, s4_ok = ask_with_retry(
        client, owner, kb_id, NORMAL_Q1, rerank=True,
        judge=lambda r: (r["done"] is not None and r["error"] is None
                         and r["done"].get("refused") is False
                         and (r["citations"] or []) and len(r["citations"]) >= 1
                         and REFUSAL_PHRASE not in final_answer(r).strip()))
    check("S4 normal not refused, citations >= 1", s4_ok, str(s4)[:300])

    # ---- S5: 孤儿清理 dry-run(不真删)----
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.purge_orphan_evalsets"],
        capture_output=True, text=True, timeout=60,
    )
    ok5 = ("DRY-RUN" in proc.stdout
           and f"{kb_id}.json" not in proc.stdout
           and f"{kb_b}.json" not in proc.stdout)
    check("S5 purge dry-run runs, created kbs not orphaned", ok5,
          proc.stdout[-300:] + proc.stderr[-200:])

    summary_and_exit()
    cleanup_kbs(kb_ids)
    print(f"cleanup done: {kb_ids}")


if __name__ == "__main__":
    main()
```

注意:`summary_and_exit()` 在 FAIL 时 `sys.exit(1)` 会跳过 cleanup——真栈验收失败时残留 KB 由人工清(与 m7 同约定);成功路径必清。

- [x] **Step 2: 起真栈并运行**

前置(服务未起时):PG+Redis 服务 → `backend\start_dev.bat` → `backend\start_worker.bat`(beat 不需要)。
Run(backend 目录): `.venv\Scripts\python scripts\m8_acceptance.py`
Expected: `M8 ACCEPTANCE: 9/9 PASS`,末行 `cleanup done: [...]`。

- [x] **Step 3: 失败时处置**

任一 FAIL:按 systematic-debugging 排查(常见:S3 话术不精确→看模型原始输出;S5 导入失败→确认 scripts 包与 EVAL_DIR)。连续两次失败同一断言才 FAIL(脚本内置 attempts=2)。

- [x] **Step 4: Commit**

```bash
git add backend/scripts/m8_acceptance.py
git commit -m "test: m8 acceptance script (zero-hit substring + dup 409 + purge dry-run)"
```

---

### Task 9: 全量回归 + 孤儿真清 + 收口

**Files:**
- Modify: `backend/eval_sets/`(删除孤儿文件,git 状态呈现)
- Verify: 全部测试套件

**Interfaces:**
- Consumes: Task 7 脚本 `--apply`;真栈仍在运行(Task 8 起的)。

- [x] **Step 1: 后端全量**

Run(backend 目录): `.venv\Scripts\python -m pytest -q`
Expected: 全 PASS,数量 = M7 基线 137 + M8 新增(Task1 净 +1、Task2 净 +1、Task3 +3、Task7 +2 ≈ 144,以实际输出为准记录到执行记录)。

- [x] **Step 2: 前端全量**

Run(frontend 目录): `pnpm vitest run && pnpm build && pnpm lint`
Expected: 全 PASS(11 + Task4 2 + Task5 3 = 16),build/lint 0 错 0 警。

- [x] **Step 3: 孤儿真清(对运行库)**

Run(backend 目录,真栈保持运行): `.venv\Scripts\python scripts\purge_orphan_evalsets.py`
先看 dry-run 清单(预期含 KB 已删的 5/6/8/9.json 中的孤儿),确认后:
`.venv\Scripts\python scripts\purge_orphan_evalsets.py --apply`
Expected: 逐行 `deleted: N.json`;`git status` 显示 eval_sets 下删除。

- [x] **Step 4: 提交清理**

```bash
git add -A backend/eval_sets
git commit -m "chore: purge orphaned eval sets against running db"
```

- [x] **Step 5: 计划回填**

勾掉本文档全部 checkbox;文末追加"M8 执行记录"节(验收输出、测试计数、purge 清单、偏差与裁决)。commit:
```bash
git add docs/superpowers/plans/2026-09-17-airag-m8-refusal-hardening.md
git commit -m "chore: m8 execution record"
```

- [x] **Step 6: 用户走查**

浏览器过:创建重名库内联报错 / 下拉重名后缀 / 暗色代码块正则高亮(可在对话中让模型输出含正则的代码块验证)/ 对话页回归。走查增量照 M7 惯例记入执行记录。

---

## M9 候选(执行后移交)

KB 删除端点(owner/admin,级联文档+chunk+权限+向量+eval_sets 钩子)、DB 唯一约束+存量去重、包裹型拒答 LLM 二审(若子串+提示词实证不足)、评估入库/多跳并行/MinerU 本地化/LDAP(等输入)。

---

## M8 执行记录(2026-09-17)

**验收**:`scripts/m8_acceptance.py` 真智谱 **11/11 PASS**(health/账号晋升/建库 201/重名 409/strip 重名 409/异名 201/pdf 本地解析 done/S3 零命中 rerank-on refused+子串/S3b 零命中 rerank-off 子串/S4 正常题不误伤+引用≥1/S5 purge dry-run);成功路径自动清库(KB 17/18,FK 顺序)。

**测试计数**:后端 pytest **144P**(M7 基线 137 + 净 7:T1 +1/T2 +1/T3 +3/T7 +2);前端 vitest **16P/7 文件**(11 + T4 2 + T5 3,最终 HEAD 复跑);build/lint 0-0。

**purge 落地**:孤儿 6.json/8.json 已 `--apply` 删除(b5f9d86);5.json/9.json/README.md 保留——审查者独立 SELECT 核验 KB 集合 {1,2,3,4,5,7,9} 存活。

**偏差与裁决**(SDD ledger 同步):
1. 计划笔误:Task 8 预期"9/9"实为脚本 11 项断言,以脚本为准记 11/11。
2. Task 8 脚本两处缺陷由实现者修复(均 script-only,复审通过):①brief 的薄文本 PDF(<50 字/页)被管道自动路由 MinerU 云 OCR 且其 OSS 上传主机当前网络不可达→补一段文本保持本地解析路径,断言未动;②promote_roles/cleanup_kbs 复用池化 SessionLocal 跨 asyncio.run 崩溃("Event loop is closed")→改 per-call NullPool 引擎(app/workers/pipeline.py 同款)。
3. Task 4 修复环 1 轮(8b0339d):Task 9 lint 门拦下 3 处裸 vi.fn() 违反 vitest/require-mock-type-parameters→补类型参数(镜像 kbApi 真实签名),vitest 2/2+全量 lint/build 复绿。
4. Task 4 测试适配(生产代码与计划逐字一致):mount 需 EP 全局插件(裸 mount 组件不解析,RED 仍命中预期断言);EP form-item 错误显示 100ms 防抖→vi.waitFor。
5. Task 1 brief"替换为以下三个"实为两函数三场景(审查确认无缺失),按代码块执行。

**环境备注**:mineru.oss-cn-shanghai.aliyuncs.com 当前 TLS 握手超时(非 M8 缺陷,M5 上午真调尚通)——后续云 OCR 上传会失败直至网络恢复。

**实现提交**(10 个,不含 spec/plan 文档):de9f069/50a67d5/3551e39/a645284/80bc812/62462b5/9a3f2dd/c8c3994/8b0339d/b5f9d86。

**终审增量**(全分支审查 Ready to merge):1 条 Important 已修——kbs.py 重名查询 `scalar_one_or_none()` 在库中同名≥2 条时抛 MultipleResultsFound(500)→改 `.scalars().first()` + 回归测试(种两条同名直接 ORM 提交再 POST 断言 409),commit 4488e29,test_kbs 11P,scoped 复审通过。测试计数更新:后端 **145P**(144+1)。Minors 分流维持递延(strip 冗余/子串误报面 spec 接受/check-then-insert M9/维护脚本边角/测试断言面/脚本全角标点)。

**待办**:用户浏览器走查(重名内联报错/下拉后缀/暗色正则高亮/对话页回归)→ 通过后收官推送。
