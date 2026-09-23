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
