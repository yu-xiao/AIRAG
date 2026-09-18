"""M9.1:MCP 真客户端走查(fastmcp.Client,真 streamable-http 客户端)。

用法(start_dev.bat 起服务后,backend 目录):
    .venv\\Scripts\\python scripts\\m91_mcp_walkthrough.py <airag_key> [query] [editor_key]

覆盖:initialize 握手、tools/list、list_knowledge_bases、search_knowledge_base
真调、ask_knowledge_base 真调(答案或拒答)、无权库 ToolError 文案。
传入第三个参数 editor_key(编辑型密钥明文,Web「API 密钥」页铸造,类型选"编辑")时,
追加 M11 文档工具走查:upload_document(base64 真文件)→ get_document →
list_documents → delete_document。
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
        print("usage: m91_mcp_walkthrough.py <airag_key> [query] [editor_key]")
        raise SystemExit(2)
    key, query = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "知识库")
    editor_key = sys.argv[3] if len(sys.argv) > 3 else None
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

    # M11 文档工具走查(ask 段之后;需要显式传入 editor key 明文)
    if editor_key:
        await walk_m11_doc_tools(BASE.removesuffix("/mcp"), editor_key, kb_id)
    else:
        print("SKIP m11 doc tools walkthrough(未提供 editor_key 参数)")
    print(f"\nM9.1 MCP WALKTHROUGH: {sum(RESULTS)}/{len(RESULTS)} PASS")
    raise SystemExit(0 if all(RESULTS) else 1)


async def walk_m11_doc_tools(base: str, editor_key: str, kb_id: int) -> None:
    """M11:文档工具走查(upload→get→list→delete,base64 真文件)。"""
    import base64
    import io

    import httpx
    from docx import Document as Dx

    ACCEPT = "application/json, text/event-stream"
    headers = {"Authorization": f"Bearer {editor_key}",
               "Accept": ACCEPT}
    msg_id = 100
    sid = None

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as c:
        init = {"jsonrpc": "2.0", "id": msg_id, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26",
                           "capabilities": {},
                           "clientInfo": {"name": "m11", "version": "0"}}}
        r = await c.post(f"{base}/mcp", json=init, headers=headers)
        sid = r.headers.get("mcp-session-id")
        await c.post(f"{base}/mcp",
                     json={"jsonrpc": "2.0",
                           "method": "notifications/initialized"},
                     headers={**headers, "mcp-session-id": sid})

        async def call(name, args):
            nonlocal msg_id
            msg_id += 1
            r = await c.post(
                f"{base}/mcp",
                json={"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                      "params": {"name": name, "arguments": args}},
                headers={**headers, "mcp-session-id": sid},
            )
            rj = r.json()
            if rj.get("error") or rj["result"].get("isError"):
                return None, rj
            return __import__("json").loads(
                rj["result"]["content"][0]["text"]), rj

        dx = Dx()
        dx.add_paragraph(f"m11-mcp-walkthrough-{kb_id} 锚点内容")
        buf = io.BytesIO()
        dx.save(buf)
        b64 = base64.b64encode(buf.getvalue()).decode()

        body, _ = await call("upload_document",
                             {"kb_id": kb_id,
                              "filename": f"m11-mcp-{kb_id}.docx",
                              "content_b64": b64})
        print("PASS mcp upload" if body and body.get("id")
              else f"FAIL mcp upload: {_}")
        doc_id = body["id"]

        got, _ = await call("get_document", {"doc_id": doc_id})
        print("PASS mcp get" if got and got.get("status") else
              f"FAIL mcp get: {_}")

        lst, _ = await call("list_documents", {"kb_id": kb_id})
        hit = doc_id in [d["id"] for d in (lst or {}).get("items", [])]
        print("PASS mcp list" if hit else f"FAIL mcp list: {lst}")

        dele, _ = await call("delete_document", {"doc_id": doc_id})
        print("PASS mcp delete" if dele and dele.get("deleted") else
              f"FAIL mcp delete: {dele}")


if __name__ == "__main__":
    asyncio.run(main())
