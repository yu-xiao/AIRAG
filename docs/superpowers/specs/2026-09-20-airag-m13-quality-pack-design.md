# AIRag M13 设计:质量包 + 治理快修(拒答二审、评估入库、ask 提速、多跳并行、KB 重命名)

## 背景与动机

M9~M12 建成了完整的 Agent 对接与权限治理面;M13 回到**质量与效率**。用户拍板(2026-09-20,范围卡片未答,按推荐默认执行,可在本审阅中调整):**质量包 + 治理快修包**;部署/集成向(MinerU 本地化/出站/A2A/LDAP)顺延 M14(LDAP 仍等输入)。

当前缺口(探索实证):

- **拒答检测只有一条子串**(`nodes.py:110`,`知识库中未找到相关内容` in answer):措辞改写的包裹型拒答("知识库中暂无该资料")得 `refused=False`,引用照常展示。M8 起持续列为优先候选,从未实现。
- **评估只打印不入库**:`eval_retrieval.py`/`eval_generation.py` 结果仅 stdout,跨版本质量对比无从谈起;eval_sets 无 `reference_answer` 字段。
- **首 token 延迟 ~32s**:全部前置工作(rewrite/grade/decompose/检索)都发生在流式首 token 之前;快乐路径每问 3 次 LLM(rewrite+grade+generate),其中 grade 在证据明确时是可省的;零命中时 grade 对空候选仍发起一次 LLM 调用。
- **多跳子问题串行检索**(`nodes.py:40-43` for 循环):≤3 个子问题串行各跑一次 hybrid_search(embedding+SQL),是零命中路径的大头。
- **M12 终审留下 7 条 minor** + **KB 重命名端点**(M12 唯一约束已为其铺路,从未有 rename)。

## 目标

1. 拒答 LLM 二审:启发式触发 + 单次 LLM 判定,包裹型拒答不再漏标;fail-open 不阻断回答
2. 评估入库:`eval_runs`/`eval_items` 两表 + CLI `--save`;eval_sets 可选 `reference_answer`,有则追加 reference 对比评审;`scripts/eval_runs.py` 查历史
3. ask 提速:零命中 grade 短路(不再对空候选烧 LLM)+ 证据强一致时跳过 grade LLM(配置开关);快乐路径 LLM 3→2 次
4. 多跳并行:子问题检索 `asyncio.gather`(session-per-query),零命中路径检索耗时 3×→1×
5. KB 重命名:`PUT /api/kbs/{id}`(name/description,owner/admin,重名 409)+ KbPage 编辑对话框
6. M12 终审七小项全清(见 E 节表)
7. 验收:pytest 全绿(基线 276P)+ eval CLI 真栈 `--save` 演示 + 延迟前后对比记录 + 前端 vitest/build

## 非目标(明确不做)

- 评估结果的 Web 管理界面(CLI 查询即可,UI 候选 M14)
- 出站集成、A2A、MinerU 本地化、LDAP/SSO(M14 候选;LDAP 仍等用户输入)
- 拒答时清空 citations(维持现状:前端按 refused 隐藏;二审修复误判后该问题自然消解)
- rewrite 与 retrieve 的投机并行(复杂度/收益不划算)
- 更换 CHAT_MODEL / grade 提示词大改(只做结构性省略,不动提示词语义)
- KB 删除/重命名暴露到 Agent 面(维持 M12 拍板:治理操作 Web only)

## A. 包裹型拒答 LLM 二审

**触发(两级门,先便宜后准)**:`generate_node` 内,子串检测未命中且 `REFUSAL_RECHECK_ENABLED`(默认 True)时,先过启发式:

```python
_REFUSAL_SIGNALS = ("未找到", "没有找到", "没找到", "无法回答", "无法提供",
                    "暂无", "无相关", "知识库中没", "抱歉", "超出")

def _looks_like_refusal(answer: str, has_hits: bool) -> bool:
    a = answer.strip()
    return (not has_hits) or len(a) <= 40 or any(s in a for s in _REFUSAL_SIGNALS)
```

任一命中(零命中、超短答案、含拒答信号词)才发起二审 LLM 调用——正常回答零额外成本。

