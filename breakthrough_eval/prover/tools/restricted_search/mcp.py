"""stdio MCP server exposing one tool: ``restricted_search``."""

from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

from .search import RestrictedSearchError, restricted_search

SERVER_INFO = {"name": "restricted-search-mcp", "version": "0.1.0"}
TOOL_NAME = "restricted_search"

TOOL_SPEC = {
    "name": TOOL_NAME,
    "description": (
        "Search the configured date-restricted providers and return all results. "
        "Providers, keys, and limits come from config.yaml. The cutoff is supplied by the harness."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
        },
        "required": ["query"],
    },
}


def _log_transcript(record: dict) -> None:
    path = os.environ.get("RESTRICTED_SEARCH_TRANSCRIPT", "").strip()
    if not path:
        return
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        raise RestrictedSearchError(f"cannot write restricted search transcript: {exc}") from exc


def _tool_call(arguments: dict) -> dict:
    query = str(arguments.get("query", "")).strip()
    cutoff = os.environ.get("RESTRICTED_SEARCH_CUTOFF", "").strip()
    if not cutoff:
        raise RestrictedSearchError("RESTRICTED_SEARCH_CUTOFF is required")
    data = restricted_search(query, cutoff=date.fromisoformat(cutoff))
    _log_transcript({
        "tool": TOOL_NAME,
        "query": query,
        "returned": [
            {
                "source": item.get("source", ""),
                "published_date": item.get("published_date", ""),
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "arxiv_id": item.get("arxiv_id"),
            }
            for item in data["results"]
        ],
    })
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}


def _handle(req: dict) -> dict | None:
    method = req.get("method", "")
    rid = req.get("id")
    if method == "initialize":
        result = {
            "protocolVersion": req.get("params", {}).get("protocolVersion", "1.0"),
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }
    elif method == "tools/list":
        result = {"tools": [TOOL_SPEC]}
    elif method == "tools/call":
        params = req.get("params", {})
        if params.get("name") != TOOL_NAME:
            result = {"content": [{"type": "text", "text": "unknown tool"}], "isError": True}
        else:
            try:
                result = _tool_call(params.get("arguments") or {})
            except Exception as exc:  # noqa: BLE001 - MCP reports tool errors as tool output.
                result = {
                    "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                    "isError": True,
                }
    elif method.startswith("notifications/") or rid is None:
        return None
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = _handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
