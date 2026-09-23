# AIRag M18 实施计划:Webhook 深化(平台适配/per-KB 订阅/重投/SSRF)与 M17 收尾

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** webhook 端点支持企微/钉钉/飞书平台适配、per-KB 订阅、手动重投、SSRF 防护,并清掉 M17 七项收尾小项。

**Architecture:** 新建纯逻辑 `webhook_providers.py` 适配器(构造/分类/SSRF 校验,IO 仅 DNS seam),`outbound.py` 投递引擎委托适配器并加 kb 过滤与轮上限,`admin.py` 扩展校验/统计/重投,前端 WebhooksPage 单页扩展。存量 generic 端点行为逐字节不变。

**Tech Stack:** FastAPI + SQLAlchemy(async)+ alembic + httpx;Vue3 + Element Plus + vitest;pytest(.venv)。

**Spec:** `docs/superpowers/specs/2026-09-23-airag-m18-webhook-deepening-design.md`(本计划与 spec 同读;有出入以 spec 为准,偏差须回写 spec 勘误)。

## Global Constraints

- 测试一律 `cd backend && .venv\Scripts\python -m pytest ...`;前端 `pnpm test` / `pnpm build`(frontend 目录)。**绝不引新依赖**(respx 等不要,monkeypatch/注入既有模式)。
- 改仓库文件一律编辑器工具,不用 python 写文件(DLP 透明加密坑,T2 曾坏 blob)。
- API 错误 detail 用英文短句(与 "name already exists"/"invalid event type" 现状一致);前端中文文案在 Vue 层。
- naive UTC 铁律:`next_attempt_at` 全链 `_utcnow_naive()`(outbound 既有)。
- secret 铁律:明文仅 POST create / rotate 响应一次;GET/PUT 永远 masked。
- 每任务门禁:该任务测试全绿 + `git add` 相关文件 + commit(消息按项目惯例 `feat(m18): ...`/`test(m18): ...`)。
- 投递引擎既有语义不回退:退避表 (1,5,15,60,60)/attempts 上限 settings.WEBHOOK_MAX_ATTEMPTS/4xx(非 429)立即 dead/禁用端点跳过不耗次/扫描 join 过滤禁用。
- vue 前端双主题:只用现有 CSS 变量(`var(--el-*)`/`var(--app-*)`),不写死色值。

## File Structure(全里程碑文件地图)

| 文件 | 动作 | 职责 |
|---|---|---|
| backend/alembic/versions/b0c1d2e3f4a5_m18_webhook_provider_and_kb_scope.py | 新建 | 两列迁移 |
| backend/app/models/webhook.py | 改 | +provider/+kb_ids |
| backend/app/services/webhook_providers.py | 新建 | 适配器:sign_headers/describe_event/build_request/classify_response/check_url_allowed |
| backend/app/services/outbound.py | 改 | deliver_one 委托适配器+SSRF+kb 过滤;deliver_due 轮上限;re-export sign_headers |
| backend/app/services/kb_ops.py | 改 | KB 删除剔除订阅 kb_id |
| backend/app/schemas/admin.py | 改 | webhook schemas +provider/+kb_ids/+WebhookStats |
| backend/app/api/admin.py | 改 | create/update 校验;list stats;redeliver |
| backend/app/core/config.py | 改 | +WEBHOOK_SSRF_ENFORCE/ALLOWLIST/DELIVER_ROUND_LIMIT |
| backend/tests/conftest.py | 改 | +_no_dns autouse(webhook DNS 替身) |
| backend/tests/test_webhook_models.py | 改 | provider/kb_ids 持久化 |
| backend/tests/test_webhook_providers.py | 新建 | 适配器单测(纯函数) |
| backend/tests/test_outbound.py | 改 | 投递集成/SSRF 死信/轮上限/kb 过滤 |
| backend/tests/test_admin_webhooks.py | 改 | API 校验/统计/重投 |
| backend/tests/test_webhook_events.py | 改 | kb_ops 删除清理 |
| frontend/src/api/admin.ts | 改 | 类型+redeliver 函数 |
| frontend/src/pages/WebhooksPage.vue | 改 | provider 表单/KB 多选/统计列/重投/筛选重查/copy 降级 |
| frontend/src/pages/__tests__/WebhooksPage.spec.ts | 改 | +6 用例 |
| docs/webhooks.md | 新建 | 接收方指南 |
| backend/scripts/m18_acceptance.py | 新建 | 真栈验收 |
| .env.example | 改 | M18 配置注释组 |

---

### Task 1: 迁移 + 模型(provider/kb_ids 两列)

**Files:**
- Create: `backend/alembic/versions/b0c1d2e3f4a5_m18_webhook_provider_and_kb_scope.py`
- Modify: `backend/app/models/webhook.py`
- Test: `backend/tests/test_webhook_models.py`(追加)

**Interfaces:**
- Consumes: M17 两表现状(a8b9c0d1e2f3)。
- Produces: `WebhookEndpoint.provider: Mapped[str]`(default "generic")、`WebhookEndpoint.kb_ids: Mapped[list | None]`;迁移 revision `b0c1d2e3f4a5`(down=a8b9c0d1e2f3)。后续任务直接使用这两列。

- [ ] **Step 1: 写失败测试**(追加到 test_webhook_models.py 末尾)

```python
# ---- M18:provider / kb_ids 两列 ----
async def test_endpoint_provider_and_kb_ids_roundtrip(db_session):
    ep = WebhookEndpoint(
        name=f"m18ep{uuid.uuid4().hex[:8]}", url="http://x/h",
        secret="s" * 16, events=[], provider="dingtalk",
        kb_ids=[1, 3], created_by=1)
    db_session.add(ep)
    await db_session.commit()
    await db_session.refresh(ep)
    assert ep.provider == "dingtalk" and ep.kb_ids == [1, 3]


async def test_endpoint_defaults_generic_and_null_kbs(db_session):
    ep = WebhookEndpoint(
        name=f"m18def{uuid.uuid4().hex[:8]}", url="http://x/h",
        secret="s" * 16, events=[], created_by=1)
    db_session.add(ep)
    await db_session.commit()
    await db_session.refresh(ep)
    assert ep.provider == "generic" and ep.kb_ids is None
```

(test_webhook_models.py 头部已有 `import uuid` 与 `from app.models import WebhookEndpoint`;若无则补。)

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_webhook_models.py -v`
Expected: 新用例 FAIL(TypeError: 'provider' is not a valid keyword / 属性不存在)

- [ ] **Step 3: 改模型 + 写迁移**

models/webhook.py 在 `events` 行后加:

```python
    provider: Mapped[str] = mapped_column(String(16), default="generic",
                                          server_default="generic")
    kb_ids: Mapped[list | None] = mapped_column(JSON)   # None/空=订阅全部 KB
```

新建迁移文件全文:

```python
# backend/alembic/versions/b0c1d2e3f4a5_m18_webhook_provider_and_kb_scope.py
"""m18 webhook_endpoints: provider + kb_ids

Revision ID: b0c1d2e3f4a5
Revises: a8b9c0d1e2f3
Create Date: 2026-09-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b0c1d2e3f4a5"
down_revision: Union[str, Sequence[str], None] = "a8b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("webhook_endpoints", sa.Column(
        "provider", sa.String(16), nullable=False,
        server_default="generic"))
    op.add_column("webhook_endpoints", sa.Column(
        "kb_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("webhook_endpoints", "kb_ids")
    op.drop_column("webhook_endpoints", "provider")
```

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_webhook_models.py tests/test_outbound.py tests/test_admin_webhooks.py -v`
Expected: 全 PASS(381 基线不受影响)

- [ ] **Step 5: 对真库执行迁移(可回滚验证)**

Run: `cd backend && .venv\Scripts\python -m alembic upgrade head && .venv\Scripts\python -m alembic downgrade -1 && .venv\Scripts\python -m alembic upgrade head`
Expected: 三步零报错

- [ ] **Step 6: Commit**

```bash
git add backend/alembic/versions/b0c1d2e3f4a5_m18_webhook_provider_and_kb_scope.py backend/app/models/webhook.py backend/tests/test_webhook_models.py
git commit -m "feat(m18): webhook_endpoints provider + kb_ids columns (migration b0c1d2e3f4a5)"
```

---

### Task 2: providers 模块 Ⅰ——sign_headers 迁移 / describe_event / build_request + conftest DNS 替身

**Files:**
- Create: `backend/app/services/webhook_providers.py`
- Modify: `backend/app/services/outbound.py`(删 sign_headers 定义,re-export)
- Modify: `backend/tests/conftest.py`(+autouse `_no_dns`)
- Test: `backend/tests/test_webhook_providers.py`(新建)

**Interfaces:**
- Consumes: 无(纯新模块;sign_headers 公式从 outbound 平移)。
- Produces(Task 3/4/6 依赖,签名必须一字不差):
  - `PROVIDERS: tuple[str, ...] = ("generic","wecom","dingtalk","feishu")`
  - `MAX_CONTENT_BYTES = 3800`
  - `sign_headers(secret: str, event_type: str, body: str) -> dict[str, str]`(从 outbound 平移,公式不变)
  - `describe_event(event_type: str, data: dict) -> str`
  - `build_request(provider: str, secret: str, event_type: str, payload: dict, url: str) -> tuple[str, str, dict[str, str]]`(返回 url/body/headers)
  - `async _resolve_host(host: str) -> list[str]`(DNS seam,测试替身目标)
- conftest `_no_dns` autouse 使全测试套零真实 DNS(默认解析为 93.184.216.34 公网地址)。

- [ ] **Step 1: 写失败测试**(新建 test_webhook_providers.py)

```python
# backend/tests/test_webhook_providers.py
"""M18 T2:适配器构造面——sign 平移等价/平台消息体/加签 URL/截断。"""
import base64
import hashlib
import hmac as hmac_mod
import json
import time

from app.services.webhook_providers import (
    MAX_CONTENT_BYTES, build_request, describe_event, sign_headers)


ENV_DONE = {"document": {"id": 7, "kb_id": 3, "filename": "手册.pdf"}}
ENV_REFUSED = {"source": "rest", "kb_ids": [3, 5], "question": "Q" * 300}


# ---- describe_event:六事件文案 + 未知事件兜底 ----
def test_describe_document_done():
    s = describe_event("document.done", ENV_DONE)
    assert "文档解析完成" in s and "手册.pdf" in s and "3" in s


def test_describe_document_failed_includes_error():
    s = describe_event("document.failed",
                       {"document": {"kb_id": 1, "filename": "f"},
                        "error": "boom " * 100})
    assert "文档解析失败" in s and len(s) < 500  # error 截 200 字


def test_describe_eval_completed_and_failed():
    s = describe_event("eval.completed",
                       {"run": {"kb_id": 2, "mode": "retrieval",
                                "item_count": 5, "summary": {"hit_rate": 0.8}}})
    assert "评估完成" in s and "hit_rate=0.8" in s
    s = describe_event("eval.failed",
                       {"run": {"kb_id": 2, "mode": "judge"}, "error": "x"})
    assert "评估失败" in s


def test_describe_chat_refused_truncates_question():
    s = describe_event("chat.refused", ENV_REFUSED)
    assert "问答拒答" in s and "rest" in s and "[3, 5]" in s
    assert "Q" * 300 not in s  # 问题截 100 字


def test_describe_test_and_unknown():
    assert "测试消息" in describe_event("test", {"message": "airag webhook test"})
    assert "weird.event" in describe_event("weird.event", {"a": 1})


# ---- generic:与 M17 sign_headers 逐字节一致 ----
def test_generic_build_request_equals_m17():
    payload = {"event_id": "e" * 32, "event_type": "document.done",
               "occurred_at": "2026-09-23T00:00:00+00:00", "data": ENV_DONE}
    url, body, headers = build_request(
        "generic", "wh_s3cret", "document.done", payload, "http://h/cb")
    assert url == "http://h/cb"
    assert body == json.dumps(payload, ensure_ascii=False)
    ts = headers["X-AIRag-Timestamp"]
    expect = hmac_mod.new(b"wh_s3cret", f"{ts}.{body}".encode(),
                          hashlib.sha256).hexdigest()
    assert headers["X-AIRag-Signature"] == expect
    assert "X-AIRag-Event" in headers


def test_sign_headers_module_formula():  # 平移后公式不变(既有等价断言)
    body = json.dumps({"a": 1}, ensure_ascii=False)
    h = sign_headers("wh_s3cret", "document.done", body)
    ts = h["X-AIRag-Timestamp"]
    assert h["X-AIRag-Signature"] == hmac_mod.new(
        b"wh_s3cret", f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()


# ---- wecom:markdown 消息体,secret 不参与,无签名头 ----
def test_wecom_build_request():
    payload = {"event_type": "document.done", "data": ENV_DONE,
               "event_id": "e" * 32, "occurred_at": "t"}
    url, body, headers = build_request(
        "wecom", "ignored", "document.done", payload, "http://qyapi/hook")
    obj = json.loads(body)
    assert obj["msgtype"] == "markdown"
    assert "文档解析完成" in obj["markdown"]["content"]
    assert url == "http://qyapi/hook"
    assert "X-AIRag-Signature" not in headers and "X-AIRag-Event" in headers


# ---- dingtalk:加签拼 URL(timestamp 毫秒);无 secret 不拼 ----
def test_dingtalk_build_request_signed():
    payload = {"event_type": "test", "data": {"message": "airag webhook test"}}
    url, body, headers = build_request(
        "dingtalk", "SECsecret123456789", "test", payload,
        "https://oapi.dingtalk.com/robot/send?access_token=abc")
    assert url.startswith("https://oapi.dingtalk.com/robot/send?access_token=abc&")
    assert "timestamp=" in url and "sign=" in url
    from urllib.parse import unquote
    ts = unquote(url).split("timestamp=")[1].split("&")[0]
    digest = hmac_mod.new(f"{ts}\nSECsecret123456789".encode(), b"",
                          hashlib.sha256).digest()
    # sign 经 urlencode(base64),先 unquote 再比对原文
    assert base64.b64encode(digest).decode() in unquote(url)
    obj = json.loads(body)
    assert obj["msgtype"] == "markdown" and obj["markdown"]["title"]
    assert "X-AIRag-Signature" not in headers


def test_dingtalk_build_request_no_secret_plain_url():
    _, _, _ = build_request("dingtalk", "", "test",
                            {"data": {"message": "x"}},
                            "https://oapi.dingtalk.com/robot/send?access_token=t")
    url2, _, _ = build_request("dingtalk", "", "test",
                               {"data": {"message": "x"}},
                               "https://oapi.dingtalk.com/robot/send?access_token=t")
    assert "sign=" not in url2  # 空 secret=不加签(自定义关键词模式)


# ---- feishu:加签放 body(timestamp 秒);无 secret 不放 ----
def test_feishu_build_request_signed(monkeypatch):
    t0 = 1770000000
    monkeypatch.setattr(time, "time", lambda: t0)  # 钉钉/飞书共用 time.time
    payload = {"event_type": "chat.refused", "data": ENV_REFUSED}
    _, body, headers = build_request(
        "feishu", "fssecret", "chat.refused", payload, "https://open.feishu.cn/hook")
    obj = json.loads(body)
    assert obj["msg_type"] == "text"
    assert "问答拒答" in obj["content"]["text"]
    assert obj["timestamp"] == str(t0)
    digest = hmac_mod.new(f"{t0}\nfssecret".encode(), b"",
                          hashlib.sha256).digest()
    assert obj["sign"] == base64.b64encode(digest).decode()
    assert "X-AIRag-Signature" not in headers


def test_feishu_build_request_no_secret():
    _, body, _ = build_request("feishu", "", "test",
                               {"data": {"message": "x"}},
                               "https://open.feishu.cn/hook")
    obj = json.loads(body)
    assert "sign" not in obj and "timestamp" not in obj


# ---- 超长内容字节安全截断 ----
def test_long_content_truncated_utf8_safe():
    data = {"document": {"kb_id": 1, "filename": "长" * 3000}}
    _, body, _ = build_request(
        "wecom", "", "document.done",
        {"event_type": "document.done", "data": data}, "http://x/h")
    assert len(body.encode("utf-8")) < MAX_CONTENT_BYTES + 600
    assert "截断" in json.loads(body)["markdown"]["content"]
    json.loads(body)  # 截断后仍是合法 JSON、合法 UTF-8
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_webhook_providers.py -v`
Expected: FAIL — ModuleNotFoundError: app/services/webhook_providers.py

- [ ] **Step 3: 实现 webhook_providers.py(构造面)**

```python
# backend/app/services/webhook_providers.py
"""M18:webhook 平台适配器(企微/钉钉/飞书)+ SSRF 校验。

