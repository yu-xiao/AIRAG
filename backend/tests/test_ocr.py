import io
import zipfile

import httpx
import pytest

from app.services.parsing.base import ParseResult, ParsedBlock
from app.services.parsing.ocr import is_thin_text, markdown_to_blocks, maybe_ocr


def _primary(chars: int = 5000, pages: int = 10) -> ParseResult:
    return ParseResult(blocks=[ParsedBlock(content="字" * chars)], page_count=pages)


def _enable_mineru(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "MINERU_API_TOKEN", "test-token")


def test_markdown_to_blocks_splits_and_flags_tables():
    md = "第一段文字\n\n| 列1 | 列2 |\n|---|---|\n| a | b |\n\n第三段"
    result = markdown_to_blocks(md)
    assert len(result.blocks) == 3
    assert result.blocks[0].is_table is False
    assert result.blocks[1].is_table is True
    assert result.blocks[2].content == "第三段"


def test_is_thin_text_threshold():
    assert is_thin_text(_primary(chars=49, pages=1)) is True
    assert is_thin_text(_primary(chars=50, pages=1)) is False
    assert is_thin_text(ParseResult(blocks=[], page_count=0)) is True


def test_maybe_ocr_off_or_no_token_returns_primary(tmp_path, monkeypatch):
    from app.core.config import settings

    p = tmp_path / "a.pdf"
    p.write_bytes(b"x")
    assert maybe_ocr(p, ".pdf", "off", _primary()).blocks[0].content == "字" * 5000
    # token 空:force 也直通
    monkeypatch.setattr(settings, "MINERU_API_TOKEN", "")
    assert maybe_ocr(p, ".pdf", "force", _primary()).blocks[0].content == "字" * 5000


def test_maybe_ocr_auto_thin_pdf_and_images(tmp_path, monkeypatch):
    import app.services.parsing.ocr as ocr_mod

    calls = []

    def fake_mineru(path, filename):
        calls.append((str(path), filename))
        return "OCR 出的内容\n\n|a|b|\n|---|---|"

    monkeypatch.setattr(ocr_mod, "parse_via_mineru", fake_mineru)
    _enable_mineru(monkeypatch)

    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"x")
    thin = ParseResult(blocks=[ParsedBlock(content="稀")], page_count=3)
    out = maybe_ocr(pdf, ".pdf", "auto", thin)
    assert calls and out.blocks[0].content == "OCR 出的内容"
    assert out.blocks[1].is_table is True

    img = tmp_path / "photo.png"
    img.write_bytes(b"x")
    out2 = maybe_ocr(img, ".png", "auto", ParseResult())
    assert len(calls) == 2
    assert out2.blocks[0].content == "OCR 出的内容"


def test_maybe_ocr_auto_thick_pdf_and_docx_skip(tmp_path, monkeypatch):
    import app.services.parsing.ocr as ocr_mod

    calls = []

    def fake_mineru(path, filename):
        calls.append(1)
        return "不应被调用"

    monkeypatch.setattr(ocr_mod, "parse_via_mineru", fake_mineru)
    _enable_mineru(monkeypatch)

    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(b"x")
    assert maybe_ocr(pdf, ".pdf", "auto", _primary()).blocks[0].content == "字" * 5000
    docx = tmp_path / "t.docx"
    docx.write_bytes(b"x")
    assert maybe_ocr(docx, ".docx", "force", _primary()).blocks[0].content == "字" * 5000
    assert calls == []


def test_maybe_ocr_mineru_empty_keeps_ocr_semantics(tmp_path, monkeypatch):
    """OCR 成功但无文字:不回退 primary(保持 ocr_used=True → 流水线不重试直落 failed)。"""
    import app.services.parsing.ocr as ocr_mod

    monkeypatch.setattr(ocr_mod, "parse_via_mineru", lambda p, f: "")
    _enable_mineru(monkeypatch)
    pdf = tmp_path / "e.pdf"
    pdf.write_bytes(b"x")
    thin = ParseResult(blocks=[ParsedBlock(content="原")], page_count=1)
    out = maybe_ocr(pdf, ".pdf", "auto", thin)
    assert out.blocks == []  # 空 OCR 结果保留,不回退到 thin primary


def _zip_with_markdown(markdown: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("full.md", markdown)
    return buf.getvalue()


_REAL_CLIENT = httpx.Client  # import 时捕获真构造器(同测试内二次打桩仍指向原始)


def _mock_httpx(monkeypatch, handler):
    """替换 httpx.Client:保留除 transport 外的构造参数。"""
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: _REAL_CLIENT(
            transport=httpx.MockTransport(handler),
            **{k: v for k, v in kw.items() if k != "transport"},
        ),
    )


