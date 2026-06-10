"""arxiv-frozen-mcp — 时间冻结 arXiv 检索的 stdio MCP server (plan §3.2, §8.2).

给 CLI 类 PROVER harness (opencode / hermes / codex) 暴露唯一外部信息源:
``search_arxiv``。红线在服务端强制:

  * ``CUTOFF_DATE`` 环境变量决定冻结日期, 所有结果硬过滤 ``submission_date <= cutoff``;
  * 每次调用以 JSONL 追加写入 ``ARXIV_MCP_TRANSCRIPT`` 指定的文件 (query + 返回的
    arxiv_id/日期), 供 §8.4 事后审计 —— harness 自己说没联网不算数, transcript 才算。

协议: MCP 的 stdio 传输 (JSON-RPC 2.0, 按行分隔)。只实现 benchmark 所需的最小面:
``initialize`` / ``notifications/initialized`` / ``tools/list`` / ``tools/call``。
纯标准库, 无 SDK 依赖。

用法 (harness 的 MCP 配置里):
    command: arxiv-frozen-mcp
    env: { CUTOFF_DATE: "2025-01-31", ARXIV_MCP_TRANSCRIPT: "/path/to/calls.jsonl" }
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date

from .arxiv_frozen import InMemoryArxivSource

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "arxiv-frozen-mcp", "version": "0.1.0"}

TOOL_SPEC = {
    "name": "search_arxiv",
    "description": (
        "检索时间冻结的 arXiv 快照, 只返回截止日期之前提交的论文。"
        "这是你唯一允许的外部信息源 (不得使用任何联网搜索)。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索关键词"},
            "max_results": {"type": "integer", "description": "返回条数 (默认 5)"},
        },
        "required": ["query"],
    },
}


def _log_transcript(path: str, record: dict) -> None:
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:  # transcript 不可写时不应阻断检索本身
        print("arxiv-frozen-mcp: transcript 写入失败", file=sys.stderr)


def _search(source: InMemoryArxivSource, transcript: str | None, args: dict) -> str:
    query = str(args.get("query", "")).strip()
    try:
        n = int(args.get("max_results", 5) or 5)
    except (TypeError, ValueError):
        n = 5
    papers = source.search(query, max_results=n)
    if transcript:
        _log_transcript(transcript, {
            "tool": "search_arxiv",
            "query": query,
            "max_results": n,
            "returned": [
                {"arxiv_id": p.arxiv_id, "submission_date": p.submission_date.isoformat()}
                for p in papers
            ],
        })
    results = [
        {
            "arxiv_id": p.arxiv_id,
            "title": p.title,
            "authors": p.authors,
            "submission_date": p.submission_date.isoformat(),
            "abstract": p.abstract,
        }
        for p in papers
    ]
    return json.dumps({"results": results}, ensure_ascii=False)


def _handle(req: dict, source: InMemoryArxivSource, transcript: str | None) -> dict | None:
    """Return a JSON-RPC response dict, or None for notifications."""
    method = req.get("method", "")
    rid = req.get("id")
    if method == "initialize":
        result = {
            "protocolVersion": req.get("params", {}).get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }
    elif method == "tools/list":
        result = {"tools": [TOOL_SPEC]}
    elif method == "tools/call":
        params = req.get("params", {})
        if params.get("name") != "search_arxiv":
            result = {"content": [{"type": "text", "text": '{"error": "unknown tool"}'}],
                      "isError": True}
        else:
            text = _search(source, transcript, params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": text}]}
    elif method.startswith("notifications/") or rid is None:
        return None  # notification: no response
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def main() -> int:
    cutoff_env = os.environ.get("CUTOFF_DATE")
    if not cutoff_env:
        print("arxiv-frozen-mcp: 缺少 CUTOFF_DATE 环境变量 (红线: 必须显式冻结)", file=sys.stderr)
        return 2
    source = InMemoryArxivSource(date.fromisoformat(cutoff_env))
    transcript = os.environ.get("ARXIV_MCP_TRANSCRIPT")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = _handle(req, source, transcript)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