**二审**:`services/chat_graph/nodes.py` 新增 `_recheck_refusal(llm, question, answer)`:

```
系统提示:你是拒答判定器。判断下面这个"答案"是否实质上在表示知识库无法回答该问题
(明确表示没有相关资料/无法回答/建议查阅其他渠道等),而非给出了实质内容。
答案确实给出与问题相关的事实内容时判 false。
只输出 JSON:{"refused": true|false, "reason": "<一句话>"}
```

- 复用 `_extract_json` 剥围栏(与 eval_judge 同法);**任何异常/解析失败 → 保持子串结果(fail-open,不阻断、不误杀正常回答)**
- 二审结果 `refused=True` 时覆盖子串结果;`state` 不加新键(refused 语义不变)
- conftest 钉死 `REFUSAL_RECHECK_ENABLED=false`(同 AGENTIC_* 模式),二审用例内 monkeypatch 打开并注入 FakeListChatModel
- 测试:包裹变体("很抱歉,知识库中暂无该资料"→True)、近似措辞("根据常识…虽然库内没有相关信息"→True)、正常短答案("预算为三千万元"→False)、LLM 坏输出 fail-open、启发式不触发(正常长答案零调用,monkeypatch llm 断言未调)

**出口零改动**:refused 经 state 流向 Web SSE/落库/导出/REST/MCP/agent_facade,全链路既有(M12 探索已列全)。

## B. 评估入库

**模型(两张表,一次迁移 `e5f6a7b8c9d0_m13_eval_tables`,down_revision=`d4e5f6a7b8c9`)**:

```python
class EvalRun(Base, TimestampMixin):          # eval_runs
    id: Mapped[int] PK
    kb_id: Mapped[int]            # 无 FK:KB 删除后历史评估保留(spec C 语义一致)
    mode: Mapped[str]             # retrieval | generation
    summary: Mapped[dict] = mapped_column(JSON)   # {hit_at_k, mrr, keyword_recall,
                                                  #  faithfulness_avg, relevancy_avg,
                                                  #  reference_avg, refused_count, ...}
    item_count: Mapped[int]

class EvalItem(Base):                          # eval_items
    id: Mapped[int] PK
    run_id: Mapped[int] = mapped_column(ForeignKey("eval_runs.id", ondelete="CASCADE"), index=True)
    question: Mapped[str] = mapped_column(Text)
    expect_doc_ids: Mapped[list | None] = mapped_column(JSON)
    expect_keywords: Mapped[list | None] = mapped_column(JSON)
    answer: Mapped[str | None] = mapped_column(Text)      # generation 模式留存答案
    refused: Mapped[bool | None]
    hit_at_k / mrr / keyword_recall / faithfulness / relevancy / reference_score:
        Mapped[float | None]                                   # 按模式可空
```

ORM relationship:`EvalRun.items` cascade delete-all(无 beat 任务,数据量小,人工/后续清理)。

**CLI**:
- `eval_retrieval.py` / `eval_generation.py` 增 `--save`:跑完后落 `EvalRun`+逐题 `EvalItem`(同一汇总口径,stdout 输出不变,末尾加一行 `saved: run_id=N`)
- `scripts/eval_runs.py`(新):`py -m scripts.eval_runs --kb N [--mode M] [--last 10]` → 表格打印历史 run(id/mode/item_count/汇总/时间);`--json` 支持
- `eval_metrics.load_eval_set` 兼容新增可选字段 `reference_answer`(旧文件零影响;`eval_sets/README.md` 增补字段说明)
- **reference 对比评审**:`eval_judge.py` 新增 `reference_score(answer, reference_answer)`:judge 提示"对照参考答案判断回答与参考的一致性(关键事实遗漏/冲突扣分),只输出 JSON {score, reasons}";`eval_generation.py` 在题目带 `reference_answer` 时调用并入 summary(`reference_avg`)与 item;无则跳过

## C. ask 延迟优化(结构性省 LLM,不动提示词)

