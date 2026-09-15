from abc import ABC, abstractmethod

from app.core.config import settings


class RerankProvider(ABC):
    @abstractmethod
    def rerank(self, query: str, documents: list[str], top_n: int = 8) -> list[int]:
        """返回保留文档的下标,按相关度降序。"""


def get_reranker():
    if not settings.RERANK_ENABLED:
        return None
    from app.services.rerank.zhipu import ZhipuRerank

    return ZhipuRerank()
