#!/usr/bin/env python3
"""
HTTP-based MCP server for C/C++ static analysis.

Implements the MCP protocol (JSON-RPC 2.0) over two transports:

  Streamable HTTP transport (MCP spec 2025-03-26) — used by opencode and newer clients:
    GET  /mcp   — optional SSE stream for server-initiated messages
    POST /mcp   — send JSON-RPC request, get response in the HTTP body (JSON or SSE)

  Legacy SSE transport (MCP spec 2024-11-05) — used by Claude Code and older clients:
    GET  /sse                    — long-lived SSE event stream
    POST /messages?sessionId=X   — JSON-RPC message posting

Usage:
  # Serve an existing database:
  python cpp_analyzer_server.py --db ./MyRepo_analysis.db

  # Analyse a repo first, then serve:
  python cpp_analyzer_server.py --db ./MyRepo_analysis.db --repo /path/to/repo

  # Custom host/port:
  python cpp_analyzer_server.py --db ./MyRepo_analysis.db --host 0.0.0.0 --port 8080
"""

import argparse
import asyncio
import json
import logging
import sys
import uuid
from typing import Any, AsyncIterator, Dict, Optional

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse, Response
from starlette.routing import Route

from database import Database
from static_analysis import CppAnalyzer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_db: Optional[Database] = None

# session_id -> asyncio.Queue[Optional[dict]]
# None in the queue signals the generator to stop
_sessions: Dict[str, asyncio.Queue] = {}

# ---------------------------------------------------------------------------
# MCP tool catalogue
# ---------------------------------------------------------------------------

