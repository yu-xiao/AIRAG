# AIRag M25 实施计划:文案补全 + 编码注释与 BOM 测试 + 验收脚本清理

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 清掉 M24 回流的三个卫生小项:拒绝文案补 `.htm/.jpeg` 别名、`_read_text` 编码歧义分析注释 + utf-8-sig 单测、m23/m24 验收脚本 finally 幂等清理。

**Architecture:** 纯卫生包——前端一处文案;后端两行注释 + 两个测试;两个脚本加共用形制的 `_cleanup` 助手(API 优先、SQL 兜底、finally 调用)。无行为变化。

**Tech Stack:** Vue3 + vitest / pytest / 纯脚本。

**Spec:** `docs/superpowers/specs/2026-09-30-airag-m25-copy-hygiene-design.md`

## Global Constraints

- 测试一律 `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest …`;vitest 一律 `cd E:\Projects\AIRag\frontend && pnpm vitest run …`(Windows CMD;`&&` 可用)。
- 基线只增不减(pytest 513P / vitest 86 / build 零错)。
- 每任务一个 commit,conventional 消息照 brief。

---

### Task 1: 拒绝文案补别名

**Files:**
- Modify: `frontend/src/pages/DocsPage.vue:72`(拒绝文案)
- Test: `frontend/src/pages/__tests__/DocsPage.spec.ts`(追加 1 断言组)

**Interfaces:**
- Consumes: 既有 ALLOWED_EXTS 常量(18 项)。
- Produces: 无(文案与门清单对齐)。

- [ ] **Step 1: 写失败测试**

在 DocsPage.spec.ts 追加(用 vite 的 `?raw` 导入源码文本,断言文案串含两别名):

```typescript
import DocsPageSource from '@/pages/DocsPage.vue?raw'

// (放在 describe 内)
  it('拒绝文案与 18 项门清单一致(含 .htm/.jpeg 别名)', () => {
    expect(DocsPageSource).toContain('.html/.htm')
    expect(DocsPageSource).toContain('.jpeg')
  })
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd E:\Projects\AIRag\frontend && pnpm vitest run src\pages\__tests__\DocsPage.spec.ts`
Expected: FAIL(现文案无 `.html/.htm` 与 `.jpeg`)。

- [ ] **Step 3: 实现**

第 72 行文案改为(仅追加两别名):

```
不支持的文件类型 ${ext},仅支持文档:.pdf/.docx/.xlsx/.pptx/.txt/.md/.csv/.json/.html/.htm 与图片:.jpg/.jpeg/.png/.bmp/.tif/.tiff/.webp/.gif
```

- [ ] **Step 4: 跑测试**

Run: `cd E:\Projects\AIRag\frontend && pnpm vitest run src\pages\__tests__\DocsPage.spec.ts`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/DocsPage.vue frontend/src/pages/__tests__/DocsPage.spec.ts
git commit -m "chore(ui): rejection copy carries all 18 extensions incl aliases (m25)"
```

---

### Task 2: 编码歧义分析注释 + utf-8-sig 单测

**Files:**
- Modify: `backend/app/services/parsing/text_parser.py:10-23`(_read_text 注释)
- Test: `backend/tests/test_parsers.py`(新增 2 用例)

**Interfaces:**
- Consumes: 无。
- Produces: 无(注释 + 测试;行为零变化)。

- [ ] **Step 1: 写失败测试**

`backend/tests/test_parsers.py` 追加:

```python
def test_txt_strips_utf8_bom(tmp_path):
    p = tmp_path / "bom.txt"
    p.write_bytes("﻿带 BOM 的段落一\n\n段落二".encode("utf-8-sig"))
    result = get_parser(".txt").parse(p)
    assert [b.content for b in result.blocks] == ["带 BOM 的段落一", "段落二"]


def test_txt_pure_ascii_uses_utf8_path(tmp_path):
    p = tmp_path / "ascii.txt"
    p.write_bytes(b"alpha paragraph\n\nbeta paragraph")
    result = get_parser(".txt").parse(p)
    assert [b.content for b in result.blocks] == \
        ["alpha paragraph", "beta paragraph"]
```

(第一个用例的字符串以真实 BOM 字节写入——`encode("utf-8-sig")` 自带;
断言内容不含 `\ufeff` 即证明剥离。)

- [ ] **Step 2: 跑测试**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_parsers.py -k "bom or ascii" -v`
Expected: **直接 PASS**(行为已在 M24 实现,本任务锁分支)——属回归锁
性质,报告如实记录(与 M24 T5 同先例:RED 已在原实现轮演示)。

- [ ] **Step 3: 实现(注释)**

`_read_text` docstring 在「两者皆败」句后补:

```python
    """utf-8(容 BOM)优先,回退 gb18030(国内存量 txt/csv 的 GBK 现实);
    两者皆败 → ValueError:流水线按损坏文件统一语义收口(3 次快速重试后 failed)。

    编码歧义不做启发式(M25 裁定):GBK 双字节(首 81-FE,次 40-FE)要整体
    落进合法 UTF-8 多字节窗口(首 C2-DF、次 80-BF 等)须逐对字节恰好对齐,
    真实中文文档全篇命中的概率可忽略;纯 ASCII 两解码等价无歧义;仅病态
    超短串可能误判,接受——chardet/双语评分的复杂度不值。"""
```

- [ ] **Step 4: 跑测试 + 相邻**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m pytest tests\test_parsers.py tests\test_pipeline.py -q`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/parsing/text_parser.py backend/tests/test_parsers.py
git commit -m "test(parsing): lock utf-8-sig/ascii branches; document no-heuristic ruling (m25)"
```

