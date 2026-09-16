from typing import TypedDict


class ChatState(TypedDict, total=False):
    question: str
    kb_ids: list[int]
    rerank: bool
    # M5 agentic:改写/自评链路
    history: list[dict]  # [{role, content}] 最近若干条,ask.py 从 DB 读入
    search_query: str  # 检索实际使用的查询(改写后或原样)
    proposed_query: str  # grade 提议的重检查询(transform 应用)
    grade: str  # "sufficient" | "insufficient"
    retries: int  # CRAG 已重检次数
    hits: list[dict]
    answer: str
    citations: list[dict]
