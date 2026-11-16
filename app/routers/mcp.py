"""Read-only MCP server for clients that speak MCP directly (Claude, MCP Inspector, agents).

SAP Integration Suite does not need this: it generates its own MCP server from the REST
integration API (`app/routers/integration.py`). This endpoint is the direct route.

`POST /mcp` speaks JSON-RPC 2.0 over HTTP: `initialize`, `ping`, `tools/list`, `tools/call`.
It accepts only an integration key (never a person's sign-in), exposes only the read tools an
integration key may use, and runs every call through `app.integration.call`: a single-use
`agent:integration` token, a scope check and an AgentRun row.
"""

import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app import integration
from app.agents.base import AgentPermissionError
from app.copilot_tools import Tool
from app.db import get_db

router = APIRouter(tags=["mcp"])

PROTOCOL_VERSION = "2025-06-18"
SERVER_VERSION = "1.0.0"

READ_TOOLS = integration.INTEGRATION_TOOLS


def _rpc_error(request_id: Any, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
        status_code=status,
    )


def _schema(params: dict[str, str]) -> dict:
    properties: dict[str, dict] = {}
    required: list[str] = []
    for name, kind in params.items():
        optional = kind.endswith("?")
        kind = kind.rstrip("?")
        if kind == "int":
            properties[name] = {"type": "integer"}
        elif kind == "str":
            properties[name] = {"type": "string"}
        elif kind == "list[int]":
            properties[name] = {"type": "array", "items": {"type": "integer"}, "maxItems": 10}
        elif kind == "list[str]":
            properties[name] = {"type": "array", "items": {"type": "string"}, "maxItems": 5}
        if not optional:
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _tool_out(tool: Tool) -> dict:
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": _schema(tool.params),
        "annotations": {"readOnlyHint": True},
    }


def _call_tool(db: Session, caller: integration.Caller, name: str, args: dict) -> dict:
    try:
        result = integration.call(db, caller, name, args)
        ok, summary, data = True, result.summary, result.data
    except (LookupError, ValueError, AgentPermissionError) as exc:
        ok, summary, data = False, f"{name} failed: {exc}", {}
    text = json.dumps({"summary": summary, "data": data}, ensure_ascii=False, default=str)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": {"summary": summary, "data": data},
        "isError": not ok,
    }


@router.post("/mcp")
async def mcp(request: Request, db: Session = Depends(get_db),
              caller: integration.Caller = Depends(integration.require_integration_key)):
    # Async only to read the raw body (so bad JSON becomes a JSON-RPC parse error).
    # All database work runs in the threadpool, so a slow query never blocks the server.
    try:
        body = await request.json()
    except Exception:
        return _rpc_error(None, -32700, "Parse error", 400)
    if not isinstance(body, dict):
        return _rpc_error(None, -32600, "Invalid Request", 400)

    request_id = body.get("id")
    method = body.get("method")
    if body.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return _rpc_error(request_id, -32600, "Invalid Request", 400)
    if method.startswith("notifications/"):
        return Response(status_code=202)

    params = body.get("params") or {}
    if not isinstance(params, dict):
        return _rpc_error(request_id, -32602, "params must be an object")

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "hiring-desk", "version": SERVER_VERSION},
            "instructions": (
                "Read-only Hiring Desk data. Ask with tools/list, then tools/call. "
                "Nothing here changes the hiring record."
            ),
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [_tool_out(tool) for tool in READ_TOOLS.values()]}
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str) or name not in READ_TOOLS:
            return _rpc_error(request_id, -32602, f"Unknown tool: {name!r}. This surface exposes: "
                                                + ", ".join(READ_TOOLS))
        if not isinstance(args, dict):
            return _rpc_error(request_id, -32602, "arguments must be an object")
        result = await run_in_threadpool(_call_tool, db, caller, name, args)
    else:
        return _rpc_error(request_id, -32601, f"Method not found: {method}")

    return {"jsonrpc": "2.0", "id": request_id, "result": result}
