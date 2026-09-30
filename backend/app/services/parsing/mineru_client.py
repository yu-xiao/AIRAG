"""MinerU 云 API 同步客户端(Celery worker 内阻塞使用)。

v4 本地文件流程(官方文档 https://mineru.net/apiManage/docs):
1. POST /api/v4/file-urls/batch 申请预签名上传链接(解析在上传后自动开始);
2. PUT 文件字节到预签名 URL(不得带 Content-Type);
3. GET /api/v4/extract-results/batch/{batch_id} 轮询,done 后取 full_zip_url;
4. 下载 zip 读 full.md。
TIFF 需本地转 PNG(MinerU 云端 -60002 不收),多页 TIFF 逐帧上传合并。
"""
import io
import time
import zipfile
from pathlib import Path

import httpx
from PIL import Image, ImageSequence

from app.core.config import settings

POLL_INTERVAL = 5  # 秒
POLL_MAX = 60  # 5 分钟上限

TIFF_EXTS = {".tif", ".tiff"}


class MineruError(RuntimeError):
    pass


def _check(resp: httpx.Response, step: str) -> dict:
    if resp.status_code == 429:
        raise MineruError(f"mineru rate limited at {step}")
    if resp.status_code != 200 or resp.json().get("code") != 0:
        raise MineruError(f"mineru {step} failed: {resp.status_code} {resp.text[:200]}")
    return resp.json()["data"]


def _upload_payloads(path, filename: str) -> list[tuple[str, bytes]]:
    """返回 (name, content) 上传清单:TIFF 逐帧转 PNG,其余文件原样直传。"""
    if Path(filename).suffix.lower() not in TIFF_EXTS:
        with open(path, "rb") as f:
            return [(filename, f.read())]
    stem = Path(filename).stem
    payloads = []
    with Image.open(path) as im:
        for i, frame in enumerate(ImageSequence.Iterator(im), start=1):
            buf = io.BytesIO()
            frame.save(buf, format="PNG")
            payloads.append((f"{stem}_{i}.png", buf.getvalue()))
    if not payloads:
        raise MineruError(f"mineru tiff has no frames: {filename}")
    return payloads


def parse_via_mineru(path, filename: str) -> str:
    """返回解析出的 markdown 文本;任何失败抛 MineruError。"""
    token = settings.MINERU_API_TOKEN
    if not token:
        raise MineruError("MINERU_API_TOKEN not configured")
    headers = {"Authorization": f"Bearer {token}"}
    payloads = _upload_payloads(path, filename)
    with httpx.Client(base_url=settings.MINERU_BASE_URL, timeout=120) as client:
        batch = client.post(
            "/api/v4/file-urls/batch",
            headers=headers,
            json={
                "files": [{"name": name, "is_ocr": True} for name, _ in payloads],
                "language": "ch",
                "enable_formula": False,
                "enable_table": True,
            },
        )
        data = _check(batch, "file-urls/batch")
        batch_id = data["batch_id"]

        for (_name, content), put_url in zip(payloads, data["file_urls"]):
            # 预签名链接:不带 Authorization/Content-Type,否则签名校验失败
            put = client.put(put_url, content=content)
            if put.status_code != 200:
                raise MineruError(f"mineru upload PUT failed: {put.status_code}")

        for _ in range(POLL_MAX):
            res = client.get(
                f"/api/v4/extract-results/batch/{batch_id}", headers=headers
            )
            items = _check(res, "poll batch")["extract_result"]
            states = [it.get("state") for it in items]
            if "failed" in states:
                bad = items[states.index("failed")]
                raise MineruError(f"mineru task failed: {bad.get('err_msg', '')[:200]}")
            if states and all(s == "done" for s in states):
                markdowns = []
                for item in items:  # 顺序与 files[] 一致,按帧序合并
                    zip_resp = client.get(item["full_zip_url"])
                    if zip_resp.status_code != 200:
                        raise MineruError(
                            f"mineru result download failed: {zip_resp.status_code}"
                        )
                    with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as z:
                        markdowns.append(z.read("full.md").decode("utf-8"))
                return "\n\n".join(markdowns)
            time.sleep(POLL_INTERVAL)
        raise MineruError(f"mineru poll timeout after {POLL_MAX} attempts")
