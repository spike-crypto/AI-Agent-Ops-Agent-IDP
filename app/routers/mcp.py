"""Read-only MCP surface for external clients (for example SAP CPI).

One endpoint, `POST /mcp`, speaks JSON-RPC 2.0 over HTTP: `initialize`, `ping`,
`tools/list` and `tools/call`. Only the read tools from `app/copilot_tools.py` are
exposed, and every call writes an AgentRun row. Authentication is the same HR admin
bearer token as the rest of the API; there is no anonymous access and no write path.
"""

import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app import ratelimit
from app.copilot_tools import TOOLS, Tool, run_read_tool
from app.db import get_db
from app.deps import require_hr
from app.models import AgentRun, User

router = APIRouter(tags=["mcp"])

PROTOCOL_VERSION = "2025-06-18"
SERVER_VERSION = "1.0.0"
REQUESTS_PER_MINUTE = 120

READ_TOOLS = {name: tool for name, tool in TOOLS.items() if tool.kind == "read"}


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


def _call_tool(db: Session, user: User, name: str, args: dict) -> dict:
    started = time.perf_counter()
    result = run_read_tool(db, name, args)
    db.add(AgentRun(
        agent="mcp", action=name, user_id=user.id,
        status="success" if result.ok else "error",
        scopes_used=sorted(READ_TOOLS[name].scopes),
        summary=result.summary[:500], error="" if result.ok else result.summary[:2000],
        duration_ms=int((time.perf_counter() - started) * 1000),
    ))
    db.commit()
    text = json.dumps({"summary": result.summary, "data": result.data},
                      ensure_ascii=False, default=str)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": {"summary": result.summary, "data": result.data},
        "isError": not result.ok,
    }


@router.post("/mcp")
async def mcp(request: Request, db: Session = Depends(get_db), user: User = Depends(require_hr)):
    allowed, retry_after = ratelimit.hit(f"mcp:{user.id}", REQUESTS_PER_MINUTE, 60)
    if not allowed:
        raise HTTPException(429, "Too many MCP requests", headers={"Retry-After": str(retry_after)})

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
        result = _call_tool(db, user, name, args)
    else:
        return _rpc_error(request_id, -32601, f"Method not found: {method}")

    return {"jsonrpc": "2.0", "id": request_id, "result": result}
