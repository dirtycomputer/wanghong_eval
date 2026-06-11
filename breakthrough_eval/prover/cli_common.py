"""CLI-harness 后端的共享件 (opencode / hermes).

两个关键设计 (plan §3.3, §8.4):

* **探针直连模型本体**: 污染是 *模型* 的属性, 与 harness 无关。CLI harness 自带
  系统提示/工具/记忆, 会干扰「无援作答」语义, 所以 probe 阶段绕过 CLI, 用
  OpenRouter 直接问裸模型 (无工具)。
* **审计取证靠 MCP transcript**: CLI 内部行为不可见, 但检索面收口在我们自己的
  ``arxiv-frozen-mcp`` 上 —— 它把每次调用写进 JSONL transcript, 这里解析回
  ``ToolCall`` 供 §8.4 审计 (返回日期均须 <= cutoff)。
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from ..llm_client import OpenRouterClient
from ..models import ToolCall, UsageStats
from .base import BackendResponse, ProverContext
from .mock import ARXIV_MCP_HOST

log = logging.getLogger(__name__)


def probe_via_api(ctx: ProverContext, client: OpenRouterClient, max_tokens: int) -> BackendResponse:
    """探针阶段: 直连裸模型, 无工具、短回答、temperature=0 (plan §3.3 的「无援」语义;
    探针是除名级裁决, 必须确定性)。"""
    res = client.chat(
        [
            {"role": "system", "content": ctx.system},
            {"role": "user", "content": ctx.user},
        ],
        max_tokens=max_tokens,
        temperature=0.0,
    )
    pt, ct = OpenRouterClient.usage_tokens(res.usage)
    return BackendResponse(
        text=res.content,
        usage=UsageStats(input_tokens=pt, output_tokens=ct),
    )


def read_mcp_transcript(path: str | Path) -> list[ToolCall]:
    """把 arxiv-frozen-mcp 写的 JSONL transcript 解析回审计用的 ToolCall 列表。"""
    p = Path(path)
    if not p.exists():
        return []
    calls: list[ToolCall] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            log.warning("MCP transcript 行无法解析, 跳过: %.80s", line)
            continue
        calls.append(
            ToolCall(
                tool=rec.get("tool", "search_arxiv"),
                query=rec.get("query", ""),
                host=ARXIV_MCP_HOST,
                returned_dates=[
                    date.fromisoformat(r["submission_date"])
                    for r in rec.get("returned", [])
                    if r.get("submission_date")
                ],
            )
        )
    return calls
