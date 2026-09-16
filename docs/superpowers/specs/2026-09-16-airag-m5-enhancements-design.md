# AIRag M5 增强设计(OOCR / Agentic / 审计 / 评估)

日期:2026-09-16 | 状态:待用户审阅 | 前置:M4 已关账(main=81b0817)

## 0. 背景与范围

spec §9-M5(按需)经用户勾选定版,本里程碑做:

| # | 项 | 一句话 |
|---|---|---|
| A | Agentic 检索增强 | 查询改写(多轮指代消解)+ Corrective RAG(检索自评、不足重检一次),LangGraph 图上加节点 |
| B | MinerU 云 API OCR | 扫描件 PDF/图片走 MinerU 云 API 解析,auto 检测 + 上传可覆盖 |
| C | 审计日志 + 导出 | 关键操作落库 + admin 审计页;会话导出 markdown |
| D | 检索评估集 | 每 KB golden 问答对(JSON)+ hit@k/MRR/关键词 recall CLI |
| E | Minor 清偿 + checkpointer | M4 交接清单 9 项 + ask.py 接线 checkpointer |

**不在本里程碑**:LDAP/SSO(仍无输入,维持本地账号)、RAGAS 框架(用轻量自研指标替代,faithfulness 类 LLM-judge 指标留 M6)、MinerU 本地部署(云 API 先行,接口抽象留换型余地)、生成式多跳(仅做一次有界重检)。

**默认决策记录**(AskUserQuestion 未获答复,按推荐项执行,如需变更在计划前提出):
1. agentic 开关 = env 全局默认开(`AGENTIC_REWRITE_ENABLED` / `AGENTIC_CRAG_ENABLED`),不加前端请求级开关。
2. 评估集 = JSON 文件 + CLI,不建表不做界面。
3. OCR 触发 = auto 检测 + 上传参数 `ocr: auto|force|off` 可覆盖(默认 auto)。
4. ask 入审计 = 记摘要(问题前 50 字 + kb_ids + 会话 id)。

## 1. Agentic 检索增强

### 1.1 现状与约束

- 图拓扑( graph.py ):`START → retrieve → rerank → generate → END`,rerank 未开/未请求时节点直通返回 `{}`(M4 模式)。
- `ask.py` 用 `graph.astream_events(version="v2")` 逐 token 转 SSE 帧,过滤条件仅为 `event == "on_chat_model_stream"`——**新增任何 LLM 节点的流式事件都会混入答案**。
- `generate_node` 不带会话历史;`retrieve_node` 与 `generate_node` 共用 `state["question"]`。
- `ask.py:51` `build_graph()` 未传 checkpointer(get_checkpointer 单例已就绪未接线)。

### 1.2 图拓扑

```
START → rewrite → retrieve → rerank → grade → cond_edge → generate → END
                       ↑                    │
                       └── transform ←──────┘
```

- **拓扑恒定**:rewrite/grade 关闭时直通返回 `{}`(复制 rerank 直通模式),`test_graph_always_has_*` 类拓扑断言稳定。
- **cond_edge**(grade 后):`grade == "insufficient" and retries < 1` → `transform`;否则 → `generate`。重检有界一次,防环。
- **关键职责分离**(自审修正:计数与换词不能放在 grade 自己身上——节点更新对同一判定立即可见,会让重试条件永假):grade 只裁决并**提议**新查询(`proposed_query`);`transform`(无 LLM 的小节点)负责应用 `proposed_query` 到 `search_query` 并把 `retries` +1,然后回 `retrieve`。第二轮 grade 即便仍 insufficient,`retries==1` 使 cond_edge 直走 generate。

### 1.3 状态与节点

`ChatState` 新增字段(`total=False` 不破坏存量):

```python
history: list[dict]        # [{role, content}] 最近 6 条,ask.py 从 DB 读入
search_query: str          # 检索实际使用的查询(改写后或原样)
proposed_query: str        # grade 提议的重检查询(transform 应用后失效)
grade: str                 # "sufficient" | "insufficient"
retries: int               # CRAG 已重检次数
```

