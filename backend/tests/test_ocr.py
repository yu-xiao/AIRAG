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


class _FakeResp:
    def __init__(self, status_code=200, json_data=None, content=b""):
        self.status_code = status_code
        self._json_data = json_data
        self.content = content

    def json(self):
        return self._json_data


class _FakeMineruClient:
    """罐头 MinerU 客户端:记录 batch json 与各 PUT 内容,zip 文本由测试注入。"""

    instances = []
    zip_texts = []  # 每个文件的 full.md 文本,顺序即 files 顺序

    def __init__(self, base_url=None, timeout=None, **kw):
        self.batch_json = None
        self.puts = []  # [(url, content)]
        self._urls = []
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None):
        self.batch_json = json
        n = len(json["files"])
        self._urls = [f"http://u/{i + 1}" for i in range(n)]
        return _FakeResp(
            json_data={"code": 0, "data": {"batch_id": "b1", "file_urls": self._urls}}
        )

    def put(self, url, content=b""):
        self.puts.append((url, bytes(content)))
        return _FakeResp(status_code=200)

    def get(self, url, headers=None):
        if url.startswith("/api/v4/extract-results/"):
            return _FakeResp(
                json_data={
                    "code": 0,
                    "data": {
                        "extract_result": [
                            {"state": "done", "full_zip_url": f"http://zip/{i + 1}"}
                            for i in range(len(self._urls))
                        ]
                    },
                }
            )
        idx = int(url.rsplit("/", 1)[-1]) - 1
        return _FakeResp(content=_zip_with_markdown(self.zip_texts[idx]))


def _use_fake_mineru(monkeypatch, zip_texts):
    from app.services.parsing import mineru_client as mc

    _FakeMineruClient.instances = []
    _FakeMineruClient.zip_texts = zip_texts
    monkeypatch.setattr(mc.httpx, "Client", _FakeMineruClient)
    monkeypatch.setattr(mc, "POLL_INTERVAL", 0)
    return _FakeMineruClient


def test_mineru_client_transcodes_tiff_to_png(tmp_path, monkeypatch):
    """M24:TIFF 云端 -60002 不收,本地逐帧转 PNG,单批上传并按帧序合并。"""
    from PIL import Image

    from app.services.parsing import mineru_client as mc

    _enable_mineru(monkeypatch)
    p = tmp_path / "scan.tif"
    im = Image.new("RGB", (4, 4), (255, 0, 0))
    im.save(p, save_all=True, append_images=[Image.new("RGB", (4, 4), (0, 0, 255))])

    fake_cls = _use_fake_mineru(monkeypatch, ["第1页内容", "第2页内容"])
    md = mc.parse_via_mineru(p, "scan.tif")

    fake = fake_cls.instances[0]
    names = [f["name"] for f in fake.batch_json["files"]]
    assert names == ["scan_1.png", "scan_2.png"]
    assert [u for u, _ in fake.puts] == ["http://u/1", "http://u/2"]
    assert all(c.startswith(b"\x89PNG") for _, c in fake.puts)
    assert "第1页内容" in md and "第2页内容" in md
    assert md.index("第1页内容") < md.index("第2页内容")  # 帧序合并


def test_mineru_client_non_tiff_uploads_raw(tmp_path, monkeypatch):
    """非 TIFF 原样直传:原名 + 原始字节,单结果返回。"""
    from app.services.parsing import mineru_client as mc

    _enable_mineru(monkeypatch)
    raw = b"\x89PNG\r\n\x1a\nraw-png-bytes"
    p = tmp_path / "photo.png"
    p.write_bytes(raw)

    fake_cls = _use_fake_mineru(monkeypatch, ["单个结果"])
    md = mc.parse_via_mineru(p, "photo.png")

    fake = fake_cls.instances[0]
    names = [f["name"] for f in fake.batch_json["files"]]
    assert names == ["photo.png"]
    assert fake.puts == [("http://u/1", raw)]
    assert md == "单个结果"


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


async def test_maybe_ocr_routes_new_image_formats(tmp_path, monkeypatch):
    """M24:bmp 等新图片格式进 OCR 门(auto 即走 MinerU)。"""
    from app.services.parsing import ocr
    from app.services.parsing.base import ParseResult

    called = []

    def fake_mineru(path, name):
        called.append(name)
        return "ocr 结果"

    monkeypatch.setattr(ocr, "parse_via_mineru", fake_mineru)
    _enable_mineru(monkeypatch)
    p = tmp_path / "x.bmp"
    p.write_bytes(b"bm")
    out = ocr.maybe_ocr(p, ".bmp", "auto", ParseResult())
    assert called == ["x.bmp"]
    assert out.blocks[0].content == "ocr 结果"
