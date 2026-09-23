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
