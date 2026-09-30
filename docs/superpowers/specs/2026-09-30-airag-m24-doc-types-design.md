# AIRag M24 设计:文档类型扩容(轻量包 + PPTX + 图片格式)

(2026-09-30;用户选定范围——轻量包(.txt/.md/.csv/.json/.html)+ .pptx + 更多
图片(.bmp/.tif/.tiff/.webp/.gif);**不含**老 Office/WPS(避开转换层,若日后
需要另立里程碑)。当前基线支持 .pdf/.docx/.xlsx/.jpg/.jpeg/.png 共 6 种。)

## 背景与架构事实

解析层是注册表模式:`Parser` ABC + `@register(ext)`(services/parsing/base.py),
pipeline 按 `get_parser(suffix)` 路由;上传白名单 `ALLOWED_EXTS` 硬编码在
doc_ops.py:22,前端 `DocsPage.vue` accept 另写一份——三处漂移面。切块器
split_blocks:文本块 ≤1000 字符整块、超长按分隔符递归切(带 overlap),表格块
(is_table)**整体不切**——大 CSV 与大 sheet 同险,维持 xlsx 现状语义一致。
`.md` 无需新逻辑:ocr.py 的 `markdown_to_blocks`(按空行分段+表格标记识别)
即 MinerU 输出路径,直接复用。图片是双端白牌(image_parser 的 @register +
ocr.py 的 IMAGE_EXTS)。新依赖:python-pptx、beautifulsoup4(均未安装;
bs4 用 stdlib `html.parser` 后端,不引 lxml)。不加 chardet/Pillow。

## T1 — 白名单单源化 + 文本族解析器(.txt/.md/.csv/.json)

- **单源化**:`ALLOWED_EXTS = frozenset(REGISTRY)`(doc_ops 从
  `app.services.parsing` import;REGISTRY 在包 __init__ 导入全部解析器后即完整,
  无环)。今后加类型只动解析器注册与前端 accept 两处。
- **`text_parser.py`**(四类一族 + 共享解码助手):
  - `_read_text(path)`:先 `utf-8-sig`,UnicodeDecodeError 回退 `gb18030`
    (国内存量 .txt/.csv 的 GBK 现实),再失败 `raise ValueError`(流水线
    按确定性 failed 收口,不重试)。
  - `.txt`/`.md`:`markdown_to_blocks(_read_text(path))` 复用(空行分段、
    `|---` 表格标记)。
  - `.csv`:`csv.reader` 逐行 → `" | ".join(cells)` → **整表一块**
    is_table=True(page_no=None)——镜像 xlsx sheet 语义。
  - `.json`:解码后 `json.loads`;顶层数组 → 每元素一块
    (`json.dumps(elem, ensure_ascii=False, indent=2)`),否则单块美化整体;
    `JSONDecodeError` → `raise ValueError`(同上确定性失败)。

## T2 — .html/.htm 解析器

- **`html_parser.py`**:BeautifulSoup(_read_text(path), "html.parser");
  删 script/style/noscript;`get_text("\n")` 后走 `markdown_to_blocks`。
- 依赖 beautifulsoup4(soupsieve 随装)。

## T3 — .pptx 解析器

- **`pptx_parser.py`**(python-pptx):逐 slide(page_no=序号,
  page_count=len(slides)):
  - 文本形状(shape.has_text_frame)非空文本 → 每形状一块(保留段内换行);
  - GraphicFrame 表格 → 每表一块 is_table=True(`" | ".join` 行内单元格,
    行间换行——与 docx/xlsx 表格格式一致);
  - 备注页(has_notes_slide)→ 追加一块。
  - 形状按 slide 内自然顺序(阅读序)。

## T4 — 图片格式扩容(.bmp/.tif/.tiff/.webp/.gif)

- `image_parser.py`:新增五个 @register;`IMAGE_EXTS` 常量的权威定义移至
  image_parser.py,ocr.py 改 import(消双端漂移)。
- MinerU 云端对这些格式为探查性支持(常规均收;gif 取首帧类语义由云端定);
  若云端拒收 → 流水线确定性 failed 并展示错误,不静默。

## T5 — 前端 + 端到端

- `DocsPage.vue` accept 扩容为 16 种;页面若有类型提示文案同步;vitest 加
  accept 含新扩展名的轻量断言(若现有 spec 结构允)。
- README/docs 中支持类型清单(若有)同步。
- `test_pipeline.py` 增端到端用例:API 上传 **GBK 编码 .txt**、.pptx、.csv →
  eager 流水线 → completed + chunks>0 + hybrid_search 命中(fake embed;
  MINERU_TOKEN 空置 → 图片路径不受扰)。
- `test_parsers.py`:`test_unknown_ext_raises` 的反例由 `.txt` 改为 `.xyz`;
  新增各解析器用例(夹具用库内生成:python-pptx 造 pptx,文本类直接写,
  GBK 夹具用 `content.encode("gb18030")`);`ALLOWED_EXTS` 单源化断言
  (等于 REGISTRY 键集,含 16 项)。

## 任务切分(SDD)

1. **T1** 单源化 + 文本族(含 requirements:无新依赖)。
2. **T2** html(bs4,requirements + 安装)。
3. **T3** pptx(python-pptx,requirements + 安装)。
4. **T4** 图片扩容 + IMAGE_EXTS 单源化。
5. **T5** 前端 accept + 端到端 + 文档清单。

## 验收

- pytest/vitest/build 全绿(502P+85T 基线只增不减)。
- 真栈 m24_acceptance:上传 GBK .txt / .pptx / .csv → done + 可检索;
  .bmp 一张走真 MinerU OCR(auto)→ done 且 ocr_used=True(真 token 在
  dev .env,先例 M5);前端 accept 串含新扩展(grep)。

## 风险与不做

- 不做老 Office(.doc/.xls/.ppt)/WPS——转换层另立里程碑(用户未选)。
- 大 CSV/大 JSON 单块 embedding 超长风险与 xlsx 大 sheet 同级,维持一致,
  不在本里程碑加行数上限(YAGNI;真出现再议)。
- .txt 行内容无空行的极端单段文件由切块器兜底(递归分隔切)。
- HTML 解析不抓取外链资源、不执行脚本(纯文本提取)。
