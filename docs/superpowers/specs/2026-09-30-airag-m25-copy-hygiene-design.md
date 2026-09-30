# AIRag M25 设计:文案补全 + 编码注释与 BOM 测试 + 验收脚本清理

(2026-09-30;范围 = M24 终审/走查回流的卫生小项;大件 A2A/MinerU 本地化/
LDAP 用户明示全挂起,老 Office/WPS 维持「等需要再立项」)

## 背景与目标

M24 收官后残留三个非功能小项:①beforeUpload 拒绝文案列了 16/18 扩展
(漏 `.htm`/`.jpeg` 别名——功能不受影响,门是常量表,但文案与清单不一致)
②`_read_text` 的 utf-8-sig(BOM)分支无单测;编码歧义(GKB 字节流恰为合法
UTF-8)未做分析记录 ③m23/m24 验收脚本早失败时清理不对称(finally 只删
部分或干脆 `pass`),验收残留污染 dev 库。

## T1 — 拒绝文案补别名

DocsPage.vue 第 72 行拒绝文案:`.html` → `.html/.htm`、`.jpg` 后补
`.jpeg`(即列出全部 18 项)。提示文案(274 行)为概括性描述无需动。
DocsPage.spec.ts 的 beforeUpload 行为断言不受文案影响,补一条文案含
`.htm` 的轻量断言可选(如加,只断言常量导出/渲染,不重复 18 项全文)。

## T2 — 编码分析注释 + utf-8-sig 单测

`_read_text`:

- 新增单测:带 BOM 的 utf-8 文本(txt)→ BOM 被剥离、内容正确(锁
  utf-8-sig 分支);顺带一例纯 ASCII(两解码等价,走 utf-8 路径)。
- docstring 补编码歧义分析,结论**不做启发式**:GBK 双字节结构
  (首字节 81-FE,次字节 40-FE)要整体成为合法 UTF-8 多字节序列,须
  每对字节恰好落在(首 C2-DF、次 80-BF)等窗口上——真实中文文档逐对
  命中的概率可忽略;纯 ASCII 两解码等价无歧义;仅病态超短串可能误判,
  接受(引入 chardet 或双语评分的复杂度不值)。

## T3 — 验收脚本 finally 全量清理

m23/m24 验收脚本统一加幂等 `_cleanup(...)` 助手并在 finally 调用:
API 优先删除(端点/KB,try/except 逐个吞错),SQL 兜底(按收集到的
kb_ids/endpoint_ids/run_ids 依 FK 序删除:webhook_deliveries→
webhook_endpoints、chunks→documents→eval_items→eval_runs→
kb_permissions→knowledge_bases)。正常路径仍走 API 删除并计数(既有
检查不变);finally 只在早失败时生效为兜底。

## 任务切分(SDD)

1. **T1** 前端文案别名(+可选 spec 断言)。
2. **T2** 编码注释 + 两个单测(TDD)。
3. **T3** 两个验收脚本清理助手(py_compile + 结构自查;脚本无测试面)。

## 验收

- pytest/vitest/build 全绿(513P+86T 基线只增不减,T2 +2P)。
- 人工核:m24 脚本 dry 结构(finally 覆盖)——不重跑真栈(纯脚本卫生,
  行为无变化);文案截图留走查。

## 风险与不做

- 不做编码启发式(分析在案);不动提示文案;老 Office/大件不在本里程碑。
