from abc import ABC, abstractmethod

from app.core.config import settings


class RerankProvider(ABC):
    @abstractmethod
    def rerank(
        self, query: str, documents: list[str], top_n: int = 8
    ) -> list[tuple[int, float]]:
        """返回 (保留文档下标, 相关度分 0~1) 列表;顺序不保证,调用方自行排序。"""


def get_reranker():
    if not settings.RERANK_ENABLED:
        return None
    from app.services.rerank.zhipu import ZhipuRerank

    return ZhipuRerank()