- **rewrite_node**:输入 `question` + `history`,一次 LLM 调用输出独立化检索查询(指代消解,如"它的负责人"→"XX 项目的负责人");输出 `{"search_query": <改写>, "retries": 0, "grade": ""}`(每轮显式重置,兼容 checkpointer 残留状态)。关闭/无 history 且无需改写时直通(`search_query = question`)。
- **retrieve_node** 改读 `state["search_query"]`;**generate_node 仍读 `state["question"]`**(生成忠实用户原话)。
- **grade_node**:一次 LLM 调用,输入 question + top hits 摘要,输出 JSON `{"verdict": "sufficient|insufficient", "query": "<改写检索词>"}`;返回 `{"grade": verdict, "proposed_query": query}`(insufficient 时),**不动 retries/search_query**。关闭时直通。解析失败按 sufficient 处理(降级不阻塞回答)。
- **transform_node**(无 LLM):`{"search_query": state["proposed_query"], "retries": state.get("retries", 0) + 1}`。
- **hits 语义**:重检后 hits 被新结果整体替换,citations 基于替换后 hits,无需合并。

### 1.4 SSE token 过滤(tag 方案)

- `generate_node` 内 `llm.ainvoke(messages, config={"tags": ["answer"]})`;
- `ask.py` 事件过滤改为 `ev["event"] == "on_chat_model_stream" and "answer" in (ev.get("tags") or [])`;
- rewrite/grade 的 `ainvoke` 不带 tag,其内部流式事件被过滤,SSE 契约(token→citations→done)不变。

### 1.5 开关与 checkpointer

- env:`AGENTIC_REWRITE_ENABLED: bool = True`、`AGENTIC_CRAG_ENABLED: bool = True`;测试与验收可关闭对比。
- `ask.py` 改为 `graph = build_graph(checkpointer=await get_checkpointer())`(惰性单例,首次调用 `setup()`);thread_id=conversation_id 既有 config 不变。
- 断连语义不变:checkpointer 只持久化图状态,消息落库逻辑不动("只保留已落库 user 消息")。

## 2. MinerU 云 API OCR

### 2.1 触发与决策

- 上传表单新增可选字段 `ocr: auto|force|off`(默认 auto),存 `documents.ocr_mode`;`MINERU_API_TOKEN` 为空 ⇒ OCR 整体视为 off(行为同 M4,兼容无 token 部署)。
- `services/parsing/ocr.py`:

```python
def maybe_ocr(path, ext, ocr_mode, primary: ParseResult) -> ParseResult:
    """决策 + 调用;不满足条件原样返回 primary。"""
```

  - auto:`ext == ".pdf"` 且 primary 文本稀薄(总字符数 / max(page_count,1) < 50)→ MinerU;图片扩展名(`.jpg/.png`)→ MinerU(无文本层);docx/xlsx 永不触发。
  - force:pdf/图片直接 MinerU;off:直通。
  - 阈值 `OCR_THIN_CHARS_PER_PAGE = 50` 进 config(env 可调)。
- 调用点:`workers/pipeline.py` 的 `_run` 在 `get_parser().parse()` 之后、`split_blocks` 之前插入 `result = maybe_ocr(...)`,回填 `doc.ocr_used`。

### 2.2 MinerU 客户端

- `services/parsing/mineru_client.py`,同步实现(Celery worker 阻塞合理):
  1. `POST` 文件上传提交任务(带 token);
  2. 轮询任务状态(间隔 5s,上限 60 次 = 5 分钟);
  3. 取结果 URL 拉 markdown 文本。