1. **零命中 grade 短路**:`grade_node` 入口 `if not hits: return {"verdict": "insufficient"}`——不再对空候选发起 LLM(route_after_grade 对空本就走 transform/decompose,行为不变,省 1 次调用)
2. **强一致跳过 grade LLM**:`grade_node` 在 `GRADE_CONFIDENT_SKIP_N`(默认 3,0=关闭)≥ `source=="both"` 的命中数时直接 `{"verdict": "sufficient"}`——向量+关键词双半场同时命中的 chunk 是 RRF 加分项(searcher both +0.001),≥3 条双中说明检索可靠;此为启发式门,配置 `GRADE_CONFIDENT_SKIP_N: int = 3`(0=关闭)
   - 保守性:跳过的只是"检索充分性"判定,generate 仍只依据同批资料回答;误判充分最坏效果 = 少走一次 transform/decompose 兜底,答案仍受提示词拒答防线约束
   - conftest 钉 0(既有 grade 行为用例不受影响);专项用例断言"≥3 both → grade LLM 未调(monkeypatch)"
3. **效果度量**:不改 m12 验收;执行记录里用 eval_generation CLI 的 elapsed 输出 + dev 栈一次手工 SSE 首 token 实测,记录前后对比数字(验收非硬门槛,记录为证)

**预期**:快乐路径(多轮、检索正常)LLM 3→2 次(grade 省去),首 token 降 ~25-35%;零命中路径 LLM 5→3 次(空 grade 短路)+ 子问题检索并行(见 D)。

## D. 多跳子问题并行检索

`retrieve_node` 的串行循环(`nodes.py:40-43`)改为:

```python
async def _search_one(query: str, kb_ids: list[int]) -> list[SearchHit]:
    async with SessionLocal() as db:            # session-per-query:并发不可共享会话
        return await hybrid_search(db, kb_ids, query)

per_query = await asyncio.gather(*(_search_one(q, state["kb_ids"]) for q in queries))
```

- 其后 44-57 的轮转合并与 chunk_id 去重**与查询顺序无关,零改动**;单查询路径(常态)结构不变(gather 单元素即原样)
- 三个子问题各含 1 次 embedding HTTP + 1 轮 SQL:串行 3× → 并行 ≈1×(受智谱并发限流约束,失败语义与现状一致——hybrid_search 自身异常上抛不变)
- 测试:monkeypatch `_search_one`/hybrid_search 断言 gather 并发(记录进入时间戳重叠)或最小化:断言多查询结果合并正确性与既有一致(合并逻辑不变,既有用例回归)+ 新增并行路径不回归

## E. 治理快修清单(M12 终审七小项)

| # | 项 | 落点 |
|---|---|------|
| ① | kb_ops busy 409 统一 DocOpError | `kb_ops.py` 的 HTTPException(409) → `DocOpError("busy", 409, "knowledge base has documents being processed")`(全局 handler 已注册,Web 面 HTTP 报文不变) |
| ② | 413 预检后移 | `documents.py`/`agent.py` 上传端点:Content-Length 预检移到可见性/权限守卫**之后**(越界库+超限 body → 404/403 而非 413,不泄露) |
| ③ | ApiKeyOut.kb_scope 默认值 | `kb_scope: list[int] | None = None`(消除第三方构造必填坑) |
| ④ | `_permitted_kb_ids` 条件重排 | denied 判定改为 不存在 → 界外(scope 先于 perm)→ 无 perm:界外库不再烧 perm DB 查询 |
| ⑤ | KeysPage loadKbOptions 收敛 | watch 仅在新值非空且 ≠ 旧值时重载(openCreate 置空路径只清选项不重载,消除双触发) |
| ⑥ | m12 验收审计配对断言 | `m12_acceptance.py` ⑦ 改拉 (action,target) 行后按对断言(`{("kb_delete", f"kb:{id}"), ("agent.upload_document", f"doc:{id}")} <= pairs`) |
| ⑦ | 测试加固 | test_auth_keys_api 未用 `kb_id` 删除;JWT ask 用例补 quota_check 未调用断言(monkeypatch 记录);mcp_server 四处工具描述括注排版统一(单行) |

## F. KB 重命名端点 + 前端