MCP_TOOLS = [
    {
        "name": "get_function_code",
        "description": (
            "Return the source code of a C/C++ function. "
            "Accepts both short names ('myFunc') and fully-qualified names ('MyClass::myFunc')."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["function_name"],
            "properties": {
                "function_name": {
                    "type": "string",
                    "description": "Function name, e.g. 'process' or 'Widget::process'",
                }
            },
        },
    },
    {
        "name": "get_global_variable",
        "description": "Return the declaration of a C/C++ global variable.",
        "inputSchema": {
            "type": "object",
            "required": ["variable_name"],
            "properties": {
                "variable_name": {
                    "type": "string",
                    "description": "Global variable name",
                }
            },
        },
    },
    {
        "name": "get_struct_definition",
        "description": (
            "Return the definition of a C/C++ struct or class. "
            "Accepts both the struct tag name and any typedef alias. "
            "E.g. both 'Node' and 'NodeT' work for 'typedef struct Node {} NodeT'."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["struct_name"],
            "properties": {
                "struct_name": {
                    "type": "string",
                    "description": "Struct tag name or typedef alias",
                }
            },
        },
    },
    {
        "name": "get_function_callers",
        "description": "Return a list of functions that call the specified function.",
        "inputSchema": {
            "type": "object",
            "required": ["function_name"],
            "properties": {
                "function_name": {
                    "type": "string",
                    "description": "Function name to find callers for",
                }
            },
        },
    },
    {
        "name": "analyze_repository",
        "description": (
            "Trigger a full static analysis of a C/C++ repository using ctags and cscope. "
            "Results are stored in the active database. "
            "This may take a while for large repositories."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["repo_path"],
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Absolute path to the C/C++ repository root",
                }
            },
        },
    },
    {
        "name": "search_function",
        "description": "Search for functions by partial name match.",
        "inputSchema": {
            "type": "object",
            "required": ["pattern"],
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Substring to search for in function names",
                }
            },
        },
    },
    {
        "name": "get_database_stats",
        "description": "Return the number of entries stored in each table of the database.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]

# ---------------------------------------------------------------------------
# Tool implementations (sync — called from thread pool if needed)
# ---------------------------------------------------------------------------

def _impl_get_function_code(args: dict) -> str:
    name = args.get("function_name", "").strip()
    if not name:
        return "Error: function_name is required"
    row = _db.get_function(name)
    if row is None:
        return f"Function '{name}' not found in the database."
    return "\n".join([
        f"Function : {row['qualified_name']}",
        f"File     : {row['file_path']}:{row['line_number']}",
        f"Signature: {row['signature'] or '(unknown)'}",
        "",
        row["source_code"] or "(source not extracted)",
    ])


def _impl_get_global_variable(args: dict) -> str:
    name = args.get("variable_name", "").strip()
    if not name:
        return "Error: variable_name is required"
    row = _db.get_variable(name)
    if row is None:
        return f"Global variable '{name}' not found."
    return "\n".join([
        f"Variable : {row['name']}",
        f"Type     : {row['type_info'] or '(unknown)'}",
        f"File     : {row['file_path']}:{row['line_number']}",
        f"Declaration: {row['source_line'] or ''}",
    ])


def _impl_get_struct_definition(args: dict) -> str:
    name = args.get("struct_name", "").strip()
    if not name:
        return "Error: struct_name is required"
    row = _db.get_struct(name)
    if row is None:
        return f"Struct '{name}' not found."
    typedef_info = f" (typedef: {row['typedef_name']})" if row.get("typedef_name") else ""
    return "\n".join([
        f"Struct : {row['name']}{typedef_info}",
        f"File   : {row['file_path']}:{row['line_number']}",
        "",
        row["source_code"] or "(source not extracted)",
    ])


def _impl_get_function_callers(args: dict) -> str:
    name = args.get("function_name", "").strip()
    if not name:
        return "Error: function_name is required"
    callers = _db.get_callers(name)
    if not callers:
        return f"No callers found for '{name}' (or function not in database)."
    lines = [f"Callers of '{name}' ({len(callers)} found):"]
    for c in callers:
        lines.append(f"  {c['caller_name']}  {c['caller_file']}:{c['caller_line']}")
    return "\n".join(lines)


def _impl_search_function(args: dict) -> str:
    pattern = args.get("pattern", "").strip()
    if not pattern:
        return "Error: pattern is required"
    rows = _db.search_function(pattern)
    if not rows:
        return f"No functions matching '{pattern}'."
    lines = [f"Functions matching '{pattern}' ({len(rows)} found):"]
    for r in rows:
        lines.append(f"  {r['qualified_name']}  {r['file_path']}:{r['line_number']}")
    return "\n".join(lines)


def _impl_get_database_stats(args: dict) -> str:
    stats = _db.stats()
    lines = ["Database statistics:"]
    for table, count in stats.items():
        lines.append(f"  {table:<20} {count}")
    lines.append(f"\nDatabase: {_db.db_path}")
    return "\n".join(lines)


def _impl_analyze_repository(args: dict) -> str:
    repo_path = args.get("repo_path", "").strip()
    if not repo_path:
        return "Error: repo_path is required"
    try:
        analyzer = CppAnalyzer(repo_path)
        analysis = analyzer.analyze()
        _db.store_analysis(analysis)
        stats = _db.stats()
        return (
            f"Analysis complete for: {repo_path}\n"
            f"  functions        : {stats['functions']}\n"
            f"  global_variables : {stats['global_variables']}\n"
            f"  structs          : {stats['structs']}\n"
            f"  macros           : {stats['macros']}\n"
            f"  caller relations : {stats['callers']}\n"
            f"  database         : {_db.db_path}\n"
        )
    except Exception as exc:
        logger.exception("Analysis failed for %s", repo_path)
        return f"Analysis failed: {exc}"


_TOOL_DISPATCH = {
    "get_function_code":     _impl_get_function_code,
    "get_global_variable":   _impl_get_global_variable,
    "get_struct_definition": _impl_get_struct_definition,
    "get_function_callers":  _impl_get_function_callers,
    "search_function":       _impl_search_function,
    "get_database_stats":    _impl_get_database_stats,
    "analyze_repository":    _impl_analyze_repository,
}

# ---------------------------------------------------------------------------
# JSON-RPC / MCP protocol handling
# ---------------------------------------------------------------------------

MCP_PROTOCOL_VERSION = "2024-11-05"


def _jsonrpc_result(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


async def _process_request(message: dict) -> Optional[dict]:
    """
    Process a single JSON-RPC message and return a response dict, or None for
    notifications (which don't require a response).
    """
    method = message.get("method", "")
    req_id = message.get("id")  # None for notifications
    params = message.get("params") or {}

    # ----- Notifications (no id, no response) -----
    if req_id is None:
        if method == "notifications/initialized":
            logger.debug("Client initialized")
        return None

    # ----- Request methods -----
    if method == "initialize":
        return _jsonrpc_result(req_id, {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "cpp-analyzer", "version": "1.0.0"},
        })

    if method == "tools/list":
        return _jsonrpc_result(req_id, {"tools": MCP_TOOLS})

    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments") or {}
        handler = _TOOL_DISPATCH.get(tool_name)
        if handler is None:
            return _jsonrpc_error(req_id, -32601, f"Unknown tool: {tool_name}")
        try:
            # Run potentially blocking tool in a thread pool
            loop = asyncio.get_event_loop()
            text = await loop.run_in_executor(None, handler, tool_args)
            return _jsonrpc_result(req_id, {
                "content": [{"type": "text", "text": text}]
            })
        except Exception as exc:
            logger.exception("Tool '%s' raised an exception", tool_name)
            return _jsonrpc_result(req_id, {
                "content": [{"type": "text", "text": f"Error: {exc}"}],
                "isError": True,
            })

    if method == "ping":
        return _jsonrpc_result(req_id, {})

    # Unknown method
    return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")


# ---------------------------------------------------------------------------
# SSE and HTTP handlers
# ---------------------------------------------------------------------------

async def _sse_event_generator(session_id: str, queue: asyncio.Queue) -> AsyncIterator[str]:
    """Yield SSE-formatted strings: endpoint announcement, then response messages."""
    # The MCP SSE transport requires the server to announce the POST endpoint
    post_url = f"/messages?sessionId={session_id}"
    yield f"event: endpoint\ndata: {post_url}\n\n"
    logger.debug("SSE session %s opened, endpoint: %s", session_id, post_url)

    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=25)
            except asyncio.TimeoutError:
                # Keepalive comment to prevent proxy/client timeout
                yield ": keepalive\n\n"
                continue

            if item is None:
                # Sentinel: session closed
                break

            data = json.dumps(item, ensure_ascii=False)
            yield f"event: message\ndata: {data}\n\n"
            logger.debug("SSE session %s → %s", session_id, data[:120])
    finally:
        _sessions.pop(session_id, None)
        logger.debug("SSE session %s closed", session_id)