构造/分类纯逻辑零 IO(单测友好);唯一 IO 项 _resolve_host(async DNS,
测试替身目标)。平台加签同构:HMAC-SHA256(key=f"{ts}\\n{secret}", msg=b"")
base64——钉钉毫秒拼 URL、飞书秒放 body;generic 保持 M17 签名三头逐字节
不变(sign_headers 自 outbound 平移至此单源)。
"""
import asyncio
import base64
import hashlib
import hmac
import json
import time
from urllib.parse import quote, urlparse

PROVIDERS = ("generic", "wecom", "dingtalk", "feishu")
MAX_CONTENT_BYTES = 3800  # 企微 markdown 上限 4096 字节,留截断标记余量


def sign_headers(secret: str, event_type: str, body: str) -> dict[str, str]:
    """签名头:hex(HMAC-SHA256(secret, f"{ts}.{body}"));ts 为 Unix 秒。

    M17 语义原样自 outbound 平移(公式单源,outbound re-export 兼容旧 import)。
    """
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.{body}".encode(),
                   hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json; charset=utf-8",
        "X-AIRag-Event": event_type,
        "X-AIRag-Timestamp": ts,
        "X-AIRag-Signature": sig,
    }


def _sign_platform(secret: str, ts: str) -> str:
    """钉钉/飞书同构加签:base64(HMAC-SHA256(key=f"{ts}\\n{secret}", msg=b""))。"""
    digest = hmac.new(f"{ts}\n{secret}".encode(), b"", hashlib.sha256)
    return base64.b64encode(digest.digest()).decode()


def _truncate(text: str) -> str:
    """UTF-8 字节安全截断(回退到多字节首字节,不劈字),尾标(截断)。"""
    raw = text.encode("utf-8")
    if len(raw) <= MAX_CONTENT_BYTES:
        return text
    cut = raw[:MAX_CONTENT_BYTES]
    i = len(cut) - 1
    while i >= 0 and (cut[i] & 0xC0) == 0x80:
        i -= 1
    return cut[:i + 1].decode("utf-8", errors="ignore") + "…(截断)"


def describe_event(event_type: str, data: dict) -> str:
    """事件 → 中文 markdown 平文(全平台共用文案源;平台消息只此一份)。"""
    d = data or {}
    if event_type == "test":
        return f"**AIRag webhook 测试消息**\n{d.get('message', '')}".strip()
    if event_type in ("document.done", "document.failed"):
        doc = d.get("document") or {}
        lines = [f"**{'文档解析完成' if event_type == 'document.done' else '文档解析失败'}**",
                 f"知识库: #{doc.get('kb_id')}  文档: {doc.get('filename', '')}"]
        if event_type == "document.failed":
            lines.append(f"错误: {str(d.get('error', ''))[:200]}")
        return "\n".join(lines)
    if event_type in ("eval.completed", "eval.failed"):
        run = d.get("run") or {}
        if event_type == "eval.completed":
            summary = run.get("summary") or {}
            key = " ".join(f"{k}={v}" for k, v in list(summary.items())[:4])
            return "\n".join(["**评估完成**",
                              f"知识库: #{run.get('kb_id')}  模式: {run.get('mode', '')}",
                              f"题数: {run.get('item_count', '')}  {key}".rstrip()])
        return "\n".join(["**评估失败**",
                          f"知识库: #{run.get('kb_id')}  模式: {run.get('mode', '')}",
                          f"错误: {str(d.get('error', ''))[:200]}"])
    if event_type == "chat.refused":
        return "\n".join(["**问答拒答**",
                          f"来源: {d.get('source', '')}  知识库: {d.get('kb_ids', [])}",
                          f"问题: {str(d.get('question', ''))[:100]}"])
    return f"**{event_type}**\n{json.dumps(d, ensure_ascii=False)[:300]}"


def build_request(provider: str, secret: str, event_type: str,
                  payload: dict, url: str) -> tuple[str, str, dict[str, str]]:
    """按 provider 构造 (url, body, headers);generic 与 M17 逐字节一致。

    平台端点 body=平台消息(信封摘要,非全量),headers 只发 Content-Type +
    X-AIRag-Event(secret 语义已变为平台加签,发 X-AIRag-Signature 会误导)。
    """
    if provider == "generic":
        body = json.dumps(payload, ensure_ascii=False)
        return url, body, sign_headers(secret, event_type, body)
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider}")
    content = _truncate(
        describe_event(event_type, (payload or {}).get("data") or {}))
    if provider == "wecom":
        obj: dict = {"msgtype": "markdown", "markdown": {"content": content}}
    elif provider == "dingtalk":
        obj = {"msgtype": "markdown",
               "markdown": {"title": "AIRag 事件通知", "text": content}}
    else:  # feishu
        obj = {"msg_type": "text", "content": {"text": content}}
        if secret:
            ts = str(int(time.time()))
            obj["timestamp"] = ts
            obj["sign"] = _sign_platform(secret, ts)
    body = json.dumps(obj, ensure_ascii=False)
    if provider == "dingtalk" and secret:
        ts = str(int(time.time() * 1000))
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}timestamp={ts}&sign={quote(_sign_platform(secret, ts))}"
    headers = {"Content-Type": "application/json; charset=utf-8",
               "X-AIRag-Event": event_type}
    return url, body, headers


async def _resolve_host(host: str) -> list[str]:
    """IO seam:全部 A/AAAA 去重。测试 monkeypatch 此处,绝不在测试打真 DNS。"""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None)
    return sorted({i[4][0] for i in infos})
```

- [ ] **Step 4: outbound.py 平移 re-export**

删除 outbound.py 内 `sign_headers` 函数定义(注释与实现),顶部 import 区加:

```python
from app.services.webhook_providers import sign_headers  # noqa: F401 — 公式单源在 providers,旧 import 路径兼容
```

(注意:此时 outbound 尚未 import build_request 等——Task 4 再接;本步只做平移,既有测试 `from app.services.outbound import sign_headers` 保持绿。)

- [ ] **Step 5: conftest 加 _no_dns autouse**(追加到 conftest.py,与其他 autouse fixture 并列)

```python
@pytest_asyncio.fixture(autouse=True)
async def _no_dns(monkeypatch):
    """M18:webhook SSRF 校验的 DNS 替身——默认解析为公网地址,全测试套零真实
    DNS;SSRF 专测另行 monkeypatch webhook_providers._resolve_host 覆写。"""
    from app.services import webhook_providers as _wp

    async def _fake(host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(_wp, "_resolve_host", _fake)
```

- [ ] **Step 6: 跑新测试 + 全量回归**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_webhook_providers.py tests/test_outbound.py tests/test_webhook_events.py -v`
Expected: 新文件全 PASS;outbound/webhook_events 既有用例全 PASS(381 基线无回退)

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/webhook_providers.py backend/app/services/outbound.py backend/tests/conftest.py backend/tests/test_webhook_providers.py
git commit -m "feat(m18): provider adapters part 1 (sign_headers moved, describe_event, build_request) + conftest DNS stub"
```

---

### Task 3: providers 模块 Ⅱ——classify_response(平台 body 码分类)+ check_url_allowed(SSRF)

**Files:**
- Modify: `backend/app/services/webhook_providers.py`(追加)
- Modify: `backend/app/core/config.py`(+三参)、`.env.example`(M18 注释组)
- Test: `backend/tests/test_webhook_providers.py`(追加)

**Interfaces:**
- Consumes: Task 2 的模块骨架/_resolve_host seam。
- Produces(Task 4/6 依赖,签名一字不差):
  - `class SsrfBlockedError(ValueError)`
  - `classify_response(provider: str, status_code: int, body: str | None) -> tuple[str | None, str | None]`——返回 (outcome, err);outcome ∈ {"succeeded","retry","dead",None};**None = 非平台判定(generic 或非 2xx),调用方走 M17 状态码原分支**
  - `async check_url_allowed(url: str, allowlist: str | None = None) -> None`——违规抛 `SsrfBlockedError`;allowlist=None 时读 `settings.WEBHOOK_SSRF_ALLOWLIST`
  - settings: `WEBHOOK_SSRF_ENFORCE: bool = True` / `WEBHOOK_SSRF_ALLOWLIST: str = ""` / `WEBHOOK_DELIVER_ROUND_LIMIT: int = 500`

- [ ] **Step 1: 写失败测试**(追加到 test_webhook_providers.py)

```python
# ---- classify_response:平台 body 码三分类;None 交还状态码规则 ----
import pytest

from app.services.webhook_providers import SsrfBlockedError, classify_response


@pytest.mark.parametrize("provider,code", [
    ("wecom", 0), ("dingtalk", 0), ("feishu", 0)])
def test_classify_platform_success(provider, code):
    assert classify_response(provider, 200, f'{{"errcode": {code}}}'
                             if provider != "feishu"
                             else f'{{"code": {code}}}') == ("succeeded", None)


def test_classify_feishu_legacy_statuscode_field():
    assert classify_response("feishu", 200, '{"StatusCode": 0}') == ("succeeded", None)


def test_classify_transient_codes_retry():
    assert classify_response("wecom", 200, '{"errcode": 45009}')[0] == "retry"
    assert classify_response("dingtalk", 200, '{"errcode": -1}')[0] == "retry"
    assert classify_response("dingtalk", 200, '{"errcode": 90001}')[0] == "retry"
    assert classify_response("feishu", 200, '{"code": 9499}')[0] == "retry"


def test_classify_permanent_codes_dead():
    assert classify_response("wecom", 200, '{"errcode": 93000}')[0] == "dead"
    r = classify_response("dingtalk", 200, '{"errcode": 310000, "errmsg": "sign not match"}')
    assert r[0] == "dead" and "310000" in r[1]


def test_classify_unknown_code_conservative_retry():
    assert classify_response("wecom", 200, '{"errcode": 88888}')[0] == "retry"


def test_classify_none_for_generic_and_non_2xx():
    assert classify_response("generic", 200, "{}")[0] is None
    assert classify_response("wecom", 404, '{"errcode": 0}')[0] is None
    assert classify_response("wecom", 500, None)[0] is None


def test_classify_non_json_body_2xx_means_success():
    assert classify_response("wecom", 200, "<html>gateway</html>") == ("succeeded", None)
    assert classify_response("feishu", 200, None) == ("succeeded", None)


def test_classify_error_message_carried():
    outcome, err = classify_response("feishu", 200, '{"code": 9499, "msg": "too fast"}')
    assert outcome == "retry" and "too fast" in err


# ---- SSRF:IP 字面量/域名多记录/白名单/解析失败/scheme ----
from app.services import webhook_providers as wp


async def test_ssrf_blocks_private_literals():
    for url in ("http://10.0.0.5/x", "http://192.168.1.1/x",
                "http://127.0.0.1/x", "http://169.254.169.254/meta",
                "http://[::1]/x", "http://[fe80::1]/x", "http://0.0.0.0/x"):
        with pytest.raises(SsrfBlockedError):
            await wp.check_url_allowed(url)


async def test_ssrf_allows_public_literal():
    await wp.check_url_allowed("https://93.184.216.34/cb")  # 无异常即过


async def test_ssrf_domain_any_blocked_record_rejects(monkeypatch):
    async def _multi(host):
        return ["93.184.216.34", "10.0.0.9"]  # 多 A 记录任一私网即拒
    monkeypatch.setattr(wp, "_resolve_host", _multi)
    with pytest.raises(SsrfBlockedError):
        await wp.check_url_allowed("https://good.example.com/cb")


async def test_ssrf_domain_all_public_passes(monkeypatch):
    async def _ok(host):
        return ["93.184.216.34"]
    monkeypatch.setattr(wp, "_resolve_host", _ok)
    await wp.check_url_allowed("https://good.example.com/cb")


async def test_ssrf_allowlist_covers_cidr(monkeypatch):
    async def _priv(host):
        return ["10.1.2.3"]
    monkeypatch.setattr(wp, "_resolve_host", _priv)
    await wp.check_url_allowed("http://in.example.com/cb",
                               allowlist="127.0.0.1,10.0.0.0/8")
    with pytest.raises(SsrfBlockedError):  # 白名单外仍拒
        await wp.check_url_allowed("http://in.example.com/cb",
                                   allowlist="127.0.0.1")


async def test_ssrf_dns_failure_rejects(monkeypatch):
    async def _boom(host):
        raise OSError("dns down")
    monkeypatch.setattr(wp, "_resolve_host", _boom)
    with pytest.raises(SsrfBlockedError):
        await wp.check_url_allowed("https://no.example.com/cb")


async def test_ssrf_empty_resolve_rejects(monkeypatch):
    async def _empty(host):
        return []
    monkeypatch.setattr(wp, "_resolve_host", _empty)
    with pytest.raises(SsrfBlockedError):
        await wp.check_url_allowed("https://void.example.com/cb")


async def test_ssrf_bad_scheme_rejects():
    with pytest.raises(SsrfBlockedError):
        await wp.check_url_allowed("ftp://x/cb")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_webhook_providers.py -v -k "classify or ssrf"`
Expected: FAIL — ImportError: cannot import name 'SsrfBlockedError'

- [ ] **Step 3: 实现追加到 webhook_providers.py**

```python
# ---- 分类与 SSRF(Task 3)----
PLATFORM_TRANSIENT: dict[str, frozenset[int]] = {
    # 限频/系统繁忙类:走退避重试(官方错误码表精选,docs/webhooks.md 照录)
    "wecom": frozenset({45009}),          # api freq limit
    "dingtalk": frozenset({-1, 90001}),   # 系统繁忙 / 发送过快
    "feishu": frozenset({9499}),          # 频控(100 次/分钟、5 次/秒)
}
PLATFORM_PERMANENT: dict[str, frozenset[int]] = {
    "wecom": frozenset({93000}),          # URL 不合法/机器人被移除
    "dingtalk": frozenset({310000}),      # 安全设置校验未通过(keywords/sign/ip)
    "feishu": frozenset(),                # 空:签名错码语义混杂,统一重试耗尽
}
_PLATFORM_SUCCESS_FIELD = {"wecom": "errcode", "dingtalk": "errcode",
                           "feishu": "code"}


def classify_response(provider: str, status_code: int,
                      body: str | None) -> tuple[str | None, str | None]:
    """平台 2xx 响应的 body 码三分类;返回 (outcome, err)。

    outcome=None 表示「非本函数管辖」(generic 或非 2xx),调用方继续 M17
    状态码原分支。平台规则:码 0=成功;瞬态集/未知非零=retry(保守,at-least-
    once,误判可重投救);永久集=dead。body 非 JSON(网关错误页)按 HTTP 语义
    2xx=成功。
    """
    if provider == "generic" or not (200 <= status_code < 300):
        return None, None
    field = _PLATFORM_SUCCESS_FIELD[provider]
    try:
        obj = json.loads(body) if body else None
    except (TypeError, ValueError):
        obj = None
    if not isinstance(obj, dict):
        return "succeeded", None
    if field not in obj and provider == "feishu" and "StatusCode" in obj:
        field = "StatusCode"  # 旧版飞书返回体
    code = obj.get(field)
    if code is None:
        return "succeeded", None
    if code == 0:
        return "succeeded", None
    detail = (f"{provider} code={code} "
              f"{obj.get('errmsg') or obj.get('msg') or ''}").strip()
    if code in PLATFORM_PERMANENT[provider]:
        return "dead", detail
    return "retry", detail


class SsrfBlockedError(ValueError):
    """URL 指向阻断地址(私网/回环/链路本地/保留等)、解析失败或 scheme 非法。"""


def _blocked_ip(addr) -> bool:
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_multicast or addr.is_reserved or addr.is_unspecified)


def _allowlist_networks(allowlist: str) -> list:
    nets = []
    for part in (allowlist or "").split(","):
        part = part.strip()
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))
    return nets


async def check_url_allowed(url: str, allowlist: str | None = None) -> None:
    """SSRF 闸:URL host 的全部解析地址逐一过闸,任一违规抛 SsrfBlockedError。

    IP 字面量直判(跳过 DNS);域名走 _resolve_host seam;解析失败/空结果按
    违规(DNS 挂了不该建端点/投递)。白名单命中即放行(本地址跳过后续判定)。
    """
    from app.core.config import settings
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise SsrfBlockedError(f"scheme/host invalid: {url[:80]}")
    nets = _allowlist_networks(
        settings.WEBHOOK_SSRF_ALLOWLIST if allowlist is None else allowlist)
    host = parsed.hostname
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    try:
        ips = [str(literal)] if literal is not None else await _resolve_host(host)
    except SsrfBlockedError:
        raise
    except Exception as e:  # DNS/解析异常一律拒(getaddrinfo 抛 OSError 族)
        raise SsrfBlockedError(f"dns resolve failed: {e}") from e
    if not ips:
        raise SsrfBlockedError("no addresses resolved")
    for ip_str in ips:
        addr = ipaddress.ip_address(ip_str)
        if any(addr in net for net in nets):
            continue
        if _blocked_ip(addr):
            raise SsrfBlockedError(f"blocked address: {ip_str}")
```

顶部 import 追加:`import ipaddress`。注意 `settings` 局部 import 是刻意的(check 单测 monkeypatch settings 属性照常生效,模块加载零配置耦合)。

- [ ] **Step 4: config.py + .env.example**

config.py 在 `WEBHOOK_TIMEOUT_S: int = 10` 后加:

```python
    # M18:webhook SSRF 防护与投递节奏
    WEBHOOK_SSRF_ENFORCE: bool = True          # 关=false 应急跳过双检查点
    WEBHOOK_SSRF_ALLOWLIST: str = ""            # 逗号分隔 IP/CIDR,如 "127.0.0.1,10.0.0.0/8"
    WEBHOOK_DELIVER_ROUND_LIMIT: int = 500      # deliver_due 单轮真实尝试上限(大积压分轮)
```

.env.example 在 `WEBHOOK_TIMEOUT_S=10` 后加:

```
# M18:SSRF 防护(建改端点 422 拦截 + 投递前复核转 dead);ALLOWLIST 逗号分隔
# IP/CIDR——本地开发接收器需放行回环(如 WEBHOOK_SSRF_ALLOWLIST=127.0.0.1),
# 生产默认留空;ENFORCE=false 应急全跳。DELIVER_ROUND_LIMIT 为单轮投递行数上限。
WEBHOOK_SSRF_ENFORCE=true
WEBHOOK_SSRF_ALLOWLIST=127.0.0.1
WEBHOOK_DELIVER_ROUND_LIMIT=500
```

(真库 dev 运行时 .env 同步加 `WEBHOOK_SSRF_ALLOWLIST=127.0.0.1`——验收脚本依赖,Task 10 步骤。)

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_webhook_providers.py -v`
Expected: 全 PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/webhook_providers.py backend/app/core/config.py .env.example backend/tests/test_webhook_providers.py
git commit -m "feat(m18): platform body-code classify + SSRF check + webhook env config"
```

---

### Task 4: outbound 集成——deliver_one 委托适配器 + SSRF 检查点2 + deliver_due 轮上限

**Files:**
- Modify: `backend/app/services/outbound.py`
- Test: `backend/tests/test_outbound.py`(追加)

**Interfaces:**
- Consumes: Task 2 `build_request`;Task 3 `classify_response`/`check_url_allowed`/`SsrfBlockedError`;模型 `ep.provider`。
- Produces(Task 6/7 及验收依赖):`deliver_one` 语义扩展(平台 body 码分类/SSRF dead);`deliver_due` 返回值不变但受 `settings.WEBHOOK_DELIVER_ROUND_LIMIT` 约束;`emit_event` 不变(Task 5 改)。

- [ ] **Step 1: 写失败测试**(追加到 test_outbound.py;`_Resp` 扩展 text 属性——改既有类,存量用例不受影响)

```python
# _Resp 加 text:改 test_outbound.py 顶部既有类
class _Resp:
    def __init__(self, code, text=None):
        self.status_code = code
        self.text = text  # M18:平台 body 码分类需要响应体(generic 不读)


class _FakeClient:
    def __init__(self, routes): self.routes, self.calls = routes, []
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, **kw):
        self.calls.append((url, kw))
        r = self.routes.get(url.rstrip("/").rsplit("/", 1)[-1])
        if isinstance(r, Exception): raise r
        if isinstance(r, tuple): return _Resp(r[0], r[1])  # (code, text)
        return _Resp(r)
```

追加用例(`from app.services import webhook_providers as wp` 已可 import):

```python
# ---- M18:平台端点经 deliver_one 的构造与分类 ----
async def _mk_platform_ep(db_session, provider, secret="SECk" + "1" * 15):
    ep = WebhookEndpoint(
        name=f"m18p{uuid.uuid4().hex[:10]}", url="http://h/wx",
        secret=secret, events=[], enabled=True, created_by=1,
        provider=provider)
    db_session.add(ep)
    await db_session.commit()
    return ep


async def _platform_delivery(db_session, ep, payload=None):
    d = WebhookDelivery(
        endpoint_id=ep.id, event_type="document.done",
        event_id=uuid.uuid4().hex,
        payload=payload or {"event_id": "e" * 32, "event_type": "document.done",
                            "occurred_at": "t", "data": {
                                "document": {"kb_id": 1, "filename": "f.pdf"}}},
        status="pending", attempts=0)
    db_session.add(d)
    await db_session.commit()
    return d


async def test_deliver_wecom_body_success_and_platform_shape(db_session):
    from app.services.outbound import deliver_one
    ep = await _mk_platform_ep(db_session, "wecom")
    d = await _platform_delivery(db_session, ep)
    ac = _FakeClient({"wx": (200, '{"errcode": 0}')})
    await deliver_one(db_session, d, client=ac)
    assert d.status == "succeeded"
    url, kw = ac.calls[0]
    body = json.loads(kw["content"])
    assert body["msgtype"] == "markdown"
    assert "X-AIRag-Signature" not in kw["headers"]


async def test_deliver_wecom_permanent_code_dead(db_session):
    from app.services.outbound import deliver_one
    ep = await _mk_platform_ep(db_session, "wecom")
    d = await _platform_delivery(db_session, ep)
    await deliver_one(db_session, d,
                      client=_FakeClient({"wx": (200, '{"errcode": 93000}')}))
    assert d.status == "dead" and "93000" in d.last_error


async def test_deliver_dingtalk_transient_code_retries(db_session):
    from app.services.outbound import deliver_one
    ep = await _mk_platform_ep(db_session, "dingtalk")
    d = await _platform_delivery(db_session, ep)
    await deliver_one(db_session, d,
                      client=_FakeClient({"wx": (200, '{"errcode": -1}')}))
    assert d.status == "retrying" and d.attempts == 1
    assert d.next_attempt_at is not None


async def test_deliver_dingtalk_signed_url_and_body(db_session):
    from app.services.outbound import deliver_one
    ep = await _mk_platform_ep(db_session, "dingtalk")
    d = await _platform_delivery(db_session, ep)
    ac = _FakeClient({"wx": (200, '{"errcode": 0}')})
    await deliver_one(db_session, d, client=ac)
    url, kw = ac.calls[0]
    assert url.startswith("http://h/wx?") and "timestamp=" in url and "sign=" in url
    assert json.loads(kw["content"])["msgtype"] == "markdown"


async def test_deliver_feishu_signed_body(db_session):
    from app.services.outbound import deliver_one
    ep = await _mk_platform_ep(db_session, "feishu", secret="fs" + "k" * 14)
    d = await _platform_delivery(db_session, ep)
    ac = _FakeClient({"wx": (200, '{"code": 0}')})
    await deliver_one(db_session, d, client=ac)
    _, kw = ac.calls[0]
    obj = json.loads(kw["content"])
    assert obj["msg_type"] == "text" and "sign" in obj and "timestamp" in obj


# ---- SSRF 检查点2:投递前复核,配置性阻断直接 dead 不耗次 ----
async def test_deliver_ssrf_blocked_goes_dead_without_attempt(db_session,
                                                              monkeypatch):
    from app.services.outbound import deliver_one

    async def _priv(host):
        return ["10.0.0.1"]
    monkeypatch.setattr(wp, "_resolve_host", _priv)
    ep = await _mk_ep(db_session, events=[], url="http://private.example/x")
    d = WebhookDelivery(endpoint_id=ep.id, event_type="test",
                        event_id="e" * 32, payload={"event_id": "e" * 32},
                        status="pending", attempts=0)
    db_session.add(d)
    await db_session.commit()
    await deliver_one(db_session, d,
                      client=_FakeClient({}))  # 无路由:若发起 POST 会 KeyError
    assert d.status == "dead" and "SSRF blocked" in d.last_error
    assert d.attempts == 0  # 配置错误不算尝试,改 URL 后可重投


async def test_deliver_ssrf_enforce_off_skips_check(db_session, monkeypatch):
    from app.core.config import settings as cfg
    from app.services.outbound import deliver_one
    monkeypatch.setattr(cfg, "WEBHOOK_SSRF_ENFORCE", False)
    ep = await _mk_ep(db_session, events=[], url="http://private.example/x")
    d = WebhookDelivery(endpoint_id=ep.id, event_type="test",
                        event_id="e" * 32, payload={"event_id": "e" * 32},
                        status="pending", attempts=0)
    db_session.add(d)
    await db_session.commit()
    await deliver_one(db_session, d, client=_FakeClient({"x": 200}))
    assert d.status == "succeeded"  # ENFORCE=false 直投(conftest 替身也无所谓)


# ---- deliver_due 轮上限 ----
async def test_deliver_due_round_limit(db_session, monkeypatch):
    from app.core.config import settings as cfg
    from app.services.outbound import deliver_due
    monkeypatch.setattr(cfg, "WEBHOOK_DELIVER_ROUND_LIMIT", 3)
    ep = await _mk_ep(db_session, events=[], url="http://h/lim")
    for _ in range(5):
        db_session.add(WebhookDelivery(
            endpoint_id=ep.id, event_type="test", event_id=uuid.uuid4().hex,
            payload={"event_id": "x"}, status="pending", attempts=0))
    await db_session.commit()
    n = await deliver_due(db_session, client=_FakeClient({"lim": 200}))
    assert n == 3  # 单轮上限即返,余 2 行留下轮(状态仍 pending)
    from sqlalchemy import select as sa_select
    left = (await db_session.execute(sa_select(WebhookDelivery))).scalars().all()
    assert sum(1 for r in left if r.status == "succeeded") == 3
    assert sum(1 for r in left if r.status == "pending") == 2
```

(test 文件顶部需 `import uuid`(已有)与 `from app.services import webhook_providers as wp` 新增。)

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_outbound.py -v -k "wecom or dingtalk or feishu or ssrf or round_limit"`
Expected: 新用例 FAIL(平台构造不生效/SSRF 无拦截/无轮上限)

- [ ] **Step 3: 改 outbound.py**

import 区改:Task 2 的 re-export 行扩为:

```python
from app.services.webhook_providers import (  # noqa: F401 — sign_headers 兼容旧 import
    SsrfBlockedError,
    build_request,
    check_url_allowed,
    classify_response,
    sign_headers,
)
```

`deliver_one` 全量替换为:

```python
async def deliver_one(db: AsyncSession, delivery: WebhookDelivery,
                      client=None) -> None:
    """投递单行并推进状态机;自建 client 随本次调用关闭(注入的不关)。

    端点已删除/禁用直接返回:不耗 attempts、状态不动,留待端点恢复后
    由下一轮扫描补投。M18:构造/平台响应分类委托 webhook_providers;POST 前
    SSRF 复核违规直接 dead 不耗次(配置错误,改 URL 后可重投)。状态分派:
    2xx→classify(平台看 body 码);429/5xx 及网络异常→退避重试,耗尽转
    dead;其余 4xx→对方明确拒收,立即 dead。
    """
    ep = await db.get(WebhookEndpoint, delivery.endpoint_id)
    if ep is None or not ep.enabled:
        return
    if settings.WEBHOOK_SSRF_ENFORCE:
        try:
            await check_url_allowed(ep.url)
        except SsrfBlockedError as e:
            delivery.status = "dead"
            delivery.last_error = f"SSRF blocked: {e}"[:500]
            await db.commit()
            return
    delivery.attempts += 1
    url, body, headers = build_request(
        ep.provider, ep.secret, delivery.event_type,
        delivery.payload or {}, ep.url)
    owned = client is None
    ac = client if client is not None else httpx.AsyncClient()
    try:
        try:
            resp = await ac.post(url, content=body, headers=headers,
                                 timeout=settings.WEBHOOK_TIMEOUT_S)
        except httpx.HTTPError as e:  # 超时/连接/传输类统一按可重试处理
            _mark_retry_or_dead(delivery, str(e))
        else:
            code = resp.status_code
            delivery.response_status = code
            # getattr 防御:既有测试替身 _Resp 可能无 text;generic 不读
            outcome, err = classify_response(
                ep.provider, code, getattr(resp, "text", None))
            if outcome == "dead":
                delivery.status = "dead"
                delivery.last_error = err
            elif outcome == "retry":
                _mark_retry_or_dead(delivery, err or "platform error")
            elif outcome == "succeeded":
                delivery.status = "succeeded"
                delivery.last_error = None
            elif 200 <= code < 300:  # generic 2xx(None outcome)M17 原语义
                delivery.status = "succeeded"
                delivery.last_error = None
            elif code == 429 or code >= 500:
                _mark_retry_or_dead(delivery, f"retryable {code}")
            else:
                delivery.status = "dead"
                delivery.last_error = f"permanent {code}"
    finally:
        if owned:  # 注入的 client 归调用方管,绝不在此关闭
            await ac.aclose()
    await db.commit()
```

`deliver_due` 主循环改(批内截断——上限是**行数**不是批数,否则单批 50 会击穿上限):

```python
    try:
        while True:
            rows = (...既有扫描不变...)
            attempted = 0
            for d in rows:
                if total + attempted >= settings.WEBHOOK_DELIVER_ROUND_LIMIT:
                    break  # 批内也须截断:达到单轮行数上限即停
                await deliver_one(db, d, client=ac)
                attempted += 1
            total += attempted
            if attempted == 0:
                break  # 空批:禁用行已被 join 挡在扫描集外,无队头饥饿
            if total >= settings.WEBHOOK_DELIVER_ROUND_LIMIT:
                break  # 大积压分轮消化,余量留给下一轮 beat/nudge(solo worker 防独占)
```

(注意:SSRF 检查在 attempts+=1 **之前**——配置性阻断不算尝试。)

- [ ] **Step 4: 跑本任务测试 + 全量回归**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_outbound.py tests/test_webhook_tasks.py tests/test_webhook_events.py -v`
Expected: 全 PASS(存量 generic 用例零回退——_Resp 加 text 属性向后兼容)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/outbound.py backend/tests/test_outbound.py
git commit -m "feat(m18): deliver_one via provider adapters + pre-delivery SSRF gate + round limit"
```

---

### Task 5: emit_event kb 过滤 + KB 删除清理订阅

**Files:**
- Modify: `backend/app/services/outbound.py`(emit_event + `_event_kb_ids`)
- Modify: `backend/app/services/kb_ops.py`(delete_knowledge_base 剔除 kb_id)
- Test: `backend/tests/test_outbound.py`(+过滤用例)、`backend/tests/test_webhook_events.py`(+删除清理用例)

**Interfaces:**
- Consumes: 模型 `ep.kb_ids`;kb_ops 现有 delete 流程。
- Produces: emit 语义「ep.kb_ids 非空且事件 kb 集合与之交集为空 → 跳过」;`_event_kb_ids(event_type: str, data: dict) -> set[int] | None`(内部函数,测试直调)。

- [ ] **Step 1: 写失败测试**

test_outbound.py 追加:

```python
# ---- M18:per-KB 订阅过滤 ----
async def _mk_kb_ep(db_session, kb_ids):
    ep = WebhookEndpoint(
        name=f"kbep{uuid.uuid4().hex[:10]}", url="http://x/h",
        secret="wh_s3cret", events=[], enabled=True, created_by=1,
        kb_ids=kb_ids)
    db_session.add(ep)
    await db_session.commit()
    return ep


def test_event_kb_ids_extraction():
    from app.services.outbound import _event_kb_ids
    assert _event_kb_ids("document.done",
                         {"document": {"kb_id": 3}}) == {3}
    assert _event_kb_ids("eval.failed", {"run": {"kb_id": 2}}) == {2}
    assert _event_kb_ids("chat.refused", {"kb_ids": [3, 5]}) == {3, 5}
    assert _event_kb_ids("document.done", {}) is None      # 缺失→None
    assert _event_kb_ids("chat.refused", {"kb_ids": []}) is None


async def test_emit_kb_subscription_filters(db_session):
    """子集命中投/子集未命中跳/空订阅全部投/chat.refused 交集。"""
    await _mk_kb_ep(db_session, kb_ids=[1])            # 订 KB1
    await _mk_kb_ep(db_session, kb_ids=[])             # 空=全部
    n = await emit_event(db_session, "document.done",
                         {"document": {"kb_id": 1, "filename": "a"}})
    await db_session.commit()
    assert n == 2
    n = await emit_event(db_session, "document.done",
                         {"document": {"kb_id": 9, "filename": "b"}})
    await db_session.commit()
    assert n == 1  # 只剩「全部」端点
    n = await emit_event(db_session, "chat.refused",
                         {"source": "web", "kb_ids": [1, 7]})
    await db_session.commit()
    assert n == 2  # [1,7] ∩ {1} 非空 → 命中


async def test_emit_kb_missing_kb_field_hits_all(db_session):
    """事件缺 kb 字段(防御):宁可多投(at-least-once)。"""
    await _mk_kb_ep(db_session, kb_ids=[1])
    n = await emit_event(db_session, "document.done",
                         {"document": {}})  # 无 kb_id
    await db_session.commit()
    assert n == 1
```

test_webhook_events.py 追加(头部补 `from sqlalchemy import select` 若无、`import uuid` 若无):

```python
async def test_kb_delete_prunes_webhook_subscriptions(db_session):
    from app.models import KnowledgeBase, WebhookEndpoint
    from app.services.kb_ops import delete_knowledge_base
    kb = KnowledgeBase(name=f"m18kb{uuid.uuid4().hex[:8]}")
    db_session.add(kb)
    await db_session.flush()
    ep = WebhookEndpoint(
        name=f"m18prune{uuid.uuid4().hex[:8]}", url="http://x/h",
        secret="s" * 16, events=[], created_by=1,
        kb_ids=[kb.id, 999])
    db_session.add(ep)
    await db_session.commit()
    await delete_knowledge_base(db_session, kb, username="tester")
    await db_session.refresh(ep)
    assert ep.kb_ids == [999]  # 目标 id 剔除,其余保留
    assert (await db_session.execute(
        select(KnowledgeBase).where(
            KnowledgeBase.id == kb.id))).scalars().first() is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_outbound.py -v -k "kb" && .venv\Scripts\python -m pytest tests/test_webhook_events.py -v -k "prune"`
Expected: FAIL(_event_kb_ids 不存在 / kb_ids 过滤未生效 / 删除未剔除)

- [ ] **Step 3: 实现**

outbound.py `emit_event` 前加辅助、循环内加过滤:

```python
def _event_kb_ids(event_type: str, data: dict) -> set[int] | None:
    """事件携带的 kb 集合;无 kb 信息返回 None(过滤语义:None=不过滤,
    宁可多投——at-least-once)。"""
    d = data or {}
    kb = None
    if event_type in ("document.done", "document.failed"):
        kb = (d.get("document") or {}).get("kb_id")
    elif event_type in ("eval.completed", "eval.failed"):
        kb = (d.get("run") or {}).get("kb_id")
    elif event_type == "chat.refused":
        ids = {int(k) for k in (d.get("kb_ids") or [])}
        return ids or None
    if kb is None:
        return None
    return {int(kb)}
```

emit_event 循环(在 events 过滤后追加):

```python
        if ep.kb_ids:
            ev_kbs = _event_kb_ids(event_type, data)
            if ev_kbs is not None and not (ev_kbs & set(ep.kb_ids)):
                continue
```

kb_ops.py `delete_knowledge_base` 在 Conversation kb_ids 清理之后、audit 之前插入:

```python
    # M18:剔除 webhook 订阅悬空 kb_id(JSON 列无级联,同 Conversation 清理哲学)
    for ep in (await db.execute(select(WebhookEndpoint))).scalars().all():
        if ep.kb_ids and kb_id in ep.kb_ids:
            ep.kb_ids = [k for k in ep.kb_ids if k != kb_id]
```

import 行加 `WebhookEndpoint`(kb_ops 现有 from app.models import ... 处)。

- [ ] **Step 4: 跑测试 + 回归**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_outbound.py tests/test_webhook_events.py tests/test_kbs.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/outbound.py backend/app/services/kb_ops.py backend/tests/test_outbound.py backend/tests/test_webhook_events.py
git commit -m "feat(m18): per-KB subscription filter on emit + prune dangling kb_ids on KB delete"
```

---

### Task 6: admin API——create/update 扩展(provider/kb_ids/URL 长度/SSRF 检查点1/secret 规则/wecom rotate 422/description 清空)

**Files:**
- Modify: `backend/app/schemas/admin.py`、`backend/app/api/admin.py`
- Test: `backend/tests/test_admin_webhooks.py`(追加)

**Interfaces:**
- Consumes: Task 3 `check_url_allowed`/`SsrfBlockedError`;Task 1 模型列;`KnowledgeBase`。
- Produces(Task 7/8 依赖):
  - `WebhookCreateIn`/`WebhookUpdateIn` + `provider: Literal[...]|None`、`kb_ids: list[int]|None`;`WebhookOut` + `provider: str`、`kb_ids: list[int]|None`
  - 辅助:`_validate_kb_ids(db, kb_ids)`(422 unknown kb id)、URL 校验内联(len>500 → 422 `url exceeds 500 character limit`;SSRF → 422 `url blocked by SSRF policy: ...`)
  - update 语义:provider/kb_ids 可改;`description=""` → 存 NULL;rotate_secret 对 wecom → 422 `rotate_secret not supported for wecom`

- [ ] **Step 1: 写失败测试**(追加到 test_admin_webhooks.py)

```python
# ---- M18:create 扩展 ----
async def test_create_with_provider_and_kb_ids(client, db_session):
    from app.models import KnowledgeBase
    headers = await _make_admin(client, db_session, "m18_admin1")
    kb = KnowledgeBase(name="m18kbA")
    db_session.add(kb)
    await db_session.commit()
    r = await client.post("/api/admin/webhooks", json={
        "name": "wecom-ep", "url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send",
        "provider": "wecom", "kb_ids": [kb.id]}, headers=headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["provider"] == "wecom" and body["kb_ids"] == [kb.id]
    r2 = await client.get("/api/admin/webhooks", headers=headers)
    item = [e for e in r2.json() if e["name"] == "wecom-ep"][0]
    assert item["provider"] == "wecom" and item["kb_ids"] == [kb.id]


async def test_create_invalid_provider_422(client, db_session):
    headers = await _make_admin(client, db_session, "m18_admin2")
    r = await client.post("/api/admin/webhooks", json={
        "name": "bad", "url": "https://h.example.com/cb",
        "provider": "slack"}, headers=headers)
    assert r.status_code == 422


async def test_create_unknown_kb_422(client, db_session):
    headers = await _make_admin(client, db_session, "m18_admin3")
    r = await client.post("/api/admin/webhooks", json={
        "name": "kbep", "url": "https://h.example.com/cb",
        "kb_ids": [424242]}, headers=headers)
    assert r.status_code == 422
    assert "unknown kb" in r.json()["detail"]


async def test_create_url_length_422(client, db_session):
    headers = await _make_admin(client, db_session, "m18_admin4")
    r = await client.post("/api/admin/webhooks", json={
        "name": "longurl", "url": "https://h.example.com/" + "a" * 500},
        headers=headers)
    assert r.status_code == 422
    assert "500" in r.json()["detail"]


async def test_create_ssrf_private_422(client, db_session, monkeypatch):
    from app.services import webhook_providers as wp

    async def _priv(host):
        return ["10.0.0.1"]
    monkeypatch.setattr(wp, "_resolve_host", _priv)
    headers = await _make_admin(client, db_session, "m18_admin5")
    r = await client.post("/api/admin/webhooks", json={
        "name": "ssrf", "url": "https://internal.example.com/cb"},
        headers=headers)
    assert r.status_code == 422
    assert "SSRF" in r.json()["detail"]


async def test_create_secret_rules_per_provider(client, db_session):
    headers = await _make_admin(client, db_session, "m18_admin6")
    # dingtalk 空签名密钥:合法(不加签),secret 存空串
    r = await client.post("/api/admin/webhooks", json={
        "name": "dt", "url": "https://oapi.dingtalk.com/robot/send?access_token=t",
        "provider": "dingtalk"}, headers=headers)
    assert r.status_code == 201
    # generic 无自定义:自动生成(M17 语义)
    r = await client.post("/api/admin/webhooks", json={
        "name": "gen", "url": "https://h.example.com/g"}, headers=headers)
    assert len(r.json()["secret"]) == 32


# ---- M18:update 扩展 ----
async def test_update_description_empty_clears_to_null(client, db_session):
    headers = await _make_admin(client, db_session, "m18_admin7")
    ep = await _create_ep(client, headers, "m18clr", description="旧描述")
    r = await client.put(f"/api/admin/webhooks/{ep['id']}",
                         json={"description": ""}, headers=headers)
    assert r.status_code == 200
    assert r.json()["description"] is None


async def test_update_url_ssrf_and_length_rechecked(client, db_session,
                                                    monkeypatch):
    from app.services import webhook_providers as wp
    headers = await _make_admin(client, db_session, "m18_admin8")
    ep = await _create_ep(client, headers, "m18url")

    async def _priv(host):
        return ["192.168.0.1"]
    monkeypatch.setattr(wp, "_resolve_host", _priv)
    r = await client.put(f"/api/admin/webhooks/{ep['id']}",
                         json={"url": "https://in.example.com/x"}, headers=headers)
    assert r.status_code == 422
    r = await client.put(f"/api/admin/webhooks/{ep['id']}",
                         json={"url": "https://h.example.com/" + "b" * 500},
                         headers=headers)
    assert r.status_code == 422


async def test_update_wecom_rotate_422(client, db_session):
    headers = await _make_admin(client, db_session, "m18_admin9")
    r = await client.post("/api/admin/webhooks", json={
        "name": "wx", "url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send",
        "provider": "wecom"}, headers=headers)
    wid = r.json()["id"]
    r = await client.put(f"/api/admin/webhooks/{wid}",
                         json={"rotate_secret": True}, headers=headers)
    assert r.status_code == 422
    assert "wecom" in r.json()["detail"]


async def test_update_provider_and_kb_ids(client, db_session):
    from app.models import KnowledgeBase
    headers = await _make_admin(client, db_session, "m18_admin10")
    kb = KnowledgeBase(name="m18kbB")
    db_session.add(kb)
    await db_session.commit()
    ep = await _create_ep(client, headers, "m18upd")
    r = await client.put(f"/api/admin/webhooks/{ep['id']}", json={
        "provider": "feishu", "kb_ids": [kb.id]}, headers=headers)
    assert r.status_code == 200
    assert r.json()["provider"] == "feishu" and r.json()["kb_ids"] == [kb.id]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_admin_webhooks.py -v -k "m18"`
Expected: 新用例 FAIL(字段不识别/校验缺失)

- [ ] **Step 3: 实现 schemas**

schemas/admin.py webhook 段改:

```python
WebhookProvider = Literal["generic", "wecom", "dingtalk", "feishu"]


class WebhookCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: HttpUrl
    events: list[str] = []
    description: str | None = Field(None, max_length=200)
    secret: str | None = Field(None, min_length=16, max_length=64)
    provider: WebhookProvider = "generic"
    kb_ids: list[int] | None = None


class WebhookUpdateIn(BaseModel):
    """无 secret 字段:明文写回一律拒绝(rotate_secret 专属通道)。
    多余键按 pydantic 默认忽略——传 secret 静默丢弃而非 422。"""

    name: str | None = Field(None, min_length=1, max_length=100)
    url: HttpUrl | None = None
    events: list[str] | None = None
    enabled: bool | None = None
    description: str | None = None
    rotate_secret: bool = False
    provider: WebhookProvider | None = None
    kb_ids: list[int] | None = None


class WebhookOut(BaseModel):
    id: int
    name: str
    url: str
    events: list[str] | None
    enabled: bool
    description: str | None
    secret_masked: str
    created_at: datetime
    provider: str = "generic"
    kb_ids: list[int] | None = None
```

- [ ] **Step 4: 实现 admin.py**

imports 补:`from app.models import KnowledgeBase`(若未导入)、`from app.services.webhook_providers import SsrfBlockedError, check_url_allowed`。

`_to_out` 加两字段:

```python
def _to_out(ep: WebhookEndpoint) -> WebhookOut:
    return WebhookOut(
        id=ep.id, name=ep.name, url=ep.url, events=ep.events,
        enabled=ep.enabled, description=ep.description,
        secret_masked=_masked(ep.secret), created_at=ep.created_at,
        provider=ep.provider, kb_ids=ep.kb_ids,
    )
```

新辅助(放 `_validate_events` 后):

```python
async def _validate_webhook_url(url_str: str) -> str:
    """M18:长度闸(HttpUrl 放行 2083 会撑爆 VARCHAR(500))+ SSRF 检查点1。"""
    if len(url_str) > 500:
        raise HTTPException(status_code=422,
                            detail="url exceeds 500 character limit")
    if settings.WEBHOOK_SSRF_ENFORCE:
        try:
            await check_url_allowed(url_str)
        except SsrfBlockedError as e:
            raise HTTPException(
                status_code=422,
                detail=f"url blocked by SSRF policy: {e}") from e
    return url_str


async def _validate_kb_ids(db: AsyncSession, kb_ids: list[int]) -> None:
    if not kb_ids:
        return
    found = (await db.execute(
        select(KnowledgeBase.id).where(KnowledgeBase.id.in_(kb_ids))
    )).scalars().all()
    missing = sorted(set(kb_ids) - set(found))
    if missing:
        raise HTTPException(status_code=422,
                            detail=f"unknown kb id: {missing}")
```

`create_webhook` 改(关键 diff):

```python
    _validate_events(payload.events)
    if payload.kb_ids:
        await _validate_kb_ids(db, payload.kb_ids)
    url_str = await _validate_webhook_url(str(payload.url))
    dup = ...(不变)
    # secret 按 provider:generic 必填(自动或自定义);wecom 占位随机
    # (列非空,永不参与计算/回显);钉钉/飞书=可选加签密钥,空=不加签
    if payload.provider == "generic":
        secret = payload.secret or secrets.token_hex(16)
    elif payload.provider == "wecom":
        secret = secrets.token_hex(16)
    else:
        secret = payload.secret or ""
    ep = WebhookEndpoint(
        name=payload.name, url=url_str, secret=secret,
        events=payload.events, description=payload.description,
        created_by=current.id, provider=payload.provider,
        kb_ids=payload.kb_ids or None)
    ...(audit detail 加 {"provider": payload.provider, "kb_ids": payload.kb_ids})
```

`update_webhook` 改:

```python
    if payload.url is not None:
        ep.url = await _validate_webhook_url(str(payload.url))
        changes["url"] = ep.url
    ...(events/enabled 分支不变)
    if payload.provider is not None and payload.provider != ep.provider:
        ep.provider = payload.provider
        changes["provider"] = payload.provider
    if payload.kb_ids is not None:
        await _validate_kb_ids(db, payload.kb_ids)
        ep.kb_ids = payload.kb_ids or None   # [] → NULL(全部)
        changes["kb_ids"] = ep.kb_ids
    if payload.description is not None:
        ep.description = payload.description or None  # "" → NULL 清空
        changes["description"] = ep.description
    new_secret = None
    if payload.rotate_secret:
        if ep.provider == "wecom":
            raise HTTPException(
                status_code=422,
                detail="rotate_secret not supported for wecom")
        new_secret = secrets.token_hex(16)
        ep.secret = new_secret
        changes["rotate_secret"] = True
```

- [ ] **Step 5: 跑测试 + 全量回归(注意存量 SSRF 兼容)**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_admin_webhooks.py tests/test_outbound.py tests/test_webhook_events.py -v`
Expected: 全 PASS。**存量用例 URL 域名(hooks.example.com)经 conftest _no_dns 替身解析为公网地址 → 不受 SSRF 检查影响。**

- [ ] **Step 6: Commit**

```bash
git add backend/app/schemas/admin.py backend/app/api/admin.py backend/tests/test_admin_webhooks.py
git commit -m "feat(m18): webhook create/update validation (provider/kb_ids/url-length/ssrf/secret rules/description-clear)"
```

---

### Task 7: admin API——端点统计聚合 + 重投端点

**Files:**
- Modify: `backend/app/schemas/admin.py`(+WebhookStats)、`backend/app/api/admin.py`
- Test: `backend/tests/test_admin_webhooks.py`(追加)

**Interfaces:**
- Consumes: Task 6 schemas;`WebhookDelivery.created_at`(TimestampMixin;**无 updated_at 列——last_activity_at 用 MAX(created_at)**);outbound `nudge`。
- Produces(Task 8 依赖):
  - `WebhookStats(BaseModel)`: `total/succeeded/pending/retrying/dead: int`、`last_activity_at: datetime | None`
  - GET /webhooks 列表项增 `stats: WebhookStats`(无投递端点全 0/None)
  - `POST /api/admin/webhooks/{webhook_id}/deliveries/{delivery_id}/redeliver` → `{"status": "pending", "delivery_id": int}`;仅 dead/retrying 可重投(422 `only dead/retrying deliveries can be redelivered`);audit action `webhook_redeliver`

- [ ] **Step 1: 写失败测试**(追加)

```python
# ---- M18:统计聚合 ----
async def test_list_webhooks_stats_aggregation(client, db_session):
    headers = await _make_admin(client, db_session, "m18_stat1")
    ep = await _create_ep(client, headers, "statep")
    _insert_delivery(db_session, ep["id"], status="succeeded")
    _insert_delivery(db_session, ep["id"], status="succeeded")
    _insert_delivery(db_session, ep["id"], status="dead")
    await db_session.commit()
    r = await client.get("/api/admin/webhooks", headers=headers)
    item = [e for e in r.json() if e["id"] == ep["id"]][0]
    assert item["stats"]["total"] == 3
    assert item["stats"]["succeeded"] == 2
    assert item["stats"]["dead"] == 1
    assert item["stats"]["retrying"] == 0
    assert item["stats"]["last_activity_at"] is not None


async def test_list_webhooks_stats_zero_for_fresh(client, db_session):
    headers = await _make_admin(client, db_session, "m18_stat2")
    ep = await _create_ep(client, headers, "freshep")
    r = await client.get("/api/admin/webhooks", headers=headers)
    item = [e for e in r.json() if e["id"] == ep["id"]][0]
    assert item["stats"]["total"] == 0
    assert item["stats"]["last_activity_at"] is None


# ---- M18:重投 ----
async def test_redeliver_dead_resets_and_nudges(client, db_session, monkeypatch):
    from app.services import outbound
    ep = await _create_ep(client, headers := await _make_admin(
        client, db_session, "m18_redel1"), "redep")
    _insert_delivery(db_session, ep["id"], status="dead")
    await db_session.commit()
    from app.models import WebhookDelivery
    d = (await db_session.execute(select(WebhookDelivery))).scalars().one()
    d.attempts = 5
    d.last_error = "permanent 404"
    await db_session.commit()
    nudged = []
    monkeypatch.setattr(outbound, "nudge", lambda: nudged.append(1))
    r = await client.post(
        f"/api/admin/webhooks/{ep['id']}/deliveries/{d.id}/redeliver",
        headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "pending"
    await db_session.refresh(d)
    assert d.status == "pending" and d.attempts == 0
    assert d.last_error is None and d.next_attempt_at is None
    assert nudged == [1]


async def test_redeliver_succeeded_422(client, db_session):
    headers = await _make_admin(client, db_session, "m18_redel2")
    ep = await _create_ep(client, headers, "okp")
    _insert_delivery(db_session, ep["id"], status="succeeded")
    await db_session.commit()
    from app.models import WebhookDelivery
    d = (await db_session.execute(select(WebhookDelivery))).scalars().one()
    r = await client.post(
        f"/api/admin/webhooks/{ep['id']}/deliveries/{d.id}/redeliver",
        headers=headers)
    assert r.status_code == 422


async def test_redeliver_cross_endpoint_404(client, db_session):
    headers = await _make_admin(client, db_session, "m18_redel3")
    ep1 = await _create_ep(client, headers, "ep1x")
    ep2 = await _create_ep(client, headers, "ep2x")
    _insert_delivery(db_session, ep1["id"], status="dead")
    await db_session.commit()
    from app.models import WebhookDelivery
    d = (await db_session.execute(select(WebhookDelivery))).scalars().one()
    r = await client.post(
        f"/api/admin/webhooks/{ep2['id']}/deliveries/{d.id}/redeliver",
        headers=headers)
    assert r.status_code == 404


async def test_redeliver_non_admin_403(client, db_session):
    other = await _register_and_login(client, "m18_redel4")
    r = await client.post("/api/admin/webhooks/1/deliveries/1/redeliver",
                          headers=other)
    assert r.status_code == 403
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_admin_webhooks.py -v -k "stat or redeliver"`
Expected: FAIL(stats 键不存在/404 路由不存在)

- [ ] **Step 3: 实现**

schemas/admin.py 追加(WebhookOut 后):

```python
class WebhookStats(BaseModel):
    """M18:端点投递聚合(last_activity_at=MAX(created_at),无 updated_at 列)。"""
    total: int
    succeeded: int
    pending: int
    retrying: int
    dead: int
    last_activity_at: datetime | None
```

WebhookOut 加字段 `stats: WebhookStats | None = None`。

admin.py:`list_webhooks` 改为聚合版:

```python
@router.get("/webhooks", response_model=list[WebhookOut])
async def list_webhooks(
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(
        select(WebhookEndpoint).order_by(WebhookEndpoint.id))).scalars().all()
    agg: dict[int, dict] = {}
    for eid, status, cnt, last in (await db.execute(
        select(WebhookDelivery.endpoint_id, WebhookDelivery.status,
               func.count(), func.max(WebhookDelivery.created_at))
        .group_by(WebhookDelivery.endpoint_id, WebhookDelivery.status))
    ).all():
        a = agg.setdefault(eid, {"total": 0, "succeeded": 0, "pending": 0,
                                 "retrying": 0, "dead": 0,
                                 "last_activity_at": None})
        a["total"] += cnt
        if status in a:
            a[status] += cnt
        if last is not None and (a["last_activity_at"] is None
                                 or last > a["last_activity_at"]):
            a["last_activity_at"] = last
    out = []
    for ep in rows:
        o = _to_out(ep)
        o.stats = WebhookStats(**agg.get(
            ep.id, {"total": 0, "succeeded": 0, "pending": 0,
                    "retrying": 0, "dead": 0, "last_activity_at": None}))
        out.append(o)
    return out
```

(func 已在文件头导入——deliveries 计数同款;若无需补 `from sqlalchemy import func`。)

redeliver 路由(放 test_webhook 路由后):

```python
@router.post("/webhooks/{webhook_id}/deliveries/{delivery_id}/redeliver")
async def redeliver_delivery(
    webhook_id: int,
    delivery_id: int,
    current: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """M18:手动重投——dead/retrying 归零重排近即时投递(nudge);审计留痕。"""
    ep = await db.get(WebhookEndpoint, webhook_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="webhook not found")
    d = await db.get(WebhookDelivery, delivery_id)
    if d is None or d.endpoint_id != ep.id:
        raise HTTPException(status_code=404, detail="delivery not found")
    if d.status not in ("dead", "retrying"):
        raise HTTPException(
            status_code=422,
            detail="only dead/retrying deliveries can be redelivered")
    old_rs = d.response_status
    d.attempts = 0
    d.status = "pending"
    d.next_attempt_at = None
    d.last_error = None
    await audit(db, current.username, "webhook_redeliver", f"webhook:{ep.id}",
                {"delivery_id": d.id, "event_type": d.event_type,
                 "old_response_status": old_rs})
    await db.commit()
    nudge()
    return {"status": "pending", "delivery_id": d.id}
```

imports:`WebhookDelivery` 已导入(admin.py 现有);补 `from app.services.outbound import nudge`(与既有 deliver_one import 同行)。

**注意**:admin.py 若直接 `from app.services.outbound import deliver_one, nudge`,而测试 monkeypatch `app.services.outbound.nudge`——模块属性补丁对 `from ... import` 早绑定无效!实现须写成 `from app.services import outbound` 后调用 `outbound.nudge()`(测试 monkeypatch outbound.nudge 才生效;test_webhook 现有 deliver_one 用的是 `app.api.admin.deliver_one` patch,别动它)。

- [ ] **Step 4: 跑测试 + 回归**

Run: `cd backend && .venv\Scripts\python -m pytest tests/test_admin_webhooks.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas/admin.py backend/app/api/admin.py backend/tests/test_admin_webhooks.py
git commit -m "feat(m18): endpoint delivery stats aggregation + redeliver API"
```

---

### Task 8: 前端——api/admin.ts + WebhooksPage(provider 表单/KB 多选/统计列/重投/筛选重查/copy 降级)+ vitest

**Files:**
- Modify: `frontend/src/api/admin.ts`、`frontend/src/pages/WebhooksPage.vue`
- Test: `frontend/src/pages/__tests__/WebhooksPage.spec.ts`(追加)

**Interfaces:**
- Consumes: Task 6/7 API JSON(provider/kb_ids/stats/redeliver);`kbApi.list()`(frontend/src/api/kb,admin 可见全部 KB)。
- Produces: `WebhookEndpoint.provider/.kb_ids/.stats`、`WebhookStats`、`adminApi.redeliverWebhook(eid: number, did: number): Promise<{status: string; delivery_id: number}>`;createWebhook/updateWebhook payload 增 `provider?/kb_ids?`。

- [ ] **Step 1: 写失败测试**(追加到 WebhooksPage.spec.ts;先做四处既有文件同步,再追加 6 用例)

**既有文件同步改**(类型变化与 payload 变化的连带,不改则存量用例红):

```ts
// 1) mock 工厂增 redeliverWebhook;新增 kb 模块 mock(import 区加 kbApi):
vi.mock('@/api/admin', () => ({
  adminApi: {
    listWebhooks: vi.fn(),
    createWebhook: vi.fn(),
    updateWebhook: vi.fn(),
    deleteWebhook: vi.fn(),
    testWebhook: vi.fn(),
    listWebhookDeliveries: vi.fn(),
    redeliverWebhook: vi.fn(),
  },
}))
vi.mock('@/api/kb', () => ({ kbApi: { list: vi.fn() } }))
import { kbApi } from '@/api/kb'

// 2) eps 夹具两行补(类型必填):eps[0] 增 provider: 'generic', kb_ids: [3],
//    stats: { total: 42, succeeded: 40, pending: 0, retrying: 1, dead: 1,
//             last_activity_at: '2026-09-22T12:00:00' };
//    eps[1] 增 provider: 'wecom', kb_ids: null, stats: null
// 3) deliveries.items 增 succeeded 行(id:13, endpoint_id:1, status:'succeeded',
//    attempts:1, response_status:200, 其余同款)
// 4) beforeEach 增:
//    vi.mocked(kbApi.list).mockReset()
//    vi.mocked(kbApi.list).mockResolvedValue([{ id: 3, name: 'KB甲' }] as never)
//    vi.mocked(adminApi.redeliverWebhook).mockReset()
//    并把既有用例「create dialog submits and shows one-time secret」的
//    toHaveBeenCalledWith 期望更新为 {name:'新端点', url:…, events: [],
//    provider:'generic'}(kb_ids 空时不带键)
```

**追加 6 用例**(模板锚点:provider 下拉加 `class="provider-select"`、KB 多选加 `class="kb-select"`、状态筛选加 `class="status-filter"`——Step 4 模板按此落):

```ts
// ---- M18 ----
it('provider 联动:wecom 隐藏 secret 输入,dingtalk 显示加签密钥', async () => {
  const w = mountPage()
  await flushPromises()
  await findBtn(w, '新建端点').trigger('click')
  await flushPromises()
  const sel = w.getComponent('.provider-select')
  expect(w.find('input[placeholder="留空自动生成"]').exists()).toBe(true) // generic
  ;(sel.vm as never as { $emit: (e: string, v: unknown) => void })
    .$emit('update:modelValue', 'wecom')
  await flushPromises()
  expect(w.find('input[placeholder="留空自动生成"]').exists()).toBe(false)
  ;(sel.vm as never as { $emit: (e: string, v: unknown) => void })
    .$emit('update:modelValue', 'dingtalk')
  await flushPromises()
  expect(w.find(
    'input[placeholder="平台机器人加签密钥,未开启加签可留空"]').exists()).toBe(true)
})

it('KB 多选:提交 payload 携带 kb_ids', async () => {
  vi.mocked(adminApi.createWebhook).mockResolvedValue({
    ...eps[0]!, id: 9, secret: 'wh_plainsecret123',
  } as never)
  const w = mountPage()
  await flushPromises()
  await findBtn(w, '新建端点').trigger('click')
  await flushPromises()
  await w.find('input[placeholder="请输入端点名称"]').setValue('KB端点')
  await w
    .find('input[placeholder="https://example.com/webhook"]')
    .setValue('https://kb.example.com/hook')
  ;(w.getComponent('.kb-select').vm as never as {
    $emit: (e: string, v: unknown) => void }).$emit('update:modelValue', [3])
  await findBtn(w, '保存').trigger('click')
  await flushPromises()
  expect(adminApi.createWebhook).toHaveBeenCalledWith({
    name: 'KB端点',
    url: 'https://kb.example.com/hook',
    events: [],
    provider: 'generic',
    kb_ids: [3],
  })
})

it('统计列渲染 stats 总数', async () => {
  const w = mountPage()
  await flushPromises()
  const row = w.findAll('.ep-table .el-table__row')
    .find((r) => r.text().includes('面板端点'))!
  expect(row.text()).toContain('42')  // stats.total(eps[0] 夹具)
})

it('重投:dead/retrying 行显示按钮并调用 API;succeeded 行不显示', async () => {
  vi.mocked(adminApi.listWebhookDeliveries).mockResolvedValue(deliveries)
  vi.mocked(adminApi.redeliverWebhook)
    .mockResolvedValue({ status: 'pending', delivery_id: 11 })
  const w = mountPage()
  await flushPromises()
  await w.findAll('.el-tabs__item')
    .find((t) => t.text() === '投递记录')!.trigger('click')
  await flushPromises()
  const rows = w.findAll('.d-table .el-table__row')
  const deadRow = rows.find((r) => r.text().includes('面板端点'))!    // dead
  const okRow = rows.find((r) => r.text().includes('已成功'))!       // succeeded 备份行名
  const btn = deadRow.findAll('button').find((b) => b.text().trim() === '重投')
  expect(btn).toBeTruthy()
  expect(okRow.findAll('button').find((b) => b.text().trim() === '重投'))
    .toBeFalsy()
  const { ElMessageBox } = await import('element-plus')
  vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue(undefined as never)
  await btn!.trigger('click')
  await flushPromises()
  expect(adminApi.redeliverWebhook).toHaveBeenCalledWith(1, 11)
})

it('投递筛选变更触发重查', async () => {
  vi.mocked(adminApi.listWebhookDeliveries).mockResolvedValue(deliveries)
  const w = mountPage()
  await flushPromises()
  await w.findAll('.el-tabs__item')
    .find((t) => t.text() === '投递记录')!.trigger('click')
  await flushPromises()
  expect(adminApi.listWebhookDeliveries).toHaveBeenCalledTimes(1)
  ;(w.getComponent('.status-filter').vm as never as {
    $emit: (e: string, v: unknown) => void }).$emit('update:modelValue', 'dead')
  await flushPromises()
  expect(adminApi.listWebhookDeliveries).toHaveBeenCalledTimes(2)
  const lastCall = vi.mocked(adminApi.listWebhookDeliveries).mock.calls[1]![0]!
  expect(lastCall.status).toBe('dead')
  expect(lastCall.page).toBe(1)
})

it('copySecret 失败降级提示', async () => {
  vi.mocked(adminApi.createWebhook).mockResolvedValue({
    ...eps[0]!, id: 9, secret: 'wh_plainsecret123',
  } as never)
  const { ElMessage } = await import('element-plus')
  const warn = vi.spyOn(ElMessage, 'warning')
  const w = mountPage()
  await flushPromises()
  await findBtn(w, '新建端点').trigger('click')
  await flushPromises()
  await w.find('input[placeholder="请输入端点名称"]').setValue('cp')
  await w.find('input[placeholder="https://example.com/webhook"]')
    .setValue('https://cp.example.com/hook')
  await findBtn(w, '保存').trigger('click')
  await vi.waitFor(() => expect(w.text()).toContain('wh_plainsecret123'))
  const clip = { writeText: vi.fn().mockRejectedValue(new Error('denied')) }
  vi.stubGlobal('navigator', { clipboard: clip })
  await findBtn(w, '复制').trigger('click')
  await flushPromises()
  expect(clip.writeText).toHaveBeenCalled()
  expect(warn).toHaveBeenCalledWith(expect.stringContaining('剪贴板不可用'))
  vi.unstubAllGlobals()
})
```

(succeeded 夹具行的 endpoint_name 取「已成功」以免与其他行名撞匹配;deliveries 第 3 行 endpoint_id=1 与 redeliver 断言 (1, 11) 对齐。)

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && pnpm vitest run src/pages/__tests__/WebhooksPage.spec.ts`
Expected: 新用例 FAIL(provider 字段/统计列/重投按钮不存在)

- [ ] **Step 3: 实现 api/admin.ts**

```ts
export interface WebhookStats {
  total: number
  succeeded: number
  pending: number
  retrying: number
  dead: number
  last_activity_at: string | null
}

export type WebhookProvider = 'generic' | 'wecom' | 'dingtalk' | 'feishu'
```

`WebhookEndpoint` 增:`provider: WebhookProvider`、`kb_ids: number[] | null`、`stats?: WebhookStats | null`。createWebhook/updateWebhook 的 payload 类型增 `provider?: WebhookProvider`、`kb_ids?: number[]`。adminApi 增:

```ts
  async redeliverWebhook(eid: number, did: number) {
    const { data } = await http.post(
      `/admin/webhooks/${eid}/deliveries/${did}/redeliver`)
    return data as { status: string; delivery_id: number }
  },
```

- [ ] **Step 4: 实现 WebhooksPage.vue**

script 关键增量(完整落进组件,以下为实现规约):

```ts
import { kbApi } from '@/api/kb'

const PROVIDER_OPTIONS = [
  { value: 'generic', label: '通用(JSON+签名)', secretMode: 'generic' },
  { value: 'wecom', label: '企业微信', secretMode: 'none' },
  { value: 'dingtalk', label: '钉钉', secretMode: 'platform' },
  { value: 'feishu', label: '飞书', secretMode: 'platform' },
] as const
const PROVIDER_LABEL: Record<string, string> = Object.fromEntries(
  PROVIDER_OPTIONS.map((p) => [p.value, p.label]),
)
const URL_PLACEHOLDER: Record<string, string> = {
  generic: 'https://example.com/webhook',
  wecom: 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...',
  dingtalk: 'https://oapi.dingtalk.com/robot/send?access_token=...',
  feishu: 'https://open.feishu.cn/open-apis/bot/v2/hook/...',
}

const kbOptions = ref<{ id: number; name: string }[]>([])
// form 增:provider: 'generic' as string、kbIds: [] as number[]
// openCreate/openEdit 相应全字段显式赋值(edit 预填 row.provider/row.kb_ids ?? [])
```

onMounted 并行:`Promise.all([loadEndpoints(), kbApi.list().then(r => kbOptions.value = r.map(k => ({ id: k.id, name: k.name }))).catch(() => {})])`(KB 列表失败静默——订阅选填)。

submit:创建 payload 增 `provider: form.provider`,`kb_ids: form.kbIds.length ? [...form.kbIds] : undefined`;secret 仅 `provider==='generic' && form.secret` 时带;平台加签密钥(`provider dingtalk/feishu && form.secret`)也走 `secret` 字段(后端按 provider 解释)。编辑全量带 `provider`/`kb_ids: [...form.kbIds]`(`[]` = 全部,语义等价 null)。

template 增量(锚点 class 与 Step 1 测试一致:provider 下拉 `class="provider-select"`、KB 多选 `class="kb-select"`、投递状态筛选 `class="status-filter"`):
- 对话框 provider 下拉(el-select,选项 PROVIDER_OPTIONS)+ 按 `form.provider` 切 URL placeholder(`:placeholder="URL_PLACEHOLDER[form.provider]"`);secret 表单项:`v-if="form.provider === 'generic'"` 标签「签名密钥」/`v-else-if="form.provider !== 'wecom'"` 标签「加签密钥(可选)」placeholder「平台机器人加签密钥,未开启加签可留空」;KB 多选 `el-select multiple` 选项 kbOptions,help「不选 = 订阅全部知识库」。
- 端点表列:平台(`{{ PROVIDER_LABEL[row.provider] ?? row.provider }}` tag)、范围(KB:`row.kb_ids?.length ? row.kb_ids.length + ' 库' : '全部'`,tooltip 列出名称)、统计(`{{ row.stats?.total ?? 0 }}` + tooltip `成功 x · 重试 x · 死信 x · 最近 {fmtTime}`)。
- 投递表操作列(窄):`<el-button v-if="row.status === 'dead' || row.status === 'retrying'" link type="warning" @click="redeliver(row)">重投</el-button>`,确认弹窗文案「重投该条投递?将立即重新发送一次。」成功 toast 后 `loadDeliveries()`。
- 筛选重查:`watch([() => dQuery.endpointId, () => dQuery.eventType, () => dQuery.status], () => searchDeliveries())`(查询按钮保留)。
- copySecret 降级:

```ts
async function copySecret() {
  if (!oneTimeSecret.value) return
  try {
    await navigator.clipboard.writeText(oneTimeSecret.value)
    ElMessage.success('已复制到剪贴板')
  } catch {
    // 非安全上下文/权限拒绝:文本区已 user-select:all,降级手动复制
    ElMessage.warning('剪贴板不可用,请点击密钥文本手动复制(Ctrl+C)')
  }
}
```

```ts
async function redeliver(row: WebhookDeliveryRow) {
  try {
    await ElMessageBox.confirm(
      `重投「${row.endpoint_name}」的 ${eventLabel(row.event_type)} 投递?将立即重新发送。`,
      '手动重投', { type: 'warning', confirmButtonText: '重投', cancelButtonText: '取消' })
  } catch { return }
  try {
    await adminApi.redeliverWebhook(row.endpoint_id, row.id)
    ElMessage.success('已重新排队投递')
    await loadDeliveries()
  } catch (e) {
    ElMessage.error(errMsg(e, '重投失败'))
  }
}
```

- [ ] **Step 5: 跑测试 + 类型门禁**

Run: `cd frontend && pnpm vitest run src/pages/__tests__/WebhooksPage.spec.ts && pnpm build`
Expected: vitest 全 PASS(存量+新 6);build 零错

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/admin.ts frontend/src/pages/WebhooksPage.vue frontend/src/pages/__tests__/WebhooksPage.spec.ts
git commit -m "feat(m18): webhooks UI provider form/kb-subscribe/stats/redeliver/filter-refetch/copy-fallback"
```

---

### Task 9: docs/webhooks.md 接收方指南

**Files:**
- Create: `docs/webhooks.md`

**Interfaces:**
- Consumes: Task 2/3 的公式与分类表(照录,不得改写数值)。
- Produces: 接收方对接文档(验收走查引用)。

- [ ] **Step 1: 写文档**(全文落地,结构如下)

```markdown
# Webhook 出站推送对接指南

(AIRag M17/M18;admin 在「出站推送」页配置端点)

## 事件与订阅
- 五事件:document.done / document.failed / eval.completed / eval.failed / chat.refused
- 订阅粒度:事件类型(多选,空=全部)× 知识库(多选,空=全部;chat.refused 按
  命中任一订阅库投递)
- at-least-once:可能重复投递,**接收方必须按 event_id 幂等去重**

## 通用端点(provider=generic)
- 请求:POST 信封 JSON(event_id/event_type/occurred_at/data)
- 签名头:hex(HMAC-SHA256(secret, f"{ts}.{body}"))
  - X-AIRag-Signature / X-AIRag-Timestamp(Unix 秒)/ X-AIRag-Event
  - 验签示例(Python):...(6 行代码:重算 hex + hmac.compare_digest)
- 响应约定:**2xx 即成功;3xx 视为永久拒收(不跟随重定向)转死信**;
  429/5xx 按退避重试(1/5/15/60 分钟,上限 5 次);其余 4xx 立即死信

## 平台端点(企微/钉钉/飞书群机器人)
- 消息为中文 markdown 摘要(非全量信封;需要全量数据请配 generic 端点)
- 内容上限 3800 UTF-8 字节,超长截断加「…(截断)」
- 成功判定 = HTTP 2xx 且平台 body 码 = 0(三平台都以 200 包业务错误):

| 平台 | 成功字段 | 瞬态(退避重试) | 永久(死信) |
|---|---|---|---|
| 企微 wecom | errcode==0 | 45009 频率限制 | 93000 URL 不合法/机器人被移除 |
| 钉钉 dingtalk | errcode==0 | -1 系统繁忙;90001 发送过快 | 310000 安全设置校验未通过 |
| 飞书 feishu | code==0(兼容 StatusCode) | 9499 频控(100/分钟、5/秒) | —(统一重试耗尽) |

  未知非零码按瞬态处理(保守重试,耗尽转死信;死信可 admin 手动重投)。
- 加签(可选):
  - 钉钉:secret=机器人加签密钥;请求 URL 追加
    `&timestamp={毫秒}&sign={urlencode(base64(HMAC-SHA256(key=f"{ts}\n{secret}", msg=b"")))}`
  - 飞书:secret=签名密钥;body 增 `"timestamp":"{秒}","sign":"{base64(同上)}"
  - 企微:key 在 webhook URL 内,无需 secret;generic 端点的 X-AIRag 签名头
    对平台端点不发(secret 语义已变为平台加签)

## 运维
- 投递记录/状态机/手动重投:admin「出站推送 → 投递记录」(dead/retrying 行)
- SSRF 防护:端点 URL 禁指向私网/回环/保留地址(建改 422 拦截 + 投递前复核
  转死信);白名单 WEBHOOK_SSRF_ALLOWLIST(IP/CIDR 逗号分隔);已知限制:
  校验与连接两次解析的 DNS rebinding 竞态不在防护范围(admin 信任边界内纵深防御)
- 参考:企微/钉钉/飞书官方机器人文档(spec 链接)
```

- [ ] **Step 2: Commit**

```bash
git add docs/webhooks.md
git commit -m "docs(m18): webhook receiver guide (envelope/signing/3xx/platform codes)"
```

---

### Task 10: 真栈验收 m18_acceptance.py + 门禁总账

**Files:**
- Create: `backend/scripts/m18_acceptance.py`
- Modify: `.env`(真库 dev 运行时,不提交:加 `WEBHOOK_SSRF_ALLOWLIST=127.0.0.1`——.env.example Task 3 已带)

**Interfaces:**
- Consumes: 全部前序任务;真栈 8001 + worker + beat(已在跑,须重启加载新代码/迁移)。
- Produces: 验收 PASS 记录;门禁总账(pytest/vitest/build/验收计数)回填本文件「执行记录」节。

- [ ] **Step 1: 环境准备**

1. 确认 `.env` 含 `WEBHOOK_SSRF_ALLOWLIST=127.0.0.1`(本地 mock 接收器在回环上)
2. `cd backend && .venv\Scripts\python -m alembic upgrade head`(真库迁移)
3. 重启栈:关旧 backend/worker/beat 进程(精确串核对:`--pool=solo`、` beat --`),再 start_dev.bat + start_worker.bat + start_beat.bat;前端 5173 若在跑则保留(vite 热更已覆盖,无需重启)

- [ ] **Step 2: 写验收脚本**

照 m17_acceptance.py 骨架(BASE/API/TIMEOUT/RESULTS/SUFFIX/check()/summary_and_exit/本地 receiver 线程)实现,mock receiver 扩展:**按路径前缀应答平台语义**——`/wxok` 回 `{"errcode":0}`、`/wxperm` 回 `{"errcode":93000}`、`/wxtrans` 回 `{"errcode":45009}`、`/dtsign` 记录完整 URL 与 body 回 `{"errcode":0}`、`/fssign` 记录 body 回 `{"code":0}`、`/ok` 回 200 空体、`/reject` 回 404。步骤:

1. admin 登录(M17 模式);非 admin 负例账号注册
2. **wecom 全链路**:建 wecom 端点(http://127.0.0.1:{port}/wxok)→ 测试发送(POST test)→ 收包断言 msgtype=markdown + content 含「测试消息」+ 无 X-AIRag-Signature 头 → 投递行 succeeded
3. **平台错误码分类**:端点 A(/wxperm)→ 投递立即 dead(last_error 含 93000);端点 B(/wxtrans)→ retrying(attempts=1,next_attempt_at 非空)——两包均 HTTP 200,body 码决定命运
4. **钉钉加签**:建 dingtalk 端点 secret 随机 ≥16 字符(/dtsign)→ test 发送 → 按同公式验 URL timestamp/sign + body markdown 结构
5. **飞书加签**:feishu 端点(/fssign)→ test → 验 body timestamp/sign/msg_type=text
6. **per-KB**:建 KB甲+KB乙;端点订阅 kb_ids=[甲];触发 document.failed(上传损坏 PDF 进 KB乙)→ 该端点零投递行;KB甲 触发(M17 模式坏 PDF 进甲)→ 投递命中 succeeded
7. **重投**:制造 dead(端点指 /reject 触发事件)→ 改端点 URL 到 /ok(update)→ redeliver → 轮询 succeeded 且 attempts=1
8. **SSRF**:POST 建端点 url=http://10.255.255.1/x → 422(detail 含 SSRF);127.0.0.1 mock 端点建立成功(白名单生效)
9. **统计与杂项**:GET /webhooks stats.total 与脚本累计对账;provider/kb_ids 回显;501 字符 URL 422;PUT description="" → GET null;非 admin:建端点/redeliver/列表全 403
10. **清理**:删全部验收端点、KB乙/KB甲、temp 用户 DB 直删(m15/m17 清理模式);summary_and_exit

head 注释注明前置:worker+beat 必须 start_worker.bat/start_beat.bat 已起;.env 须含 WEBHOOK_SSRF_ALLOWLIST=127.0.0.1。

- [ ] **Step 3: 跑验收至全绿**

Run: `cd backend && .venv\Scripts\python scripts/m18_acceptance.py`
Expected: 全 PASS 零 SKIP(chat.refused 真栈链路已由 M17 验收覆盖,M18 平台面不重复;若 LLM key 缺失相关步骤允许 SKIP 但须明示)

- [ ] **Step 4: 门禁总账(全量)**

```bash
cd backend && .venv\Scripts\python -m pytest -q          # 381 → 预计 ~420+
cd frontend && pnpm vitest run                            # 67 → 预计 ~73+
cd frontend && pnpm build                                 # 零错
```

- [ ] **Step 5: Commit + 执行记录回填**

```bash
git add backend/scripts/m18_acceptance.py
git commit -m "test(m18): acceptance script with platform-mock receiver"
```

执行记录节(追加本文件末尾):各门禁数字、偏差台账、修复波记录。

---

## 用户走查清单(M17 延后 + M18 合并;走查账号 admin/secret123,前端 localhost:5173,后端 8001)

**M17 存量项**(原清单,随 M18 代码一起走):
1. 新建端点+一次性 secret 弹窗(明文仅一次/复制按钮);外部接收器验签(验收脚本输出的本地命令)
2. 测试发送反馈;投递记录状态 tag 与筛选分页
3. retry→dead 状态机可见性(退避时刻列)
4. 启停/rotate/删除级联(端点删→投递记录保留)
5. 非 admin 全路由 403;双主题(亮/暗)各过一遍

**M18 新增项**:
6. 新建钉钉端点(加签密钥可选/URL placeholder 联动);企微端点无 secret 输入
7. KB 多选订阅(空=全部);表端「范围」列与 tooltip
8. 平台 tag/统计列(数值与投递页签对账)
9. dead/retrying 行「重投」按钮流转(确认→toast→列表刷新→成功)
10. 投递筛选下拉变更即时重查(免点查询)
11. copySecret 拒绝场景降级提示(非安全上下文浏览器打开时)

## M19 候选(排期备忘,不在本计划)

C 集群(M16 遗留:取消运行中评估/题集导入导出/趋势全未测量空态);大件仍等输入(A2A/MinerU 本地化/LDAP)。

## 风险与执行备注

- **存量测试 SSRF 兼容**:conftest `_no_dns` autouse(Task 2)是全计划的地基——先于任何 SSRF 检查落地,否则 Task 4/6 一上来存量用例全红。
- **monkeypatch 绑定**:Task 7 nudge 必须 `outbound.nudge()` 点名调用(from-import 早绑定躲 monkeypatch)。
- **_Resp 扩展向后兼容**(Task 4):只加属性不改构造签名,存量 `_FakeClient` 路由 int 分支不受影响。
- **Windows beat 独立窗口**(start_beat.bat)与进程核对精确串(`--pool=solo`、` beat --`)遵循 M17 备忘。
- 前端 EP 组件 valueOnClear 类坑:KB 多选 clearable 时置 undefined 而非 [](M14 教训;submit 已按 length 判空兜底)。

## 执行记录(2026-09-23 SDD 完成终稿)

**门禁总账**:pytest 381→**443P/0F**(+62);vitest 67→**74 passed**(16 文件,WebhooksPage.spec 13/13);`pnpm build` 零错;真栈 m18_acceptance **42/42 PASS 0 SKIP 首轮绿**(平台 mock 接收器七路径;清理后零残留);alembic 迁移 b0c1d2e3f4a5 真库 upgrade/downgrade/upgrade 三步验证。

**任务与提交**:T1 c9acf9a 模型+迁移 / T2 cea9e5e 适配器构造面+conftest DNS 替身 / T3 59672a3 body 码分类+SSRF+config / T4 aef7b01 deliver_one 委托+检查点2+轮上限(修复波 7e61da2:last_error 截断,毒化队列防线)/ T5 8c402e2 kb 过滤+删除清理 / T6 11cbc70 create/update 校验 / T7 7b17546 统计+重投 / T8 a72e986 前端全量 / T9 36f7aba 接收方指南 / T10 8532068+d636aba 验收脚本+测试封闭性修复 / 终审修复波 2a7064c 编辑清空描述(F6 前端半边,`|| undefined` 吞空串)。**全部 10 任务经独立任务审查;终审(whole-branch)With fixes→已修并通过复验。**

**裁定台账**(完整记录在 SDD 工作区,已随工作区清理;关键五条):
1. 直接在 main 实施(项目 M1~M17 惯例),不建 worktree。
2. T4 平台 dead 分支 `last_error=err[:500]`:计划固有缺陷的修正(审查证实 >500 字符 errmsg 会 asyncpg 22001→dead 永不落库→毒行堵死 deliver_due)。
3. T7 WebhookStats 类置于 WebhookOut 前(计划排序在 py3.12 无 future-annotations 下会 NameError)。
4. T10 单测钉 `allowlist=""`:走查 .env 白名单(127.0.0.1)泄入单测属环境耦合,测试须封闭。
5. 终审 F6:编辑框空提交必须发 `description: ""`(后端 ""→NULL 已就绪),前端 `|| undefined` 吞串为违规。

**M19 候选**(终审 triage 全部 ride,无一须合并前修):ALLOWLIST 拼错抛裸 ValueError(投递侧可成毒环,优先)/wecom 占位 secret 回显抑制+文档/切 provider 旧 secret 结转提示/PLATFORM_TRANSIENT 死表清理/范围列 KB 名 tooltip/wecom 编辑态隐藏轮换开关/.env.example ALLOWLIST 默认空/补充测试(IPv6 带端口、settings 默认路径、10.0.0.9 宽 CIDR 封闭);+M16 遗留三项(取消运行中评估/题集导入导出/趋势空态);大件仍等输入(A2A/MinerU 本地化/LDAP)。

**环境备忘(本里程碑新增)**:cmd echo 重定向写 UTF-8 跟踪文件必乱码(GBK 代码页),仓库文件一律编辑器工具;栈重启后孤儿 spawn 子进程需单独清理;dev .env 已加 WEBHOOK_SSRF_ALLOWLIST=127.0.0.1(走查/验收需要,生产留空)。

**走查待办**:M17+M18 合并走查,清单见本文件「用户走查清单」节(M17 存量五项+M18 新增六项);栈已在新代码上运行(8001/worker/beat/5173);**走查通过后再推 origin**(M17 先推码属特例拍板,M18 恢复走查后推送惯例)。