def test_mineru_client_happy_path(monkeypatch, tmp_path):
    from app.services.parsing import mineru_client as mc

    _enable_mineru(monkeypatch)
    state = {"polls": 0, "put": None}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v4/file-urls/batch":
            assert request.url.host == "mineru.net"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"batch_id": "b1", "file_urls": ["https://put/u.pdf"]},
                },
            )
        if request.url.host == "put" and request.url.path == "/u.pdf":
            state["put"] = request.content
            return httpx.Response(200)
        if request.url.path == "/api/v4/extract-results/batch/b1":
            state["polls"] += 1
            if state["polls"] < 2:
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"extract_result": [{"state": "running"}]}},
                )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "extract_result": [
                            {"state": "done", "full_zip_url": "https://f/r.zip"}
                        ]
                    },
                },
            )
        if request.url.host == "f" and request.url.path == "/r.zip":
            return httpx.Response(200, content=_zip_with_markdown("# 扫描件内容"))
        raise AssertionError(f"unexpected {request.url}")

    _mock_httpx(monkeypatch, handler)
    monkeypatch.setattr(mc, "POLL_INTERVAL", 0)
    p = tmp_path / "s.pdf"
    p.write_bytes(b"pdf-bytes")
    assert mc.parse_via_mineru(p, "s.pdf") == "# 扫描件内容"
    assert state["put"] == b"pdf-bytes"


def test_mineru_client_429_and_timeout(monkeypatch, tmp_path):
    from app.services.parsing import mineru_client as mc

    _enable_mineru(monkeypatch)

    def handler_429(request):
        return httpx.Response(429, json={"code": 429})

    _mock_httpx(monkeypatch, handler_429)
    p = tmp_path / "s.pdf"
    p.write_bytes(b"pdf")
    with pytest.raises(mc.MineruError):
        mc.parse_via_mineru(p, "s.pdf")

    state = {"n": 0}

    def handler_stuck(request):
        if request.url.path == "/api/v4/file-urls/batch":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"batch_id": "b1", "file_urls": ["https://put/u.pdf"]}},
            )
        if request.url.host == "put":
            return httpx.Response(200)
        state["n"] += 1
        return httpx.Response(
            200, json={"code": 0, "data": {"extract_result": [{"state": "running"}]}}
        )

    _mock_httpx(monkeypatch, handler_stuck)
    monkeypatch.setattr(mc, "POLL_MAX", 2)
    monkeypatch.setattr(mc, "POLL_INTERVAL", 0)
    with pytest.raises(mc.MineruError, match="timeout"):
        mc.parse_via_mineru(p, "s.pdf")


async def test_upload_jpg_and_ocr_mode(client, auth_headers):
    png = io.BytesIO(b"\x89PNG\r\n\x1a\nfaked")
    resp = await client.post(
        "/api/kbs/1/documents",
        files={"file": ("扫描.png", png, "image/png")},
        data={"ocr": "force"},
        headers=auth_headers,
    )
    assert resp.status_code == 404  # kb 1 不存在:权限/存在性先行,白名单放行(不是 415)

    kb = await client.post("/api/kbs", json={"name": "ocr上传库"}, headers=auth_headers)
    kb_id = kb.json()["id"]
    png2 = io.BytesIO(b"\x89PNG\r\n\x1a\nfaked")
    ok = await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("扫描.png", png2, "image/png")},
        data={"ocr": "force"},
        headers=auth_headers,
    )
    assert ok.status_code == 201
    assert ok.json()["ocr_mode"] == "force"
    assert ok.json()["ocr_used"] is False  # eager 流水线失败(假 png),但字段已落

    bad = await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("a.pdf", io.BytesIO(b"x"), "application/pdf")},
        data={"ocr": "sometimes"},
        headers=auth_headers,
    )
    assert bad.status_code == 422


async def test_ocr_empty_result_fails_without_retry(
    client, auth_headers, monkeypatch, db_session
):
    """OCR 成功但无文字(如纯图形图片)是确定性失败:一次调用即落 failed,不重试。"""
    import app.services.parsing.ocr as ocr_mod

    _enable_mineru(monkeypatch)
    calls = []

    def fake_mineru(path, filename):
        calls.append(filename)
        return ""

    monkeypatch.setattr(ocr_mod, "parse_via_mineru", fake_mineru)

    kb = await client.post("/api/kbs", json={"name": "空结果库"}, headers=auth_headers)
    kb_id = kb.json()["id"]
    png = io.BytesIO(b"\x89PNG\r\n\x1a\nfaked")
    up = await client.post(
        f"/api/kbs/{kb_id}/documents",
        files={"file": ("纯图形.png", png, "image/png")},
        data={"ocr": "force"},
        headers=auth_headers,
    )
    assert up.status_code == 201
    doc_id = up.json()["id"]

    # worker 侧状态断言用 get+refresh 绕开 API 会话的 identity map 缓存
    # (client 夹具跨请求共享 db_session,与生产每请求新建会话不同)
    from app.models import Document

    doc = await db_session.get(Document, doc_id)
    await db_session.refresh(doc)
    assert doc.status == "failed"
    assert "no text" in doc.error_msg
    assert len(calls) == 1  # 只调一次 MinerU,不烧重试额度