async def handle_sse(request: Request) -> StreamingResponse:
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _sessions[session_id] = queue

    return StreamingResponse(
        _sse_event_generator(session_id, queue),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


async def handle_message(request: Request) -> Response:
    """Receive a JSON-RPC message from the client and queue the response."""
    session_id = request.query_params.get("sessionId", "")
    queue = _sessions.get(session_id)
    if queue is None:
        return JSONResponse(
            {"error": f"Session '{session_id}' not found or already closed"},
            status_code=404,
        )

    try:
        body = await request.json()
    except Exception as exc:
        return JSONResponse({"error": f"Invalid JSON: {exc}"}, status_code=400)

    logger.debug("SSE session %s ← %s", session_id, json.dumps(body)[:120])

    # Handle batch requests (array of messages)
    messages = body if isinstance(body, list) else [body]

    for msg in messages:
        response = await _process_request(msg)
        if response is not None:
            await queue.put(response)

    # MCP spec: respond 202 Accepted to the POST; actual result arrives via SSE
    return Response(status_code=202)


# ---------------------------------------------------------------------------
# Streamable HTTP transport handlers (MCP spec 2025-03-26)
# Used by opencode and other newer MCP clients.
# ---------------------------------------------------------------------------

async def handle_mcp_post(request: Request) -> Response:
    """
    POST /mcp — receive a JSON-RPC message and return the response directly
    in the HTTP body.  If the client sends Accept: text/event-stream the
    response is formatted as an SSE stream; otherwise plain JSON.
    """
    try:
        body = await request.json()
    except Exception as exc:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32700, "message": f"Parse error: {exc}"}},
            status_code=400,
        )

    logger.debug("POST /mcp ← %s", json.dumps(body)[:200])

    messages = body if isinstance(body, list) else [body]
    responses = []
    for msg in messages:
        resp = await _process_request(msg)
        if resp is not None:
            responses.append(resp)

    if not responses:
        # Notifications only — no response body needed
        return Response(status_code=202)

    accept = request.headers.get("accept", "")

    if "text/event-stream" in accept:
        async def event_gen():
            for resp in responses:
                data = json.dumps(resp, ensure_ascii=False)
                yield f"event: message\ndata: {data}\n\n"
        return StreamingResponse(
            event_gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    result = responses[0] if len(responses) == 1 else responses
    return JSONResponse(result)


async def handle_mcp_get(request: Request) -> StreamingResponse:
    """
    GET /mcp — optional SSE stream for server-initiated push messages.
    Most clients use POST /mcp for request/response; this endpoint exists
    for completeness with the 2025-03-26 spec.
    """
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _sessions[session_id] = queue

    return StreamingResponse(
        _sse_event_generator(session_id, queue),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def handle_mcp(request: Request) -> Response:
    """Dispatch GET/POST /mcp to the correct handler."""
    if request.method == "GET":
        return await handle_mcp_get(request)
    return await handle_mcp_post(request)


# ---------------------------------------------------------------------------
# Starlette app
# ---------------------------------------------------------------------------

def build_app() -> Starlette:
    return Starlette(
        routes=[
            # Streamable HTTP transport (opencode, newer clients — MCP 2025-03-26)
            Route("/mcp", endpoint=handle_mcp, methods=["GET", "POST"]),
            # Legacy SSE transport (Claude Code, older clients — MCP 2024-11-05)
            Route("/sse", endpoint=handle_sse, methods=["GET"]),
            Route("/messages", endpoint=handle_message, methods=["POST"]),
        ]
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="C/C++ Analyzer — HTTP MCP server (Python 3.8+ compatible)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--db", required=True, help="Path to SQLite database file")
    parser.add_argument(
        "--repo", default=None,
        help="Run full analysis on this repo before starting the server",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port")
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
        stream=sys.stderr,
    )

    global _db
    _db = Database.open(args.db)

    if args.repo:
        logger.info("Running initial analysis on: %s", args.repo)
        text = _impl_analyze_repository({"repo_path": args.repo})
        logger.info(text)

    app = build_app()

    logger.info("Starting cpp-analyzer MCP server on %s:%d", args.host, args.port)
    logger.info("  Streamable HTTP (opencode)  : http://%s:%d/mcp", args.host, args.port)
    logger.info("  Legacy SSE (Claude Code)    : http://%s:%d/sse", args.host, args.port)

    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())


if __name__ == "__main__":
    main()
