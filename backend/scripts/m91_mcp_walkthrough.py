"""M9.1:MCP 真客户端走查(fastmcp.Client,真 streamable-http 客户端)。

用法(start_dev.bat 起服务后,backend 目录):
    .venv\\Scripts\\python scripts\\m91_mcp_walkthrough.py <airag_key> [query] [editor_key]

覆盖:initialize 握手、tools/list、list_knowledge_bases、search_knowledge_base
真调、ask_knowledge_base 真调(答案或拒答)、无权库 ToolError 文案。
传入第三个参数 editor_key(编辑型密钥明文,Web「API 密钥」页铸造,类型选"编辑")时,
追加 M11 文档工具走查:upload_document(base64 真文件)→ get_document →
list_documents → delete_document;四步判定计入汇总与退出码,锚点/文件名带
唯一 run 标记,可对同一 KB 重复运行。
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
    """M11:文档工具走查(upload→get→list→delete,base64 真文件)。

    四步判定经 check() 计入全局 RESULTS(汇总与退出码);任一步返回错误体
    即记 FAIL 并提前返回,不崩溃、不让后续步骤静默跳过。
    """
    import base64
    import io
    import uuid

    import httpx
    from docx import Document as Dx

    ACCEPT = "application/json, text/event-stream"
    headers = {"Authorization": f"Bearer {editor_key}",
               "Accept": ACCEPT}
    msg_id = 100
    sid = None
    run_tag = uuid.uuid4().hex[:6]  # 锚点/文件名每次运行唯一,防 SHA256 去重 409

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
            try:
                rj = r.json()
            except ValueError:
                return None, {"http_status": r.status_code,
                              "text": r.text[:200]}
            result = rj.get("result") if isinstance(rj, dict) else None
            if (not isinstance(result, dict) or rj.get("error")
                    or result.get("isError")):
                return None, rj  # error-only 体无 "result" 键,不可直接下标
            try:
                return json.loads(result["content"][0]["text"]), rj
            except (KeyError, IndexError, TypeError, ValueError):
                return None, rj

        dx = Dx()
        dx.add_paragraph(f"m11-mcp-walkthrough-{kb_id}-{run_tag} 锚点内容")
        buf = io.BytesIO()
        dx.save(buf)
        b64 = base64.b64encode(buf.getvalue()).decode()

        body, rj = await call("upload_document",
                              {"kb_id": kb_id,
                               "filename": f"m11-mcp-{kb_id}-{run_tag}.docx",
                               "content_b64": b64})
        check("mcp upload_document 返回 id", bool(body and body.get("id")),
              str(rj)[:200])
        if not (body and body.get("id")):
            return  # 上传失败无 doc_id,提前返回,不孤儿化后续步骤
        doc_id = body["id"]

        got, rj = await call("get_document", {"doc_id": doc_id})
        check("mcp get_document 返回状态", bool(got and got.get("status")),
              str(rj)[:200])
        if got is None:
            return

        lst, rj = await call("list_documents", {"kb_id": kb_id})
        hit = doc_id in [d["id"] for d in (lst or {}).get("items", [])]
        check("mcp list_documents 含新上传文档", hit,
              str(rj)[:200] if lst is None else str(lst)[:200])
        if lst is None:
            return

        dele, rj = await call("delete_document", {"doc_id": doc_id})
        check("mcp delete_document 确认删除",
              bool(dele and dele.get("deleted")), str(rj)[:200])


if __name__ == "__main__":
    asyncio.run(main())
