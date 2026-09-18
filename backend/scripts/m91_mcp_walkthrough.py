"""M9.1:MCP 真客户端走查(fastmcp.Client,真 streamable-http 客户端)。

用法(start_dev.bat 起服务后,backend 目录):
    .venv\\Scripts\\python scripts\\m91_mcp_walkthrough.py <airag_key> [query]

覆盖:initialize 握手、tools/list、list_knowledge_bases、search_knowledge_base
真调、ask_knowledge_base 真调(答案或拒答)、无权库 ToolError 文案。
退出码 1 = 走查失败。
"""
import asyncio
import json
import sys

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

BASE = "http://127.0.0.1:8001/mcp"
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if not cond else ""))


def _text(result) -> str:
    try:
        return result.content[0].text if result.content else ""
    except Exception:
        return str(result)


async def main():
    if len(sys.argv) < 2:
        print("usage: m91_mcp_walkthrough.py <airag_key> [query]")
        raise SystemExit(2)
    key, query = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "知识库")
    transport = StreamableHttpTransport(
        url=BASE, headers={"Authorization": f"Bearer {key}"}
    )
    async with Client(transport) as c:
        tools = await c.list_tools()
        tool_list = getattr(tools, "tools", tools)  # 版本差异:有的包 .tools,有的直接 list
        names = [t.name for t in tool_list]
        check("tools/list 两工具", "list_knowledge_bases" in names
              and "search_knowledge_base" in names, str(names))
        check("tools/list 含 ask 工具", "ask_knowledge_base" in names, str(names))

        r1 = await c.call_tool("list_knowledge_bases", {})
        kbs = json.loads(_text(r1))["items"]
        check("list_knowledge_bases 非空", len(kbs) > 0, _text(r1)[:200])
        kb_id = kbs[0]["id"]

        r2 = await c.call_tool(
            "search_knowledge_base", {"kb_ids": [kb_id], "query": query, "top_k": 5}
        )
        body = json.loads(_text(r2))
        check("search_knowledge_base 返回结构",
              {"hits", "total", "elapsed_ms"} <= set(body), _text(r2)[:200])

        r4 = await c.call_tool(
            "ask_knowledge_base", {"kb_ids": [kb_id], "query": query}
        )
        body4 = json.loads(_text(r4))
        check("ask_knowledge_base 返回结构",
              {"answer", "citations", "refused", "tokens_used",
               "elapsed_ms"} <= set(body4), _text(r4)[:200])
        check("ask 答案非空或拒答",
              bool(body4["answer"]) or body4["refused"] is True,
              _text(r4)[:200])

        try:
            r3 = await c.call_tool(
                "search_knowledge_base", {"kb_ids": [999999], "query": "x"}
            )
            check("无权库 ToolError 文案", "kb_forbidden" in _text(r3), _text(r3)[:200])
        except Exception as e:  # fastmcp 客户端把 tool 错误抛成异常
            check("无权库 ToolError 文案", "kb_forbidden" in str(e), str(e)[:200])
    print(f"\nM9.1 MCP WALKTHROUGH: {sum(RESULTS)}/{len(RESULTS)} PASS")
    raise SystemExit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    asyncio.run(main())
