from typing import TypedDict


class ChatState(TypedDict, total=False):
    question: str
    kb_ids: list[int]
    hits: list[dict]
    answer: str
    citations: list[dict]