- 返回 markdown → blocks:按空行分段;含 `|---|` 相邻行的段 `is_table=True`;`page_no=None`(v1 不逐页,MinerU 结果页码在 middle json,v2 再取);`page_count` 置 None。
- 失败路径:429/网络错/超时 → raise ⇒ 现有 celery retry(3 次退避)⇒ failed + error_msg 落库,前端可见。OCR 发生在 chunk 写入之前,celery 层重试安全(不会双写 chunks)。
- 上传白名单 `ALLOWED_EXTS` 增 `.jpg`/`.png`(mime image/*)。

### 2.3 数据模型

- `documents` 表新列(Alembic 迁移):`ocr_mode: str | None = "auto"`、`ocr_used: bool = False`;`DocumentOut` 增两字段,DocsPage 状态列显示 "OCR" 标记(仅 ocr_used)。
- `.env.example` 增 `MINERU_API_TOKEN=`。

## 3. 审计日志 + 导出

### 3.1 表与 helper

```python
class AuditLog(Base, TimestampMixin):
    __tablename__ = "audit_logs"
    id / created_at(索引 desc)
    username: str(64)      # 冒充尝试记原始输入名
    action: str(32)        # 见下表
    target: str(128)       # 如 "kb:3" "doc:12" "user:5" "conv:8"
    detail: Text | None    # JSON 字符串(变更前后/问题摘要等)
    ip: str(45) | None     # Request.client.host
```

`services/audit.py`:`async def audit(db, username, action, target, detail=None, ip=None)`——与业务同事务提交(业务失败不记成功审计);helper 只 add,不自行 commit。

### 3.2 动作清单(显式挂点,现状无 KB 删除端点故无该动作)

| action | 挂点 | detail |
|---|---|---|
| login_success / login_fail | auth.login | username |
| register | auth.register | username |
| kb_create | kbs.create | kb 名 |
| kb_grant / kb_revoke | kbs PUT/DELETE permissions | 目标用户名+perm |
| doc_upload / doc_reprocess | documents 上传/重解析 | filename |
| conv_delete | conversations.delete | 会话 id+标题 |
| user_admin_update | admin.patch | role/is_active 变更前后 |
| ask | ask.py(generation 完成后) | 问题前 50 字 + kb_ids + conv_id |

ask 挂点在 `gen()` 落库 assistant 消息的同事务旁(SessionLocal s2 里顺带 add),失败异常路径不记(避免把失败当成功审计)。

### 3.3 API 与前端

- `GET /admin/audit-logs?username=&action=&page=&page_size=`(admin only,时间倒序分页,page_size 上限 100);`AuditLogOut` schema。
- 前端 `AuditLogPage`(/admin/audit-logs,admin 菜单):el-table 时间/用户/动作(tag 色)/target/detail + 顶部筛选(用户名输入、action 下拉、查询按钮)+ 分页。

### 3.4 会话导出

- `GET /chat/conversations/{conv_id}/export`(owner only,同 delete 权限):拼 markdown——标题、创建时间、逐消息(`**用户**:` / `**助手**:`),助手消息含引用编号,文末附录引用明细(chunk 所在文件/页码/摘录);`Response(media_type="text/markdown", headers={"Content-Disposition": f"attachment; filename=conv-{id}.md"})`。
- ChatPage 会话项操作区(与删除图标并列)加导出图标:前端 `GET` 拿 blob 触发下载(`URL.createObjectURL`)。

## 4. 检索评估集

- 文件:`backend/eval_sets/{kb_id}.json`,git 版本化:

```json
{"kb_id": 3, "items": [
  {"question": "XX 项目的负责人是谁?",
   "expect_doc_ids": [12], "expect_keywords": ["张三"]}
]}
```

- CLI:`backend/scripts/eval_retrieval.py`,`py -m scripts.eval_retrieval --kb 3 [--top-k 8] [--rerank]`:
  - 每 item 跑 `hybrid_search`(可选 rerank 重排);
  - 指标:**hit@k**(expect_doc_ids 是否命中 top-k)、**MRR**(首个命中文档排名倒数)、**关键词 recall**(expect_keywords 在 top-k 内容中的覆盖率);
  - 输出:逐题表格 + 汇总均值,`--json` 出机器可读结果;
  - 指标计算抽成纯函数(`scripts/eval_metrics.py`)便于单测。
- 依赖真库真嵌入:定位为开发/调参工具,不进 pytest 默认路径;单测只测指标纯函数与 CLI 参数解析(mock search)。

## 5. Minor 清偿 + checkpointer

| 项 | 方案 |
|---|---|
| make_chat_llm 单例 | 模块级 `@lru_cache` 缓存(参数固定),图测试注入不受影响 |
| rerank 负下标防御 | nodes.py rerank_node 过滤条件加 `i >= 0` |
| markdown 流式渲染节流 | ChatPage 渲染改为 120ms 节流(定时器合并 token 批次),完成后全量终渲一次;DOMPurify 保留 |
| Alt/Meta+Enter | ChatPage 输入框 keydown:`(e.altKey || e.metaKey) && e.key === 'Enter'` 提交 |
| KbPage 文档数列 | list_kbs 聚合 `documents.count` GROUP BY(kb_id),KBOut 增 `doc_count` |
| kb 菜单子路由高亮 | MainLayout activeIndex 由 `route.path` 前缀推导(`/kbs/3/docs` → `/kbs`) |
| DocsPage 轮询闪烁 | 轮询刷新与首载 loading 分离:静默更新数据不置 loading |
| poll-after-unmount 窄窗 | 轮询回调检查卸载标志/清理定时器收紧 |
| oxlint 三旧错 | auth.store.spec.ts 三处修复至 0 错误 |
| checkpointer 接线 | 见 §1.5 |

## 6. 测试与验收

- **后端单测**(预期 +28~36):图拓扑/直通(rewrite、grade 关闭)、cond_edge 两分支、tag 过滤(rewrite 的 LLM 事件不进 SSE 流)、checkpointer 传参;maybe_ocr 决策矩阵(auto 稀薄/厚/force/off/无 token/图片)、mineru_client mock(requests 打桩:成功/429/超时)、markdown→blocks(含表格)、ocr_mode/ocr_used 列与 API 字段;audit helper 落库、audit-logs API(admin 200/403/筛选分页)、export(内容含引用附录/权限 404);eval 指标纯函数(hit@k/MRR/recall 边界);list_kbs 聚合。
- **无头验收**(真栈:PG/Redis/worker/智谱 key/MinerU token):
  1. 造图片型 PDF(fitz 插入渲染文字的图片页,零文本层)→ 上传 auto → done + ocr_used=true → chunks 有内容 → ask 能引用其内容;
  2. `ocr=off` 传同文件 → 文本层为零 → 无 chunks → 按现有逻辑 RuntimeError("no content extracted") → failed;
  3. 多轮指代:先问"XX 项目的预算",再问"它的负责人是谁" → 改写生效(日志/断言 search_query 含实体);CRAG:问一个首轮检索不佳的问题验证重检路径;
  4. 关闭 AGENTIC_* env 重启 → 行为同 M4(回归);
  5. 审计:依次触发登录失败/授权/上传/重解析/会话删除/用户变更/ask → 审计 API 全部可见、筛选分页正确、非 admin 403;
  6. 导出:GET export 200 markdown 含对话与引用;前端 blob 下载留浏览器走查;
  7. eval:给"M4验收库"写 ≥3 条 golden 对 → CLI 输出三项指标;
  8. Minor 项抽查:KbPage 文档数非"—"、Alt+Enter 提交、oxlint 0 错误。
- **浏览器走查留给用户**:OCR 标记、审计页、导出按钮、输入体验(节流)。
- 回归门:后端全量 pytest + 前端 build/vitest 绿;SSE 契约不变(M3 存量 ask 测试不动)。

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| MinerU 云 API 限流(每日 1 万页)/外网依赖 | token 空 = 整体 off 兜底;429 走 celery retry→failed;client 独立模块,换本地 MinerU 只改一处 |
| 新 LLM 节点拖慢问答(改写+评分各一次调用) | 直通模式保底;grade 失败降级 sufficient;SSE 首 token 延迟在验收观测记录 |
| checkpointer 残留状态污染下一轮 | 节点每轮显式写全 `search_query/retries/grade`;history 每轮由 ask.py 全量传入 |
| astream_events tag 过滤对 FakeListChatModel 的兼容 | 实现期 TDD 先写过滤测试(langchain-core 1.6 tags 随 config 传播,事件里可读);不成立则退化为按 ev["name"]/run_id 区分 |
| 审计表膨胀 | ask 摘要截断 50 字;分页 + 筛选;不做自动清理(量级企业内可控,清理策略留运维) |
| 图片 PDF 造样质量 | 验收脚本用 PIL 画字转图插 PDF,零外部样例依赖 |

## 8. 里程碑结构(供 writing-plans 展开)

后端先行(迁移 → agentic 图 → checkpointer → OCR → 审计 → 导出 → 评估),前端跟进(审计页/导出按钮/OCR 标记/Minor),验收收尾。预计 10-12 个 task,一个计划文档,TDD 每任务全绿再提交。
