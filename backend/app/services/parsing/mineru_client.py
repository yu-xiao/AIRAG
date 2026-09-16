"""MinerU 云 API 同步客户端(Celery worker 内阻塞使用)。

v4 本地文件流程(官方文档 https://mineru.net/apiManage/docs):
1. POST /api/v4/file-urls/batch 申请预签名上传链接(解析在上传后自动开始);
2. PUT 文件字节到预签名 URL(不得带 Content-Type);
3. GET /api/v4/extract-results/batch/{batch_id} 轮询,done 后取 full_zip_url;
4. 下载 zip 读 full.md。
"""
import io
import time
import zipfile

import httpx

from app.core.config import settings

POLL_INTERVAL = 5  # 秒
POLL_MAX = 60  # 5 分钟上限


class MineruError(RuntimeError):
    pass


def _check(resp: httpx.Response, step: str) -> dict:
    if resp.status_code == 429:
        raise MineruError(f"mineru rate limited at {step}")
    if resp.status_code != 200 or resp.json().get("code") != 0:
        raise MineruError(f"mineru {step} failed: {resp.status_code} {resp.text[:200]}")
    return resp.json()["data"]


def parse_via_mineru(path, filename: str) -> str:
    """返回解析出的 markdown 文本;任何失败抛 MineruError。"""
    token = settings.MINERU_API_TOKEN
    if not token:
        raise MineruError("MINERU_API_TOKEN not configured")
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=settings.MINERU_BASE_URL, timeout=120) as client:
        batch = client.post(
            "/api/v4/file-urls/batch",
            headers=headers,
            json={
                "files": [{"name": filename, "is_ocr": True}],
                "language": "ch",
                "enable_formula": False,
                "enable_table": True,
            },
        )
        data = _check(batch, "file-urls/batch")
        batch_id = data["batch_id"]
        put_url = data["file_urls"][0]

        with open(path, "rb") as f:
            # 预签名链接:不带 Authorization/Content-Type,否则签名校验失败
            put = client.put(put_url, content=f.read())
        if put.status_code != 200:
            raise MineruError(f"mineru upload PUT failed: {put.status_code}")

        for _ in range(POLL_MAX):
            res = client.get(
                f"/api/v4/extract-results/batch/{batch_id}", headers=headers
            )
            item = _check(res, "poll batch")["extract_result"][0]
            state = item.get("state")
            if state == "done":
                zip_resp = client.get(item["full_zip_url"])
                if zip_resp.status_code != 200:
                    raise MineruError(
                        f"mineru result download failed: {zip_resp.status_code}"
                    )
                with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as z:
                    return z.read("full.md").decode("utf-8")
            if state == "failed":
                raise MineruError(f"mineru task failed: {item.get('err_msg', '')[:200]}")
            time.sleep(POLL_INTERVAL)
        raise MineruError(f"mineru poll timeout after {POLL_MAX} attempts")
