import httpx

from app.core.config import settings
from app.services.rerank.base import RerankProvider


class ZhipuRerank(RerankProvider):
    def rerank(self, query: str, documents: list[str], top_n: int = 8) -> list[int]:
        resp = httpx.post(
            f"{settings.ZHIPU_BASE_URL}/rerank",
            headers={"Authorization": f"Bearer {settings.ZHIPU_API_KEY}"},
            json={
                "model": settings.RERANK_MODEL,
                "query": query,
                "documents": documents,
                "top_n": top_n,
            },
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json()["results"]
        return [r["index"] for r in results][:top_n]
