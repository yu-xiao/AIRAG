from functools import partial

from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from app.core.config import settings
from app.services.chat_graph.nodes import generate_node, rerank_node, retrieve_node
from app.services.chat_graph.state import ChatState


def make_chat_llm():
    return ChatOpenAI(
        base_url=settings.ZHIPU_BASE_URL,
        api_key=settings.ZHIPU_API_KEY,
        model=settings.CHAT_MODEL,
        temperature=settings.CHAT_TEMPERATURE,
        max_tokens=settings.CHAT_MAX_TOKENS,
    )


def build_graph(llm=None, checkpointer=None):
    gen = partial(generate_node, llm=llm or make_chat_llm())
    g = StateGraph(ChatState)
    g.add_node("retrieve", retrieve_node)
    g.add_node("rerank", rerank_node)
    g.add_node("generate", gen)
    g.add_edge(START, "retrieve")
    if settings.RERANK_ENABLED:
        g.add_edge("retrieve", "rerank")
        g.add_edge("rerank", "generate")
    else:
        g.add_edge("retrieve", "generate")
    g.add_edge("generate", END)
    return g.compile(checkpointer=checkpointer)
