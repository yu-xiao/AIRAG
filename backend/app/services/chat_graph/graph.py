from functools import lru_cache, partial

from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from app.core.config import settings
from app.services.chat_graph.nodes import (
    decompose_node,
    generate_node,
    grade_node,
    rerank_node,
    retrieve_node,
    rewrite_node,
    transform_node,
)
from app.services.chat_graph.state import ChatState


@lru_cache(maxsize=1)
def make_chat_llm():
    # Minor 清偿:单例复用,避免每次 ask 新建 OpenAI 客户端
    return ChatOpenAI(
        base_url=settings.ZHIPU_BASE_URL,
        api_key=settings.ZHIPU_API_KEY,
        model=settings.CHAT_MODEL,
        temperature=settings.CHAT_TEMPERATURE,
        max_tokens=settings.CHAT_MAX_TOKENS,
    )


def route_after_rerank(state: dict) -> str:
    if state.get("hopped"):
        return "generate"  # 多跳合并后直通生成,省一次 grade 评审
    return "grade"


def route_after_grade(state: dict) -> str:
    if state.get("grade") == "insufficient" and state.get("retries", 0) < 1:
        return "transform"
    if (
        settings.MULTI_HOP_ENABLED
        and not state.get("hopped")
        and (state.get("grade") == "insufficient" or not state.get("hits"))
    ):
        return "decompose"
    return "generate"


def build_graph(llm=None, checkpointer=None):
    chat_llm = llm or make_chat_llm()
    gen = partial(generate_node, llm=chat_llm)
    rw = partial(rewrite_node, llm=chat_llm)
    gr = partial(grade_node, llm=chat_llm)
    dec = partial(decompose_node, llm=chat_llm)
    g = StateGraph(ChatState)
    g.add_node("rewrite", rw)
    g.add_node("retrieve", retrieve_node)
    g.add_node("rerank", rerank_node)
    g.add_node("grade", gr)
    g.add_node("transform", transform_node)
    g.add_node("generate", gen)
    g.add_node("decompose", dec)
    # 拓扑恒含全部节点:开关关闭时节点直通(M4 rerank 同模式)
    g.add_edge(START, "rewrite")
    g.add_edge("rewrite", "retrieve")
    g.add_edge("retrieve", "rerank")
    g.add_conditional_edges(
        "rerank", route_after_rerank, {"grade": "grade", "generate": "generate"}
    )
    g.add_conditional_edges(
        "grade",
        route_after_grade,
        {"transform": "transform", "decompose": "decompose", "generate": "generate"},
    )
    g.add_edge("transform", "retrieve")
    g.add_edge("decompose", "retrieve")
    g.add_edge("generate", END)
    return g.compile(checkpointer=checkpointer)