- `PUT /api/kbs/{kb_id}`,body `{"name": str|null, "description": str|null}`(至少一项;name strip 后空 → 422;均省略 → 422):权限同删除(不可见 404 / 可见非 owner/admin 403 / 通过 → 200 返回 KBOut);重名 → 应用层 409(复用 create 的 SELECT 检查)+ `IntegrityError` 兜底 409(rollback);审计 `kb_update`(detail: name 变更前后)
- **实现内聚**:`services/kb_ops.py` 增 `rename_knowledge_base(db, kb, *, name, description, username)`(唯一性检查+审计+commit),端点薄壳——与删除同文件同风格
- 前端:KbPage owner 卡片增「重命名」按钮(与删除同 `v-if`)→ 复用样式新增编辑对话框(name 必填/description 可选,409 内联错误同创建对话框模式)→ 成功刷新;`kbApi.rename(id, payload)`
- 测试:端点权限矩阵(404/403/200)、重名 409、strip 空名 422、审计断言;vitest:编辑对话框 409 内联错误/提交体

## 配置项

| 新增 | 默认 | 说明 |
|---|---|---|
| `REFUSAL_RECHECK_ENABLED` | `True` | 拒答 LLM 二审总开关(conftest 钉 False) |
| `GRADE_CONFIDENT_SKIP_N` | `3` | ≥N 条 source=="both" 跳过 grade LLM;0=禁用(conftest 钉 0) |

`.env.example` 增补两行。

## 测试与验收

### pytest(基线 276P)

- 二审:包裹/近似措辞/正常答案/fail-open/启发式不触发(零调用断言)——`test_chat_graph.py` 扩展
- 入库:模型迁移(create_all 真源)、`--save` 落库断言(run/item 行数与 summary)、reference_score 判定(Fake LLM)、eval_runs 查询纯函数
- 提速:零命中短路(verdict=insufficient 且 LLM 未调)、both-confident 跳过(LLM 未调 + 行为 sufficient)、配置 0 时旧行为回归
- 并行:多查询合并结果与串行等价(既有用例)+ `_search_one` 并发记录断言
- 快修:①busy DocOpError 出口、②预检顺序(越界+超限→404/403)、③构造默认、④界外不查 perm(记录 get_kb_perm 调用)、⑦三项
- 重命名:全矩阵 + 审计 + IntegrityError 兜底
- 前端 vitest:KbPage 编辑对话框、KeysPage watch 收敛既有用例回归

### 无头验收 `scripts/m13_acceptance.py`(真栈,8001)

真 LLM 走两条拒答题(库内无答案的问题):一条诱导包裹型措辞、一条零命中——断言 refused=true(二审生效);正常事实题 refused=false 且引用正确;`eval_retrieval --save` + `eval_generation --save` 真跑后 `eval_runs` 查询出两条记录(reference_answer 题目含一条);KB 重命名 200/重名 409;记录一次 SSE 首 token 与 ask elapsed 数字入执行记录。

## 风险与权衡

- **二审误杀正常回答**:judge 提示已限定"确实给出实质内容判 false";fail-open 兜底;总开关可关。误杀代价 = 正常答案被标 refused(前端隐藏引用、API 语义错位),验收两条正常题护栏
- **both-confident 跳过误判充分**:最坏少走一次兜底,提示词拒答防线仍在;阈值 3 保守;可配置 0 禁用
- **并行检索撞智谱 embedding 限流**:gather 3 并发远低于配额;失败语义与串行一致(单查询异常上抛)
- **EvalRun 无 FK 的孤儿性**:KB 删除后 run 保留属设计意图(历史质量记录);kb_id 冗余为 int,查询按需过滤
- **grade 短路对 M6 多跳触发条件的影响**:零命中路径原本 grade→insufficient 才进 decompose,短路后 verdict 仍为 insufficient,路由行为逐字不变(既有 test_chat_graph 多跳用例回归护栏)

## M14 候选(本里程碑分流)

- 评估 Web 管理界面(趋势图/对比)、出站集成、A2A、MinerU 本地化、LDAP/SSO(等输入)
- rewrite/retrieve 投机并行、citations 拒答语义收紧(出口清空)