---

### Task 3: 验收脚本 finally 幂等清理

**Files:**
- Modify: `backend/scripts/m23_acceptance.py`(102 一带 finally)
- Modify: `backend/scripts/m24_acceptance.py`(189 一带 finally)

**Interfaces:**
- Consumes: 两脚本既有的 kb_ids/endpoint_ids/run_ids 收集变量与 sql() 助手(m23 有 sql;若 m24 缺则补同款)。
- Produces: 幂等 `_cleanup`(API 优先 try/except 逐个吞错,SQL 按 FK 序兜底),finally 调用;正常路径检查不变。

- [ ] **Step 1: 实现(m23)**

在 m23 脚本 main() 的 try 之前定义:

```python
            async def _cleanup(c, jwt):
                """幂等兜底:API 优先(逐个吞错),SQL 按 FK 序清残;
                正常路径的 204 检查在 try 内不变,这里只兜早失败。"""
                for e in endpoint_ids:
                    try:
                        await c.delete(f"{API}/admin/webhooks/{e}",
                                       headers=jwt)
                    except Exception:
                        pass
                for k in kb_ids:
                    try:
                        await c.delete(f"{API}/kbs/{k}", headers=jwt)
                    except Exception:
                        pass
                for rid in run_ids:
                    try:
                        await sql("DELETE FROM eval_runs WHERE id = :i",
                                  {"i": rid})
                    except Exception:
                        pass
                for k in kb_ids:  # SQL 兜底(API 删失败时)
                    try:
                        await sql(
                            "DELETE FROM chunks WHERE kb_id = :k", {"k": k})
                        await sql(
                            "DELETE FROM documents WHERE kb_id = :k", {"k": k})
                        await sql(
                            "DELETE FROM eval_questions WHERE kb_id = :k",
                            {"k": k})
                        await sql(
                            "DELETE FROM kb_permissions WHERE kb_id = :k",
                            {"k": k})
                        await sql(
                            "DELETE FROM knowledge_bases WHERE id = :k",
                            {"k": k})
                    except Exception:
                        pass
                for e in endpoint_ids:
                    try:
                        await sql(
                            "DELETE FROM webhook_endpoints WHERE id = :e",
                            {"e": e})
                    except Exception:
                        pass
```

原 `finally:` 块改为 `finally: await _cleanup(c, admin)`(run 行 SQL 删除
从 finally 挪入 _cleanup,行为超集)。

- [ ] **Step 2: 实现(m24)**

m24 脚本同款 `_cleanup`(其收集变量为 kb_ids,无 run_ids/endpoint_ids
列表——端点 id 在局部变量 ep;将 ep 纳入一个 endpoint_ids 列表统一管理,
`finally: await _cleanup(c, admin)` 替换 `finally: pass`)。

- [ ] **Step 3: 验证**

Run: `cd E:\Projects\AIRag\backend && .venv\Scripts\python -m py_compile scripts\m23_acceptance.py scripts\m24_acceptance.py && echo COMPILE_OK`
Expected: COMPILE_OK。(脚本无测试面;结构自查:finally 覆盖、检查项不减。)

- [ ] **Step 4: Commit**

```bash
git add backend/scripts/m23_acceptance.py backend/scripts/m24_acceptance.py
git commit -m "chore(test): idempotent finally cleanup in m23/m24 acceptance scripts (m25)"
```

---

## 收尾(控制端执行,不派子代理)

1. **全量门禁**:pytest -q(≥515P:513+T2 两例)、vitest(≥87)+ build 零错。
2. **终审 whole-branch**(65d74a8 起,小 diff 快审)+ M26 候选回流。
3. **执行记录** + 记忆(推送等走查;文案截图走查项)。

---

## 执行记录(2026-09-30,单批实现 + 单轮合并评审 Ready-merge-Yes)

**交付(65d74a8..87e9014,5 提交含 spec/plan,main 本地未推)**:
- 41a2b38 spec / 3efad8a plan
- c2b526d T1 拒绝文案补 .htm/.jpeg(与 18 项门清单集相等;?raw 源码断言,
  .html/.htm 斜杠形为精确判别子串)
- 3557786 T2 编码歧义 no-heuristic 裁定入 docstring(GBK 双节 vs UTF-8 窗口
  概率论证)+ utf-8-sig BOM 剥离/纯 ASCII 两例回归锁(即时 PASS 属预期,
  M24 T5 先例)
- 87e9014 T3 m23/m24 验收脚本 finally 幂等 _cleanup(API 优先逐项吞错 + SQL
  FK 序兜底;admin 预绑 None 防登录失败击穿 finally;m24 补 sql() 同款)

**门禁**:pytest 513→**517P/0F**、vitest 86→**87/87**、build 零错。无真栈
验收(纯卫生包,计划明文)。

**评审(合并制,65d74a8..87e9014)**:Ready to merge — Yes,零 Critical/
Important。实现者披露三偏差全证实:m24 无端点可清(计划记忆过时)、m24 补
sql()、admin=None 预绑;并**抓掉计划自身的坑**(BOM 测试字面量内嵌 U+FEFF
会双 BOM 假失败,已静默修正并披露)。Minor 三条全 cosmetic(?raw 断言中
.jpeg 子断言不判别、SQL 兜底正常路径也跑、参数遮蔽)——不动。

**M26 候选**:大件全挂起(A2A 触发条件在案/MinerU 本地化/LDAP 等输入);
老 Office/WPS 转换层等需要再立项;小项无积压——**M24+M25 后卫生队列再次
清零**。

**栈态**:无需重启(前端文案热更、后端仅注释/测试/脚本)。

